import AppKit
import AVFoundation
import Carbon.HIToolbox
import Foundation
import IOKit.pwr_mgt

/// The global hotkeys Vox owns, with meaning carried by the gesture.
///
/// Both live on **§**, the key left of 1 — `kVK_ISO_Section`, verified to map to
/// "§" under this machine's layout. Carbon registers them with no permission
/// grant at all, which a bare Fn key (Wispr's default) cannot do without an event
/// tap and Input Monitoring. Plain ⌘ carries the everyday gesture, so the key
/// reached dozens of times a day costs two fingers and no more.
///
///   ⌘§ tapped   read the selection aloud; while an agent speaks, take the floor now
///   ⌘§ held     dictate at the cursor, release to inject
///   ⌘⇧§ tapped  flip the floor for the utterance playing right now
///
/// A tap opens the microphone only when it cuts an agent utterance that had
/// already promised the person a reply listen. Dictation otherwise happens
/// only while the key is held.
/// `holdThreshold` is what tells the two gestures apart.
///
/// ⌘⇧§ still changes the floor after an utterance finishes; ⌘§ takes it now.
/// Carbon matches modifiers exactly, so ⌘⇧§ never fires the plain binding.
struct HotKeyBinding {
    let id: UInt32
    let keyCode: UInt32
    let modifiers: UInt32
    let label: String

    /// How long ⌘§ must stay down before it counts as dictation rather than a
    /// tap. Long enough that a deliberate tap is never mistaken for a hold,
    /// short enough to be imperceptible on a key you meant to hold.
    static let holdThreshold: TimeInterval = 0.35

    static let voice = HotKeyBinding(
        id: 1,
        keyCode: UInt32(kVK_ISO_Section),
        modifiers: UInt32(cmdKey),
        label: "⌘§"
    )

    /// Take or decline the floor. Tap only — there is no hold gesture here, so
    /// it acts on the press and the release is ignored.
    static let floor = HotKeyBinding(
        id: 2,
        keyCode: UInt32(kVK_ISO_Section),
        modifiers: UInt32(cmdKey | shiftKey),
        label: "⌘⇧§"
    )

    static let all: [HotKeyBinding] = [.voice, .floor]
}

/// Reads whatever text is selected in the frontmost app.
///
/// `AXSelectedText` first, because it is instant and leaves the pasteboard
/// alone. It returns nothing in plenty of Chromium and Electron surfaces
/// though, so a synthesized ⌘C is the fallback — with the pasteboard restored
/// afterwards, and a `changeCount` check so "nothing was selected" is
/// distinguishable from "the copy landed".
enum SelectionReader {
    static func read() -> String? {
        if let viaAX = accessibilitySelection(), !viaAX.isEmpty { return viaAX }
        return clipboardSelection()
    }

    private static func accessibilitySelection() -> String? {
        let system = AXUIElementCreateSystemWide()
        var focused: CFTypeRef?
        guard
            AXUIElementCopyAttributeValue(
                system, kAXFocusedUIElementAttribute as CFString, &focused
            ) == .success,
            let element = focused
        else { return nil }

        var selected: CFTypeRef?
        guard
            AXUIElementCopyAttributeValue(
                element as! AXUIElement, kAXSelectedTextAttribute as CFString, &selected
            ) == .success,
            let text = selected as? String
        else { return nil }
        return text.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func clipboardSelection() -> String? {
        let pasteboard = NSPasteboard.general
        let before = pasteboard.changeCount
        let saved = (pasteboard.pasteboardItems ?? []).map { item -> [String: Data] in
            var stored: [String: Data] = [:]
            for type in item.types {
                if let data = item.data(forType: type) { stored[type.rawValue] = data }
            }
            return stored
        }

        postCommandC()

        // Give the frontmost app a moment to service the copy. If changeCount
        // never moves, nothing was selected — say so rather than reading back
        // whatever happened to be on the clipboard already.
        var text: String?
        let deadline = Date().addingTimeInterval(0.25)
        while Date() < deadline {
            if pasteboard.changeCount != before {
                text = pasteboard.string(forType: .string)
                break
            }
            Thread.sleep(forTimeInterval: 0.02)
        }

        pasteboard.clearContents()
        if !saved.isEmpty {
            pasteboard.writeObjects(
                saved.map { stored in
                    let item = NSPasteboardItem()
                    for (raw, data) in stored {
                        item.setData(data, forType: NSPasteboard.PasteboardType(raw))
                    }
                    return item
                }
            )
        }
        guard let text else { return nil }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private static func postCommandC() {
        guard let source = CGEventSource(stateID: .combinedSessionState) else { return }
        let c = CGKeyCode(kVK_ANSI_C)
        guard
            let down = CGEvent(keyboardEventSource: source, virtualKey: c, keyDown: true),
            let up = CGEvent(keyboardEventSource: source, virtualKey: c, keyDown: false)
        else { return }
        down.flags = .maskCommand
        up.flags = .maskCommand
        down.post(tap: .cghidEventTap)
        up.post(tap: .cghidEventTap)
    }
}

/// Puts dictated text at the cursor of whatever app is focused.
///
/// Clipboard plus a synthesized ⌘V, deliberately, over the alternatives:
/// posting one key event per character is slow for a minute of dictation and
/// breaks outright on Arabic, and writing `AXSelectedText` is unimplemented or
/// read-only in Chromium and Electron — which is most of where the text is
/// wanted. Every app with a Paste command works.
///
/// The transcript is **left on the pasteboard afterwards**. Restoring what was
/// there before looks tidier and is worse: a paste into a surface with no
/// editable field silently goes nowhere, and the words are then simply gone.
/// Leaving them there means the fallback is always ⌘V.
enum TextInjector {
    static func paste(_ text: String) {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)
        postCommandV()
    }

    private static func postCommandV() {
        guard let source = CGEventSource(stateID: .combinedSessionState) else { return }
        let v = CGKeyCode(kVK_ANSI_V)
        guard
            let down = CGEvent(keyboardEventSource: source, virtualKey: v, keyDown: true),
            let up = CGEvent(keyboardEventSource: source, virtualKey: v, keyDown: false)
        else { return }
        down.flags = .maskCommand
        up.flags = .maskCommand
        down.post(tap: .cghidEventTap)
        up.post(tap: .cghidEventTap)
    }
}

/// A live scrolling waveform of the microphone level.
///
/// Fed every level the runtime measured since the last poll — frames are 20 ms,
/// so that is ~50 Hz of real detail arriving in bursts of four or so. Taking one
/// sample per poll instead threw three of every four away and turned a smooth
/// signal into a 12.5 Hz staircase.
final class LevelMeterView: NSView {
    private static let capacity = 64
    private static let barWidth: CGFloat = 3
    private static let gap: CGFloat = 2

    private var history: [CGFloat] = Array(repeating: 0, count: LevelMeterView.capacity)
    /// Whether the mic is live. Idle draws a calm baseline in a muted colour;
    /// active draws in the system accent so it reads as "hearing you."
    var active = false { didSet { needsDisplay = true } }
    /// Overrides the active colour. The HUD uses it to say where the words are
    /// going — an agent turn and a dictation look identical otherwise, and which
    /// one is running decides whether the text lands in the frontmost app.
    var tint: NSColor? { didSet { needsDisplay = true } }

    override var intrinsicContentSize: NSSize { NSSize(width: NSView.noIntrinsicMetric, height: 30) }
    override var isFlipped: Bool { false }

    /// Push one burst of samples, oldest first, and scroll the waveform left.
    /// One redraw for the whole burst, not one per sample.
    func push(_ levels: [CGFloat]) {
        guard !levels.isEmpty else { return }
        for level in levels {
            history.removeFirst()
            history.append(max(0, min(1, level)))
        }
        needsDisplay = true
    }

    /// Flatten the waveform (used when the mic closes) without a jarring jump.
    func settle() {
        history = history.map { $0 * 0.5 }
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        guard bounds.width > 0 else { return }
        let pitch = LevelMeterView.barWidth + LevelMeterView.gap
        // Draw as many bars as actually fit and show the newest of them. The
        // history is one length; the panel and the pill are different widths, and
        // a fixed bar count overflowed the narrower one straight past its edge.
        let fits = max(1, min(history.count, Int((bounds.width + LevelMeterView.gap) / pitch)))
        let visible = history.suffix(fits)
        let midY = bounds.midY
        let maxHalf = bounds.height / 2 - 1
        let color = active ? (tint ?? NSColor.controlAccentColor) : NSColor.tertiaryLabelColor
        color.setFill()
        // Right-align, so the newest sample sits at a fixed edge instead of the
        // whole waveform shifting whenever the visible count changes.
        let inset = bounds.width - (CGFloat(fits) * pitch - LevelMeterView.gap)
        for (index, sample) in visible.enumerated() {
            // A small floor keeps a visible resting waveform instead of a blank
            // strip; real signal lifts the bars well above it.
            let half = max(1.5, sample * maxHalf)
            let x = inset + CGFloat(index) * pitch
            let rect = NSRect(
                x: x, y: midY - half, width: LevelMeterView.barWidth, height: half * 2
            )
            NSBezierPath(
                roundedRect: rect,
                xRadius: LevelMeterView.barWidth / 2,
                yRadius: LevelMeterView.barWidth / 2
            ).fill()
        }
    }
}

/// Whether some other menu-bar app is currently showing Vox's state for it.
///
/// Vox is shared: several agents drive it, and at least one of them (bro) draws
/// its own always-visible menu bar item that already renders speaking and
/// listening. Two icons then say the same thing side by side. So a host may
/// claim the display by writing a file in **Vox's own directory** — no app name,
/// no path into anyone else's install, and nothing here that knows what claimed
/// it:
///
///     ~/.vox/status-host.json   {"name":"bro","pid":1234,"since":1786990000}
///
/// While the claimant is ALIVE, Vox hides its status item; the hotkey, the HUD
/// pill, the runtime and every control are untouched. Absent file, dead pid,
/// recycled pid, unreadable JSON, or a machine with no such app at all — and
/// Vox shows its item exactly as it always has. The liveness check is the
/// point: a claimant that is SIGKILLed or crashes can never take the icon with
/// it. `since` is load-bearing for that, not decoration — pid alone is a
/// number the kernel hands out again within a day.
///
/// `VOX_STATUS_ITEM=always` opts out entirely and pins the item on screen.
enum StatusHost {
    static var file: URL {
        let env = ProcessInfo.processInfo.environment
        if let override = env["VOX_STATUS_HOST_FILE"], !override.isEmpty {
            return URL(fileURLWithPath: (override as NSString).expandingTildeInPath)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".vox/status-host.json")
    }

    /// When the process holding `pid` started, in Unix seconds, or nil if that
    /// cannot be read.  `kill(pid, 0)` answers "does *a* process have this
    /// number", which is not the question — see `claimant()`.
    private static func startTime(of pid: Int) -> Double? {
        var info = kinfo_proc()
        var size = MemoryLayout<kinfo_proc>.stride
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, Int32(pid)]
        guard sysctl(&mib, u_int(mib.count), &info, &size, nil, 0) == 0, size > 0 else {
            return nil
        }
        let started = info.kp_proc.p_starttime
        return Double(started.tv_sec) + Double(started.tv_usec) / 1_000_000
    }

    /// The name of the live claimant, or nil if nobody is showing Vox for us.
    static func claimant() -> String? {
        guard
            let data = try? Data(contentsOf: file),
            let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let pid = payload["pid"] as? Int, pid > 0
        else { return nil }
        // EPERM means the process exists and is simply not ours to signal.
        errno = 0
        guard kill(pid_t(pid), 0) == 0 || errno == EPERM else { return nil }
        // ...but pids are recycled, and a stale claim file outlives the app that
        // wrote it.  Measured here: a claim written 23 Aug 10:17 named pid 851,
        // which by the next morning belonged to metrickitd — started 19 hours
        // *after* the claim.  kill() said alive, so Vox hid its icon and kept it
        // hidden across every restart, with no way for the user to tell why.
        // A claimant cannot have started after it announced itself, so a start
        // time later than `since` is a different process wearing the number.
        // The two-second slack is for the gap between spawning and writing.
        if let since = payload["since"] as? Double, let started = startTime(of: pid),
            started > since + 2 {
            return nil
        }
        return (payload["name"] as? String) ?? "another menu bar"
    }
}

/// The menu-bar companion is deliberately a session controller, not a hidden
/// always-listening recorder. A click opens this panel; the microphone only
/// opens when an MCP conversation explicitly starts a bounded listen.
final class VoxAppDelegate: NSObject, NSApplicationDelegate, NSPopoverDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let popover = NSPopover()
    private let panelController = NSViewController()

    private let statusDot = NSView()
    private let heroNameLabel = NSTextField(labelWithString: "Vox")
    private let stateBadgeLabel = NSTextField(labelWithString: "Starting")
    private let meterView = LevelMeterView()
    private let detailLabel = NSTextField(wrappingLabelWithString: "Waiting for the local runtime")
    private let modeControl = NSSegmentedControl(
        labels: ["Talk", "Narrate", "Dictate"],
        trackingMode: .selectOne,
        target: nil,
        action: nil
    )
    private let primaryButton = NSButton(title: "Leave a note", target: nil, action: nil)
    private let repeatButton = NSButton(title: "Repeat", target: nil, action: nil)
    private let moreButton = NSButton(title: "More…", target: nil, action: nil)
    private var agents: [String] = []
    private var notesWaiting: [String] = []
    private var lastSpokenAgent: String?
    private var micLevel: CGFloat = 0
    /// Levels received from the last poll, awaiting one push into the meters.
    private var pendingLevels: [CGFloat] = []
    /// How far through the runtime's level stream we have drawn.
    private var levelsSeq = 0
    private var ttsLevelsSeq = 0
    private var pendingTtsLevels: [CGFloat] = []
    private var hotKeyRefs: [EventHotKeyRef] = []
    // ⌘§ is tap-or-hold, and which one it is cannot be known at key-down. The
    // pending work item is the undecided state; the flag remembers that the
    // press already resolved into a dictation so the release ends it.
    private var voiceKeyHoldTimer: DispatchWorkItem?
    private var voiceKeyBecameHold = false
    private let hud = VoxHUD()
    private let hudEnabled =
        (ProcessInfo.processInfo.environment["VOX_HUD"] ?? "1").lowercased()
        != "0"
    private var gateOpen = false
    private var streamOpen = false
    private var dictating = false

    private var runtime: Process?
    private var timer: Timer?
    /// Held while a turn is live. Talking generates no key or mouse events,
    /// so a five-minute display-sleep timer runs out mid-conversation; the
    /// screen going dark then reads as "inactive" and pauses the session
    /// under the person's sentence. A live microphone or a playing utterance
    /// *is* the person using the machine, and this says so to power
    /// management. Released the moment the turn ends, so an idle Vox never
    /// keeps a lid-open Mac awake.
    private var turnAwakeAssertion: IOPMAssertionID = 0
    private var pollInterval: TimeInterval = 0
    private var runtimeFailures: [Date] = []
    private var restartWorkItem: DispatchWorkItem?
    private var runtimeProbePending = false
    private var controlInFlight = false
    private var quitting = false
    private var state = "starting"
    private var detail = "Waiting for Vox runtime"
    private var actionNotice: String?
    private var microphoneOpen = false
    // The mic is open *during* playback so speaking over Vox interrupts it.
    // Distinct from plain listening: the agent is still talking.
    private var micArmedForBargeIn = false
    // Whether the microphone follows the utterance currently playing — the one
    // thing the floor indicator draws. The runtime owns it and reports it false
    // whenever nothing is speaking, so this needs no clearing of its own.
    private var floorOpen = false
    /// The runtime publishes state as a string; three separate gestures turn on
    /// this one reading of it, so it is spelled out once.
    private var isSpeaking: Bool { state.lowercased() == "speaking" }
    /// When the floor was last set from a toggle's own answer. A health poll
    /// issued before that moment saw the old floor, so its `floor_open` is
    /// stale however late it lands — and applying it would blank the tint for a
    /// frame, which is the only confirmation the person gets that the key
    /// landed at all.
    private var floorSettledAt = Date.distantPast
    private var lastStopReason: String?
    private var ioMode = "talk"
    private let baseURL = URL(string: "http://127.0.0.1:8766")!
    // Whether another menu bar is showing our state (see StatusHost). Checked at
    // most once a second: presentation runs up to 12 times a second while the
    // pill is live, and this is a file read plus a kill(2) probe.
    private var statusItemHosted = false
    private var lastStatusHostCheck = Date.distantPast
    private let statusItemHandoffAllowed =
        (ProcessInfo.processInfo.environment["VOX_STATUS_ITEM"] ?? "auto").lowercased()
        != "always"

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        configurePanel()
        guard let button = statusItem.button else { return }
        button.target = self
        button.action = #selector(statusItemClicked)
        button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        button.toolTip = "Vox — click to stop listening when the mic is live; right-click for controls"
        // Re-bake the glyph colour whenever the system switches light/dark, read
        // from the authoritative AppleInterfaceStyle change broadcast (the app's
        // own effectiveAppearance is unreliable for an accessory app on Tahoe).
        DistributedNotificationCenter.default().addObserver(
            self,
            selector: #selector(systemAppearanceChanged),
            name: NSNotification.Name("AppleInterfaceThemeChangedNotification"),
            object: nil
        )
        updatePresentation()

        requestMicrophonePermission(startRuntimeAfterward: true)
        let workspaceNotifications = NSWorkspace.shared.notificationCenter
        workspaceNotifications.addObserver(
            self,
            selector: #selector(pauseBeforeSleep),
            name: NSWorkspace.willSleepNotification,
            object: nil
        )
        workspaceNotifications.addObserver(
            self,
            selector: #selector(protectPrivacyForInactiveSession),
            name: NSWorkspace.sessionDidResignActiveNotification,
            object: nil
        )
        workspaceNotifications.addObserver(
            self,
            selector: #selector(protectPrivacyForInactiveSession),
            name: NSWorkspace.screensDidSleepNotification,
            object: nil
        )
        // Poll fast so the menu-bar glyph tracks the real mic state almost
        // instantly instead of lagging a second behind what the mic is doing.
        setPollInterval(0.4)
        // Clicking the pill ends the turn, exactly as a left-click on the menu
        // bar glyph does — the cue you are looking at should also be the control.
        hud.onTap { [weak self] in
            guard let self else { return }
            // While Vox is speaking the pill shows a stop glyph, so a click on
            // it stops the read — the same thing ⌘§ does mid-speech.
            if self.isSpeaking {
                self.sendControl("cancel", notice: "Stopped reading.", serialized: false)
                return
            }
            guard self.microphoneOpen, !self.controlInFlight, !self.dictating else { return }
            self.endTurn()
        }
        registerHotKeys()
        refreshStatus()
    }

    // Permission-free global hotkeys (Carbon RegisterEventHotKey — unlike an
    // NSEvent global monitor, it needs no Input Monitoring or Accessibility
    // grant), usable from any app. Carbon delivers both pressed and released,
    // so hold-to-talk works without a grant too. A bare Fn key is not
    // registrable this way; use combos, do not chase Fn.
    private func registerHotKeys() {
        var eventTypes = [
            EventTypeSpec(
                eventClass: OSType(kEventClassKeyboard),
                eventKind: OSType(kEventHotKeyPressed)
            ),
            EventTypeSpec(
                eventClass: OSType(kEventClassKeyboard),
                eventKind: OSType(kEventHotKeyReleased)
            ),
        ]
        InstallEventHandler(
            GetApplicationEventTarget(),
            { _, event, userData in
                guard let userData, let event else { return noErr }
                // The handler used to ignore its arguments entirely, so a
                // second hotkey would have been indistinguishable from the
                // first. Read which key, and whether it went down or up.
                var identifier = EventHotKeyID()
                let status = GetEventParameter(
                    event,
                    EventParamName(kEventParamDirectObject),
                    EventParamType(typeEventHotKeyID),
                    nil,
                    MemoryLayout<EventHotKeyID>.size,
                    nil,
                    &identifier
                )
                guard status == noErr else { return noErr }
                let pressed = GetEventKind(event) == UInt32(kEventHotKeyPressed)
                let delegate = Unmanaged<VoxAppDelegate>.fromOpaque(userData).takeUnretainedValue()
                DispatchQueue.main.async {
                    delegate.hotKeyFired(id: identifier.id, pressed: pressed)
                }
                return noErr
            },
            2,
            &eventTypes,
            Unmanaged.passUnretained(self).toOpaque(),
            nil
        )
        for binding in HotKeyBinding.all {
            var reference: EventHotKeyRef?
            RegisterEventHotKey(
                binding.keyCode,
                binding.modifiers,
                EventHotKeyID(signature: OSType(0x564F_5858), id: binding.id),  // 'VOXX'
                GetApplicationEventTarget(),
                0,
                &reference
            )
            if let reference { hotKeyRefs.append(reference) }
        }
    }

    func hotKeyFired(id: UInt32, pressed: Bool) {
        switch id {
        case HotKeyBinding.voice.id:
            if pressed { voiceKeyDown() } else { voiceKeyUp() }
        case HotKeyBinding.floor.id:
            // Tap only: act on the way down and let the release go by, so a
            // key held down by accident is still one flip and not two.
            if pressed { floorKeyTapped() }
        default:
            return
        }
    }

    // ⌘§ down: nothing happens yet, because what it means is not yet known.
    // Dictation is committed to only once the key has been held past the
    // threshold — before that it could still be a tap, and starting a dictation
    // on every press would open the microphone for something the user is about
    // to turn into an agent turn instead.
    private func voiceKeyDown() {
        guard voiceKeyHoldTimer == nil, !voiceKeyBecameHold else { return }
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.voiceKeyHoldTimer = nil
            self.voiceKeyBecameHold = true
            self.beginDictation()
        }
        voiceKeyHoldTimer = work
        DispatchQueue.main.asyncAfter(deadline: .now() + HotKeyBinding.holdThreshold, execute: work)
    }

    // ⌘§ up: whichever way the press resolved, end it that way.
    private func voiceKeyUp() {
        if let pending = voiceKeyHoldTimer {
            // Released before the threshold — it was a tap.
            pending.cancel()
            voiceKeyHoldTimer = nil
            voiceKeyBecameHold = false
            voiceKeyTapped()
            return
        }
        guard voiceKeyBecameHold else { return }
        voiceKeyBecameHold = false
        endDictation()
    }

    // Speaking → take the floor now. An agent's mic already open → end that
    // turn (which closes a gate, never opens one). Otherwise → read the
    // selection aloud.
    private func voiceKeyTapped() {
        if isSpeaking {
            sendControl("take_floor", notice: "Taking the floor…", serialized: false)
            return
        }
        if gateOpen {
            // Deliberately not serialised: this must never be swallowed by a
            // poll-triggered control still in flight, or the turn would stay
            // open with no way to close it.
            sendControl("gate_close", notice: "Sending…", serialized: false)
            return
        }
        readSelectionAloud()
    }

    // Verbatim — the runtime hands the text straight to Kokoro with no model
    // anywhere on the path.
    private func readSelectionAloud() {
        guard ensureAccessibility(for: "reading the selection") else { return }
        guard let selection = SelectionReader.read() else {
            actionNotice = "Nothing is selected to read."
            updatePresentation()
            return
        }
        sendControl(
            "read_aloud",
            notice: "Reading \(selection.count) characters…",
            extra: ["text": selection],
            serialized: false
        )
    }

    // ⌘⇧§: flip who speaks next for the utterance playing right now.
    //
    // It only means something while Vox is talking, so any other time it does
    // nothing at all — no request, no notice, no state the person cannot see.
    // Nothing audible either: the key only ever fires while the agent is
    // speaking, and an earcon would play over its own voice.
    private func floorKeyTapped() {
        guard isSpeaking else { return }
        sendFloorToggle()
    }

    /// Ask the runtime to flip the floor, and take the answer as the truth.
    ///
    /// Deliberately not `sendControl`: this one's body says which way the floor
    /// ended up, which is both the notice's wording and — until the next poll
    /// lands — what the pill draws. It is never serialised, because a flip
    /// swallowed by a control still in flight would leave the person looking at
    /// an indicator that disagrees with the key they just pressed.
    ///
    /// A read-aloud has no agent on the other end, so no floor is ever on air
    /// for one; the runtime answers `not_speaking` and nothing changes here.
    private func sendFloorToggle() {
        // A flag flip, answered without touching the utterance — if this is
        // slow, something is wrong, and waiting the full control timeout would
        // only delay a pill the person is watching.
        let request = controlRequest("toggle_floor", timeout: 3.0)
        URLSession.shared.dataTask(with: request) { [weak self] data, _, _ in
            DispatchQueue.main.async {
                guard let self else { return }
                guard
                    let result = VoxAppDelegate.controlResult(data),
                    (result["status"] as? String) == "floor_toggled",
                    let open = result["floor_open"] as? Bool
                else {
                    // Nothing was speaking after all, or the runtime could not
                    // be reached. Either way the floor is where it was.
                    return
                }
                self.floorOpen = open
                self.floorSettledAt = Date()
                // Past tense on purpose: notices in this panel are never
                // cleared, and one worded as a promise about the utterance
                // would go on making it long after that utterance ended.
                self.actionNotice =
                    open
                    ? "You took the floor for that utterance."
                    : "You declined the floor for that utterance."
                self.updatePresentation()
            }
        }.resume()
    }

    func applicationWillTerminate(_ notification: Notification) {
        quitting = true
        timer?.invalidate()
        for reference in hotKeyRefs { UnregisterEventHotKey(reference) }
        hotKeyRefs.removeAll()
        DistributedNotificationCenter.default().removeObserver(self)
        NSWorkspace.shared.notificationCenter.removeObserver(self)
        stopChildRuntime()
    }

    @objc private func systemAppearanceChanged() {
        DispatchQueue.main.async { [weak self] in self?.updatePresentation() }
    }

    // While the panel is open, poll fast enough that the waveform reads as live;
    // when it closes, drop back to the lighter glyph-tracking cadence.
    private func setPollInterval(_ interval: TimeInterval) {
        // Called from every status refresh, so it has to be a no-op when the
        // cadence has not actually changed — otherwise the timer is torn down
        // and rebuilt several times a second.
        guard interval != pollInterval || timer == nil else { return }
        pollInterval = interval
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            self?.refreshStatus()
        }
    }

    private func configurePanel() {
        let contentWidth: CGFloat = 288
        let root = NSView(frame: NSRect(x: 0, y: 0, width: contentWidth + 28, height: 0))
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false

        // Hero row: a state dot, the product name, and a right-aligned state word.
        statusDot.translatesAutoresizingMaskIntoConstraints = false
        statusDot.wantsLayer = true
        statusDot.layer?.cornerRadius = 4
        heroNameLabel.font = NSFont.systemFont(ofSize: 15, weight: .bold)
        stateBadgeLabel.font = NSFont.systemFont(ofSize: 12, weight: .semibold)
        stateBadgeLabel.textColor = .secondaryLabelColor
        let heroSpacer = NSView()
        heroSpacer.translatesAutoresizingMaskIntoConstraints = false
        let heroRow = NSStackView(views: [statusDot, heroNameLabel, heroSpacer, stateBadgeLabel])
        heroRow.orientation = .horizontal
        heroRow.alignment = .centerY
        heroRow.spacing = 8
        heroRow.translatesAutoresizingMaskIntoConstraints = false
        heroSpacer.setContentHuggingPriority(.defaultLow, for: .horizontal)

        meterView.translatesAutoresizingMaskIntoConstraints = false

        detailLabel.font = NSFont.systemFont(ofSize: 12)
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.maximumNumberOfLines = 2

        modeControl.segmentDistribution = .fillEqually
        modeControl.translatesAutoresizingMaskIntoConstraints = false
        modeControl.target = self
        modeControl.action = #selector(modeChanged)

        primaryButton.target = self
        primaryButton.bezelStyle = .rounded
        primaryButton.controlSize = .large
        primaryButton.translatesAutoresizingMaskIntoConstraints = false

        for button in [repeatButton, moreButton] {
            button.target = self
            button.bezelStyle = .rounded
            button.controlSize = .regular
            button.translatesAutoresizingMaskIntoConstraints = false
        }
        repeatButton.image = NSImage(systemSymbolName: "arrow.counterclockwise", accessibilityDescription: "Repeat")
        repeatButton.imagePosition = .imageLeading
        repeatButton.action = #selector(repeatLast)
        moreButton.action = #selector(showMoreMenu)
        let footer = NSStackView(views: [repeatButton, moreButton])
        footer.orientation = .horizontal
        footer.distribution = .fillEqually
        footer.spacing = 8
        footer.translatesAutoresizingMaskIntoConstraints = false

        stack.addArrangedSubview(heroRow)
        stack.addArrangedSubview(meterView)
        stack.addArrangedSubview(detailLabel)
        stack.addArrangedSubview(modeControl)
        stack.addArrangedSubview(primaryButton)
        stack.addArrangedSubview(footer)

        root.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 14),
            stack.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -14),
            stack.topAnchor.constraint(equalTo: root.topAnchor, constant: 14),
            stack.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -14),
            statusDot.widthAnchor.constraint(equalToConstant: 8),
            statusDot.heightAnchor.constraint(equalToConstant: 8),
            heroRow.widthAnchor.constraint(equalToConstant: contentWidth),
            meterView.widthAnchor.constraint(equalTo: heroRow.widthAnchor),
            meterView.heightAnchor.constraint(equalToConstant: 30),
            detailLabel.widthAnchor.constraint(equalTo: heroRow.widthAnchor),
            modeControl.widthAnchor.constraint(equalTo: heroRow.widthAnchor),
            primaryButton.widthAnchor.constraint(equalTo: heroRow.widthAnchor),
            footer.widthAnchor.constraint(equalTo: heroRow.widthAnchor),
        ])
        panelController.view = root
        popover.contentViewController = panelController
        popover.behavior = .transient
        popover.animates = true
        popover.delegate = self
    }

    // Clicking outside dismisses a transient popover without routing through
    // togglePanel; refresh so the cadence is recomputed now the panel is gone.
    func popoverDidClose(_ notification: Notification) {
        updatePresentation()
    }

    // A left-click while the mic is live ends the turn immediately — the fix for
    // "I can stop talking but you can't stop listening." It preserves what was
    // said and sends it to transcription (unlike Cancel, which discards). A
    // right-click (or a click when the mic is closed) opens the controls panel.
    @objc private func statusItemClicked() {
        let event = NSApp.currentEvent
        let rightClick = event?.type == .rightMouseUp
            || (event?.modifierFlags.contains(.control) ?? false)
        if !rightClick, microphoneOpen, !controlInFlight {
            endTurn()
            return
        }
        togglePanel()
    }

    @objc private func togglePanel() {
        guard let button = statusItem.button else { return }
        if popover.isShown {
            popover.performClose(nil)
            updatePresentation()
        } else {
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            updatePresentation()
            refreshStatus()
        }
    }

    private func startChildRuntime() {
        guard !quitting, runtime?.isRunning != true, !runtimeProbePending else { return }
        guard restartWorkItem == nil, runtimeFailures.count < 5 else { return }
        runtimeProbePending = true
        var request = URLRequest(url: baseURL.appendingPathComponent("health"))
        request.timeoutInterval = 0.5
        URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
            DispatchQueue.main.async {
                guard let self else { return }
                self.runtimeProbePending = false
                guard !self.quitting else { return }
                if let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) {
                    self.runtimeFailures.removeAll()
                    self.detail = "Local runtime connected"
                    self.updatePresentation()
                    return
                }
                self.launchChildRuntime()
            }
        }.resume()
    }

    private func launchChildRuntime() {
        guard !quitting, runtime?.isRunning != true else { return }
        guard let executable = ProcessInfo.processInfo.environment["VOX_RUNTIME"], !executable.isEmpty else {
            detail = "VOX_RUNTIME is not configured"
            updatePresentation()
            return
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        var environment = ProcessInfo.processInfo.environment
        environment["NO_PROXY"] = "127.0.0.1,localhost,::1"
        environment["no_proxy"] = "127.0.0.1,localhost,::1"
        environment["VOX_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        process.environment = environment
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                self?.runtime = nil
                self?.scheduleRuntimeRestart(status: process.terminationStatus)
            }
        }
        do {
            try process.run()
            runtime = process
            detail = "Starting local runtime…"
        } catch {
            detail = "Could not start runtime: \(error.localizedDescription)"
            scheduleRuntimeRestart(status: -1)
        }
        updatePresentation()
    }

    private func scheduleRuntimeRestart(status: Int32) {
        let now = Date()
        runtimeFailures = runtimeFailures.filter { now.timeIntervalSince($0) < 60 }
        runtimeFailures.append(now)
        guard runtimeFailures.count < 5 else {
            state = "error"
            detail = "Runtime stopped after 5 failures. Choose Restart runtime."
            updatePresentation()
            return
        }
        let delay = min(30.0, pow(2.0, Double(runtimeFailures.count - 1)))
        detail = "Runtime exited (\(status)); retrying in \(Int(delay))s"
        let work = DispatchWorkItem { [weak self] in
            self?.restartWorkItem = nil
            self?.startChildRuntime()
        }
        restartWorkItem?.cancel()
        restartWorkItem = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
        updatePresentation()
    }

    private func stopChildRuntime() {
        restartWorkItem?.cancel()
        restartWorkItem = nil
        guard let process = runtime else { return }
        process.terminationHandler = nil
        if process.isRunning {
            process.terminate()
            process.waitUntilExit()
        }
        runtime = nil
    }

    private func requestMicrophonePermission(startRuntimeAfterward: Bool) {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            detail = "Microphone permission granted"
            if startRuntimeAfterward { startChildRuntime() }
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .audio) { [weak self] granted in
                DispatchQueue.main.async {
                    guard let self else { return }
                    self.detail = granted ? "Microphone permission granted" : "Microphone permission denied"
                    if startRuntimeAfterward { self.startChildRuntime() }
                    self.updatePresentation()
                }
            }
        case .denied, .restricted:
            detail = "Microphone denied; speech output still works"
            if startRuntimeAfterward { startChildRuntime() }
        @unknown default:
            detail = "Unknown microphone permission state"
            if startRuntimeAfterward { startChildRuntime() }
        }
    }

    private func refreshStatus() {
        // Ask only for the waveform samples we have not drawn yet. Reading is
        // non-destructive on the runtime side, so anything else polling /health
        // cannot steal them from us — and we cannot steal them from it.
        var url = baseURL.appendingPathComponent("health")
        if var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            parts.queryItems = [
                URLQueryItem(name: "levels_since", value: String(levelsSeq)),
                URLQueryItem(name: "tts_levels_since", value: String(ttsLevelsSeq)),
            ]
            url = parts.url ?? url
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.0
        // When this poll asked its question, so a late answer can be told from
        // a current one.
        let issuedAt = Date()
        URLSession.shared.dataTask(with: request) { [weak self] data, _, error in
            guard let self else { return }
            DispatchQueue.main.async {
                if let error {
                    self.state = "offline"
                    self.detail = error.localizedDescription
                    self.microphoneOpen = false
                    self.gateOpen = false
                    self.streamOpen = false
                    self.updatePresentation()
                    if self.runtime == nil { self.startChildRuntime() }
                    return
                }
                guard
                    let data,
                    let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
                else {
                    self.state = "error"
                    self.detail = "Invalid health response"
                    self.updatePresentation()
                    return
                }
                self.state = (payload["state"] as? String) ?? (payload["status"] as? String) ?? "online"
                self.microphoneOpen = (payload["microphone_open"] as? Bool) ?? false
                // The runtime owns the gate; the key only ever asks for the
                // flip. Reading it back is what keeps a missed or duplicated
                // tap from leaving the toggle inverted forever.
                self.gateOpen = (payload["gate_open"] as? Bool) ?? self.microphoneOpen
                self.micArmedForBargeIn = (payload["mic_armed_for_barge_in"] as? Bool) ?? false
                // Read back every poll for the same reason the gate is: the key
                // only asks for the flip, and the runtime's answer is the one
                // that decides what the pill says. A poll already in flight
                // when the flip landed is the one exception — it answers about
                // the floor as it was, and at the pill's 80 ms cadence it can
                // arrive after the toggle's own answer.
                if issuedAt > self.floorSettledAt {
                    self.floorOpen = (payload["floor_open"] as? Bool) ?? false
                }
                // The device, as distinct from the gate. A stream open with the
                // gate shut is the warm-up window: the macOS microphone
                // indicator is lit but nothing can be heard yet.
                self.streamOpen = (payload["stream_open"] as? Bool) ?? false
                self.micLevel = CGFloat((payload["mic_level"] as? Double) ?? 0)
                // Every level measured since our last poll, oldest first, with the
                // cursor to ask from next time. It resets to 0 when a capture
                // ends, which is exactly right: the next one starts a new
                // waveform. Buffered here rather than pushed straight into the
                // meters, because updatePresentation also runs for appearance
                // changes and panel toggles and must not redraw a stale burst.
                self.levelsSeq = (payload["mic_levels_seq"] as? Int) ?? 0
                if let burst = payload["mic_levels"] as? [Double], !burst.isEmpty {
                    self.pendingLevels = burst.map { CGFloat($0) }
                }
                // The speaking waveform: the envelope of the clip actually
                // playing, measured runtime-side from its samples. Same burst
                // contract as the mic levels, separate cursor.
                self.ttsLevelsSeq = (payload["tts_levels_seq"] as? Int) ?? 0
                if let burst = payload["tts_levels"] as? [Double], !burst.isEmpty {
                    self.pendingTtsLevels = burst.map { CGFloat($0) }
                }
                self.detail = (payload["detail"] as? String) ?? "Local-only runtime connected"
                self.lastStopReason = payload["last_stop_reason"] as? String
                self.ioMode = (payload["io_mode"] as? String) ?? self.ioMode
                self.agents = (payload["agents"] as? [String]) ?? self.agents
                self.notesWaiting = (payload["notes_waiting"] as? [String]) ?? []
                self.lastSpokenAgent = payload["last_spoken_agent"] as? String
                self.updatePresentation()
            }
        }.resume()
    }

    /// One icon in the menu bar, not two. Everything below still runs while the
    /// item is hidden — the tooltip, the glyph and the panel are all rebuilt as
    /// usual, so the moment the claim lapses the item comes back already correct.
    private func updateStatusItemVisibility() {
        guard statusItemHandoffAllowed else {
            if !statusItem.isVisible { statusItem.isVisible = true }
            return
        }
        let now = Date()
        if now.timeIntervalSince(lastStatusHostCheck) >= 1.0 {
            lastStatusHostCheck = now
            statusItemHosted = StatusHost.claimant() != nil
        }
        guard statusItem.isVisible == statusItemHosted else { return }
        statusItem.isVisible = !statusItemHosted
        // A popover anchored to an item that just vanished would float loose.
        if statusItemHosted, popover.isShown { popover.performClose(nil) }
    }

    private func updatePresentation() {
        updateStatusItemVisibility()
        let normalized = state.lowercased()
        let title: String
        switch normalized {
        case "listening": title = "Vox Listening"
        case "speaking": title = "Vox Speaking"
        case "processing": title = "Vox Processing"
        case "paused": title = "Vox Paused"
        case "idle": title = "Vox Ready · Mic Off"
        case "off": title = "Vox Off"
        case "offline": title = "Vox Offline"
        case "error": title = "Vox Error"
        default: title = "Vox Starting"
        }
        applyStatusGlyph(normalized: normalized, title: title)
        if micArmedForBargeIn {
            statusItem.button?.toolTip =
                "Mic is LIVE while Vox speaks — just start talking to interrupt. \(panelDetail())"
        } else {
            statusItem.button?.toolTip = microphoneOpen
                ? "Mic is LIVE — click to stop listening. \(panelDetail())"
                : panelDetail()
        }

        // Hero row: a coloured dot + one state word instead of a wall of labels.
        let badge: String
        let accent: NSColor
        if micArmedForBargeIn {
            // Both things are true at once and hiding either would be a lie:
            // Vox is talking, and the mic is live so you can cut in.
            badge = "Speaking · cut in"; accent = .systemRed
        } else if microphoneOpen {
            badge = "Listening"; accent = .systemRed
        } else {
            switch normalized {
            case "speaking": badge = "Speaking"; accent = .systemBlue
            case "processing": badge = "Thinking"; accent = .systemBlue
            case "paused": badge = "Paused"; accent = .systemOrange
            case "idle": badge = "Ready"; accent = .systemGreen
            case "off": badge = "Off"; accent = .tertiaryLabelColor
            case "offline", "error": badge = "Offline"; accent = .systemRed
            default: badge = "Starting"; accent = .systemGray
            }
        }
        statusDot.layer?.backgroundColor = accent.cgColor
        stateBadgeLabel.stringValue = badge
        stateBadgeLabel.textColor = microphoneOpen ? .systemRed : .secondaryLabelColor

        // Live waveform: the real mic level while listening, the real playback
        // envelope while speaking; settles to a calm baseline otherwise.
        // Drained here so a burst is consumed once even though
        // updatePresentation runs for appearance changes and panel toggles
        // too, not only for polls.
        let burst = pendingLevels
        pendingLevels = []
        let ttsBurst = pendingTtsLevels
        pendingTtsLevels = []
        let speaking = normalized == "speaking" && !microphoneOpen
        meterView.active = microphoneOpen || speaking
        if microphoneOpen {
            meterView.push(burst)
        } else if speaking {
            meterView.push(ttsBurst)
        } else {
            meterView.settle()
        }

        // The pill's waveform has to read as live, so poll fast whenever it — or
        // the panel — is on screen, and fall back to the light glyph-tracking
        // cadence the moment neither is.
        let hudShowing = hudEnabled ? hudState(normalized: normalized) : nil
        keepDisplayAwake(microphoneOpen || streamOpen || dictating || normalized == "speaking")
        if hudEnabled {
            hud.apply(
                hudShowing,
                levels: hudShowing == .speaking ? ttsBurst : burst,
                floorOpen: floorOpen
            )
        }
        setPollInterval(hudShowing != nil || popover.isShown ? 0.08 : 0.4)

        detailLabel.stringValue = meterCaption(normalized)

        // Modes as one native segmented control; the active segment is selected.
        modeControl.selectedSegment = ["talk", "narrate", "dictate"].firstIndex(of: ioMode.lowercased()) ?? 0
        modeControl.isEnabled = !controlInFlight && normalized != "offline"

        applyPrimaryButton(normalized: normalized)
        repeatButton.isEnabled = !controlInFlight && !["offline", "off"].contains(normalized)
        moreButton.isEnabled = !controlInFlight
    }

    /// What the floating pill should show, or nil to hide it.
    ///
    /// Only the states worth interrupting the screen for. Everything else —
    /// idle, thinking, paused, offline — is the menu bar's job, and a pill that
    /// hung around for those would stop meaning anything.
    private func hudState(normalized: String) -> HUDState? {
        if dictating { return .dictating }
        if microphoneOpen { return .listening }
        if normalized == "speaking" { return .speaking }
        // The device is up but deaf: the stream-open guard is being waited out.
        // Shown so that first second reads as warm-up rather than as a key press
        // that did nothing at all.
        if streamOpen && !gateOpen { return .warming }
        return nil
    }

    // A short, human caption under the waveform — what Vox is doing right now,
    // not a paragraph. Falls back to the runtime detail for errors/notices.
    private func meterCaption(_ normalized: String) -> String {
        if let actionNotice { return actionNotice }
        if !notesWaiting.isEmpty {
            let targets = notesWaiting.map { $0 == "*" ? "any agent" : $0 }.joined(separator: ", ")
            return "Note waiting for \(targets)"
        }
        if microphoneOpen { return "Hearing you…" }
        switch normalized {
        case "speaking": return "Speaking…"
        case "processing": return "Thinking…"
        case "paused": return "Paused — mic closed"
        case "idle": return "Ready · mic off · \(modeTitle(ioMode))"
        case "off": return state.lowercased() == "off" && lastStopReason == "idle_timeout"
            ? "Stopped after 10 idle minutes" : "Vox is off"
        case "offline", "error": return detail
        default: return detail
        }
    }

    // One contextual primary action instead of a stack of always-visible
    // buttons: Stop when the mic is hot, start/resume when Vox is down, and
    // Leave a note when it is idle and free to take one.
    private func applyPrimaryButton(normalized: String) {
        let symbol: String
        let text: String
        let color: NSColor
        let selector: Selector
        var enabled = !controlInFlight
        if microphoneOpen {
            symbol = "stop.fill"; text = "Stop listening"; color = .systemRed
            selector = #selector(endTurn)
        } else {
            switch normalized {
            case "off", "offline", "error":
                symbol = "power"; text = "Turn Vox on"; color = .controlAccentColor
                selector = #selector(toggleSession)
            case "paused":
                symbol = "play.fill"; text = "Resume Vox"; color = .controlAccentColor
                selector = #selector(toggleSession)
            default:
                // Reply auto-targets whoever last spoke — the fast path. The
                // agent-picker note lives in More… for the rarer addressed case.
                symbol = "arrowshape.turn.up.left.fill"
                text = lastSpokenAgent.map { "Reply to \($0)" } ?? "Reply"
                color = .controlAccentColor
                selector = #selector(replyToLastSpeaker)
                // A reply needs the mic free; only offer it when Vox is idle.
                enabled = enabled && normalized == "idle"
            }
        }
        primaryButton.action = selector
        primaryButton.bezelColor = color
        primaryButton.image = NSImage(systemSymbolName: symbol, accessibilityDescription: text)
        primaryButton.imagePosition = .imageLeading
        primaryButton.attributedTitle = NSAttributedString(
            string: text,
            attributes: [
                .foregroundColor: NSColor.white,
                .font: NSFont.systemFont(ofSize: 13, weight: .semibold),
            ]
        )
        primaryButton.contentTintColor = .white
        primaryButton.isEnabled = enabled
    }

    // The menu-bar glyph shows a red mic ONLY when the microphone is genuinely
    // capturing (driven by the runtime's microphone_open truth, never stale
    // session state).
    //
    // The colour is BAKED INTO THE IMAGE PIXELS (a non-template, palette-tinted
    // symbol) rather than left to the system to tint. On macOS Tahoe an
    // accessory (LSUIElement) app's effectiveAppearance can resolve to Aqua
    // even in Dark mode, so both template auto-tint and contentTintColor paint
    // the glyph black and it vanishes on the dark menu bar. We read the real
    // system setting (AppleInterfaceStyle) and colour the pixels ourselves, so
    // nothing downstream can re-tint it wrong.
    private func applyStatusGlyph(normalized: String, title: String) {
        guard let button = statusItem.button else { return }
        let symbol: String
        if micArmedForBargeIn {
            // Red, so the live mic still reads as live, but a distinct glyph:
            // Vox is speaking and listening at the same time.
            symbol = "waveform.badge.mic"
        } else if microphoneOpen {
            symbol = "mic.fill"
        } else {
            switch normalized {
            case "speaking": symbol = "waveform"
            case "processing": symbol = "waveform.badge.mic"
            case "paused": symbol = "pause.circle"
            case "idle": symbol = "mic.slash"
            case "off": symbol = "mic.slash.circle"
            case "offline", "error": symbol = "exclamationmark.triangle"
            default: symbol = "waveform.circle"
            }
        }
        let darkMenuBar = UserDefaults.standard.string(forKey: "AppleInterfaceStyle")?
            .lowercased() == "dark"
        let glyphColor: NSColor = microphoneOpen
            ? .systemRed
            : (darkMenuBar ? .white : .black)
        let config = NSImage.SymbolConfiguration(pointSize: 15, weight: .semibold)
            .applying(NSImage.SymbolConfiguration(paletteColors: [glyphColor]))
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: title)?
            .withSymbolConfiguration(config)
        image?.isTemplate = false
        if let image {
            button.image = image
            button.imagePosition = .imageOnly
            button.title = ""
        } else {
            // Fall back to text if the SF Symbol is unavailable on this OS.
            button.image = nil
            button.title = title
        }
        button.contentTintColor = nil
    }

    private func modeTitle(_ mode: String) -> String {
        switch mode.lowercased() {
        case "narrate": return "Narrate"
        case "dictate": return "Dictate"
        default: return "Talk"
        }
    }

    private func panelDetail() -> String {
        if let actionNotice { return actionNotice }
        if !notesWaiting.isEmpty {
            let targets = notesWaiting.map { $0 == "*" ? "any agent" : $0 }.joined(separator: ", ")
            return "Note waiting for: \(targets) — delivered on that agent's next turn."
        }
        if state.lowercased() == "off", lastStopReason == "idle_timeout" {
            return "Stopped after 10 minutes without activity. The microphone is closed."
        }
        return detail
    }

    /// Every control goes to the same route with the same headers; only the
    /// action, the body and how long it is worth waiting differ.
    private func controlRequest(_ action: String, timeout: TimeInterval, extra: [String: Any] = [:])
        -> URLRequest
    {
        var request = URLRequest(url: baseURL.appendingPathComponent("control"))
        request.httpMethod = "POST"
        request.timeoutInterval = timeout
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("status-bar", forHTTPHeaderField: "X-Vox-Control-Source")
        if let token = controlToken() {
            request.setValue(token, forHTTPHeaderField: "X-Vox-Token")
        }
        var payload: [String: Any] = ["action": action]
        payload.merge(extra) { _, new in new }
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        return request
    }

    /// The `result` object a control answers with, or nil if the runtime could
    /// not be reached or refused. Two senders read their own body; the rest
    /// discard it.
    private static func controlResult(_ data: Data?) -> [String: Any]? {
        guard
            let data,
            let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        return payload["result"] as? [String: Any]
    }

    /// - Parameter serialized: pass `false` for hotkey-driven controls. The
    ///   in-flight guard exists so panel buttons cannot be double-fired, but a
    ///   turn key whose second tap gets swallowed leaves the microphone open
    ///   with no way to close it — a far worse failure than a duplicate.
    private func sendControl(
        _ action: String,
        notice: String,
        extra: [String: Any] = [:],
        serialized: Bool = true
    ) {
        if serialized {
            guard !controlInFlight else { return }
            controlInFlight = true
        }
        actionNotice = notice
        updatePresentation()
        // Stop/pause wait up to ~3s for the mic to close server-side. A 2s
        // client timeout made Stop look broken while the runtime was still
        // finishing the privacy shutdown.
        let request = controlRequest(action, timeout: 6.0, extra: extra)
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self else { return }
                if serialized { self.controlInFlight = false }
                if let error {
                    self.actionNotice = "Could not \(action): \(error.localizedDescription)"
                } else if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                    var reason = "HTTP \(http.statusCode)"
                    if let data,
                       let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                       let errorName = payload["error"] as? String {
                        reason += ": \(errorName)"
                    }
                    self.actionNotice = "Could not \(action): \(reason)"
                } else {
                    self.actionNotice = self.successNotice(for: action)
                }
                self.updatePresentation()
                self.refreshStatus()
            }
        }.resume()
    }

    /// Dictation has to get the transcript back so it can be typed where the
    /// user is looking — one of the two controls whose *body* matters, the
    /// other being the floor toggle. Every other sender discards the response.
    private func sendDictationEnd(completion: @escaping (String?) -> Void) {
        // Whisper small on a two-minute hold is the worst case this has to
        // survive; the shared 6s ceiling would abandon the transcript.
        let request = controlRequest("dictate_end", timeout: 30.0)
        URLSession.shared.dataTask(with: request) { data, _, _ in
            guard
                let text = VoxAppDelegate.controlResult(data)?["text"] as? String,
                !text.isEmpty
            else {
                DispatchQueue.main.async { completion(nil) }
                return
            }
            DispatchQueue.main.async { completion(text) }
        }.resume()
    }

    private func controlToken() -> String? {
        let path = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".vox/control.token")
        guard let token = try? String(contentsOf: path, encoding: .utf8) else { return nil }
        return token.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // MARK: - Dictation

    private func beginDictation() {
        // Ask before capturing, not after: a hold that records fine and then
        // silently fails to type is the worst possible way to learn that the
        // permission is missing.
        guard ensureAccessibility(for: "dictation") else { return }
        dictating = true
        sendControl("dictate_start", notice: "Dictating — release ⌘§ to type it.", serialized: false)
    }

    private func endDictation() {
        guard dictating else { return }
        dictating = false
        actionNotice = "Transcribing…"
        updatePresentation()
        sendDictationEnd { [weak self] text in
            guard let self else { return }
            guard let text else {
                self.actionNotice = "Nothing was said."
                self.updatePresentation()
                return
            }
            TextInjector.paste(text)
            // Says "and on your clipboard" because that is the recovery path when
            // the paste had nowhere to land, and it is only useful if known.
            self.actionNotice = "Typed \(text.count) characters — and on your clipboard."
            self.updatePresentation()
        }
    }

    /// Prompts once, then reports honestly. TCC grants are pinned to the code
    /// signature, so this can start failing after a rebuild — never let that
    /// look like the feature simply doing nothing.
    @discardableResult
    private func ensureAccessibility(for feature: String) -> Bool {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue(): true] as CFDictionary
        if AXIsProcessTrustedWithOptions(options) { return true }
        actionNotice = "Vox needs Accessibility to use \(feature). "
            + "Enable Vox in System Settings › Privacy & Security › Accessibility."
        updatePresentation()
        return false
    }

    private func successNotice(for action: String) -> String {
        switch action {
        case "start": return "Voice session ready. Shared — any agent may use it. Mic closed until a turn listens."
        case "pause": return "Voice session paused. The microphone is closed."
        case "resume": return "Voice session resumed. Shared queue; mic still closed until a turn listens."
        case "cancel": return "Current turn cancelled. Mic closing."
        case "take_floor": return "Agent stopped. Listening if it was your turn next."
        case "end_turn": return "Got it. Recording closed; transcribing what you said."
        case "stop": return "Voice stopped. Microphone closed."
        case "cycle_mode": return "Mode cycled. Talk = both · Narrate = agent speaks · Dictate = you only."
        case "note": return "Listening for your note — speak now, then you can walk away."
        case "reply": return "Listening for your reply — speak now, then you can walk away."
        case "gate_close": return "Got it. Recording closed; transcribing what you said."
        case "dictate_start": return "Dictating — release ⌘§ to type it."
        case "read_aloud": return "Reading your selection. Tap ⌘§ again to stop."
        case "repeat": return "Replaying the agent's last speech."
        default: return "Vox control applied."
        }
    }

    @objc private func modeChanged() {
        let mode = ["talk", "narrate", "dictate"][max(0, min(2, modeControl.selectedSegment))]
        sendControl("set_mode", notice: "Switching to \(modeTitle(mode))…", extra: ["mode": mode])
    }

    @objc private func endTurn() {
        sendControl("end_turn", notice: "Got it. Closing recording and transcribing…")
    }

    @objc private func repeatLast() {
        sendControl("repeat", notice: "Replaying the last thing I said…")
    }

    // Grab the mic and answer whoever last spoke, no agent picker. Reuses the
    // note delivery path, so the reply reaches that agent on its next turn.
    @objc private func replyToLastSpeaker() {
        let who = lastSpokenAgent ?? "the last agent"
        sendControl("reply", notice: "Listening — reply to \(who), then you can walk away.")
    }

    // Speak a note without waiting for an agent to open the mic. First pick WHO
    // it's for (each agent = a project/voice), so it reaches only that agent —
    // not whoever happens to poll first. Then the mic opens and you talk.
    @objc private func leaveNote() {
        let menu = NSMenu()
        let header = NSMenuItem(title: "Speak a note to…", action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)
        menu.addItem(NSMenuItem.separator())
        for agent in agents {
            let waiting = notesWaiting.contains(agent)
            let item = NSMenuItem(
                title: waiting ? "\(agent)   ● note waiting" : agent,
                action: #selector(sendNoteTo(_:)),
                keyEquivalent: ""
            )
            item.representedObject = agent
            item.target = self
            menu.addItem(item)
        }
        if !agents.isEmpty { menu.addItem(NSMenuItem.separator()) }
        let any = NSMenuItem(title: "Any agent (first to check)", action: #selector(sendNoteTo(_:)), keyEquivalent: "")
        any.representedObject = ""
        any.target = self
        menu.addItem(any)
        if let event = NSApp.currentEvent {
            NSMenu.popUpContextMenu(menu, with: event, for: primaryButton)
        } else {
            menu.popUp(positioning: nil, at: NSPoint(x: 0, y: primaryButton.bounds.height), in: primaryButton)
        }
    }

    @objc private func sendNoteTo(_ sender: NSMenuItem) {
        let target = (sender.representedObject as? String) ?? ""
        let who = target.isEmpty ? "the next agent that checks" : target
        var extra: [String: Any] = [:]
        if !target.isEmpty { extra["target_agent"] = target }
        sendControl("note", notice: "Listening — speak your note for \(who), then walk away.", extra: extra)
    }

    // One session on/off control — the whole Stop / Start / Resume / Pause /
    // Cancel pile-up collapsed into a single contextual toggle.
    @objc private func toggleSession() {
        switch state.lowercased() {
        case "off", "offline", "error":
            sendControl("start", notice: "Turning Vox on…")
        case "paused":
            sendControl("resume", notice: "Resuming Vox…")
        default:
            sendControl("stop", notice: "Turning Vox off. Microphone closed.")
        }
    }

    @objc private func showMoreMenu() {
        let menu = NSMenu()
        let normalized = state.lowercased()
        let toggleTitle: String
        switch normalized {
        case "off", "offline", "error": toggleTitle = "Turn Vox on"
        case "paused": toggleTitle = "Resume Vox"
        default: toggleTitle = "Turn Vox off"
        }
        let noteItem = NSMenuItem(title: "Leave a note for a specific agent…", action: #selector(leaveNote), keyEquivalent: "")
        noteItem.isEnabled = !controlInFlight && normalized == "idle"
        menu.addItem(noteItem)
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: toggleTitle, action: #selector(toggleSession), keyEquivalent: ""))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Restart Vox (if it's stuck)", action: #selector(restartRuntime), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Open Vox folder", action: #selector(openActivity), keyEquivalent: ""))
        for item in menu.items where item.action != nil {
            item.target = self
        }
        if let event = NSApp.currentEvent {
            NSMenu.popUpContextMenu(menu, with: event, for: moreButton)
        } else {
            menu.popUp(positioning: nil, at: NSPoint(x: 0, y: moreButton.bounds.height), in: moreButton)
        }
    }

    /// Idle display sleep only — a closed lid, a lock, or an explicit sleep
    /// still pause exactly as before.
    private func keepDisplayAwake(_ awake: Bool) {
        if awake, turnAwakeAssertion == 0 {
            var id = IOPMAssertionID(0)
            let created = IOPMAssertionCreateWithName(
                kIOPMAssertionTypePreventUserIdleDisplaySleep as CFString,
                IOPMAssertionLevel(kIOPMAssertionLevelOn),
                "Vox voice turn in progress" as CFString,
                &id
            )
            if created == kIOReturnSuccess { turnAwakeAssertion = id }
        } else if !awake, turnAwakeAssertion != 0 {
            IOPMAssertionRelease(turnAwakeAssertion)
            turnAwakeAssertion = 0
        }
    }

    @objc private func pauseBeforeSleep() {
        actionNotice = "Pausing before sleep and closing the microphone…"
        if !sendControlSynchronously("pause", timeout: 3.5) {
            actionNotice = "Could not confirm the privacy pause before sleep"
        } else {
            actionNotice = "Paused before sleep. The microphone is closed."
        }
        updatePresentation()
    }

    @objc private func protectPrivacyForInactiveSession() {
        sendControl("pause", notice: "Pausing because the Mac became inactive…")
    }

    private func sendControlSynchronously(_ action: String, timeout: TimeInterval) -> Bool {
        var request = URLRequest(url: baseURL.appendingPathComponent("control"))
        request.httpMethod = "POST"
        request.timeoutInterval = timeout
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("status-bar", forHTTPHeaderField: "X-Vox-Control-Source")
        if let token = try? String(
            contentsOf: FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent(".vox/control.token"),
            encoding: .utf8
        ) {
            request.setValue(token.trimmingCharacters(in: .whitespacesAndNewlines), forHTTPHeaderField: "X-Vox-Token")
        }
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["action": action])
        let finished = DispatchSemaphore(value: 0)
        var accepted = false
        URLSession.shared.dataTask(with: request) { _, response, _ in
            if let http = response as? HTTPURLResponse { accepted = (200..<300).contains(http.statusCode) }
            finished.signal()
        }.resume()
        return finished.wait(timeout: .now() + timeout) == .success && accepted
    }

    @objc private func restartRuntime() {
        actionNotice = "Restarting Vox runtime…"
        stopChildRuntime()
        quitting = false
        runtimeFailures.removeAll()
        startChildRuntime()
        updatePresentation()
    }

    @objc private func openActivity() {
        let folder = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".vox")
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        NSWorkspace.shared.open(folder)
    }
}

@main
enum VoxStatusMain {
    static func main() {
        let application = NSApplication.shared
        let delegate = VoxAppDelegate()
        application.delegate = delegate
        application.run()
    }
}

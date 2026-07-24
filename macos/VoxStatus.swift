import AppKit
import AVFoundation
import Foundation

/// The menu-bar companion is deliberately a session controller, not a hidden
/// always-listening recorder. A click opens this panel; the microphone only
/// opens when an MCP conversation explicitly starts a bounded listen.
final class VoxAppDelegate: NSObject, NSApplicationDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let popover = NSPopover()
    private let panelController = NSViewController()

    private let stateLabel = NSTextField(labelWithString: "Starting Vox…")
    private let detailLabel = NSTextField(wrappingLabelWithString: "Waiting for the local runtime")
    private let microphoneLabel = NSTextField(labelWithString: "Microphone: checking")
    private let modeLabel = NSTextField(labelWithString: "Mode: Talk")
    private let modeButton = NSButton(title: "Cycle mode", target: nil, action: nil)
    private let stopButton = NSButton(title: "Stop voice", target: nil, action: nil)
    private let endTurnButton = NSButton(title: "I'm done talking", target: nil, action: nil)
    private let cancelButton = NSButton(title: "Cancel this turn", target: nil, action: nil)
    private let moreButton = NSButton(title: "More…", target: nil, action: nil)

    private var runtime: Process?
    private var timer: Timer?
    private var runtimeFailures: [Date] = []
    private var restartWorkItem: DispatchWorkItem?
    private var runtimeProbePending = false
    private var controlInFlight = false
    private var quitting = false
    private var state = "starting"
    private var detail = "Waiting for Vox runtime"
    private var actionNotice: String?
    private var microphoneOpen = false
    private var lastStopReason: String?
    private var ioMode = "talk"
    private let baseURL = URL(string: "http://127.0.0.1:8766")!

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        configurePanel()
        guard let button = statusItem.button else { return }
        button.target = self
        button.action = #selector(statusItemClicked)
        button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        button.toolTip = "Vox — click to stop listening when the mic is live; right-click for controls"
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
        timer = Timer.scheduledTimer(withTimeInterval: 0.4, repeats: true) { [weak self] _ in
            self?.refreshStatus()
        }
        refreshStatus()
    }

    func applicationWillTerminate(_ notification: Notification) {
        quitting = true
        timer?.invalidate()
        NSWorkspace.shared.notificationCenter.removeObserver(self)
        stopChildRuntime()
    }

    private func configurePanel() {
        let root = NSView(frame: NSRect(x: 0, y: 0, width: 350, height: 0))
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false

        stateLabel.font = NSFont.systemFont(ofSize: 15, weight: .semibold)
        detailLabel.font = NSFont.systemFont(ofSize: 12)
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.maximumNumberOfLines = 3
        microphoneLabel.font = NSFont.systemFont(ofSize: 12, weight: .medium)
        modeLabel.font = NSFont.systemFont(ofSize: 12, weight: .medium)

        for button in [modeButton, stopButton, endTurnButton, cancelButton, moreButton] {
            button.target = self
            button.bezelStyle = .rounded
        }
        modeButton.action = #selector(cycleMode)
        stopButton.action = #selector(stopSession)
        endTurnButton.action = #selector(endTurn)
        cancelButton.action = #selector(cancelTurn)
        moreButton.action = #selector(showMoreMenu)

        let sessionRow = NSStackView(views: [modeButton, stopButton])
        sessionRow.orientation = .horizontal
        sessionRow.distribution = .fillEqually
        sessionRow.spacing = 8
        sessionRow.translatesAutoresizingMaskIntoConstraints = false

        let divider = NSBox()
        divider.boxType = .separator
        divider.translatesAutoresizingMaskIntoConstraints = false

        stack.addArrangedSubview(stateLabel)
        stack.addArrangedSubview(detailLabel)
        stack.addArrangedSubview(microphoneLabel)
        stack.addArrangedSubview(modeLabel)
        stack.addArrangedSubview(divider)
        stack.addArrangedSubview(sessionRow)
        stack.addArrangedSubview(endTurnButton)
        stack.addArrangedSubview(cancelButton)
        stack.addArrangedSubview(moreButton)

        root.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 14),
            stack.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -14),
            stack.topAnchor.constraint(equalTo: root.topAnchor, constant: 14),
            stack.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -14),
            detailLabel.widthAnchor.constraint(equalToConstant: 322),
            microphoneLabel.widthAnchor.constraint(equalTo: detailLabel.widthAnchor),
            modeLabel.widthAnchor.constraint(equalTo: detailLabel.widthAnchor),
            divider.widthAnchor.constraint(equalTo: detailLabel.widthAnchor),
            sessionRow.widthAnchor.constraint(equalTo: detailLabel.widthAnchor),
            endTurnButton.widthAnchor.constraint(equalTo: detailLabel.widthAnchor),
            cancelButton.widthAnchor.constraint(equalTo: detailLabel.widthAnchor),
            moreButton.widthAnchor.constraint(equalTo: detailLabel.widthAnchor),
        ])
        panelController.view = root
        popover.contentViewController = panelController
        popover.behavior = .transient
        popover.animates = true
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
        } else {
            updatePresentation()
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
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
        var request = URLRequest(url: baseURL.appendingPathComponent("health"))
        request.timeoutInterval = 1.0
        URLSession.shared.dataTask(with: request) { [weak self] data, _, error in
            guard let self else { return }
            DispatchQueue.main.async {
                if let error {
                    self.state = "offline"
                    self.detail = error.localizedDescription
                    self.microphoneOpen = false
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
                self.detail = (payload["detail"] as? String) ?? "Local-only runtime connected"
                self.lastStopReason = payload["last_stop_reason"] as? String
                self.ioMode = (payload["io_mode"] as? String) ?? self.ioMode
                self.updatePresentation()
            }
        }.resume()
    }

    private func updatePresentation() {
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
        statusItem.button?.toolTip = microphoneOpen
            ? "Mic is LIVE — click to stop listening. \(panelDetail())"
            : panelDetail()

        stateLabel.stringValue = title
        detailLabel.stringValue = panelDetail()
        microphoneLabel.stringValue = microphoneOpen
            ? "Microphone: listening now"
            : "Microphone: closed — never records in the background"
        modeLabel.stringValue = "Mode: \(modeTitle(ioMode)) — Talk both · Narrate agent only · Dictate you only"

        let activeTurn = ["listening", "speaking", "processing"].contains(normalized)
        // Surface the contextual action right on the "done" button so the panel
        // leads with what matters now: stop listening while the mic is live.
        endTurnButton.title = microphoneOpen ? "Stop listening (keep what I said)" : "I'm done talking"
        modeButton.title = "Mode: \(modeTitle(ioMode))"
        modeButton.isEnabled = !controlInFlight && normalized != "offline"
        stopButton.isEnabled = !controlInFlight && !["off", "offline", "error"].contains(normalized)
        // Off/paused: Stop becomes Start for one-tap session open.
        if normalized == "off" || normalized == "paused" || normalized == "error" {
            stopButton.title = normalized == "paused" ? "Resume voice" : "Start voice"
        } else {
            stopButton.title = "Stop voice"
        }
        cancelButton.isEnabled = !controlInFlight && activeTurn
        // Unlike Cancel, this preserves the recording and sends it straight to
        // local transcription. It is meaningful only while the user is speaking.
        endTurnButton.isEnabled = !controlInFlight && normalized == "listening"
        moreButton.isEnabled = !controlInFlight
    }

    // The menu-bar item is a glanceable glyph, not a wordy string. It shows a
    // red mic ONLY when the microphone is genuinely capturing (driven by the
    // runtime's microphone_open truth, never by stale session state), which is
    // the whole fix for "it always looks like it's listening."
    private func applyStatusGlyph(normalized: String, title: String) {
        guard let button = statusItem.button else { return }
        let symbol: String
        if microphoneOpen {
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
        let config = NSImage.SymbolConfiguration(pointSize: 15, weight: .semibold)
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: title)?
            .withSymbolConfiguration(config)
        image?.isTemplate = true
        if let image {
            button.image = image
            button.imagePosition = .imageOnly
            button.title = ""
        } else {
            // Fall back to text if the SF Symbol is unavailable on this OS.
            button.image = nil
            button.title = title
        }
        // Template images adopt this tint; nil lets the menu bar pick its own
        // adaptive color so idle/closed states never read as "hot."
        button.contentTintColor = microphoneOpen ? NSColor.systemRed : nil
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
        if state.lowercased() == "off", lastStopReason == "idle_timeout" {
            return "Stopped after 10 minutes without activity. The microphone is closed."
        }
        return detail
    }

    private func sendControl(_ action: String, notice: String, extra: [String: Any] = [:]) {
        guard !controlInFlight else { return }
        controlInFlight = true
        actionNotice = notice
        updatePresentation()
        var request = URLRequest(url: baseURL.appendingPathComponent("control"))
        request.httpMethod = "POST"
        // Stop/pause wait up to ~3s for the mic to close server-side. A 2s
        // client timeout made Stop look broken while the runtime was still
        // finishing the privacy shutdown.
        request.timeoutInterval = 6.0
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("status-bar", forHTTPHeaderField: "X-Vox-Control-Source")
        if let token = try? String(contentsOf: FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".vox/control.token"), encoding: .utf8) {
            request.setValue(token.trimmingCharacters(in: .whitespacesAndNewlines), forHTTPHeaderField: "X-Vox-Token")
        }
        var payload: [String: Any] = ["action": action]
        payload.merge(extra) { _, new in new }
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self else { return }
                self.controlInFlight = false
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

    private func successNotice(for action: String) -> String {
        switch action {
        case "start": return "Voice session ready. Shared — any agent may use it. Mic closed until a turn listens."
        case "pause": return "Voice session paused. The microphone is closed."
        case "resume": return "Voice session resumed. Shared queue; mic still closed until a turn listens."
        case "cancel": return "Current turn cancelled. Mic closing."
        case "end_turn": return "Got it. Recording closed; transcribing what you said."
        case "stop": return "Voice stopped. Microphone closed."
        case "cycle_mode": return "Mode cycled. Talk = both · Narrate = agent speaks · Dictate = you only."
        default: return "Vox control applied."
        }
    }

    @objc private func cycleMode() {
        sendControl("cycle_mode", notice: "Cycling Talk → Narrate → Dictate…")
    }

    @objc private func selectMode(_ sender: NSMenuItem) {
        guard let mode = sender.representedObject as? String else { return }
        sendControl("set_mode", notice: "Switching to \(modeTitle(mode))…", extra: ["mode": mode])
    }

    @objc private func cancelTurn() { sendControl("cancel", notice: "Cancelling current turn…") }

    @objc private func endTurn() {
        sendControl("end_turn", notice: "Got it. Closing recording and transcribing…")
    }

    @objc private func stopSession() {
        switch state.lowercased() {
        case "off", "offline", "error":
            sendControl("start", notice: "Starting voice session…")
        case "paused":
            sendControl("resume", notice: "Resuming voice session…")
        default:
            sendControl("stop", notice: "Stopping voice…")
        }
    }

    @objc private func showMoreMenu() {
        let menu = NSMenu()
        // Jump straight to a mode instead of cycling through all three. A tick
        // marks the current one.
        for (mode, label) in [
            ("talk", "Talk — both of us speak"),
            ("narrate", "Narrate — I speak, you type"),
            ("dictate", "Dictate — you speak, I type"),
        ] {
            let item = NSMenuItem(title: label, action: #selector(selectMode(_:)), keyEquivalent: "")
            item.representedObject = mode
            item.state = ioMode.lowercased() == mode ? .on : .off
            menu.addItem(item)
        }
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Start / resume session", action: #selector(startOrResume), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Pause (privacy hold)", action: #selector(pauseSession), keyEquivalent: ""))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Restart runtime", action: #selector(restartRuntime), keyEquivalent: ""))
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

    @objc private func startOrResume() {
        switch state.lowercased() {
        case "paused": sendControl("resume", notice: "Resuming…")
        default: sendControl("start", notice: "Starting…")
        }
    }

    @objc private func pauseSession() {
        sendControl("pause", notice: "Pausing and closing the microphone…")
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

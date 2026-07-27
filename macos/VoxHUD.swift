import AppKit

/// The floating pill that says, on screen, what Vox is doing with the microphone
/// and the speaker.
///
/// Until now the only cues were a 22-point menu-bar glyph and an earcon, which is
/// not enough for something that can be listening while you look at a different
/// app entirely. This is the Wispr-Flow-shaped answer: a capsule at the bottom of
/// the screen that exists *only* while something is happening, so its mere
/// presence is the signal and its absence is the reassurance.
///
/// Two rules it must never break:
///
///  * **It never takes focus.** A non-activating panel, ordered front with
///    `orderFrontRegardless()` and never `makeKeyAndOrderFront`. Stealing key
///    status would move the insertion point out of the field dictation is about
///    to paste into.
///  * **It never invents a level.** Every bar is measured: listening and
///    dictating draw the microphone, and `speaking` draws the envelope of the
///    clip actually playing, published by the runtime frame by frame as the
///    clock plays it. `afplay` itself exposes no output level — the runtime
///    reads the file it is playing instead, which is the same signal.
enum HUDState: Equatable {
    /// The device is open but the gate is still shut: the stream-open guard is
    /// being waited out. Shown so that first second reads as deliberate warm-up
    /// rather than as a key press that did nothing.
    case warming
    /// Listening for an agent turn.
    case listening
    /// Listening for the cursor — the text is going into the frontmost app, not
    /// to an agent. Deliberately a different colour, because "who gets this
    /// text" is the single most important thing to be able to see at a glance.
    case dictating
    /// Vox is talking. Distinct from every listening state.
    case speaking
}

final class HUDPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

final class VoxHUD {
    /// Bottom-centre, roughly a thumb's width up from the edge — clear of the
    /// Dock, and out of the way of whatever is being typed into.
    private static let bottomInset: CGFloat = 120
    private static let size = NSSize(width: 148, height: 46)

    private let panel: HUDPanel
    private let meter = LevelMeterView()
    private var state: HUDState?
    private var onClick: () -> Void = {}

    init() {
        panel = HUDPanel(
            contentRect: NSRect(origin: .zero, size: VoxHUD.size),
            // .nonactivatingPanel is the load-bearing flag: without it, showing
            // the HUD would activate Vox and the frontmost app would lose focus.
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.level = .statusBar
        panel.isMovable = false
        panel.hidesOnDeactivate = false
        panel.ignoresMouseEvents = false
        // Follow the user across spaces and into full-screen apps, and stay out
        // of ⌘` window cycling.
        panel.collectionBehavior = [
            .canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle,
        ]

        let blur = NSVisualEffectView()
        blur.material = .hudWindow
        blur.blendingMode = .behindWindow
        blur.state = .active
        blur.wantsLayer = true
        blur.layer?.cornerRadius = VoxHUD.size.height / 2
        blur.layer?.cornerCurve = .continuous
        blur.layer?.masksToBounds = true
        blur.translatesAutoresizingMaskIntoConstraints = false

        meter.translatesAutoresizingMaskIntoConstraints = false
        blur.addSubview(meter)

        let click = NSClickGestureRecognizer(target: self, action: #selector(clicked))
        blur.addGestureRecognizer(click)

        let root = NSView(frame: NSRect(origin: .zero, size: VoxHUD.size))
        root.addSubview(blur)
        NSLayoutConstraint.activate([
            blur.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            blur.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            blur.topAnchor.constraint(equalTo: root.topAnchor),
            blur.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            meter.leadingAnchor.constraint(equalTo: blur.leadingAnchor, constant: 16),
            meter.trailingAnchor.constraint(equalTo: blur.trailingAnchor, constant: -16),
            meter.centerYAnchor.constraint(equalTo: blur.centerYAnchor),
            meter.heightAnchor.constraint(equalToConstant: 22),
        ])
        panel.contentView = root
        panel.alphaValue = 0

        NotificationCenter.default.addObserver(
            self,
            selector: #selector(screensChanged),
            name: NSApplication.didChangeScreenParametersNotification,
            object: nil
        )
    }

    /// What a click on the pill does — ending the turn, matching the menu bar.
    func onTap(_ handler: @escaping () -> Void) {
        onClick = handler
    }

    @objc private func clicked() {
        onClick()
    }

    @objc private func screensChanged() {
        if state != nil { reposition() }
    }

    /// Drive the HUD from one status poll. `nil` hides it.
    ///
    /// `levels` is every level the runtime measured since the last poll, oldest
    /// first — a burst, not a single reading, which is what lets the bars carry
    /// the microphone's real ~50 Hz detail at a 12.5 Hz poll rate.
    func apply(_ next: HUDState?, levels: [CGFloat]) {
        guard let next else {
            hide()
            return
        }
        if state != next {
            state = next
            meter.tint = next.tint
            if panel.alphaValue == 0 { reposition() }
            show()
        }
        switch next {
        case .warming:
            // Flat and dim: the device is open, but nothing is being heard yet
            // and pretending otherwise would be the same lie as a fake meter.
            meter.active = false
            meter.settle()
        case .listening, .dictating, .speaking:
            // Speaking bars are as measured as listening bars: the runtime
            // publishes the envelope of the clip actually playing, frame by
            // frame as the clock plays it, so this is the audio leaving the
            // speaker — not an animation of one.
            meter.active = true
            meter.push(levels)
        }
    }

    private func show() {
        guard panel.alphaValue == 0 else { return }
        // orderFrontRegardless, never makeKeyAndOrderFront: this must appear
        // without Vox becoming the active application.
        panel.orderFrontRegardless()
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0.15
            panel.animator().alphaValue = 1
        }
    }

    private func hide() {
        state = nil
        guard panel.alphaValue != 0 || panel.isVisible else { return }
        NSAnimationContext.runAnimationGroup(
            { context in
                context.duration = 0.15
                panel.animator().alphaValue = 0
            },
            completionHandler: { [panel] in
                if panel.alphaValue == 0 { panel.orderOut(nil) }
            }
        )
    }

    /// Bottom-centre of whichever screen the user is actually looking at, which
    /// is best approximated by where the pointer is.
    private func reposition() {
        let pointer = NSEvent.mouseLocation
        let screen =
            NSScreen.screens.first { $0.frame.contains(pointer) }
            ?? NSScreen.main
            ?? NSScreen.screens.first
        guard let frame = screen?.visibleFrame else { return }
        panel.setFrameOrigin(
            NSPoint(
                x: frame.midX - VoxHUD.size.width / 2,
                y: frame.minY + VoxHUD.bottomInset
            )
        )
    }
}

extension HUDState {
    /// Colour carries the one thing the shape cannot: where the words are going.
    var tint: NSColor {
        switch self {
        case .warming: return .tertiaryLabelColor
        case .listening: return .systemRed
        case .dictating: return .systemTeal
        case .speaking: return .systemBlue
        }
    }
}

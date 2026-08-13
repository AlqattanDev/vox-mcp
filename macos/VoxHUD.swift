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
    /// Bottom-centre, a short hop up from the edge — clear of the Dock, low
    /// enough to sit at the bottom of vision rather than in the middle of it.
    private static let bottomInset: CGFloat = 72
    /// A small circle, not a pill: the presence is the signal, so it wants the
    /// least real estate that still reads at a glance. Sized so the meter inside
    /// has room to swing — a loudness you have to squint at is not a meter.
    private static let size = NSSize(width: 52, height: 52)

    private let panel: HUDPanel
    private let orb = OrbMeterView()
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
        // Above the full-screen layer, not just the menu bar. `.statusBar`
        // with canJoinAllSpaces follows you between ordinary spaces but a
        // full-screen app's own window sits above it and swallows the pill —
        // which is exactly where being able to see the mic state matters most.
        // The shielding level floats over full-screen apps the way Wispr Flow's
        // does; the pill is tiny and only present mid-turn, so it earns it.
        panel.level = NSWindow.Level(rawValue: Int(CGShieldingWindowLevel()))
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

        orb.translatesAutoresizingMaskIntoConstraints = false
        blur.addSubview(orb)

        let click = NSClickGestureRecognizer(target: self, action: #selector(clicked))
        blur.addGestureRecognizer(click)

        let root = NSView(frame: NSRect(origin: .zero, size: VoxHUD.size))
        root.addSubview(blur)
        NSLayoutConstraint.activate([
            blur.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            blur.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            blur.topAnchor.constraint(equalTo: root.topAnchor),
            blur.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            // The orb fills the circle, square and centred, with a hair of
            // breathing room to the frosted rim.
            orb.leadingAnchor.constraint(equalTo: blur.leadingAnchor, constant: 5),
            orb.trailingAnchor.constraint(equalTo: blur.trailingAnchor, constant: -5),
            orb.topAnchor.constraint(equalTo: blur.topAnchor, constant: 5),
            orb.bottomAnchor.constraint(equalTo: blur.bottomAnchor, constant: -5),
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
            orb.tint = next.tint
            orb.speaking = next == .speaking
            if panel.alphaValue == 0 { reposition() }
            show()
        }
        switch next {
        case .warming:
            // Flat and dim: the device is open, but nothing is being heard yet
            // and pretending otherwise would be the same lie as a fake meter.
            orb.active = false
            orb.settle()
        case .listening, .dictating:
            orb.active = true
            orb.push(levels)
        case .speaking:
            // While Vox talks the orb is not a meter but a control: a stop
            // glyph, because what you want during read-aloud is a way to end
            // it, not a picture of the sound. A click stops the speech.
            orb.active = true
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

/// The round face of the pill: a solid disc that grows with your voice, with a
/// soft glow swelling behind it — the shape ChatGPT's voice mode uses, chosen
/// deliberately over anything invented here.
///
/// Everything drawn before this was a shape inside another shape: a baseline
/// ring with a fan of bars around it and a dot pulsing in the middle, then a
/// rippling outline. Both made the eye assemble a reading out of parts, at 40
/// points across, for the one question actually being asked — *how loud am I
/// right now*. So there is now no outline, no ring, no core: a single filled
/// disc whose radius is the level, and light behind it that swells with the
/// same number. Nothing to decode.
///
/// While speaking it drops the meter for a stop glyph — the pill becomes the
/// button that ends the read.
///
/// Same contract as the meters before it: `active`, `tint`, `push`, `settle`.
final class OrbMeterView: NSView {
    /// Meter ballistics, applied per 20 ms sample: jump most of the way to a
    /// louder reading at once, fall back slowly. This is what a level meter has
    /// always done — a needle that tracked every sample exactly would twitch too
    /// fast to read, and one that lagged both ways would miss the peak that
    /// makes speech legible. It shapes *when* a measured level is shown, never
    /// what it is: nothing here can move without a sample having moved first.
    private static let attack: CGFloat = 0.45
    private static let release: CGFloat = 0.10

    /// The slice of the runtime's 0..1 dBFS scale a human voice actually uses,
    /// stretched to fill the meter. `quietLevel` is about -55 dBFS — a still
    /// room — and `loudLevel` about -18, which is already a raised voice. See
    /// `draw` for why drawing against the full scale reads as permanently quiet.
    private static let quietLevel: CGFloat = 0.08
    private static let loudLevel: CGFloat = 0.70

    /// The displayed level, 0...1 — the measured level after ballistics.
    private var level: CGFloat = 0
    var active = false { didSet { needsDisplay = true } }
    var tint: NSColor? { didSet { needsDisplay = true } }
    /// Speaking swaps the meter for a stop control.
    var speaking = false { didSet { needsDisplay = true } }

    override var isFlipped: Bool { false }

    func push(_ levels: [CGFloat]) {
        guard !levels.isEmpty else { return }
        for sample in levels {
            let measured = max(0, min(1, sample))
            let rate = measured > level ? OrbMeterView.attack : OrbMeterView.release
            level += (measured - level) * rate
        }
        needsDisplay = true
    }

    func settle() {
        level *= 0.5
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        guard bounds.width > 0 else { return }
        let center = NSPoint(x: bounds.midX, y: bounds.midY)
        let color = active ? (tint ?? NSColor.controlAccentColor) : NSColor.tertiaryLabelColor

        if speaking {
            // A rounded square, centred: the universal "stop", sized to sit
            // comfortably inside the circle. The whole pill is the hit target.
            let side: CGFloat = 12
            let square = NSRect(
                x: center.x - side / 2, y: center.y - side / 2, width: side, height: side
            )
            color.setFill()
            NSBezierPath(roundedRect: square, xRadius: 3, yRadius: 3).fill()
            return
        }

        // The runtime's 0..1 level is linear in dBFS from -60 up to 0, and the
        // top of that scale is unreachable: 0 dBFS is a clipped signal, and
        // ordinary speech lives around -30 (~0.5) while a raised voice barely
        // passes -18 (~0.7). Drawn against full scale the orb therefore spends
        // every turn in the bottom half of its travel, which is exactly what it
        // looked like. So the band a voice actually occupies is stretched to
        // fill the face: room tone sits at the floor, a normal speaking voice
        // lands high, and a raised one reaches the top. It is a gain and a
        // curve on a measured number — the orb still cannot move unless the
        // microphone did.
        let normalized = max(
            0, min(1, (level - OrbMeterView.quietLevel) / (OrbMeterView.loudLevel - OrbMeterView.quietLevel))
        )
        let loudness = pow(normalized, 0.55)
        let maxR = min(bounds.width, bounds.height) / 2 - 1
        // Silence is a small disc rather than nothing: the pill is only on
        // screen when the microphone is genuinely open, and it should look
        // ready. Loud very nearly fills the face — the swing between the two is
        // the whole point of the thing.
        let restR = maxR * 0.16
        let discR = restR + (maxR * 0.88 - restR) * loudness

        // The glow, first, so the disc sits on top of it. It reaches past the
        // disc and fades to nothing, and both its reach and its strength ride
        // the same measured level, which is what makes a loud moment read
        // instantly from the corner of your eye.
        let glowR = discR + (maxR - discR) * (0.45 + 0.55 * loudness)
        if active, glowR > discR,
            let glow = NSGradient(
                colors: [
                    color.withAlphaComponent(0.30 + 0.35 * loudness),
                    color.withAlphaComponent(0),
                ],
                atLocations: [0, 1],
                colorSpace: .deviceRGB
            )
        {
            glow.draw(
                fromCenter: center, radius: discR, toCenter: center, radius: glowR, options: []
            )
        }

        color.withAlphaComponent(active ? 0.95 : 0.4).setFill()
        NSBezierPath(
            ovalIn: NSRect(
                x: center.x - discR, y: center.y - discR, width: discR * 2, height: discR * 2
            )
        ).fill()
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

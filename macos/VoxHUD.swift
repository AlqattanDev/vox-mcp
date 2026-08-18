import AppKit
import QuartzCore

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
    /// least real estate that still reads at a glance. Sized so the glass
    /// sphere inside has room for its shading to read — a marble you have to
    /// squint at is just a dot.
    private static let size = NSSize(width: 64, height: 64)

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
            orb.leadingAnchor.constraint(equalTo: blur.leadingAnchor, constant: 3),
            orb.trailingAnchor.constraint(equalTo: blur.trailingAnchor, constant: -3),
            orb.topAnchor.constraint(equalTo: blur.topAnchor, constant: 3),
            orb.bottomAnchor.constraint(equalTo: blur.bottomAnchor, constant: -3),
        ])
        panel.contentView = root
        panel.contentView?.wantsLayer = true
        panel.alphaValue = 0

        NotificationCenter.default.addObserver(
            self,
            selector: #selector(screensChanged),
            name: NSApplication.didChangeScreenParametersNotification,
            object: nil
        )
        NSWorkspace.shared.notificationCenter.addObserver(
            self,
            selector: #selector(screensChanged),
            name: NSWorkspace.activeSpaceDidChangeNotification,
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
        }
        // Full-screen terminals (tmux, iTerm) and Space switches can bury the
        // pill after it was first shown. Climb back every poll, not only on
        // the listening/speaking edge.
        reposition()
        show()
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
            // While Vox talks the sphere is both meter and control: the exact
            // same meter, because the runtime publishes the envelope of the
            // clip actually playing and that is the same kind of signal the
            // microphone gives. Only the tint changes, plus a stop glyph at
            // the centre — the pill is still the button that ends the read.
            orb.active = true
            orb.push(levels)
        }
    }

    private func show() {
        // orderFrontRegardless, never makeKeyAndOrderFront: this must appear
        // without Vox becoming the active application. Call it every poll —
        // a full-screen app can steal the layer after the first show.
        panel.orderFrontRegardless()
        if let layer = panel.contentView?.layer {
            layer.removeAllAnimations()
            layer.transform = CATransform3DIdentity
            layer.opacity = 1
        }
        if panel.alphaValue == 1 { return }
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0.15
            panel.animator().alphaValue = 1
        }
    }

    private func hide() {
        state = nil
        guard panel.alphaValue != 0 || panel.isVisible else { return }
        guard let layer = panel.contentView?.layer else {
            panel.orderOut(nil)
            return
        }
        layer.removeAllAnimations()
        // The transform scales about the layer's anchor point, and a layer-backed
        // NSView does not promise that anchor is the centre. Pin it here, moving
        // `position` to match so the layer does not jump as we do it.
        centerAnchor(of: layer)
        let pop = CAAnimationGroup()
        pop.duration = 0.28
        pop.fillMode = .forwards
        pop.isRemovedOnCompletion = false
        pop.timingFunction = CAMediaTimingFunction(name: .easeOut)
        // Shrink away, never outward. The panel is exactly orb-sized, so a
        // scale above 1 pushes the sphere past the window's own bounds and it
        // comes back clipped into wedges — which is what the exit had become.
        let scale = CABasicAnimation(keyPath: "transform.scale")
        scale.fromValue = 1
        scale.toValue = 0.55
        let fade = CABasicAnimation(keyPath: "opacity")
        fade.fromValue = 1
        fade.toValue = 0
        pop.animations = [scale, fade]
        CATransaction.begin()
        CATransaction.setCompletionBlock { [panel] in
            layer.removeAllAnimations()
            layer.transform = CATransform3DIdentity
            layer.opacity = 1
            panel.alphaValue = 0
            panel.orderOut(nil)
        }
        layer.add(pop, forKey: "explode")
        CATransaction.commit()
    }

    /// Move a layer's anchor point to its centre without moving the layer.
    private func centerAnchor(of layer: CALayer) {
        guard layer.anchorPoint != CGPoint(x: 0.5, y: 0.5) else { return }
        let frame = layer.frame
        layer.anchorPoint = CGPoint(x: 0.5, y: 0.5)
        layer.position = CGPoint(x: frame.midX, y: frame.midY)
    }

    /// Bottom-centre of whichever screen the user is actually looking at, which
    /// is best approximated by where the pointer is.
    private func reposition() {
        let pointer = NSEvent.mouseLocation
        let screen =
            NSScreen.screens.first { $0.frame.contains(pointer) }
            ?? NSScreen.main
            ?? NSScreen.screens.first
        // Use the full screen frame, not visibleFrame. In a macOS full-screen
        // space visibleFrame still describes the desktop, so the pill can land
        // off the space the user is actually looking at.
        guard let frame = screen?.frame else { return }
        panel.setFrameOrigin(
            NSPoint(
                x: frame.midX - VoxHUD.size.width / 2,
                y: frame.minY + VoxHUD.bottomInset
            )
        )
    }
}

/// The round face of the pill: a liquid-glass sphere — the visionOS look,
/// chosen deliberately over anything invented here — with a coloured liquid
/// core suspended inside it that swells with your voice.
///
/// The flat disc before it (ChatGPT voice mode's shape) was honest but cheap:
/// a single filled circle is a meter, not an object. The sphere is built from
/// stacked pieces, each doing the job it does in a real glass marble: a shaded
/// shell lit from the top-left, a saturated core deep inside it, a terminator
/// darkening the far side over that core, a hard specular glint that never
/// moves (lights don't), a bright refracted crescent at the bottom edge, and a
/// glow behind the whole thing.
///
/// Every one of those rides the measured level — shell radius, core radius and
/// heat, glow reach, glint and rim brightness all move together on the same
/// number. That is deliberate: the first version moved only the core inside a
/// fixed shell, which was legible at arm's length and invisible from across a
/// desk. Six cues moving at once is what makes a raised voice unmistakable
/// without any of them being invented.
///
/// While speaking, the sphere behaves *identically* — it is one animation, not
/// two. Vox's turn briefly had orbiting eddies inside the core instead, which
/// read as a spinning fan and made the two halves of a conversation look like
/// unrelated widgets. Colour already says who is talking; the shape says how
/// loud, and it should say it the same way in both directions. The only thing
/// speaking adds is the stop glyph at the centre, because the pill is still the
/// button that ends the read.
///
/// Same contract as the meters before it: `active`, `tint`, `push`, `settle`.
/// Nothing here moves without a measured sample having moved first.
final class OrbMeterView: NSView {
    /// Meter ballistics, applied per 20 ms sample: jump most of the way to a
    /// louder reading at once, fall back slowly. This is what a level meter has
    /// always done — a needle that tracked every sample exactly would twitch too
    /// fast to read, and one that lagged both ways would miss the peak that
    /// makes speech legible. It shapes *when* a measured level is shown, never
    /// what it is: nothing here can move without a sample having moved first.
    private static let attack: CGFloat = 0.55
    private static let release: CGFloat = 0.09

    /// The slice of the runtime's 0..1 dBFS scale a human voice actually uses,
    /// stretched to fill the meter. `quietLevel` is about -56 dBFS — a still
    /// room — and `loudLevel` about -22, which is already a raised voice. See
    /// `draw` for why drawing against the full scale reads as permanently quiet.
    private static let quietLevel: CGFloat = 0.06
    private static let loudLevel: CGFloat = 0.62

    /// The runtime measures a level roughly every 20 ms. The menu-bar app polls
    /// far more slowly and hands over the whole burst at once, so replaying the
    /// burst at the rate it was recorded — rather than collapsing it into one
    /// redraw — is what turns a 12 Hz flip-book back into motion.
    private static let sampleInterval: CFTimeInterval = 0.02
    /// How long the drawn level takes to close most of the gap to the meter's
    /// level, in seconds. Purely a render smoother between measured points.
    private static let slewTime: CGFloat = 0.09

    /// Measured samples that have arrived and not yet been shown.
    private var queue: [CGFloat] = []
    /// The meter's level, 0...1 — measured samples after ballistics. Where the
    /// picture is heading.
    private var level: CGFloat = 0
    /// The level actually drawn. It chases `level` every frame, which is what
    /// keeps the sphere moving continuously between two measured points instead
    /// of stepping from one to the next.
    private var shown: CGFloat = 0
    private var sampleDebt: CFTimeInterval = 0
    private var lastFrame: CFTimeInterval = 0
    private var frames: Timer?

    var active = false { didSet { needsDisplay = true } }
    var tint: NSColor? { didSet { needsDisplay = true } }
    /// Speaking changes nothing but the tint and the stop glyph: the sphere
    /// behaves identically whoever is talking, because it is the same meter
    /// reading the same kind of signal.
    var speaking = false { didSet { needsDisplay = true } }

    override var isFlipped: Bool { false }

    deinit { frames?.invalidate() }

    func push(_ levels: [CGFloat]) {
        guard !levels.isEmpty else { return }
        queue.append(contentsOf: levels.map { max(0, min(1, $0)) })
        // A poll that arrives late must not turn into slow motion: if the
        // backlog is longer than a couple of polls, fold the oldest samples in
        // at once so the picture stays on the present rather than replaying the
        // past. The samples are still all applied — none is invented or skipped.
        if queue.count > 16 {
            while queue.count > 8 { advance(queue.removeFirst()) }
        }
        run()
    }

    func settle() {
        queue.removeAll()
        level *= 0.5
        run()
    }

    /// One measured sample through the ballistics.
    private func advance(_ measured: CGFloat) {
        let rate = measured > level ? OrbMeterView.attack : OrbMeterView.release
        level += (measured - level) * rate
    }

    /// The 60 Hz clock. It runs only while something is still moving — a queue
    /// left to drain, or a gap between `shown` and `level` — and stops itself
    /// the moment the picture would stop changing, so a silent HUD costs
    /// nothing.
    private func run() {
        guard frames == nil else { return }
        lastFrame = 0
        let timer = Timer(timeInterval: 1.0 / 60.0, repeats: true) { [weak self] _ in
            self?.tick()
        }
        // .common, not .default: without it the sphere would freeze for as long
        // as any menu or tracking loop is up.
        RunLoop.main.add(timer, forMode: .common)
        frames = timer
    }

    private func tick() {
        let now = CACurrentMediaTime()
        let dt = lastFrame == 0 ? OrbMeterView.sampleInterval : min(0.1, now - lastFrame)
        lastFrame = now

        sampleDebt += dt
        while sampleDebt >= OrbMeterView.sampleInterval, !queue.isEmpty {
            sampleDebt -= OrbMeterView.sampleInterval
            advance(queue.removeFirst())
        }
        if queue.isEmpty { sampleDebt = min(sampleDebt, OrbMeterView.sampleInterval) }

        // Exponential chase, framed in seconds so it is identical on a 60 Hz
        // and a 120 Hz display rather than twice as fast on one of them.
        let slew = 1 - pow(0.001, dt / Double(OrbMeterView.slewTime))
        shown += (level - shown) * CGFloat(slew)
        needsDisplay = true

        if queue.isEmpty, abs(level - shown) < 0.0008 {
            shown = level
            frames?.invalidate()
            frames = nil
        }
    }

    override func draw(_ dirtyRect: NSRect) {
        guard bounds.width > 0 else { return }
        let center = NSPoint(x: bounds.midX, y: bounds.midY)
        let color = active ? (tint ?? NSColor.controlAccentColor) : NSColor.tertiaryLabelColor
        let alpha: CGFloat = active ? 1 : 0.45
        let maxR = min(bounds.width, bounds.height) / 2 - 1

        // The runtime's 0..1 level is linear in dBFS from -60 up to 0, and the
        // top of that scale is unreachable: 0 dBFS is a clipped signal, and
        // ordinary speech lives around -30 (~0.5) while a raised voice barely
        // passes -18 (~0.7). Drawn against full scale the core therefore spends
        // every turn in the bottom half of its travel, which is exactly what it
        // looked like. So the band a voice actually occupies is stretched to
        // fill the sphere: room tone sits at the floor, a normal speaking voice
        // lands high, and a raised one reaches the top. It is a gain and a
        // curve on a measured number — the core still cannot move unless the
        // microphone did.
        let normalized = max(
            0, min(1, (shown - OrbMeterView.quietLevel) / (OrbMeterView.loudLevel - OrbMeterView.quietLevel))
        )
        // An S-curve, not the old square-rootish one. Square root lifted the
        // quiet end so hard that room tone and a sentence looked nearly alike;
        // smoothstep does the opposite, pressing the bottom of the band down
        // and letting the top run away, so the difference between speaking and
        // speaking *up* is the thing you see.
        let loudness = normalized * normalized * (3 - 2 * normalized)

        // The shell is the meter too. It used to be a fixed object with a
        // moving core, which meant the loudest moment changed only the inside
        // of a marble that stayed exactly the same size — legible up close,
        // invisible from across the desk. So the whole sphere breathes, and
        // every layer below is sized from this one radius.
        // Stopping short of the view's edge is deliberate: the glow needs
        // somewhere to go. Let the sphere fill the frame at full volume and the
        // halo has no room to fall off, so it renders as a hard painted ring
        // instead of light.
        let sphereR = maxR * (0.52 + 0.28 * loudness)
        let sphere = NSBezierPath(ovalIn: NSRect(
            x: center.x - sphereR, y: center.y - sphereR,
            width: sphereR * 2, height: sphereR * 2
        ))
        let light = color.blended(withFraction: 0.72, of: .white) ?? .white
        let deep = color.blended(withFraction: 0.62, of: .black) ?? color

        // 1. The glow, first, so the sphere sits on top of it. Its reach and
        // strength ride the measured level, which is what makes a loud moment
        // read instantly from the corner of your eye. Capped at `maxR`: the
        // view clips at its own bounds, and a gradient that runs past them
        // ends in a hard circular cut instead of fading out.
        if active {
            let glowR = min(maxR, sphereR * (1.42 + 0.20 * loudness))
            // The falloff has to *begin* at the sphere's own edge. Starting it
            // further out leaves a band of undiluted colour ringing the sphere,
            // which reads as a flat painted donut rather than as light coming
            // off the object — the second stop is where the glass ends.
            let edge = max(0.05, min(0.95, sphereR / glowR))
            // Four stops approximating an exponential falloff. Two stops give a
            // linear ramp, and a linear ramp of flat colour is precisely what a
            // painted disc looks like.
            let span = 1 - edge
            let bloom = (0.07 + 0.17 * loudness) * alpha
            if glowR > sphereR, let glow = NSGradient(
                colors: [
                    color.withAlphaComponent(bloom),
                    color.withAlphaComponent(bloom),
                    color.withAlphaComponent(bloom * 0.42),
                    color.withAlphaComponent(bloom * 0.12),
                    color.withAlphaComponent(0),
                ],
                atLocations: [0, edge, edge + span * 0.28, edge + span * 0.60, 1],
                colorSpace: .deviceRGB
            ) {
                glow.draw(fromCenter: center, radius: 0, toCenter: center, radius: glowR, options: [])
            }
        }

        // 2. Shell shading — the body of the glass. The gradient centre sits
        // off toward the key light, high and to the left, the way a lit marble
        // reads: bright on the lit shoulder, falling to near-black at the far
        // edge. Everything after this is either inside that glass or on it.
        if let shell = NSGradient(
            colors: [
                light.withAlphaComponent(0.95 * alpha),
                color.withAlphaComponent(0.92 * alpha),
                deep.withAlphaComponent(0.96 * alpha),
            ],
            atLocations: [0, 0.52, 1],
            colorSpace: .deviceRGB
        ) {
            shell.draw(in: sphere, relativeCenterPosition: NSPoint(x: -0.30, y: 0.30))
        }

        NSGraphicsContext.saveGraphicsState()
        sphere.addClip()

        // 3. The liquid core, hanging slightly low inside the glass, with the
        // light it throws into the surrounding glass drawn first. Its radius is
        // the meter — a bead in a still room, nearly filling the shell at a
        // shout — and it runs hotter as it swells, because brightness is the
        // cue that survives peripheral vision while size is still being read.
        let coreCenter = NSPoint(x: center.x, y: center.y - sphereR * 0.06)
        let coreR = sphereR * (0.16 + 0.70 * loudness)
        let coreLight = color.blended(withFraction: 0.70 + 0.28 * loudness, of: .white) ?? .white
        let coreDeep = color.blended(withFraction: 0.26 - 0.16 * loudness, of: .black) ?? color

        let bleedR = coreR * 1.9
        if let bleed = NSGradient(
            colors: [
                coreLight.withAlphaComponent((0.14 + 0.20 * loudness) * alpha),
                color.withAlphaComponent(0),
            ],
            atLocations: [0, 1],
            colorSpace: .deviceRGB
        ) {
            bleed.draw(fromCenter: coreCenter, radius: coreR * 0.2, toCenter: coreCenter, radius: bleedR, options: [])
        }

        // The core's own edge fades out rather than ending on a hard line. A
        // hard line was the tell that made this read as two balls stacked
        // instead of pigment suspended in glass.
        let core = NSBezierPath(ovalIn: NSRect(
            x: coreCenter.x - coreR, y: coreCenter.y - coreR,
            width: coreR * 2, height: coreR * 2
        ))
        if let coreGradient = NSGradient(
            colors: [
                coreLight.withAlphaComponent(alpha),
                color.withAlphaComponent(alpha),
                coreDeep.withAlphaComponent(0.45 * alpha),
                coreDeep.withAlphaComponent(0),
            ],
            atLocations: [0, 0.40, 0.78, 1],
            colorSpace: .deviceRGB
        ) {
            coreGradient.draw(in: core, relativeCenterPosition: NSPoint(x: -0.12, y: 0.16))
        }

        // 4. The caustic: light that went through the core, converged, and
        // landed on the inside of the far wall. A real marble does this and it
        // is most of why one looks lit from within rather than painted. It is
        // brightest exactly when the core is fattest, so it is the meter too.
        let causticW = sphereR * (1.10 + 0.50 * loudness)
        let caustic = NSBezierPath(ovalIn: NSRect(
            x: center.x - causticW / 2,
            y: center.y - sphereR * 0.98,
            width: causticW,
            height: sphereR * 0.52
        ))
        if let causticGlow = NSGradient(
            colors: [
                light.withAlphaComponent((0.16 + 0.30 * loudness) * alpha),
                light.withAlphaComponent(0),
            ],
            atLocations: [0, 1],
            colorSpace: .deviceRGB
        ) {
            causticGlow.draw(in: caustic, relativeCenterPosition: .zero)
        }

        // 5. The terminator: the shell's far side darkening *over* the core.
        // Drawn after the core rather than before is the whole point — it is
        // glass in front of the pigment, not paint beside it, and it is what
        // stops a lit circle with a blob in it from looking flat when the core
        // swells to fill the shell.
        if let terminator = NSGradient(
            colors: [
                NSColor.black.withAlphaComponent(0),
                NSColor.black.withAlphaComponent(0.16 * alpha),
                NSColor.black.withAlphaComponent(0.58 * alpha),
            ],
            atLocations: [0.20, 0.62, 1],
            colorSpace: .deviceRGB
        ) {
            terminator.draw(in: sphere, relativeCenterPosition: NSPoint(x: -0.42, y: 0.42))
        }

        // 6. Fresnel. A glass sphere goes mirror-bright at its silhouette,
        // where you are looking along the surface rather than into it, and the
        // effect is strongest away from the key light. This one band is the
        // difference between a sphere and a circle with a gradient in it —
        // it is the edge that tells the eye the object is round.
        if let fresnel = NSGradient(
            colors: [
                NSColor.white.withAlphaComponent(0),
                NSColor.white.withAlphaComponent(0.05 * alpha),
                NSColor.white.withAlphaComponent(0.30 * alpha),
            ],
            atLocations: [0, 0.62, 1],
            colorSpace: .deviceRGB
        ) {
            fresnel.draw(in: sphere, relativeCenterPosition: NSPoint(x: 0.22, y: -0.22))
        }

        // 7. The bounce: a soft second light low and left, the cool one every
        // product shot puts opposite the key so the shadow side is not dead.
        // A stroked arc was tried here and it read as a scratch — an arc has
        // two ends, and light does not. A radial falloff has no ends.
        let bounceR = sphereR * 0.62
        let bounceCenter = NSPoint(x: center.x - sphereR * 0.42, y: center.y - sphereR * 0.52)
        if let bounceLight = NSGradient(
            colors: [
                NSColor.white.withAlphaComponent(0.16 * alpha),
                NSColor.white.withAlphaComponent(0),
            ],
            atLocations: [0, 1],
            colorSpace: .deviceRGB
        ) {
            bounceLight.draw(
                fromCenter: bounceCenter, radius: 0,
                toCenter: bounceCenter, radius: bounceR, options: []
            )
        }

        NSGraphicsContext.restoreGraphicsState()

        // 8. The specular highlight — the one thing that says "this surface is
        // wet and hard". It is not a decal: a real highlight is the light
        // source reflected in a curved mirror, so as the sphere swells the
        // reflection tightens toward a point and burns brighter, and it slides
        // a little further up the shoulder as the surface turns under it. The
        // light itself never moves. That is the difference between a highlight
        // that is animated and one that is simply *there* while the object it
        // sits on changes shape.
        let tighten = 1 - 0.30 * loudness
        let glintW = sphereR * 0.60 * tighten
        let glintH = sphereR * 0.30 * tighten
        let glintCenter = NSPoint(
            x: center.x - sphereR * (0.30 - 0.04 * loudness),
            y: center.y + sphereR * (0.40 + 0.06 * loudness)
        )
        let glint = NSBezierPath(ovalIn: NSRect(
            x: -glintW / 2, y: -glintH / 2, width: glintW, height: glintH
        ))
        // Order matters and the obvious order is wrong: `AffineTransform`'s
        // mutating methods *prepend*, so building a rotation and then adding a
        // translation rotates the already-moved point about the view's origin
        // and flings the highlight to the opposite side of the sphere. Which is
        // exactly where it had been sitting — a bright smear low and right,
        // opposite the light that is supposed to be casting it. Start from the
        // translation and prepend the rotation, so the ellipse turns about its
        // own centre and then moves onto the lit shoulder.
        var tilt = AffineTransform(translationByX: glintCenter.x, byY: glintCenter.y)
        tilt.rotate(byDegrees: -28)
        glint.transform(using: tilt)
        if let glintGradient = NSGradient(
            colors: [
                NSColor.white.withAlphaComponent((0.62 + 0.30 * loudness) * alpha),
                NSColor.white.withAlphaComponent(0.18 * alpha),
                NSColor.white.withAlphaComponent(0),
            ],
            atLocations: [0, 0.45, 1],
            colorSpace: .deviceRGB
        ) {
            glintGradient.draw(in: glint, relativeCenterPosition: .zero)
        }

        // The hot centre of that same reflection. Bright to the point of white,
        // but with a falloff rather than an edge — a flat-filled dot reads as a
        // sticker sitting on the glass instead of a light reflected in it.
        let hotR = max(1, sphereR * 0.13 * tighten)
        if let hotGradient = NSGradient(
            colors: [
                NSColor.white.withAlphaComponent((0.85 + 0.15 * loudness) * alpha),
                NSColor.white.withAlphaComponent((0.60 + 0.20 * loudness) * alpha),
                NSColor.white.withAlphaComponent(0),
            ],
            atLocations: [0, 0.42, 1],
            colorSpace: .deviceRGB
        ) {
            hotGradient.draw(
                fromCenter: glintCenter, radius: 0,
                toCenter: glintCenter, radius: hotR, options: []
            )
        }

        // 9. The rims. The thin bright edge all round is the glass catching the
        // room; the crescent under it is the refracted band real glass throws
        // back at its base. Both brighten with the level, so even the outline
        // of the object is carrying the meter.
        sphere.lineWidth = 1
        NSColor.white.withAlphaComponent((0.30 + 0.32 * loudness) * alpha).setStroke()
        sphere.stroke()
        // Round caps on every arc below. A butt cap ends a stroke on a square
        // edge, which at this size is a visible chip of white — the arcs looked
        // like scratches in the glass rather than light along it.
        let crescent = NSBezierPath()
        crescent.appendArc(withCenter: center, radius: sphereR - 1.6, startAngle: 212, endAngle: 328)
        crescent.lineWidth = 1.6
        crescent.lineCapStyle = .round
        light.withAlphaComponent((0.26 + 0.30 * loudness) * alpha).setStroke()
        crescent.stroke()

        // Dispersion: the faintest warm/cool split at the silhouette, warm on
        // the lit shoulder and cool opposite it. At this size it is barely a
        // colour, but it is the last thing that separates rendered glass from
        // a stroked circle, so it earns its two arcs.
        let warm = NSColor(calibratedRed: 1.0, green: 0.84, blue: 0.58, alpha: 0.20 * alpha)
        let cool = NSColor(calibratedRed: 0.56, green: 0.82, blue: 1.0, alpha: 0.20 * alpha)
        let warmArc = NSBezierPath()
        warmArc.appendArc(withCenter: center, radius: sphereR - 0.4, startAngle: 95, endAngle: 185)
        warmArc.lineWidth = 1
        warmArc.lineCapStyle = .round
        warm.setStroke()
        warmArc.stroke()
        let coolArc = NSBezierPath()
        coolArc.appendArc(withCenter: center, radius: sphereR - 0.4, startAngle: 300, endAngle: 20)
        coolArc.lineWidth = 1
        coolArc.lineCapStyle = .round
        cool.setStroke()
        coolArc.stroke()

        // 10a. Listening: a dark pupil at the centre, the aperture every camera
        // and every record light has had. It appears only while the microphone
        // is actually open — `active` is false during the warm-up, so the dot
        // arriving *is* the moment the device starts hearing you, and it is not
        // decoration: it is the same promise the red tint makes, said twice.
        if active, !speaking {
            let pupilR = max(2.2, min(5.0, sphereR * 0.17))
            let pupil = NSBezierPath(ovalIn: NSRect(
                x: center.x - pupilR, y: center.y - pupilR,
                width: pupilR * 2, height: pupilR * 2
            ))
            if let well = NSGradient(
                colors: [
                    NSColor.black.withAlphaComponent(0.88 * alpha),
                    NSColor.black.withAlphaComponent(0.78 * alpha),
                    NSColor.black.withAlphaComponent(0),
                ],
                atLocations: [0, 0.72, 1],
                colorSpace: .deviceRGB
            ) {
                well.draw(in: pupil, relativeCenterPosition: NSPoint(x: 0.15, y: -0.15))
            }
            // The catchlight: one bright point up-left, from the same lamp as
            // the sphere's own highlight. Without it the dot is a hole punched
            // in the glass; with it, it is a lens looking back at you.
            let catchR = max(0.6, pupilR * 0.26)
            let catch1 = NSPoint(x: center.x - pupilR * 0.34, y: center.y + pupilR * 0.34)
            if let spark = NSGradient(
                colors: [
                    NSColor.white.withAlphaComponent(0.75 * alpha),
                    NSColor.white.withAlphaComponent(0),
                ],
                atLocations: [0, 1],
                colorSpace: .deviceRGB
            ) {
                spark.draw(fromCenter: catch1, radius: 0, toCenter: catch1, radius: catchR, options: [])
            }
        }

        // 10b. Speaking only: the stop glyph at the centre. Rounded square,
        // white, the universal "stop" — the whole pill stays the hit target.
        // Given a soft dark halo so it stays legible when the core behind it
        // goes bright.
        if speaking {
            // Sized off the sphere, not fixed: a 12-point square swallowed the
            // orb whole at the quiet end, where the sphere is at its smallest.
            let side = max(8, min(13, sphereR * 0.46))
            let square = NSRect(
                x: center.x - side / 2, y: center.y - side / 2, width: side, height: side
            )
            if let halo = NSGradient(
                colors: [
                    NSColor.black.withAlphaComponent(0.30),
                    NSColor.black.withAlphaComponent(0),
                ],
                atLocations: [0, 1],
                colorSpace: .deviceRGB
            ) {
                halo.draw(fromCenter: center, radius: side * 0.3, toCenter: center, radius: side * 1.3, options: [])
            }
            NSColor.white.withAlphaComponent(0.96).setFill()
            NSBezierPath(roundedRect: square, xRadius: 3.5, yRadius: 3.5).fill()
        }
    }
}

extension HUDState {
    /// Colour carries the one thing the shape cannot: where the words are going.
    var tint: NSColor {
        switch self {
        case .warming: return .tertiaryLabelColor
        case .listening: return NSColor(calibratedRed: 1.0, green: 0.22, blue: 0.18, alpha: 1)
        case .dictating: return .systemTeal
        case .speaking: return .systemBlue
        }
    }
}

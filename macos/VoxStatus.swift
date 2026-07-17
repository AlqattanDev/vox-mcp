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
    private let primaryButton = NSButton(title: "Start voice session", target: nil, action: nil)
    private let stopButton = NSButton(title: "Stop", target: nil, action: nil)
    private let cancelButton = NSButton(title: "Cancel current turn", target: nil, action: nil)
    private let restartButton = NSButton(title: "Restart runtime", target: nil, action: nil)
    private let activityButton = NSButton(title: "Open Vox activity", target: nil, action: nil)

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
    private let baseURL = URL(string: "http://127.0.0.1:8766")!

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        configurePanel()
        guard let button = statusItem.button else { return }
        button.target = self
        button.action = #selector(togglePanel)
        button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        button.toolTip = "Vox local voice controls"
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
        timer = Timer.scheduledTimer(withTimeInterval: 1.5, repeats: true) { [weak self] _ in
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

        for button in [primaryButton, stopButton, cancelButton, restartButton, activityButton] {
            button.target = self
            button.bezelStyle = .rounded
        }
        primaryButton.action = #selector(primaryAction)
        stopButton.action = #selector(stopSession)
        cancelButton.action = #selector(cancelTurn)
        restartButton.action = #selector(restartRuntime)
        activityButton.action = #selector(openActivity)

        let sessionRow = NSStackView(views: [primaryButton, stopButton])
        sessionRow.orientation = .horizontal
        sessionRow.distribution = .fillEqually
        sessionRow.spacing = 8
        sessionRow.translatesAutoresizingMaskIntoConstraints = false
        let utilityRow = NSStackView(views: [restartButton, activityButton])
        utilityRow.orientation = .horizontal
        utilityRow.distribution = .fillEqually
        utilityRow.spacing = 8
        utilityRow.translatesAutoresizingMaskIntoConstraints = false

        let divider = NSBox()
        divider.boxType = .separator
        divider.translatesAutoresizingMaskIntoConstraints = false

        stack.addArrangedSubview(stateLabel)
        stack.addArrangedSubview(detailLabel)
        stack.addArrangedSubview(microphoneLabel)
        stack.addArrangedSubview(divider)
        stack.addArrangedSubview(sessionRow)
        stack.addArrangedSubview(cancelButton)
        stack.addArrangedSubview(utilityRow)

        root.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 14),
            stack.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -14),
            stack.topAnchor.constraint(equalTo: root.topAnchor, constant: 14),
            stack.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -14),
            detailLabel.widthAnchor.constraint(equalToConstant: 322),
            microphoneLabel.widthAnchor.constraint(equalTo: detailLabel.widthAnchor),
            divider.widthAnchor.constraint(equalTo: detailLabel.widthAnchor),
            sessionRow.widthAnchor.constraint(equalTo: detailLabel.widthAnchor),
            cancelButton.widthAnchor.constraint(equalTo: detailLabel.widthAnchor),
            utilityRow.widthAnchor.constraint(equalTo: detailLabel.widthAnchor),
        ])
        panelController.view = root
        popover.contentViewController = panelController
        popover.behavior = .transient
        popover.animates = true
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
        case "idle": title = "Vox Ready"
        case "off": title = "Vox Off"
        case "offline": title = "Vox Offline"
        case "error": title = "Vox Error"
        default: title = "Vox Starting"
        }
        statusItem.button?.title = title
        statusItem.button?.toolTip = panelDetail()

        stateLabel.stringValue = title
        detailLabel.stringValue = panelDetail()
        microphoneLabel.stringValue = microphoneOpen
            ? "Microphone: listening now"
            : "Microphone: closed — Vox never records in the background"

        let activeTurn = ["listening", "speaking", "processing"].contains(normalized)
        if normalized == "off" || normalized == "offline" || normalized == "error" {
            primaryButton.title = "Start voice session"
        } else if normalized == "paused" {
            primaryButton.title = "Resume voice session"
        } else {
            primaryButton.title = "Pause voice session"
        }
        primaryButton.isEnabled = !controlInFlight && normalized != "offline" && normalized != "error"
        stopButton.isEnabled = !controlInFlight && !["off", "offline", "error"].contains(normalized)
        cancelButton.isEnabled = !controlInFlight && activeTurn
        restartButton.isEnabled = !controlInFlight
        activityButton.isEnabled = true
    }

    private func panelDetail() -> String {
        if let actionNotice { return actionNotice }
        if state.lowercased() == "off", lastStopReason == "idle_timeout" {
            return "Stopped after 10 minutes without activity. The microphone is closed."
        }
        return detail
    }

    private func sendControl(_ action: String, notice: String) {
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
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["action": action])
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
        case "start": return "Voice session is ready. The microphone remains closed until Codex or Claude asks to listen."
        case "pause": return "Voice session paused. The microphone is closed."
        case "resume": return "Voice session resumed and ready. The microphone remains closed until a voice turn starts."
        case "cancel": return "Current voice turn cancelled. The microphone is closing."
        case "stop": return "Voice mode stopped. The microphone is closed."
        default: return "Vox control applied."
        }
    }

    @objc private func primaryAction() {
        switch state.lowercased() {
        case "off", "offline", "error": sendControl("start", notice: "Starting a voice session…")
        case "paused": sendControl("resume", notice: "Resuming voice session…")
        default: sendControl("pause", notice: "Pausing voice session and closing the microphone…")
        }
    }

    @objc private func cancelTurn() { sendControl("cancel", notice: "Cancelling current voice turn…") }
    @objc private func stopSession() { sendControl("stop", notice: "Stopping voice mode…") }

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

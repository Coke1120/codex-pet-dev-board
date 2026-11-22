import AppKit
import AVFoundation
import CoreMedia
import Foundation

// MARK: - Domain Types

enum PetState: String, CaseIterable {
    case idle
    case running
    case waiting
    case review

    var label: String {
        switch self {
        case .idle: return "Idle"
        case .running: return "Running"
        case .waiting: return "Waiting"
        case .review: return "Review"
        }
    }

    var shortLabel: String {
        switch self {
        case .idle: return "Idle"
        case .running: return "Run"
        case .waiting: return "Wait"
        case .review: return "Rev"
        }
    }

    var accentColor: NSColor {
        switch self {
        case .idle: return NSColor(calibratedRed: 0.34, green: 0.78, blue: 0.56, alpha: 1.0)
        case .running: return NSColor(calibratedRed: 0.30, green: 0.62, blue: 0.98, alpha: 1.0)
        case .waiting: return NSColor(calibratedRed: 0.96, green: 0.70, blue: 0.24, alpha: 1.0)
        case .review: return NSColor(calibratedRed: 0.72, green: 0.52, blue: 0.96, alpha: 1.0)
        }
    }
}

enum MediaKind: String {
    case video
    case gif
}

struct MediaEntry {
    let url: URL
    let kind: MediaKind
}

struct CurrentStatePayload: Decodable {
    let version: Int
    let state: String
    let source: String
    let active_sessions: Int
    let updated_at: Double
}

struct PlayerOptions {
    var stateFilePath: String?
    var mediaMapPath: String?
    var forceState: PetState?
    var pollMs: Int = 250
    var width: CGFloat = 300
    var height: CGFloat = 324
    var alwaysOnTop: Bool = true
    var emitEvents: Bool = false
    var statusFilePath: String?
    var autoExitSeconds: Double?
    var showDockIcon: Bool = false
}

// MARK: - CLI Parsing

func printUsage() {
    let text = """
    Usage: CodexPetPlayer [options]
      --state-file PATH          Watch current_state.json for lifecycle state updates
      --media-map PATH           JSON mapping idle/running/waiting/review to MP4/GIF paths
      --force-state STATE        Start in manual override state (idle|running|waiting|review)
      --poll-ms MS               State file check interval in ms (default: 250, min: 100)
      --width PX                 Window width in points (default: 300)
      --height PX                Window height in points (default: 324)
      --no-always-on-top         Do not float above normal windows
      --emit-events              Emit structured JSONL playback events on stdout
      --status-file PATH         Write latest player status JSON on transitions
      --auto-exit-seconds SEC    Exit cleanly after SEC seconds
      --dock-icon                Show app icon in the Dock (default: accessory floating window)
      --help                     Show this help
    """
    fputs(text + "\n", stderr)
}

func parseOptions(_ args: [String]) -> PlayerOptions? {
    var opts = PlayerOptions()
    var index = 1
    while index < args.count {
        let arg = args[index]
        switch arg {
        case "--help", "-h":
            printUsage()
            exit(0)
        case "--state-file":
            guard index + 1 < args.count else { fputs("Missing value for --state-file\n", stderr); return nil }
            opts.stateFilePath = args[index + 1]
            index += 2
        case "--media-map":
            guard index + 1 < args.count else { fputs("Missing value for --media-map\n", stderr); return nil }
            opts.mediaMapPath = args[index + 1]
            index += 2
        case "--force-state":
            guard index + 1 < args.count, let st = PetState(rawValue: args[index + 1].lowercased()) else {
                fputs("Invalid value for --force-state (expected idle|running|waiting|review)\n", stderr)
                return nil
            }
            opts.forceState = st
            index += 2
        case "--poll-ms":
            guard index + 1 < args.count, let ms = Int(args[index + 1]), ms >= 100 else {
                fputs("Invalid value for --poll-ms (expected integer >= 100)\n", stderr)
                return nil
            }
            opts.pollMs = ms
            index += 2
        case "--width":
            guard index + 1 < args.count, let w = Double(args[index + 1]), w >= 180, w <= 1200 else {
                fputs("Invalid value for --width (expected 180..1200)\n", stderr)
                return nil
            }
            opts.width = CGFloat(w)
            index += 2
        case "--height":
            guard index + 1 < args.count, let h = Double(args[index + 1]), h >= 180, h <= 1200 else {
                fputs("Invalid value for --height (expected 180..1200)\n", stderr)
                return nil
            }
            opts.height = CGFloat(h)
            index += 2
        case "--no-always-on-top":
            opts.alwaysOnTop = false
            index += 1
        case "--emit-events":
            opts.emitEvents = true
            index += 1
        case "--status-file":
            guard index + 1 < args.count else { fputs("Missing value for --status-file\n", stderr); return nil }
            opts.statusFilePath = args[index + 1]
            index += 2
        case "--auto-exit-seconds":
            guard index + 1 < args.count, let sec = Double(args[index + 1]), sec > 0, sec.isFinite else {
                fputs("Invalid value for --auto-exit-seconds\n", stderr)
                return nil
            }
            opts.autoExitSeconds = sec
            index += 2
        case "--dock-icon":
            opts.showDockIcon = true
            index += 1
        default:
            fputs("Unknown argument: \(arg)\n", stderr)
            printUsage()
            return nil
        }
    }
    return opts
}

// MARK: - Media Map Loading

let videoExtensions: Set<String> = ["mp4", "m4v", "mov"]

func classifyMediaURL(_ url: URL) -> MediaEntry? {
    let ext = url.pathExtension.lowercased()
    if videoExtensions.contains(ext) {
        return MediaEntry(url: url, kind: .video)
    }
    if ext == "gif" {
        return MediaEntry(url: url, kind: .gif)
    }
    return nil
}

func loadMediaMap(from path: String?) -> [PetState: URL] {
    guard let rawPath = path, !rawPath.isEmpty else { return [:] }
    let expanded = (rawPath as NSString).expandingTildeInPath
    let configURL = URL(fileURLWithPath: expanded).standardizedFileURL
    let baseDir = configURL.deletingLastPathComponent()

    guard let data = try? Data(contentsOf: configURL),
          let rawObject = try? JSONSerialization.jsonObject(with: data, options: []),
          let dict = rawObject as? [String: Any] else {
        return [:]
    }

    var resolved: [PetState: URL] = [:]
    for state in PetState.allCases {
        guard let value = dict[state.rawValue] as? String else { continue }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { continue }
        let expandedValue = (trimmed as NSString).expandingTildeInPath
        let url: URL
        if expandedValue.hasPrefix("/") {
            url = URL(fileURLWithPath: expandedValue).standardizedFileURL
        } else {
            url = baseDir.appendingPathComponent(expandedValue).standardizedFileURL
        }
        resolved[state] = url
    }
    return resolved
}

// MARK: - First-Click Control View

final class FirstClickView: NSView {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool {
        return true
    }
}

final class MediaCanvasView: NSView {
    let playerLayer = AVPlayerLayer()
    let gifView = NSImageView()
    let placeholderContainer = NSView()
    let placeholderDot = NSView()
    let placeholderStateLabel = NSTextField(labelWithString: "IDLE")
    let placeholderHintLabel = NSTextField(labelWithString: "Configure local MP4 or GIF clips in media-map.json")

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.backgroundColor = NSColor(calibratedRed: 0.08, green: 0.09, blue: 0.11, alpha: 1.0).cgColor

        playerLayer.videoGravity = .resizeAspect
        playerLayer.backgroundColor = NSColor.clear.cgColor
        playerLayer.isHidden = true
        layer?.addSublayer(playerLayer)

        gifView.translatesAutoresizingMaskIntoConstraints = false
        gifView.imageScaling = .scaleProportionallyUpOrDown
        gifView.animates = true
        gifView.isHidden = true
        addSubview(gifView)

        placeholderContainer.translatesAutoresizingMaskIntoConstraints = false
        addSubview(placeholderContainer)

        placeholderDot.translatesAutoresizingMaskIntoConstraints = false
        placeholderDot.wantsLayer = true
        placeholderDot.layer?.cornerRadius = 8
        placeholderContainer.addSubview(placeholderDot)

        placeholderStateLabel.translatesAutoresizingMaskIntoConstraints = false
        placeholderStateLabel.font = NSFont.systemFont(ofSize: 22, weight: .semibold)
        placeholderStateLabel.textColor = NSColor(calibratedWhite: 0.94, alpha: 1.0)
        placeholderStateLabel.alignment = .center
        placeholderContainer.addSubview(placeholderStateLabel)

        placeholderHintLabel.translatesAutoresizingMaskIntoConstraints = false
        placeholderHintLabel.font = NSFont.systemFont(ofSize: 11, weight: .regular)
        placeholderHintLabel.textColor = NSColor(calibratedWhite: 0.65, alpha: 1.0)
        placeholderHintLabel.alignment = .center
        placeholderHintLabel.lineBreakMode = .byWordWrapping
        placeholderHintLabel.maximumNumberOfLines = 3
        placeholderContainer.addSubview(placeholderHintLabel)

        NSLayoutConstraint.activate([
            gifView.leadingAnchor.constraint(equalTo: leadingAnchor),
            gifView.trailingAnchor.constraint(equalTo: trailingAnchor),
            gifView.topAnchor.constraint(equalTo: topAnchor),
            gifView.bottomAnchor.constraint(equalTo: bottomAnchor),

            placeholderContainer.centerXAnchor.constraint(equalTo: centerXAnchor),
            placeholderContainer.centerYAnchor.constraint(equalTo: centerYAnchor),
            placeholderContainer.leadingAnchor.constraint(greaterThanOrEqualTo: leadingAnchor, constant: 16),
            placeholderContainer.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -16),

            placeholderDot.centerXAnchor.constraint(equalTo: placeholderContainer.centerXAnchor),
            placeholderDot.topAnchor.constraint(equalTo: placeholderContainer.topAnchor),
            placeholderDot.widthAnchor.constraint(equalToConstant: 16),
            placeholderDot.heightAnchor.constraint(equalToConstant: 16),

            placeholderStateLabel.topAnchor.constraint(equalTo: placeholderDot.bottomAnchor, constant: 10),
            placeholderStateLabel.leadingAnchor.constraint(equalTo: placeholderContainer.leadingAnchor),
            placeholderStateLabel.trailingAnchor.constraint(equalTo: placeholderContainer.trailingAnchor),

            placeholderHintLabel.topAnchor.constraint(equalTo: placeholderStateLabel.bottomAnchor, constant: 6),
            placeholderHintLabel.leadingAnchor.constraint(equalTo: placeholderContainer.leadingAnchor),
            placeholderHintLabel.trailingAnchor.constraint(equalTo: placeholderContainer.trailingAnchor),
            placeholderHintLabel.bottomAnchor.constraint(equalTo: placeholderContainer.bottomAnchor),
        ])
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func layout() {
        super.layout()
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        playerLayer.frame = bounds
        CATransaction.commit()
    }

    func showPlaceholder(state: PetState, reason: String) {
        playerLayer.isHidden = true
        gifView.isHidden = true
        placeholderContainer.isHidden = false
        placeholderDot.layer?.backgroundColor = state.accentColor.cgColor
        placeholderStateLabel.stringValue = state.label.uppercased()
        placeholderHintLabel.stringValue = reason
    }

    func showVideoLayer() {
        placeholderContainer.isHidden = true
        gifView.isHidden = true
        playerLayer.isHidden = false
    }

    func showGifLayer() {
        placeholderContainer.isHidden = true
        playerLayer.isHidden = true
        gifView.isHidden = false
    }
}

final class PetWindow: NSWindow {
    var onKeyStateCommand: ((String) -> Void)?

    override func keyDown(with event: NSEvent) {
        guard let chars = event.charactersIgnoringModifiers?.lowercased() else {
            super.keyDown(with: event)
            return
        }
        switch chars {
        case "0", "a":
            onKeyStateCommand?("auto")
        case "1":
            onKeyStateCommand?("idle")
        case "2":
            onKeyStateCommand?("running")
        case "3":
            onKeyStateCommand?("waiting")
        case "4":
            onKeyStateCommand?("review")
        case "r":
            onKeyStateCommand?("reload")
        default:
            super.keyDown(with: event)
        }
    }
}

// MARK: - Player Controller

final class CodexPetPlayerController: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private let options: PlayerOptions
    private var mediaMap: [PetState: URL] = [:]

    // Single active video decoder for the entire process lifetime.
    private let player = AVPlayer()
    private var itemStatusObservation: NSKeyValueObservation?
    private var displayObservation: NSKeyValueObservation?
    private var endObserver: NSObjectProtocol?
    private var activeMediaURL: URL?
    private var activeMediaKind: MediaKind?
    private var lastValidMedia: MediaEntry?

    private var window: PetWindow!
    private var canvas: MediaCanvasView!
    private var statusDot: NSView!
    private var statusLabel: NSTextField!
    private var modeSegment: NSSegmentedControl!

    private var currentState: PetState = .idle
    private var liveState: PetState = .idle
    private var liveSource: String = "sessions"
    private var manualOverride: PetState?
    private var usingFallback: Bool = false
    private var fallbackReason: String?

    private var lastFileStat: (dev: Int32, ino: UInt64, size: Int64, mtimeSec: Int, mtimeNsec: Int)?
    private var pollTimer: DispatchSourceTimer?
    private var exitTimer: DispatchSourceTimer?
    private var switchStartNanos: UInt64 = 0
    private var activeDecoderCount: Int = 0

    init(options: PlayerOptions) {
        self.options = options
        self.manualOverride = options.forceState
        super.init()
        self.player.isMuted = true
        self.player.volume = 0.0
        self.player.actionAtItemEnd = .none
        self.player.automaticallyWaitsToMinimizeStalling = false
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        mediaMap = loadMediaMap(from: options.mediaMapPath)
        setupMenu()
        setupWindow()
        installLoopObserver()

        if let forced = manualOverride {
            currentState = forced
            applyStateTransition(to: forced, trigger: "manual_cli")
        } else if !refreshStateFile(initial: true) {
            applyStateTransition(to: currentState, trigger: "startup")
        }

        startStatePolling()
        startAutoExitTimerIfNeeded()
        emitEvent(kind: "player_started", extra: [
            "always_on_top": options.alwaysOnTop,
            "state": currentState.rawValue,
            "mode": manualOverride == nil ? "auto" : "manual",
            "mapped_states": mediaMap.keys.map { $0.rawValue }.sorted(),
        ])
    }

    func applicationWillTerminate(_ notification: Notification) {
        cleanup()
    }

    func windowWillClose(_ notification: Notification) {
        cleanup()
        NSApp.terminate(nil)
    }

    private func cleanup() {
        pollTimer?.cancel()
        pollTimer = nil
        exitTimer?.cancel()
        exitTimer = nil
        stopVideo()
        if let observer = endObserver {
            NotificationCenter.default.removeObserver(observer)
            endObserver = nil
        }
    }

    private func setupMenu() {
        let mainMenu = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Reload Current Clip", action: #selector(reloadCurrentClip), keyEquivalent: "r")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "Quit Codex Pet", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        mainMenu.addItem(appItem)
        NSApp.mainMenu = mainMenu
    }

    private func setupWindow() {
        let contentRect = NSRect(x: 0, y: 0, width: options.width, height: options.height)
        let style: NSWindow.StyleMask = [.titled, .closable, .resizable, .miniaturizable]
        window = PetWindow(contentRect: contentRect, styleMask: style, backing: .buffered, defer: false)
        window.delegate = self
        window.title = "Codex Pet"
        window.minSize = NSSize(width: 220, height: 240)
        window.maxSize = NSSize(width: 720, height: 760)
        window.isReleasedWhenClosed = false
        window.backgroundColor = NSColor(calibratedRed: 0.08, green: 0.09, blue: 0.11, alpha: 1.0)
        window.level = options.alwaysOnTop ? .floating : .normal
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        window.onKeyStateCommand = { [weak self] command in
            self?.handleKeyCommand(command)
        }

        if let screen = NSScreen.main {
            let visible = screen.visibleFrame
            let origin = NSPoint(
                x: visible.maxX - options.width - 24,
                y: visible.minY + 24
            )
            window.setFrameOrigin(origin)
        }

        let root = FirstClickView(frame: contentRect)
        root.translatesAutoresizingMaskIntoConstraints = false
        window.contentView = root

        canvas = MediaCanvasView(frame: .zero)
        canvas.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(canvas)

        let footer = FirstClickView(frame: .zero)
        footer.translatesAutoresizingMaskIntoConstraints = false
        footer.wantsLayer = true
        footer.layer?.backgroundColor = NSColor(calibratedRed: 0.11, green: 0.12, blue: 0.15, alpha: 1.0).cgColor
        root.addSubview(footer)

        statusDot = NSView(frame: .zero)
        statusDot.translatesAutoresizingMaskIntoConstraints = false
        statusDot.wantsLayer = true
        statusDot.layer?.cornerRadius = 4
        footer.addSubview(statusDot)

        statusLabel = NSTextField(labelWithString: "Live · Idle")
        statusLabel.translatesAutoresizingMaskIntoConstraints = false
        statusLabel.font = NSFont.systemFont(ofSize: 11, weight: .medium)
        statusLabel.textColor = NSColor(calibratedWhite: 0.88, alpha: 1.0)
        statusLabel.lineBreakMode = .byTruncatingTail
        footer.addSubview(statusLabel)

        modeSegment = NSSegmentedControl(
            labels: ["Auto", "Idle", "Run", "Wait", "Rev"],
            trackingMode: .selectOne,
            target: self,
            action: #selector(handleSegmentSelection(_:))
        )
        modeSegment.translatesAutoresizingMaskIntoConstraints = false
        modeSegment.controlSize = .mini
        modeSegment.segmentStyle = .rounded
        footer.addSubview(modeSegment)

        NSLayoutConstraint.activate([
            canvas.topAnchor.constraint(equalTo: root.topAnchor),
            canvas.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            canvas.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            canvas.bottomAnchor.constraint(equalTo: footer.topAnchor),

            footer.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            footer.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            footer.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            footer.heightAnchor.constraint(equalToConstant: 54),

            statusDot.leadingAnchor.constraint(equalTo: footer.leadingAnchor, constant: 10),
            statusDot.topAnchor.constraint(equalTo: footer.topAnchor, constant: 10),
            statusDot.widthAnchor.constraint(equalToConstant: 8),
            statusDot.heightAnchor.constraint(equalToConstant: 8),

            statusLabel.leadingAnchor.constraint(equalTo: statusDot.trailingAnchor, constant: 6),
            statusLabel.trailingAnchor.constraint(equalTo: footer.trailingAnchor, constant: -10),
            statusLabel.centerYAnchor.constraint(equalTo: statusDot.centerYAnchor),

            modeSegment.leadingAnchor.constraint(equalTo: footer.leadingAnchor, constant: 8),
            modeSegment.trailingAnchor.constraint(equalTo: footer.trailingAnchor, constant: -8),
            modeSegment.bottomAnchor.constraint(equalTo: footer.bottomAnchor, constant: -6),
        ])

        syncControlUI()
        window.orderFrontRegardless()
    }

    private func installLoopObserver() {
        endObserver = NotificationCenter.default.addObserver(
            forName: .AVPlayerItemDidPlayToEndTime,
            object: nil,
            queue: .main
        ) { [weak self] notification in
            guard let self = self else { return }
            guard let endedItem = notification.object as? AVPlayerItem,
                  endedItem === self.player.currentItem else {
                return
            }
            self.player.seek(to: .zero, toleranceBefore: .zero, toleranceAfter: .zero) { [weak self, weak endedItem] finished in
                DispatchQueue.main.async {
                    guard finished, let self = self, let item = endedItem,
                          self.player.currentItem === item else { return }
                    self.player.play()
                    if self.options.emitEvents {
                        self.emitEvent(kind: "media_loop", extra: [
                            "media": self.activeMediaURL?.lastPathComponent ?? "",
                        ])
                    }
                }
            }
        }
    }

    private func startStatePolling() {
        guard options.stateFilePath != nil else { return }
        let timer = DispatchSource.makeTimerSource(queue: DispatchQueue.main)
        let intervalMs = max(100, options.pollMs)
        timer.schedule(
            deadline: .now() + .milliseconds(intervalMs),
            repeating: .milliseconds(intervalMs),
            leeway: .milliseconds(50)
        )
        timer.setEventHandler { [weak self] in
            self?.refreshStateFile(initial: false)
        }
        timer.resume()
        pollTimer = timer
    }

    private func startAutoExitTimerIfNeeded() {
        guard let seconds = options.autoExitSeconds else { return }
        let timer = DispatchSource.makeTimerSource(queue: DispatchQueue.main)
        timer.schedule(deadline: .now() + seconds)
        timer.setEventHandler { [weak self] in
            self?.emitEvent(kind: "auto_exit", extra: ["state": self?.currentState.rawValue ?? "idle"])
            self?.cleanup()
            NSApp.terminate(nil)
        }
        timer.resume()
        exitTimer = timer
    }

    private func statSignature(for path: String) -> (dev: Int32, ino: UInt64, size: Int64, mtimeSec: Int, mtimeNsec: Int)? {
        var st = stat()
        guard stat(path, &st) == 0 else { return nil }
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtimespec.tv_sec, st.st_mtimespec.tv_nsec)
    }

    @discardableResult
    private func refreshStateFile(initial: Bool) -> Bool {
        guard let rawPath = options.stateFilePath, !rawPath.isEmpty else { return false }
        let path = (rawPath as NSString).expandingTildeInPath
        guard let sig = statSignature(for: path) else { return false }
        if !initial, let prev = lastFileStat, prev == sig {
            return false
        }
        lastFileStat = sig

        guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
              let payload = try? JSONDecoder().decode(CurrentStatePayload.self, from: data),
              payload.version == 1,
              let parsedState = PetState(rawValue: payload.state) else {
            return false
        }

        let stateChanged = (parsedState != liveState) || (payload.source != liveSource)
        liveState = parsedState
        liveSource = payload.source

        if manualOverride == nil && (initial || stateChanged || parsedState != currentState) {
            let trigger = payload.source == "manual" ? "state_file_manual" : "state_file"
            applyStateTransition(to: parsedState, trigger: trigger)
        }
        return true
    }

    @objc private func reloadCurrentClip() {
        applyStateTransition(to: currentState, trigger: "reload", reload: true)
    }

    @objc private func handleSegmentSelection(_ sender: NSSegmentedControl) {
        switch sender.selectedSegment {
        case 0:
            manualOverride = nil
            applyStateTransition(to: liveState, trigger: "ui_auto")
        case 1:
            setManualOverride(.idle)
        case 2:
            setManualOverride(.running)
        case 3:
            setManualOverride(.waiting)
        case 4:
            setManualOverride(.review)
        default:
            break
        }
    }

    private func handleKeyCommand(_ command: String) {
        switch command {
        case "auto":
            manualOverride = nil
            applyStateTransition(to: liveState, trigger: "key_auto")
        case "reload":
            reloadCurrentClip()
        default:
            if let state = PetState(rawValue: command) {
                setManualOverride(state)
            }
        }
    }

    private func setManualOverride(_ state: PetState) {
        manualOverride = state
        applyStateTransition(to: state, trigger: "manual_ui")
    }

    private func applyStateTransition(to state: PetState, trigger: String, reload: Bool = false) {
        currentState = state
        switchStartNanos = DispatchTime.now().uptimeNanoseconds

        guard let targetURL = mediaMap[state] else {
            enterSoftFallback(for: state, reason: "No media mapped for '\(state.rawValue)'", trigger: trigger)
            return
        }
        guard FileManager.default.isReadableFile(atPath: targetURL.path) else {
            enterSoftFallback(for: state, reason: "Missing file: \(targetURL.lastPathComponent)", trigger: trigger)
            return
        }
        guard let entry = classifyMediaURL(targetURL) else {
            enterSoftFallback(for: state, reason: "Unsupported format: \(targetURL.lastPathComponent)", trigger: trigger)
            return
        }

        if !reload, activeMediaURL == entry.url, activeMediaKind == entry.kind {
            usingFallback = false
            fallbackReason = nil
            syncControlUI()
            let elapsedMs = elapsedMillis(since: switchStartNanos)
            emitEvent(kind: "state_applied", extra: [
                "state": state.rawValue,
                "trigger": trigger,
                "media": entry.url.lastPathComponent,
                "kind": entry.kind.rawValue,
                "reused": true,
                "fallback": false,
                "active_decoders": activeDecoderCount,
                "switch_ms": elapsedMs,
            ])
            return
        }

        play(entry, for: state, trigger: trigger)
    }

    private func stopVideo() {
        itemStatusObservation?.invalidate()
        itemStatusObservation = nil
        displayObservation?.invalidate()
        displayObservation = nil
        canvas.playerLayer.player = nil
        player.pause()
        player.replaceCurrentItem(with: nil)
    }

    private func play(_ entry: MediaEntry, for state: PetState, trigger: String, fallback: String? = nil) {
        // Release the previous item/image before attaching a new decoder or presentation path.
        stopVideo()
        canvas.gifView.image = nil
        activeMediaURL = nil
        activeMediaKind = nil
        activeDecoderCount = 0
        usingFallback = fallback != nil
        fallbackReason = fallback
        syncControlUI()

        switch entry.kind {
        case .video:
            loadVideoClip(entry, for: state, trigger: trigger, fallback: fallback)
        case .gif:
            guard let image = NSImage(contentsOf: entry.url) else {
                handleMediaFailure(entry, for: state, reason: "Unreadable GIF: \(entry.url.lastPathComponent)",
                                   trigger: trigger, fallback: fallback)
                return
            }
            canvas.gifView.image = image
            canvas.showGifLayer()
            markMediaReady(entry, for: state, trigger: trigger, fallback: fallback)
        }
    }

    private func loadVideoClip(_ entry: MediaEntry, for state: PetState, trigger: String, fallback: String?) {
        let item = AVPlayerItem(url: entry.url)

        itemStatusObservation = item.observe(\.status, options: [.initial, .new]) { [weak self] item, _ in
            DispatchQueue.main.async {
                guard let self = self, item === self.player.currentItem, item.status == .failed else { return }
                self.handleMediaFailure(entry, for: state, reason: "Could not play \(entry.url.lastPathComponent)",
                                        trigger: trigger, fallback: fallback)
            }
        }
        displayObservation = canvas.playerLayer.observe(\.isReadyForDisplay, options: [.new]) {
            [weak self, weak item] layer, change in
            guard change.newValue == true else { return }
            DispatchQueue.main.async {
                guard let self = self, let item = item, item === self.player.currentItem,
                      self.displayObservation != nil, layer.isReadyForDisplay else { return }
                self.displayObservation?.invalidate()
                self.displayObservation = nil
                self.markMediaReady(entry, for: state, trigger: trigger, fallback: fallback)
            }
        }

        player.replaceCurrentItem(with: item)
        activeDecoderCount = 1
        canvas.playerLayer.player = player
        canvas.showVideoLayer()
        player.play()

        // A readable container may have no usable video track.
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self, weak item] in
            guard let self = self, let item = item, item === self.player.currentItem,
                  self.displayObservation != nil else { return }
            self.handleMediaFailure(entry, for: state, reason: "No displayable frame in \(entry.url.lastPathComponent)",
                                    trigger: trigger, fallback: fallback)
        }
    }

    private func markMediaReady(_ entry: MediaEntry, for state: PetState, trigger: String, fallback: String?) {
        activeMediaURL = entry.url
        activeMediaKind = entry.kind
        activeDecoderCount = 1
        if let reason = fallback {
            enterSoftFallback(for: state, reason: reason, trigger: trigger)
            return
        }
        lastValidMedia = entry
        emitEvent(kind: "media_ready", extra: [
            "state": state.rawValue,
            "trigger": trigger,
            "media": entry.url.lastPathComponent,
            "kind": entry.kind.rawValue,
            "fallback": false,
            "active_decoders": activeDecoderCount,
            "switch_ms": elapsedMillis(since: switchStartNanos),
        ])
    }

    private func handleMediaFailure(_ entry: MediaEntry, for state: PetState, reason: String,
                                    trigger: String, fallback: String?) {
        stopVideo()
        activeMediaURL = nil
        activeMediaKind = nil
        activeDecoderCount = 0
        if fallback != nil || lastValidMedia?.url == entry.url {
            lastValidMedia = nil
        }
        enterSoftFallback(for: state, reason: fallback ?? reason, trigger: trigger)
    }

    private func enterSoftFallback(for state: PetState, reason: String, trigger: String) {
        if displayObservation != nil {
            stopVideo()
            activeDecoderCount = 0
        }
        if activeMediaURL == nil, let previous = lastValidMedia {
            if FileManager.default.isReadableFile(atPath: previous.url.path) {
                play(previous, for: state, trigger: trigger, fallback: reason)
                return
            }
            lastValidMedia = nil
        }
        usingFallback = true
        fallbackReason = reason
        let keptLastClip = (activeMediaURL != nil)
        if !keptLastClip {
            canvas.showPlaceholder(state: state, reason: reason)
        }
        syncControlUI()
        let elapsedMs = elapsedMillis(since: switchStartNanos)
        emitEvent(kind: "media_fallback", extra: [
            "state": state.rawValue,
            "trigger": trigger,
            "reason": reason,
            "kept_last_clip": keptLastClip,
            "active_media": activeMediaURL?.lastPathComponent ?? "",
            "fallback": true,
            "active_decoders": activeDecoderCount,
            "switch_ms": elapsedMs,
        ])
    }

    private func syncControlUI() {
        statusDot.layer?.backgroundColor = currentState.accentColor.cgColor
        let modeText: String
        if manualOverride != nil {
            modeText = "Manual"
        } else if liveSource == "manual" {
            modeText = "Manual (CLI)"
        } else {
            modeText = "Live"
        }
        let suffix = usingFallback ? " · fallback" : ""
        statusLabel.stringValue = "\(modeText) · \(currentState.label)\(suffix)"
        window.title = "Codex Pet — \(currentState.label)"

        if let forced = manualOverride {
            switch forced {
            case .idle: modeSegment.selectedSegment = 1
            case .running: modeSegment.selectedSegment = 2
            case .waiting: modeSegment.selectedSegment = 3
            case .review: modeSegment.selectedSegment = 4
            }
        } else {
            modeSegment.selectedSegment = 0
        }
    }

    private func elapsedMillis(since startNanos: UInt64) -> Double {
        let now = DispatchTime.now().uptimeNanoseconds
        guard now >= startNanos else { return 0.0 }
        let raw = Double(now - startNanos) / 1_000_000.0
        return (raw * 100.0).rounded() / 100.0
    }

    private func emitEvent(kind: String, extra: [String: Any]) {
        var payload: [String: Any] = [
            "event": kind,
            "timestamp": (Date().timeIntervalSince1970 * 1000.0).rounded() / 1000.0,
        ]
        for (k, v) in extra {
            payload[k] = v
        }

        if let statusPath = options.statusFilePath, !statusPath.isEmpty {
            writeStatusSnapshot(to: statusPath, lastEvent: payload)
        }

        guard options.emitEvents else { return }
        if let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]),
           let line = String(data: data, encoding: .utf8) {
            print(line)
            fflush(stdout)
        }
    }

    private func writeStatusSnapshot(to path: String, lastEvent: [String: Any]) {
        let snapshot: [String: Any] = [
            "state": currentState.rawValue,
            "mode": manualOverride == nil ? "auto" : "manual",
            "live_source": liveSource,
            "always_on_top": options.alwaysOnTop,
            "fallback": usingFallback,
            "fallback_reason": fallbackReason ?? "",
            "active_media": activeMediaURL?.lastPathComponent ?? "",
            "active_kind": activeMediaKind?.rawValue ?? "",
            "active_decoders": activeDecoderCount,
            "last_event": lastEvent,
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: snapshot, options: [.sortedKeys]) else {
            return
        }
        let destURL = URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
        try? FileManager.default.createDirectory(
            at: destURL.deletingLastPathComponent(),
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        try? data.write(to: destURL, options: .atomic)
    }
}

// MARK: - Entry Point

guard let options = parseOptions(CommandLine.arguments) else {
    exit(2)
}

let app = NSApplication.shared
app.setActivationPolicy(options.showDockIcon ? .regular : .accessory)
let controller = CodexPetPlayerController(options: options)
app.delegate = controller
app.run()

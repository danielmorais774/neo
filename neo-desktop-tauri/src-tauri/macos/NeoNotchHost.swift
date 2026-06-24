import AppKit
import AVFoundation
import QuartzCore

// MARK: - IPC

/// Single JSON line to Rust over stdout.
func emitEvent(_ event: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: event),
          let line = String(data: data, encoding: .utf8)
    else { return }
    fputs(line + "\n", stdout)
    fflush(stdout)
}

/// Diagnostic logging to stderr (visible in the `tauri dev` console).
func logErr(_ message: String) {
    FileHandle.standardError.write((message + "\n").data(using: .utf8) ?? Data())
}

struct VoiceConfig {
    var wsBaseUrl: String = "ws://127.0.0.1:5002"
    var apiKey: String?
    var voice: String?
    var backend: String = "coqui"
    var language: String?
}

// MARK: - Models

enum IslandState {
    case idle
    case peek
    case expanded
}

/// What the expanded stage area is currently showing.
enum Activity {
    case none        // notifications list
    case listening   // mic open, showing live caption
    case thinking    // agent turn in flight
    case answering   // agent answered, possibly speaking
}

struct NotchNotification {
    let id: String
    let kind: String          // "chat" | "spark"
    let title: String
    let subtitle: String
    let createdAt: TimeInterval
    let primaryAction: String? // action_type of the first action, if any
}

// MARK: - Voice (native STT + TTS)

/// Captures the microphone, streams PCM int16 16k to neo-voice `/ws/panel`
/// (STT), and plays back TTS PCM from `/ws/tts`. Half-duplex: the mic is muted
/// while Neo speaks. Mirrors the protocol in liveVoiceService.ts.
final class VoiceController {
    var cfg = VoiceConfig()

    var onPartial: ((String) -> Void)?
    var onFinal: ((String) -> Void)?
    var onError: ((String) -> Void)?
    var onListeningChanged: ((Bool) -> Void)?
    var onSpeakingChanged: ((Bool) -> Void)?
    var onLevel: ((CGFloat) -> Void)?

    private let session = URLSession(configuration: .default)

    // STT
    private var sttTask: URLSessionWebSocketTask?
    private let captureEngine = AVAudioEngine()
    private var converter: AVAudioConverter?
    private var outFormat: AVAudioFormat?
    private var micMuted = false
    private(set) var listening = false
    private var framesReceived = 0

    // TTS
    private var ttsTask: URLSessionWebSocketTask?
    private let playEngine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private var playerFormat: AVAudioFormat?
    private var pendingBuffers = 0
    private var ttsDone = false
    private(set) var speaking = false

    // ------------------------------------------------------------------ STT
    func toggleListening() {
        if listening { stopListening() } else { requestAndStart() }
    }

    private func requestAndStart() {
        AVCaptureDevice.requestAccess(for: .audio) { [weak self] granted in
            DispatchQueue.main.async {
                guard let self else { return }
                if !granted {
                    self.onError?("Permissão de microfone negada.")
                    return
                }
                self.beginCapture()
            }
        }
    }

    private func sttURL() -> URL? {
        var base = cfg.wsBaseUrl
        if base.hasSuffix("/") { base.removeLast() }
        var str = base + "/ws/panel"
        if let key = cfg.apiKey, !key.isEmpty {
            let encoded = key.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? key
            str += "?api_key=" + encoded
        }
        return URL(string: str)
    }

    private func beginCapture() {
        guard let url = sttURL() else {
            onError?("URL de STT inválida.")
            return
        }

        let task = session.webSocketTask(with: url)
        sttTask = task
        task.resume()
        receiveSTT()

        let input = captureEngine.inputNode
        let inFormat = input.outputFormat(forBus: 0)
        guard inFormat.sampleRate > 0 else {
            onError?("Microfone indisponível.")
            return
        }
        guard let target = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: 16000,
            channels: 1,
            interleaved: true
        ) else {
            onError?("Formato de áudio inválido.")
            return
        }
        outFormat = target
        converter = AVAudioConverter(from: inFormat, to: target)

        input.installTap(onBus: 0, bufferSize: 1600, format: inFormat) { [weak self] buffer, _ in
            self?.processCaptured(buffer)
        }

        do {
            framesReceived = 0
            captureEngine.prepare()
            try captureEngine.start()
            listening = true
            micMuted = false
            onListeningChanged?(true)
            // Diagnostic: if the tap never fires, the mic isn't actually feeding
            // us audio (almost always the TCC permission for this bare binary).
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.6) { [weak self] in
                guard let self, self.listening else { return }
                if self.framesReceived == 0 {
                    logErr("[NeoNotch] mic tap delivered 0 frames after 1.6s — permission?")
                    self.onError?("Sem áudio do microfone — confira Ajustes › Privacidade › Microfone.")
                }
            }
        } catch {
            onError?("Falha ao iniciar captura: \(error.localizedDescription)")
            cleanupCapture()
        }
    }

    private func processCaptured(_ buffer: AVAudioPCMBuffer) {
        framesReceived += 1
        // VU meter from the raw float input. Speech RMS is small (~0.02–0.15),
        // so apply a perceptual gain + curve, otherwise the bars barely move.
        if let ch = buffer.floatChannelData, buffer.frameLength > 0 {
            let n = Int(buffer.frameLength)
            var sum: Float = 0
            let p = ch[0]
            for i in 0..<n { sum += p[i] * p[i] }
            let rms = sqrtf(sum / Float(n))
            let level = CGFloat(min(1, powf(min(1, rms * 11), 0.6)))
            DispatchQueue.main.async { [weak self] in
                self?.onLevel?(level)
            }
        }

        if micMuted { return }
        guard let converter, let outFormat,
              let task = sttTask else { return }

        let ratio = outFormat.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 64
        guard let outBuffer = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: capacity) else { return }

        var consumed = false
        let inputBlock: AVAudioConverterInputBlock = { _, status in
            if consumed { status.pointee = .noDataNow; return nil }
            consumed = true
            status.pointee = .haveData
            return buffer
        }
        var err: NSError?
        converter.convert(to: outBuffer, error: &err, withInputFrom: inputBlock)
        guard err == nil, outBuffer.frameLength > 0, let ch = outBuffer.int16ChannelData else { return }

        let byteCount = Int(outBuffer.frameLength) * 2
        let data = Data(bytes: ch[0], count: byteCount)
        task.send(.data(data)) { _ in }
    }

    private func receiveSTT() {
        sttTask?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let message):
                switch message {
                case .string(let s): self.handleSttJSON(s)
                case .data(let d):
                    if let s = String(data: d, encoding: .utf8) { self.handleSttJSON(s) }
                @unknown default: break
                }
                self.receiveSTT()
            case .failure:
                break // socket closed
            }
        }
    }

    private func handleSttJSON(_ s: String) {
        guard let data = s.data(using: .utf8),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = root["type"] as? String else { return }
        let text = (root["text"] as? String) ?? ""
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            if type == "partial" {
                self.onPartial?(text)
            } else if type == "final" {
                let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
                if !trimmed.isEmpty { self.onFinal?(trimmed) }
            }
        }
    }

    func stopListening() {
        cleanupCapture()
        listening = false
        onListeningChanged?(false)
    }

    private func cleanupCapture() {
        if captureEngine.isRunning {
            captureEngine.inputNode.removeTap(onBus: 0)
            captureEngine.stop()
        }
        sttTask?.cancel(with: .goingAway, reason: nil)
        sttTask = nil
        converter = nil
    }

    // ------------------------------------------------------------------ TTS
    func speak(text: String, voice: String?, language: String?, backend: String?) {
        let clean = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { return }

        var base = cfg.wsBaseUrl
        if base.hasSuffix("/") { base.removeLast() }
        guard let url = URL(string: base + "/ws/tts") else { return }

        pauseMic()
        speaking = true
        ttsDone = false
        pendingBuffers = 0
        onSpeakingChanged?(true)

        let task = session.webSocketTask(with: url)
        ttsTask = task
        task.resume()

        var payload: [String: Any] = ["text": clean, "backend": backend ?? cfg.backend]
        if let v = voice ?? cfg.voice { payload["voice"] = v }
        if let l = language ?? cfg.language { payload["language"] = l }
        if let data = try? JSONSerialization.data(withJSONObject: payload),
           let json = String(data: data, encoding: .utf8) {
            task.send(.string(json)) { _ in }
        }
        receiveTTS()
    }

    private func receiveTTS() {
        ttsTask?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let message):
                switch message {
                case .string(let s): self.handleTtsJSON(s)
                case .data(let d): self.handleTtsChunk(d)
                @unknown default: break
                }
                self.receiveTTS()
            case .failure:
                DispatchQueue.main.async { [weak self] in self?.finishSpeaking() }
            }
        }
    }

    private func handleTtsJSON(_ s: String) {
        guard let data = s.data(using: .utf8),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = root["type"] as? String else { return }
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            switch type {
            case "start":
                let sr = (root["sr"] as? Int) ?? 24000
                self.setupPlayer(sampleRate: Double(sr))
            case "chunk":
                if let sr = root["sr"] as? Int { self.setupPlayer(sampleRate: Double(sr)) }
            case "done":
                self.ttsDone = true
                self.maybeFinishSpeaking()
            case "error":
                self.finishSpeaking()
            default:
                break
            }
        }
    }

    private func setupPlayer(sampleRate: Double) {
        if let f = playerFormat, abs(f.sampleRate - sampleRate) < 1 { return }
        guard let format = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: sampleRate,
            channels: 1,
            interleaved: false
        ) else { return }
        playerFormat = format

        if playEngine.isRunning { playEngine.stop() }
        if player.engine == nil { playEngine.attach(player) }
        playEngine.connect(player, to: playEngine.mainMixerNode, format: format)
        do {
            playEngine.prepare()
            try playEngine.start()
            player.play()
        } catch {
            // best-effort
        }
    }

    private func handleTtsChunk(_ data: Data) {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            if self.playerFormat == nil { self.setupPlayer(sampleRate: 24000) }
            guard let format = self.playerFormat else { return }

            let count = data.count / 2
            guard count > 0,
                  let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(count)),
                  let out = buffer.floatChannelData else { return }
            buffer.frameLength = AVAudioFrameCount(count)
            data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
                let src = raw.bindMemory(to: Int16.self)
                let dst = out[0]
                for i in 0..<count { dst[i] = Float(src[i]) / 32768.0 }
            }
            self.pendingBuffers += 1
            self.player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
                DispatchQueue.main.async {
                    guard let self else { return }
                    self.pendingBuffers -= 1
                    self.maybeFinishSpeaking()
                }
            }
        }
    }

    private func maybeFinishSpeaking() {
        if ttsDone && pendingBuffers <= 0 { finishSpeaking() }
    }

    private func finishSpeaking() {
        guard speaking else { return }
        speaking = false
        ttsTask?.cancel(with: .goingAway, reason: nil)
        ttsTask = nil
        if player.isPlaying { player.stop() }
        if playEngine.isRunning { playEngine.stop() }
        onSpeakingChanged?(false)
        resumeMic()
    }

    /// Interrupt any in-flight speech (e.g. user starts talking again).
    func cancelSpeak() {
        if speaking { finishSpeaking() }
    }

    private func pauseMic() { micMuted = true; onLevel?(0) }
    private func resumeMic() { if listening { micMuted = false } }

    func dispose() {
        cancelSpeak()
        stopListening()
    }
}

// MARK: - Island view

final class NeoIslandView: NSView, NSTextFieldDelegate {
    var islandState: IslandState = .idle {
        didSet { applyState() }
    }

    /// Called when the user submits the prompt field (Enter).
    var onSubmitPrompt: ((String) -> Void)?
    /// Called when the mic button is tapped.
    var onMicTapped: (() -> Void)?
    /// Called when a notification card is tapped (id, primaryAction?).
    var onNotificationTapped: ((String, String?) -> Void)?

    // Dynamic state
    private(set) var notifications: [NotchNotification] = []
    var activity: Activity = .none { didSet { needsDisplay = true } }
    var caption: String = "" { didSet { needsDisplay = true } }   // live transcript / answer
    var errorText: String? { didSet { needsDisplay = true } }
    var isListening = false {
        didSet {
            if isListening && !oldValue {
                levelHistory = [CGFloat](repeating: 0, count: levelHistory.count)
            }
            needsDisplay = true
        }
    }
    var isSpeaking = false { didSet { needsDisplay = true } }
    var micLevel: CGFloat = 0 {
        didSet {
            levelHistory.removeFirst()
            levelHistory.append(micLevel)
            if islandState == .expanded { needsDisplay = true }
        }
    }
    private var levelHistory = [CGFloat](repeating: 0, count: 30)

    private let promptField = NSTextField(frame: .zero)
    private let horizontalPadding: CGFloat = 24
    private let interItemSpacing: CGFloat = 10

    private var pulsePhase: CGFloat = 0
    private var pulseTimer: Timer?

    // Hit-test rects (refreshed every draw, expanded state only)
    private var micHitRect: NSRect = .zero
    private var notifHitRects: [(NSRect, String, String?)] = []

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true

        let placeholderAttrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 14, weight: .regular),
            .foregroundColor: NSColor.white.withAlphaComponent(0.38)
        ]
        promptField.placeholderAttributedString = NSAttributedString(
            string: "Pergunte ou peça algo ao Neo...",
            attributes: placeholderAttrs
        )
        promptField.font = NSFont.systemFont(ofSize: 14, weight: .regular)
        promptField.focusRingType = .none
        promptField.isBordered = false
        promptField.drawsBackground = false
        promptField.textColor = .white
        promptField.delegate = self
        promptField.isHidden = true
        addSubview(promptField)

        applyState()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override var isFlipped: Bool { true }

    // ---------------------------------------------------------------- data
    func setNotifications(_ list: [NotchNotification]) {
        notifications = list
        needsDisplay = true
    }

    func addNotification(_ n: NotchNotification) {
        notifications.removeAll { $0.id == n.id }
        notifications.insert(n, at: 0)
        if notifications.count > 6 { notifications = Array(notifications.prefix(6)) }
        needsDisplay = true
    }

    func removeNotification(id: String) {
        notifications.removeAll { $0.id == id }
        needsDisplay = true
    }

    // --------------------------------------------------------------- layout
    override func layout() {
        super.layout()
        let promptCardY: CGFloat = 122
        let promptCardHeight: CGFloat = 46
        let leftInset: CGFloat = 44
        let rightInset: CGFloat = 92
        let cardX = horizontalPadding
        let cardWidth = bounds.width - horizontalPadding * 2
        promptField.frame = NSRect(
            x: cardX + leftInset,
            y: promptCardY + (promptCardHeight - 24) / 2,
            width: max(120, cardWidth - (leftInset + rightInset)),
            height: 24
        )
    }

    private func applyState() {
        promptField.isHidden = islandState != .expanded
        needsDisplay = true
        if islandState == .expanded {
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.window?.makeFirstResponder(self.promptField)
            }
        }
    }

    // ---------------------------------------------------------- animation
    func startPulse() {
        if pulseTimer != nil { return }
        let timer = Timer(timeInterval: 1.0 / 30.0, repeats: true) { [weak self] _ in
            guard let self else { return }
            self.pulsePhase += 0.12
            if self.islandState == .expanded { self.needsDisplay = true }
        }
        RunLoop.main.add(timer, forMode: .common)
        pulseTimer = timer
    }

    func stopPulse() {
        pulseTimer?.invalidate()
        pulseTimer = nil
    }

    // -------------------------------------------------------------- drawing
    override func draw(_ dirtyRect: NSRect) {
        // Subtle dark tint over the glass blur so text stays readable on bright
        // desktops. Rounding is handled by the container layer's mask.
        NSColor.black.withAlphaComponent(0.30).setFill()
        bounds.fill()

        switch islandState {
        case .idle: drawIdle()
        case .peek: drawPeek()
        case .expanded: drawExpanded()
        }
    }

    private func drawIdle() {
        // Slim glowing bar — a tiny presence indicator inside the pill.
        let bar = NSBezierPath(roundedRect: bounds.insetBy(dx: 14, dy: 11), xRadius: 6, yRadius: 6)
        NSColor.white.withAlphaComponent(0.12).setFill()
        bar.fill()
        drawGlowDot(center: NSPoint(x: bounds.midX, y: bounds.midY), radius: 2.4)
    }

    private func drawPeek() {
        drawBotIcon(in: NSRect(x: 22, y: 9, width: 28, height: 28))
        drawText("Neo", in: NSRect(x: 60, y: 12, width: 90, height: 22), size: 16, weight: .semibold, color: .white, alignment: .left)
        let dotX = bounds.width - 92
        drawStatusDot(center: NSPoint(x: dotX, y: 23), active: isListening || isSpeaking)
        let label = isSpeaking ? "falando" : (isListening ? "ouvindo" : "online")
        drawText(label, in: NSRect(x: dotX + 12, y: 12, width: 64, height: 22), size: 14, weight: .medium, color: NSColor.white.withAlphaComponent(0.72), alignment: .left)
    }

    private func drawExpanded() {
        notifHitRects.removeAll()
        drawHeaderCard()
        drawPromptCard()

        let stageTop: CGFloat = 188
        let stageBottom = bounds.height - 44
        let stage = NSRect(x: horizontalPadding, y: stageTop, width: bounds.width - horizontalPadding * 2, height: stageBottom - stageTop)

        switch activity {
        case .listening:
            drawVoiceStage(in: stage, title: "Ouvindo…", body: caption.isEmpty ? "Pode falar — estou te ouvindo." : caption, accent: NSColor(calibratedRed: 0.36, green: 0.78, blue: 0.42, alpha: 1.0))
        case .thinking:
            drawVoiceStage(in: stage, title: "Pensando…", body: caption, accent: NSColor(calibratedRed: 0.53, green: 0.43, blue: 0.86, alpha: 1.0))
        case .answering:
            drawVoiceStage(in: stage, title: isSpeaking ? "Neo está falando" : "Neo respondeu", body: caption, accent: NSColor(calibratedRed: 0.36, green: 0.46, blue: 1.0, alpha: 1.0))
        case .none:
            drawNotifications(in: stage)
        }

        if let err = errorText {
            drawText("⚠ \(err)", in: NSRect(x: horizontalPadding, y: bounds.height - 60, width: bounds.width - horizontalPadding * 2, height: 18), size: 11, weight: .semibold, color: NSColor.systemRed.withAlphaComponent(0.9), alignment: .left)
        }

        drawText("⌘ Espaço · abrir Neo", in: NSRect(x: 24, y: bounds.height - 30, width: 160, height: 16), size: 11, weight: .medium, color: NSColor.white.withAlphaComponent(0.42), alignment: .left)
        drawKeycap("fn", in: NSRect(x: bounds.width - 78, y: bounds.height - 32, width: 25, height: 20))
        drawText("ditar", in: NSRect(x: bounds.width - 48, y: bounds.height - 28, width: 32, height: 14), size: 11, weight: .semibold, color: NSColor.white.withAlphaComponent(0.48), alignment: .left)
    }

    private func drawHeaderCard() {
        let card = NSRect(x: horizontalPadding, y: 30, width: bounds.width - horizontalPadding * 2, height: 72)
        fillRoundedCard(card, radius: 20, alpha: 0.22)
        let avatarRect = NSRect(x: card.minX + 18, y: card.minY + 18, width: 34, height: 34)
        drawBotIcon(in: avatarRect)

        // Mic button
        let micSize: CGFloat = 46
        let mic = NSRect(x: card.maxX - micSize - 16, y: card.minY + (card.height - micSize) / 2, width: micSize, height: micSize)
        micHitRect = mic
        let micPath = NSBezierPath(ovalIn: mic)
        if isListening {
            NSColor(calibratedRed: 0.92, green: 0.30, blue: 0.38, alpha: 1.0).setFill()
        } else if isSpeaking {
            NSColor(calibratedRed: 0.36, green: 0.46, blue: 1.0, alpha: 1.0).setFill()
        } else {
            NSColor(calibratedRed: 0.37, green: 0.30, blue: 0.94, alpha: 1.0).setFill()
        }
        micPath.fill()
        if isListening {
            // pulsing ring
            let r = 4 + sin(pulsePhase) * 2
            NSColor(calibratedRed: 0.92, green: 0.30, blue: 0.38, alpha: 0.35).setStroke()
            let ring = NSBezierPath(ovalIn: mic.insetBy(dx: -r, dy: -r))
            ring.lineWidth = 2
            ring.stroke()
        }
        drawMicGlyph(in: mic, scale: 0.34)

        let textX = avatarRect.maxX + interItemSpacing
        let textMaxWidth = max(120, mic.minX - textX - 14)
        let greeting: String
        switch activity {
        case .listening: greeting = "Estou ouvindo…"
        case .thinking: greeting = "Só um instante…"
        case .answering: greeting = isSpeaking ? "Falando com você" : "Pronto!"
        case .none: greeting = "Olá, em que posso ajudar?"
        }
        drawText(greeting, in: NSRect(x: textX, y: card.minY + 18, width: textMaxWidth, height: 20), size: 14, weight: .semibold, color: .white, alignment: .left)

        if isListening {
            drawWaveform(x: textX, y: card.minY + 38, width: min(textMaxWidth, 168), height: 18)
        } else {
            drawDottedLine(x: textX, y: card.minY + 43, dots: max(8, Int(min(14, (textMaxWidth / 12)))))
        }
    }

    private func drawPromptCard() {
        let card = NSRect(x: horizontalPadding, y: 122, width: bounds.width - horizontalPadding * 2, height: 46)
        fillRoundedCard(card, radius: 14, alpha: 0.32)
        let searchRect = NSRect(x: card.minX + 16, y: card.minY + 14, width: 18, height: 18)
        drawSearchGlyph(in: searchRect)

        let keySize: CGFloat = 26
        let keysY = card.minY + (card.height - keySize) / 2
        let rightKeyRect = NSRect(x: card.maxX - keySize - 10, y: keysY, width: keySize, height: keySize)
        let leftKeyRect = NSRect(x: rightKeyRect.minX - keySize - 6, y: keysY, width: keySize, height: keySize)
        drawKeyIcon("↵", in: leftKeyRect)
        drawKeyIcon("↑", in: rightKeyRect)
    }

    private func drawNotifications(in stage: NSRect) {
        drawText("NOTIFICAÇÕES", in: NSRect(x: stage.minX + 6, y: stage.minY, width: 250, height: 18), size: 11, weight: .bold, color: NSColor.white.withAlphaComponent(0.56), alignment: .left)
        drawText("\(notifications.count)", in: NSRect(x: stage.maxX - 64, y: stage.minY, width: 58, height: 18), size: 11, weight: .bold, color: NSColor.white.withAlphaComponent(0.64), alignment: .right)

        if notifications.isEmpty {
            let empty = NSRect(x: stage.minX, y: stage.minY + 30, width: stage.width, height: 64)
            fillRoundedCard(empty, radius: 14, alpha: 0.22)
            drawText("Tudo limpo por aqui ✨", in: empty.offsetBy(dx: 0, dy: 24), size: 13, weight: .medium, color: NSColor.white.withAlphaComponent(0.5), alignment: .center)
            return
        }

        var y = stage.minY + 28
        let cardHeight: CGFloat = 52
        let pitch: CGFloat = 60
        let maxCards = max(1, Int((stage.maxY - y) / pitch))
        for n in notifications.prefix(maxCards) {
            let card = NSRect(x: stage.minX, y: y, width: stage.width, height: cardHeight)
            drawNotificationCard(card, n: n)
            notifHitRects.append((card, n.id, n.primaryAction))
            y += pitch
        }
    }

    private func drawNotificationCard(_ card: NSRect, n: NotchNotification) {
        fillRoundedCard(card, radius: 13, alpha: 0.30)
        let iconRect = NSRect(x: card.minX + 14, y: card.minY + 11, width: 30, height: 30)
        drawAppIcon(n.kind, in: iconRect)

        let textX = iconRect.maxX + 12
        let rightMetaWidth: CGFloat = 58
        let textWidth = max(110, card.maxX - textX - rightMetaWidth - 12)
        drawText(n.title, in: NSRect(x: textX, y: card.minY + 10, width: textWidth, height: 17), size: 13, weight: .semibold, color: .white, alignment: .left)
        drawText(n.subtitle, in: NSRect(x: textX, y: card.minY + 27, width: textWidth, height: 16), size: 11, weight: .medium, color: NSColor.white.withAlphaComponent(0.62), alignment: .left)
        drawText(relativeTime(n.createdAt), in: NSRect(x: card.maxX - rightMetaWidth, y: card.minY + 12, width: rightMetaWidth - 10, height: 16), size: 11, weight: .semibold, color: NSColor.white.withAlphaComponent(0.55), alignment: .right)
    }

    private func drawVoiceStage(in stage: NSRect, title: String, body: String, accent: NSColor) {
        let card = NSRect(x: stage.minX, y: stage.minY, width: stage.width, height: min(stage.height, 220))
        fillRoundedCard(card, radius: 16, alpha: 0.26)

        // animated accent dot
        let dotAlpha = activity == .answering && !isSpeaking ? 1.0 : (0.55 + 0.45 * (0.5 + 0.5 * sin(pulsePhase)))
        accent.withAlphaComponent(dotAlpha).setFill()
        NSBezierPath(ovalIn: NSRect(x: card.minX + 18, y: card.minY + 20, width: 10, height: 10)).fill()
        drawText(title, in: NSRect(x: card.minX + 36, y: card.minY + 15, width: card.width - 52, height: 20), size: 13, weight: .bold, color: NSColor.white.withAlphaComponent(0.85), alignment: .left)

        let bodyRect = NSRect(x: card.minX + 18, y: card.minY + 44, width: card.width - 36, height: card.height - 58)
        if body.isEmpty && activity == .thinking {
            drawThinkingDots(at: NSPoint(x: card.minX + 22, y: card.minY + 58))
        } else {
            drawWrapped(body, in: bodyRect, size: 15, weight: .regular, color: NSColor.white.withAlphaComponent(0.9))
        }
    }

    // ----------------------------------------------------------- hit-test
    override func mouseDown(with event: NSEvent) {
        guard islandState == .expanded else {
            super.mouseDown(with: event)
            return
        }
        let p = convert(event.locationInWindow, from: nil)

        if micHitRect.contains(p) {
            onMicTapped?()
            return
        }
        for (rect, id, action) in notifHitRects where rect.contains(p) {
            onNotificationTapped?(id, action)
            removeNotification(id: id)
            return
        }
        // Click anywhere else focuses the prompt.
        window?.makeFirstResponder(promptField)
    }

    // --------------------------------------------------------- prompt submit
    func controlTextDidEndEditing(_ obj: Notification) {
        let movement = (obj.userInfo?["NSTextMovement"] as? Int) ?? 0
        guard movement == NSReturnTextMovement else { return }
        let text = promptField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        promptField.stringValue = ""
        onSubmitPrompt?(text)
    }

    // ------------------------------------------------------------- helpers
    private func relativeTime(_ ts: TimeInterval) -> String {
        if ts <= 0 { return "agora" }
        let delta = Date().timeIntervalSince1970 - ts
        if delta < 60 { return "agora" }
        if delta < 3600 { return "\(Int(delta / 60))m" }
        if delta < 86400 { return "\(Int(delta / 3600))h" }
        return "\(Int(delta / 86400))d"
    }

    private func fillRoundedCard(_ rect: NSRect, radius: CGFloat, alpha: CGFloat) {
        let path = NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)
        NSColor(calibratedWhite: 1.0, alpha: alpha * 0.18).setFill()
        path.fill()
        NSColor.white.withAlphaComponent(0.10).setStroke()
        path.lineWidth = 1
        path.stroke()
    }

    private func drawWrapped(_ text: String, in rect: NSRect, size: CGFloat, weight: NSFont.Weight, color: NSColor) {
        let p = NSMutableParagraphStyle()
        p.lineBreakMode = .byWordWrapping
        p.lineSpacing = 3
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: size, weight: weight),
            .foregroundColor: color,
            .paragraphStyle: p
        ]
        (text as NSString).draw(in: rect, withAttributes: attrs)
    }

    private func drawThinkingDots(at origin: NSPoint) {
        for i in 0..<3 {
            let phase = pulsePhase - CGFloat(i) * 0.5
            let a = 0.3 + 0.5 * (0.5 + 0.5 * sin(phase))
            NSColor.white.withAlphaComponent(a).setFill()
            NSBezierPath(ovalIn: NSRect(x: origin.x + CGFloat(i) * 14, y: origin.y, width: 7, height: 7)).fill()
        }
    }

    /// Scrolling live waveform: each bar is a sample from `levelHistory`, so the
    /// shape visibly moves with the voice (newest on the right).
    private func drawWaveform(x: CGFloat, y: CGFloat, width: CGFloat, height: CGFloat) {
        let bars = levelHistory.count
        guard bars > 0 else { return }
        let gap: CGFloat = 3
        let barW = max(2, (width - CGFloat(bars - 1) * gap) / CGFloat(bars))
        for i in 0..<bars {
            let v = levelHistory[i]
            let h = max(2.5, v * height)
            let rect = NSRect(x: x + CGFloat(i) * (barW + gap), y: y + (height - h) / 2, width: barW, height: h)
            NSColor(calibratedRed: 0.53, green: 0.43, blue: 0.86, alpha: 0.55 + 0.45 * v).setFill()
            NSBezierPath(roundedRect: rect, xRadius: barW / 2, yRadius: barW / 2).fill()
        }
    }

    private func drawBotIcon(in rect: NSRect) {
        NSColor(calibratedRed: 0.39, green: 0.31, blue: 0.80, alpha: 0.22).setFill()
        NSBezierPath(ovalIn: rect.insetBy(dx: -9, dy: -9)).fill()

        let antenna = NSBezierPath()
        antenna.move(to: NSPoint(x: rect.midX, y: rect.minY - 11))
        antenna.line(to: NSPoint(x: rect.midX, y: rect.minY))
        NSColor(calibratedRed: 0.53, green: 0.43, blue: 0.86, alpha: 1.0).setStroke()
        antenna.lineWidth = 2
        antenna.stroke()
        NSColor(calibratedRed: 0.53, green: 0.43, blue: 0.86, alpha: 1.0).setFill()
        NSBezierPath(ovalIn: NSRect(x: rect.midX - 3.5, y: rect.minY - 15, width: 7, height: 7)).fill()

        let outer = NSBezierPath(roundedRect: rect, xRadius: 13, yRadius: 13)
        NSColor(calibratedWhite: 0.16, alpha: 1.0).setFill()
        outer.fill()
        NSColor.white.withAlphaComponent(0.12).setStroke()
        outer.lineWidth = 1
        outer.stroke()

        let face = NSBezierPath(roundedRect: rect.insetBy(dx: 8, dy: 12), xRadius: 6, yRadius: 6)
        NSColor(calibratedWhite: 0.08, alpha: 1.0).setFill()
        face.fill()

        drawGlowDot(center: NSPoint(x: rect.midX - 7, y: rect.midY + 2), radius: 3)
        drawGlowDot(center: NSPoint(x: rect.midX + 7, y: rect.midY + 2), radius: 3)
    }

    private func drawAppIcon(_ kind: String, in rect: NSRect) {
        let path = NSBezierPath(roundedRect: rect, xRadius: 9, yRadius: 9)
        if kind == "chat" {
            NSColor(calibratedRed: 0.12, green: 0.78, blue: 0.34, alpha: 1.0).setFill()
            path.fill()
            drawChatGlyph(in: rect)
        } else {
            NSColor(calibratedRed: 0.36, green: 0.28, blue: 0.94, alpha: 1.0).setFill()
            path.fill()
            drawSparkGlyph(in: rect)
        }
    }

    private func drawSparkGlyph(in rect: NSRect) {
        let path = NSBezierPath()
        path.move(to: NSPoint(x: rect.midX, y: rect.minY + 8))
        path.line(to: NSPoint(x: rect.midX + 4, y: rect.midY - 2))
        path.line(to: NSPoint(x: rect.maxX - 8, y: rect.midY))
        path.line(to: NSPoint(x: rect.midX + 4, y: rect.midY + 3))
        path.line(to: NSPoint(x: rect.midX, y: rect.maxY - 8))
        path.line(to: NSPoint(x: rect.midX - 4, y: rect.midY + 3))
        path.line(to: NSPoint(x: rect.minX + 8, y: rect.midY))
        path.line(to: NSPoint(x: rect.midX - 4, y: rect.midY - 2))
        path.close()
        NSColor.white.setFill()
        path.fill()
    }

    private func drawChatGlyph(in rect: NSRect) {
        let bubble = NSBezierPath(ovalIn: rect.insetBy(dx: 8, dy: 8))
        NSColor.white.setFill()
        bubble.fill()
        NSColor(calibratedRed: 0.12, green: 0.78, blue: 0.34, alpha: 1.0).setFill()
        NSBezierPath(ovalIn: NSRect(x: rect.midX - 6, y: rect.midY - 2, width: 2.5, height: 2.5)).fill()
        NSBezierPath(ovalIn: NSRect(x: rect.midX, y: rect.midY + 1, width: 2.5, height: 2.5)).fill()
    }

    private func drawMicGlyph(in rect: NSRect, scale: CGFloat = 1.0) {
        let scaledWidth = rect.width * scale
        let scaledHeight = rect.height * scale
        let r = NSRect(x: rect.midX - scaledWidth / 2, y: rect.midY - scaledHeight / 2, width: scaledWidth, height: scaledHeight)
        let s = min(scaledWidth, scaledHeight) / 20
        NSColor.white.setStroke()
        let capsule = NSBezierPath(roundedRect: NSRect(x: r.midX - 3.4 * s, y: r.midY - 8.9 * s, width: 6.8 * s, height: 13 * s), xRadius: 3.4 * s, yRadius: 3.4 * s)
        capsule.lineWidth = 1.4 * s
        capsule.stroke()
        let arc = NSBezierPath()
        arc.appendArc(withCenter: NSPoint(x: r.midX, y: r.midY - 1.4 * s), radius: 8.8 * s, startAngle: 205, endAngle: 335, clockwise: true)
        arc.lineWidth = 1.4 * s
        arc.stroke()
        let stem = NSBezierPath()
        stem.move(to: NSPoint(x: r.midX, y: r.midY + 9.2 * s))
        stem.line(to: NSPoint(x: r.midX, y: r.midY + 11.6 * s))
        stem.move(to: NSPoint(x: r.midX - 4.2 * s, y: r.midY + 11.6 * s))
        stem.line(to: NSPoint(x: r.midX + 4.2 * s, y: r.midY + 11.6 * s))
        stem.lineWidth = 1.4 * s
        stem.stroke()
    }

    private func drawSearchGlyph(in rect: NSRect) {
        NSColor.white.withAlphaComponent(0.48).setStroke()
        let circle = NSBezierPath(ovalIn: NSRect(x: rect.minX + 2, y: rect.minY + 2, width: 13, height: 13))
        circle.lineWidth = 1.7
        circle.stroke()
        let handle = NSBezierPath()
        handle.move(to: NSPoint(x: rect.minX + 14, y: rect.minY + 15))
        handle.line(to: NSPoint(x: rect.maxX - 2, y: rect.maxY - 2))
        handle.lineWidth = 1.7
        handle.stroke()
    }

    private func drawKeyIcon(_ text: String, in rect: NSRect) {
        let path = NSBezierPath(roundedRect: rect, xRadius: 8, yRadius: 8)
        NSColor.white.withAlphaComponent(0.12).setFill()
        path.fill()
        drawText(text, in: rect.offsetBy(dx: 0, dy: 5), size: 14, weight: .bold, color: .white, alignment: .center)
    }

    private func drawKeycap(_ text: String, in rect: NSRect) {
        let path = NSBezierPath(roundedRect: rect, xRadius: 7, yRadius: 7)
        NSColor.white.withAlphaComponent(0.11).setFill()
        path.fill()
        NSColor.white.withAlphaComponent(0.14).setStroke()
        path.lineWidth = 1
        path.stroke()
        drawText(text, in: rect.offsetBy(dx: 0, dy: 4), size: 12, weight: .bold, color: NSColor.white.withAlphaComponent(0.80), alignment: .center)
    }

    private func drawDottedLine(x: CGFloat, y: CGFloat, dots: Int) {
        NSColor(calibratedRed: 0.53, green: 0.43, blue: 0.86, alpha: 0.72).setFill()
        for index in 0..<dots {
            let dotX = x + CGFloat(index) * 6
            NSBezierPath(ovalIn: NSRect(x: dotX, y: y, width: 2.5, height: 2.5)).fill()
        }
    }

    private func drawGlowDot(center: NSPoint, radius: CGFloat) {
        NSColor(calibratedRed: 0.61, green: 0.50, blue: 1.0, alpha: 0.34).setFill()
        NSBezierPath(ovalIn: NSRect(x: center.x - radius - 5, y: center.y - radius - 5, width: radius * 2 + 10, height: radius * 2 + 10)).fill()
        NSColor(calibratedRed: 0.72, green: 0.62, blue: 1.0, alpha: 1.0).setFill()
        NSBezierPath(ovalIn: NSRect(x: center.x - radius, y: center.y - radius, width: radius * 2, height: radius * 2)).fill()
    }

    private func drawStatusDot(center: NSPoint, active: Bool) {
        let radius: CGFloat = 4
        let rect = NSRect(x: center.x - radius, y: center.y - radius, width: radius * 2, height: radius * 2)
        (active ? NSColor.systemGreen : NSColor.systemGreen).setFill()
        NSBezierPath(ovalIn: rect).fill()
    }

    private func drawText(_ text: String, in rect: NSRect, size: CGFloat, weight: NSFont.Weight, color: NSColor, alignment: NSTextAlignment) {
        let paragraph = NSMutableParagraphStyle()
        paragraph.alignment = alignment
        paragraph.lineBreakMode = .byTruncatingTail
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: size, weight: weight),
            .foregroundColor: color,
            .paragraphStyle: paragraph
        ]
        text.draw(in: rect, withAttributes: attrs)
    }
}

// MARK: - Controller

final class NotchController {
    private let panel: NSPanel
    private let container: NSView
    private let blur: NSVisualEffectView
    private let islandView: NeoIslandView
    private let voice = VoiceController()

    /// Emits a prompt for the full agent (set by AppDelegate so it can tag the
    /// turn's origin — notch vs. Live chip).
    var onPrompt: ((String) -> Void)?

    private var state: IslandState = .idle
    private var hoverTimer: Timer?
    private var hoverBeganAt: TimeInterval?
    private var stableBeganAt: TimeInterval?
    private var lastPointer: NSPoint?

    private var lastTurnWasVoice = false

    private let idleSize = NSSize(width: 138, height: 30)
    private let peekSize = NSSize(width: 460, height: 46)
    private let expandedSize = NSSize(width: 560, height: 524)
    private let movementTolerance: CGFloat = 8
    private let peekIntentDelay: TimeInterval = 0.14
    private let expandIntentDelay: TimeInterval = 0.72

    init(screen: NSScreen) {
        let startFrame = Self.frame(for: idleSize, on: screen)
        panel = NeoInputPanel(
            contentRect: startFrame,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )

        container = NSView(frame: NSRect(origin: .zero, size: idleSize))
        container.wantsLayer = true
        container.layer?.masksToBounds = true
        container.layer?.cornerRadius = idleSize.height / 2
        container.layer?.borderWidth = 1
        container.layer?.borderColor = NSColor.white.withAlphaComponent(0.10).cgColor

        blur = NSVisualEffectView(frame: container.bounds)
        blur.autoresizingMask = [.width, .height]
        blur.material = .hudWindow
        blur.blendingMode = .behindWindow
        blur.state = .active
        blur.wantsLayer = true
        container.addSubview(blur)

        islandView = NeoIslandView(frame: container.bounds)
        islandView.autoresizingMask = [.width, .height]
        container.addSubview(islandView)

        panel.contentView = container
        panel.isFloatingPanel = true
        panel.level = .statusBar
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.ignoresMouseEvents = false
        panel.setFrame(startFrame, display: true)
        panel.orderFrontRegardless()

        wireVoice()
        wireIsland()
    }

    private func wireIsland() {
        islandView.onSubmitPrompt = { [weak self] text in
            guard let self else { return }
            self.lastTurnWasVoice = false
            self.islandView.errorText = nil
            self.islandView.caption = ""
            self.islandView.activity = .thinking
            self.onPrompt?(text)
        }
        islandView.onMicTapped = { [weak self] in
            self?.voice.toggleListening()
        }
        islandView.onNotificationTapped = { id, action in
            if let action {
                emitEvent(["type": "notification_action", "notification_id": id, "action_type": action])
            } else {
                emitEvent(["type": "notification_dismiss", "notification_id": id])
            }
        }
    }

    private func wireVoice() {
        voice.onPartial = { [weak self] text in
            guard let self else { return }
            if self.islandView.activity == .listening { self.islandView.caption = text }
        }
        voice.onFinal = { [weak self] text in
            guard let self else { return }
            self.lastTurnWasVoice = true
            self.islandView.caption = ""
            self.islandView.activity = .thinking
            self.onPrompt?(text)
        }
        voice.onListeningChanged = { [weak self] on in
            guard let self else { return }
            self.islandView.isListening = on
            if on {
                self.islandView.errorText = nil
                self.islandView.activity = .listening
            } else if self.islandView.activity == .listening {
                self.islandView.activity = .none
            }
        }
        voice.onSpeakingChanged = { [weak self] on in
            self?.islandView.isSpeaking = on
        }
        voice.onLevel = { [weak self] level in
            self?.islandView.micLevel = level
        }
        voice.onError = { [weak self] msg in
            self?.islandView.errorText = msg
        }
    }

    // ----------------------------------------------------------------- IPC
    func applyConfig(_ voiceCfg: VoiceConfig) {
        voice.cfg = voiceCfg
    }

    func addNotification(_ n: NotchNotification) {
        islandView.addNotification(n)
    }

    func agentComplete(text: String, voice voiceOverride: [String: Any]?) {
        islandView.caption = text
        islandView.activity = .answering
        if lastTurnWasVoice {
            let v = voiceOverride?["voice"] as? String
            let lang = voiceOverride?["language"] as? String
            let eng = voiceOverride?["engine"] as? String
            voice.speak(text: text, voice: v, language: lang, backend: eng)
        }
    }

    func agentError(_ message: String) {
        islandView.errorText = message
        islandView.activity = .none
    }

    func start() {
        apply(.idle, on: activeScreen() ?? NSScreen.main, animated: false)
        islandView.startPulse()
        hoverTimer = Timer.scheduledTimer(withTimeInterval: 0.025, repeats: true) { [weak self] _ in
            self?.tick()
        }
        RunLoop.main.add(hoverTimer!, forMode: .common)
    }

    private func tick() {
        guard let screen = activeScreen() else { return }
        let pointer = NSEvent.mouseLocation
        let now = CACurrentMediaTime()
        let interactiveFrame = panel.frame.insetBy(dx: state == .idle ? 8 : 14, dy: state == .idle ? 8 : 12)
        let isOverIsland = interactiveFrame.contains(pointer)
        let isOverNotch = notchZone(on: screen).contains(pointer)

        // Keep the island open while a voice/agent turn is alive even if the
        // pointer drifts away.
        let busy = voice.listening || voice.speaking || islandView.activity == .thinking

        if isOverIsland || isOverNotch {
            updateIntent(pointer: pointer, now: now, on: screen)
        } else if state != .idle && !busy {
            resetIntent()
            apply(.idle, on: screen, animated: true)
        } else {
            resetIntent()
        }
    }

    private func updateIntent(pointer: NSPoint, now: TimeInterval, on screen: NSScreen) {
        if hoverBeganAt == nil {
            hoverBeganAt = now
            stableBeganAt = now
            lastPointer = pointer
            return
        }
        let moved = distance(from: lastPointer ?? pointer, to: pointer)
        if moved > movementTolerance { stableBeganAt = now }
        lastPointer = pointer

        let stableFor = now - (stableBeganAt ?? now)
        if state == .idle && stableFor >= peekIntentDelay {
            apply(.peek, on: screen, animated: true)
        }
        if state != .expanded && stableFor >= expandIntentDelay {
            apply(.expanded, on: screen, animated: true)
        }
    }

    private func resetIntent() {
        hoverBeganAt = nil
        stableBeganAt = nil
        lastPointer = nil
    }

    private func distance(from a: NSPoint, to b: NSPoint) -> CGFloat {
        let dx = a.x - b.x
        let dy = a.y - b.y
        return sqrt(dx * dx + dy * dy)
    }

    private func apply(_ next: IslandState, on screen: NSScreen?, animated: Bool) {
        guard let screen else { return }
        state = next
        islandView.islandState = next

        // Returning to the pill ends the visible conversation: clear transient
        // answer/caption so the next expand starts on the notifications list.
        if next == .idle {
            islandView.activity = .none
            islandView.caption = ""
            islandView.errorText = nil
        }

        let size: NSSize
        let radius: CGFloat
        switch next {
        case .idle:
            size = idleSize
            radius = idleSize.height / 2     // true capsule (fixes the "knife")
        case .peek:
            size = peekSize
            radius = peekSize.height / 2     // true capsule
        case .expanded:
            size = expandedSize
            radius = 28
        }

        container.layer?.cornerRadius = radius
        blur.layer?.cornerRadius = radius

        let nextFrame = Self.frame(for: size, on: screen)

        if animated {
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.32
                context.timingFunction = CAMediaTimingFunction(controlPoints: 0.34, 1.56, 0.64, 1)
                panel.animator().setFrame(nextFrame, display: true)
            }
        } else {
            panel.setFrame(nextFrame, display: true)
        }

        if next == .expanded {
            panel.makeKeyAndOrderFront(nil)
        }
    }

    private func activeScreen() -> NSScreen? {
        let point = NSEvent.mouseLocation
        return NSScreen.screens.first(where: { $0.frame.contains(point) }) ?? NSScreen.main
    }

    private func notchZone(on screen: NSScreen) -> NSRect {
        let frame = screen.frame
        return NSRect(x: frame.midX - 150, y: frame.maxY - 34, width: 300, height: 34)
    }

    private static func frame(for size: NSSize, on screen: NSScreen) -> NSRect {
        let screenFrame = screen.frame
        let x = screenFrame.midX - size.width / 2
        let y = screenFrame.maxY - size.height
        return NSRect(x: x, y: y, width: size.width, height: size.height)
    }
}

final class NeoInputPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

// MARK: - Live Mode chip

/// The hands-free "Live Mode" chip that drops in just below the macOS notch
/// when the wake word fires. Driven by the webview's Live pipeline (forwarded
/// over IPC) — this is purely the native presentation. Three states:
///   listening → a compact pill with a live waveform + close dot
///   thinking  → "Deixa comigo…" with an indeterminate progress bar
///   speaking  → a card with Neo's spoken answer
final class LiveChipView: NSView {
    var liveState: String = "idle" { didSet { needsDisplay = true } }
    var spoken: String = "" { didSet { needsDisplay = true } }
    var partial: String = "" { didSet { needsDisplay = true } }
    var level: CGFloat = 0 {
        didSet {
            levelHistory.removeFirst()
            levelHistory.append(level)
            needsDisplay = true
        }
    }
    var onClose: (() -> Void)?
    var onAbort: (() -> Void)?

    private var levelHistory = [CGFloat](repeating: 0, count: 22)
    private var pulsePhase: CGFloat = 0
    private var progressPhase: CGFloat = 0
    private var timer: Timer?
    private var closeHitRect: NSRect = .zero
    private var abortHitRect: NSRect = .zero

    override var isFlipped: Bool { true }

    func startAnimating() {
        if timer != nil { return }
        let t = Timer(timeInterval: 1.0 / 30.0, repeats: true) { [weak self] _ in
            guard let self else { return }
            self.pulsePhase += 0.13
            self.progressPhase += 0.018
            if self.progressPhase > 1 { self.progressPhase -= 1 }
            self.needsDisplay = true
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    func stopAnimating() {
        timer?.invalidate()
        timer = nil
    }

    override func draw(_ dirtyRect: NSRect) {
        // Light tint over the blur — keep it glassy (the desktop shows through).
        NSColor.black.withAlphaComponent(0.18).setFill()
        bounds.fill()
        abortHitRect = .zero
        closeHitRect = .zero
        switch liveState {
        case "listening": drawListening()
        case "thinking": drawThinking()
        case "speaking": drawSpeaking()
        default: break
        }
    }

    // listening pill
    private func drawListening() {
        let avatar = NSRect(x: 16, y: bounds.midY - 20, width: 40, height: 40)
        drawNeoFace(in: avatar, glow: true)

        let leftmost = drawControls(centerY: bounds.midY, rightEdge: bounds.maxX - 18, showAbort: false)

        let waveX = avatar.maxX + 16
        let waveW = leftmost - 14 - waveX
        drawWaveform(x: waveX, y: bounds.midY - 11, width: waveW, height: 22)
    }

    // thinking / transition card
    private func drawThinking() {
        let avatar = NSRect(x: 18, y: bounds.midY - 22, width: 44, height: 44)
        drawNeoFace(in: avatar, glow: true)

        _ = drawControls(centerY: 22, rightEdge: bounds.maxX - 18, showAbort: true)

        let textX = avatar.maxX + 14
        drawText("Deixa comigo", in: NSRect(x: textX, y: bounds.minY + 18, width: 200, height: 22), size: 16, weight: .semibold, color: .white, alignment: .left)
        drawThinkingDots(at: NSPoint(x: textX + 124, y: bounds.minY + 27))

        // indeterminate progress bar
        let track = NSRect(x: textX, y: bounds.maxY - 24, width: bounds.maxX - 22 - textX, height: 4)
        NSColor.white.withAlphaComponent(0.10).setFill()
        NSBezierPath(roundedRect: track, xRadius: 2, yRadius: 2).fill()
        let segW = track.width * 0.32
        let travel = track.width + segW
        let x = track.minX - segW + travel * progressPhase
        let clampedX = max(track.minX, x)
        let clampedMax = min(track.maxX, x + segW)
        if clampedMax > clampedX {
            let seg = NSRect(x: clampedX, y: track.minY, width: clampedMax - clampedX, height: 4)
            NSColor(calibratedRed: 0.45, green: 0.58, blue: 1.0, alpha: 0.95).setFill()
            NSBezierPath(roundedRect: seg, xRadius: 2, yRadius: 2).fill()
        }
    }

    // speaking / response card
    private func drawSpeaking() {
        let avatar = NSRect(x: 18, y: 16, width: 36, height: 36)
        drawNeoFace(in: avatar, glow: false)
        drawText("Neo", in: NSRect(x: avatar.maxX + 12, y: 22, width: 60, height: 22), size: 15, weight: .bold, color: .white, alignment: .left)
        drawMiniBars(x: avatar.maxX + 58, y: 26, active: true)

        let leftmost = drawControls(centerY: 30, rightEdge: bounds.maxX - 18, showAbort: true)
        // "⌥ falar" hint, left of the controls
        drawText("⌥ falar", in: NSRect(x: leftmost - 76, y: 22, width: 64, height: 18), size: 12, weight: .semibold, color: NSColor.white.withAlphaComponent(0.5), alignment: .right)

        let body = NSRect(x: 22, y: 58, width: bounds.width - 44, height: bounds.height - 70)
        let p = NSMutableParagraphStyle()
        p.lineBreakMode = .byWordWrapping
        p.lineSpacing = 4
        (spoken as NSString).draw(in: body, withAttributes: [
            .font: NSFont.systemFont(ofSize: 16, weight: .regular),
            .foregroundColor: NSColor.white.withAlphaComponent(0.92),
            .paragraphStyle: p
        ])
    }

    // ------------------------------------------------------------- hit-test
    override func mouseDown(with event: NSEvent) {
        let p = convert(event.locationInWindow, from: nil)
        if abortHitRect != .zero, abortHitRect.insetBy(dx: -8, dy: -8).contains(p) {
            onAbort?()
            return
        }
        if closeHitRect.insetBy(dx: -8, dy: -8).contains(p) {
            onClose?()
        }
    }

    // ------------------------------------------------------------- helpers
    /// Draws the abort (stop) + close controls at the right edge. Returns the
    /// x of the leftmost control so callers can lay out content beside them.
    @discardableResult
    private func drawControls(centerY: CGFloat, rightEdge: CGFloat, showAbort: Bool) -> CGFloat {
        let closeR: CGFloat = 7
        let close = NSRect(x: rightEdge - closeR * 2, y: centerY - closeR, width: closeR * 2, height: closeR * 2)
        closeHitRect = close
        NSColor(calibratedRed: 0.92, green: 0.32, blue: 0.34, alpha: 0.95).setFill()
        NSBezierPath(ovalIn: close).fill()

        guard showAbort else { return close.minX }

        let aR: CGFloat = 9
        let abort = NSRect(x: close.minX - 12 - aR * 2, y: centerY - aR, width: aR * 2, height: aR * 2)
        abortHitRect = abort
        NSColor.white.withAlphaComponent(0.14).setFill()
        NSBezierPath(ovalIn: abort).fill()
        NSColor.white.withAlphaComponent(0.18).setStroke()
        let ring = NSBezierPath(ovalIn: abort)
        ring.lineWidth = 1
        ring.stroke()
        // stop square
        let sq = abort.insetBy(dx: aR * 0.62, dy: aR * 0.62)
        NSColor.white.withAlphaComponent(0.85).setFill()
        NSBezierPath(roundedRect: sq, xRadius: 1.5, yRadius: 1.5).fill()
        return abort.minX
    }

    private func drawNeoFace(in rect: NSRect, glow: Bool) {
        if glow {
            NSColor(calibratedRed: 0.42, green: 0.55, blue: 0.98, alpha: 0.22).setFill()
            NSBezierPath(ovalIn: rect.insetBy(dx: -7, dy: -7)).fill()
        }
        NSColor(calibratedRed: 0.30, green: 0.41, blue: 0.92, alpha: 1.0).setFill()
        NSBezierPath(ovalIn: rect).fill()
        NSColor(calibratedRed: 0.55, green: 0.66, blue: 1.0, alpha: 0.5).setStroke()
        let ring = NSBezierPath(ovalIn: rect.insetBy(dx: 0.5, dy: 0.5))
        ring.lineWidth = 1
        ring.stroke()

        let eyeR = rect.width * 0.085
        let eyeY = rect.midY - eyeR
        let dx = rect.width * 0.17
        for ex in [rect.midX - dx, rect.midX + dx] {
            NSColor.white.withAlphaComponent(0.95).setFill()
            NSBezierPath(ovalIn: NSRect(x: ex - eyeR, y: eyeY, width: eyeR * 2, height: eyeR * 2)).fill()
        }
    }

    private func drawWaveform(x: CGFloat, y: CGFloat, width: CGFloat, height: CGFloat) {
        let bars = levelHistory.count
        guard bars > 0, width > 0 else { return }
        let gap: CGFloat = 3
        let barW = max(2, (width - CGFloat(bars - 1) * gap) / CGFloat(bars))
        for i in 0..<bars {
            let v = levelHistory[i]
            let h = max(3, v * height)
            let rect = NSRect(x: x + CGFloat(i) * (barW + gap), y: y + (height - h) / 2, width: barW, height: h)
            NSColor(calibratedRed: 0.50, green: 0.62, blue: 1.0, alpha: 0.55 + 0.45 * v).setFill()
            NSBezierPath(roundedRect: rect, xRadius: barW / 2, yRadius: barW / 2).fill()
        }
    }

    private func drawMiniBars(x: CGFloat, y: CGFloat, active: Bool) {
        for i in 0..<4 {
            let phase = pulsePhase - CGFloat(i) * 0.6
            let h = active ? 4 + (0.5 + 0.5 * sin(phase)) * 9 : 5
            let rect = NSRect(x: x + CGFloat(i) * 5, y: y + (12 - h) / 2, width: 3, height: h)
            NSColor(calibratedRed: 0.50, green: 0.62, blue: 1.0, alpha: 0.9).setFill()
            NSBezierPath(roundedRect: rect, xRadius: 1.5, yRadius: 1.5).fill()
        }
    }

    private func drawThinkingDots(at origin: NSPoint) {
        for i in 0..<3 {
            let phase = pulsePhase - CGFloat(i) * 0.5
            let a = 0.3 + 0.5 * (0.5 + 0.5 * sin(phase))
            NSColor.white.withAlphaComponent(a).setFill()
            NSBezierPath(ovalIn: NSRect(x: origin.x + CGFloat(i) * 13, y: origin.y, width: 7, height: 7)).fill()
        }
    }

    private func drawText(_ text: String, in rect: NSRect, size: CGFloat, weight: NSFont.Weight, color: NSColor, alignment: NSTextAlignment) {
        let paragraph = NSMutableParagraphStyle()
        paragraph.alignment = alignment
        paragraph.lineBreakMode = .byTruncatingTail
        text.draw(in: rect, withAttributes: [
            .font: NSFont.systemFont(ofSize: size, weight: weight),
            .foregroundColor: color,
            .paragraphStyle: paragraph
        ])
    }
}

final class LiveChipController {
    private let panel: NSPanel
    private let container: NSView
    private let blur: NSVisualEffectView
    private let chip: LiveChipView
    private var visible = false
    private var currentState = "idle"

    var onClose: (() -> Void)?
    var onAbort: (() -> Void)?

    init() {
        panel = NeoInputPanel(
            contentRect: NSRect(x: 0, y: 0, width: 320, height: 64),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        container = NSView(frame: panel.contentView?.bounds ?? .zero)
        container.wantsLayer = true
        container.layer?.masksToBounds = true
        container.layer?.cornerRadius = 32
        container.layer?.borderWidth = 1
        container.layer?.borderColor = NSColor.white.withAlphaComponent(0.10).cgColor

        blur = NSVisualEffectView(frame: container.bounds)
        blur.autoresizingMask = [.width, .height]
        blur.material = .hudWindow
        blur.blendingMode = .behindWindow
        blur.state = .active
        blur.wantsLayer = true
        container.addSubview(blur)

        chip = LiveChipView(frame: container.bounds)
        chip.autoresizingMask = [.width, .height]
        container.addSubview(chip)

        panel.contentView = container
        panel.isFloatingPanel = true
        panel.level = .statusBar
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.ignoresMouseEvents = false

        chip.onClose = { [weak self] in self?.onClose?() }
        chip.onAbort = { [weak self] in self?.onAbort?() }
    }

    func update(active: Bool, state: String, partial: String, spoken: String, level: CGFloat) {
        if !active {
            hide()
            return
        }
        chip.liveState = state
        chip.partial = partial
        chip.spoken = spoken
        chip.level = level

        if currentState != state {
            currentState = state
            resize(for: state, spoken: spoken)
        } else if state == "speaking" {
            // answer text may have grown — keep the card sized to it
            resize(for: state, spoken: spoken)
        }
        show()
    }

    /// Frequent mic-level updates without re-sizing the panel.
    func setLevel(_ level: CGFloat) {
        chip.level = level
    }

    func hideChip() {
        currentState = "idle"
        hide()
    }

    private func show() {
        if !visible {
            visible = true
            chip.startAnimating()
            panel.orderFrontRegardless()
        }
    }

    private func hide() {
        if visible {
            visible = false
            chip.stopAnimating()
            panel.orderOut(nil)
        }
    }

    private func resize(for state: String, spoken: String) {
        guard let screen = NSScreen.main else { return }
        let size = Self.size(for: state, spoken: spoken)
        let radius: CGFloat = state == "listening" ? size.height / 2 : 26
        container.layer?.cornerRadius = radius
        blur.layer?.cornerRadius = radius

        let visibleTop = screen.visibleFrame.maxY - 2   // just under the notch/menu bar
        let frame = NSRect(
            x: screen.frame.midX - size.width / 2,
            y: visibleTop - size.height,
            width: size.width,
            height: size.height
        )
        if visible {
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.26
                context.timingFunction = CAMediaTimingFunction(controlPoints: 0.34, 1.4, 0.64, 1)
                panel.animator().setFrame(frame, display: true)
            }
        } else {
            panel.setFrame(frame, display: true)
        }
    }

    private static func size(for state: String, spoken: String) -> NSSize {
        switch state {
        case "listening":
            return NSSize(width: 320, height: 64)
        case "thinking":
            return NSSize(width: 460, height: 84)
        case "speaking":
            let width: CGFloat = 600
            let bodyWidth = width - 44
            let attr = NSAttributedString(string: spoken.isEmpty ? " " : spoken, attributes: [
                .font: NSFont.systemFont(ofSize: 16, weight: .regular)
            ])
            let bounding = attr.boundingRect(with: NSSize(width: bodyWidth, height: 600), options: [.usesLineFragmentOrigin, .usesFontLeading])
            let textHeight = min(220, max(24, ceil(bounding.height)))
            return NSSize(width: width, height: 58 + textHeight + 22)
        default:
            return NSSize(width: 320, height: 64)
        }
    }
}

/// Orchestrates hands-free Live Mode natively: owns its own VoiceController so
/// it keeps capturing even when the main app window is minimized. Drives the
/// LiveChip and routes the conversation (STT → agent prompt → answer + TTS).
final class LiveSession {
    private let chip: LiveChipController
    private let voice = VoiceController()
    var onPrompt: ((String) -> Void)?

    private var active = false
    /// The answer card lingers after TTS ends; it only collapses back to
    /// "listening" once the user actually speaks again (a partial arrives).
    private var showingAnswer = false

    init(chip: LiveChipController) {
        self.chip = chip
        wire()
    }

    func applyVoiceConfig(_ cfg: VoiceConfig) { voice.cfg = cfg }
    var isActive: Bool { active }

    func start() {
        if active { return }
        active = true
        showingAnswer = false
        chip.update(active: true, state: "listening", partial: "", spoken: "", level: 0)
        voice.toggleListening()
    }

    func stop() {
        if !active { return }
        active = false
        showingAnswer = false
        voice.dispose()
        chip.hideChip()
    }

    /// Abort just the current turn (stop speaking/agent) and keep listening.
    func abort() {
        guard active else { return }
        voice.cancelSpeak()
        showingAnswer = false
        chip.update(active: true, state: "listening", partial: "", spoken: "", level: 0)
        emitEvent(["type": "live_abort"])
    }

    func agentComplete(text: String, voice voiceOverride: [String: Any]?) {
        guard active else { return }
        showingAnswer = true
        chip.update(active: true, state: "speaking", partial: "", spoken: text, level: 0)
        let v = voiceOverride?["voice"] as? String
        let lang = voiceOverride?["language"] as? String
        let eng = voiceOverride?["engine"] as? String
        voice.speak(text: text, voice: v, language: lang, backend: eng)
    }

    func agentError(_ message: String) {
        guard active else { return }
        showingAnswer = true
        chip.update(active: true, state: "speaking", partial: "", spoken: "⚠ " + message, level: 0)
    }

    private func wire() {
        chip.onClose = { [weak self] in
            emitEvent(["type": "live_close"])
            self?.stop()
        }
        chip.onAbort = { [weak self] in self?.abort() }

        voice.onListeningChanged = { [weak self] on in
            guard let self, self.active else { return }
            if on && !self.showingAnswer {
                self.chip.update(active: true, state: "listening", partial: "", spoken: "", level: 0)
            }
        }
        voice.onPartial = { [weak self] text in
            guard let self, self.active else { return }
            // User is speaking again → dismiss the lingering answer.
            self.showingAnswer = false
            self.chip.update(active: true, state: "listening", partial: text, spoken: "", level: 0)
        }
        voice.onFinal = { [weak self] text in
            guard let self, self.active else { return }
            self.showingAnswer = false
            self.chip.update(active: true, state: "thinking", partial: "", spoken: "", level: 0)
            self.onPrompt?(text)
        }
        voice.onSpeakingChanged = { _ in
            // Intentionally no-op: when playback ends we KEEP the answer card.
        }
        voice.onLevel = { [weak self] level in
            guard let self, self.active, !self.showingAnswer else { return }
            self.chip.setLevel(level)
        }
        voice.onError = { [weak self] msg in
            guard let self, self.active else { return }
            self.showingAnswer = true
            self.chip.update(active: true, state: "speaking", partial: "", spoken: "⚠ " + msg, level: 0)
        }
    }
}

// MARK: - App delegate + IPC read loop

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var controller: NotchController?
    private var liveChip: LiveChipController?
    private var liveSession: LiveSession?
    /// Tracks whether the in-flight agent turn came from the Live chip or the
    /// notch island, so the answer is routed back to the right surface.
    private var pendingFromLive = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        guard let screen = NSScreen.main else { return }
        let controller = NotchController(screen: screen)
        self.controller = controller
        controller.start()
        controller.onPrompt = { [weak self] text in self?.submitPrompt(text, fromLive: false) }

        let chip = LiveChipController()
        liveChip = chip
        let session = LiveSession(chip: chip)
        session.onPrompt = { [weak self] text in self?.submitPrompt(text, fromLive: true) }
        liveSession = session

        emitEvent(["type": "ready"])
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.readLoop()
        }
    }

    private func submitPrompt(_ text: String, fromLive: Bool) {
        pendingFromLive = fromLive
        emitEvent(["type": "prompt", "text": text])
    }

    private func readLoop() {
        while let line = readLine(strippingNewline: true) {
            guard let data = line.data(using: .utf8),
                  let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let type = root["type"] as? String else { continue }
            DispatchQueue.main.async { [weak self] in
                self?.handle(type: type, root: root)
            }
        }
    }

    private func handle(type: String, root: [String: Any]) {
        guard let controller else { return }
        switch type {
        case "config":
            if let v = root["voice"] as? [String: Any] {
                var cfg = VoiceConfig()
                if let s = v["wsBaseUrl"] as? String, !s.isEmpty { cfg.wsBaseUrl = s }
                cfg.apiKey = v["apiKey"] as? String
                cfg.voice = v["voice"] as? String
                if let b = v["backend"] as? String, !b.isEmpty { cfg.backend = b }
                cfg.language = v["language"] as? String
                controller.applyConfig(cfg)
                liveSession?.applyVoiceConfig(cfg)
            }
        case "notification":
            if let n = root["notification"] as? [String: Any] {
                controller.addNotification(Self.parseNotification(n))
            }
        case "agent_complete":
            let text = (root["text"] as? String) ?? ""
            let voice = root["voice"] as? [String: Any]
            if pendingFromLive {
                liveSession?.agentComplete(text: text, voice: voice)
            } else {
                controller.agentComplete(text: text, voice: voice)
            }
        case "agent_error":
            let message = (root["message"] as? String) ?? "Falha ao processar."
            if pendingFromLive {
                liveSession?.agentError(message)
            } else {
                controller.agentError(message)
            }
        case "live_start":
            liveSession?.start()
        case "live_stop":
            liveSession?.stop()
        default:
            break
        }
    }

    private static func parseNotification(_ n: [String: Any]) -> NotchNotification {
        let id = (n["id"] as? String) ?? UUID().uuidString
        let title = (n["title"] as? String) ?? "Notificação"
        let subtitle = (n["description"] as? String) ?? (n["body"] as? String) ?? ""
        let kindRaw = ((n["kind"] as? String) ?? "").lowercased()
        let chatLike = kindRaw.contains("message") || kindRaw.contains("chat")
            || kindRaw.contains("whatsapp") || kindRaw.contains("dm")
        let kind = chatLike ? "chat" : "spark"
        let createdAt: TimeInterval = (n["created_at_unix"] as? Double)
            ?? (n["created_at_unix"] as? Int).map(Double.init)
            ?? Date().timeIntervalSince1970
        var primaryAction: String?
        if let actions = n["actions"] as? [[String: Any]], let first = actions.first {
            primaryAction = first["action_type"] as? String
        }
        return NotchNotification(
            id: id,
            kind: kind,
            title: title,
            subtitle: subtitle,
            createdAt: createdAt,
            primaryAction: primaryAction
        )
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()

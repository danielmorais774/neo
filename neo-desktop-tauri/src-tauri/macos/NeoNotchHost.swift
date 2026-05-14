import AppKit
import QuartzCore

enum IslandState {
    case idle
    case peek
    case expanded
}

final class NeoIslandView: NSView, NSTextFieldDelegate {
    var islandState: IslandState = .idle {
        didSet { applyState() }
    }

    private let promptField = NSTextField(frame: .zero)
    private var isThinking = false
    private var pulsePhase = false
    private var pulseTimer: Timer?
    private let horizontalPadding: CGFloat = 24
    private let interItemSpacing: CGFloat = 10

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.masksToBounds = true

        promptField.placeholderString = "Pergunte ou peça algo ao Neo..."
        promptField.font = NSFont.systemFont(ofSize: 14, weight: .regular)
        promptField.focusRingType = .none
        promptField.isBordered = false
        promptField.drawsBackground = true
        promptField.backgroundColor = .clear
        promptField.textColor = .white
        promptField.placeholderAttributedString = NSAttributedString(
            string: "Pergunte ou peça algo ao Neo...",
            attributes: [
                .font: NSFont.systemFont(ofSize: 14, weight: .regular),
                .foregroundColor: NSColor.white.withAlphaComponent(0.38)
            ]
        )
        promptField.delegate = self
        promptField.isHidden = true
        addSubview(promptField)

        applyState()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override var isFlipped: Bool { true }

    override func layout() {
        super.layout()
        let promptCardY: CGFloat = 122
        let promptCardHeight: CGFloat = 44
        let promptCardX = horizontalPadding
        let promptCardWidth = bounds.width - horizontalPadding * 2
        let leftInset: CGFloat = 44
        let rightInset: CGFloat = 92
        promptField.frame = NSRect(
            x: promptCardX + leftInset,
            y: promptCardY + (promptCardHeight - 24) / 2,
            width: max(120, promptCardWidth - (leftInset + rightInset)),
            height: 24
        )
    }

    private func applyState() {
        promptField.isHidden = islandState != .expanded
        layer?.cornerRadius = islandState == .idle ? 19 : 34
        needsDisplay = true
        if islandState == .expanded {
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.window?.makeFirstResponder(self.promptField)
            }
        }
    }

    override func draw(_ dirtyRect: NSRect) {
        drawBackground()

        switch islandState {
        case .idle:
            drawIdle()
        case .peek:
            drawPeek()
        case .expanded:
            drawExpanded()
        }
    }

    private func drawBackground() {
        let radius = islandState == .idle ? bounds.height / 2 : 34
        let path = NSBezierPath(roundedRect: bounds.insetBy(dx: 1, dy: 1), xRadius: radius, yRadius: radius)
        NSColor.black.withAlphaComponent(0.985).setFill()
        path.fill()

        NSColor.white.withAlphaComponent(0.10).setStroke()
        path.lineWidth = 1
        path.stroke()
    }

    private func drawIdle() {
        let glint = NSBezierPath(roundedRect: bounds.insetBy(dx: 14, dy: 9), xRadius: 8, yRadius: 8)
        NSColor.white.withAlphaComponent(0.1).setFill()
        glint.fill()
    }

    private func drawPeek() {
        drawRobotIcon(at: NSRect(x: 24, y: 8, width: 24, height: 24))
        drawText("Neo", in: NSRect(x: 56, y: 10, width: 80, height: 22), size: 16, weight: .semibold, color: .white, alignment: .left)
        drawStatusDot(center: NSPoint(x: bounds.width - 88, y: 23))
        drawText("online", in: NSRect(x: bounds.width - 78, y: 10, width: 54, height: 22), size: 14, weight: .medium, color: NSColor.white.withAlphaComponent(0.72), alignment: .left)
    }

    private func drawExpanded() {
        drawHeaderCard()
        drawPromptCard()

        drawSectionTitle("NOTIFICAÇÕES", count: "3", y: 170)
        drawNotificationCard(y: 196, kind: .spark, title: "Resumo do call pronto", subtitle: "12 itens · 4 ações sugeridas", time: "agora", badge: nil)
        drawNotificationCard(y: 256, kind: .chat, title: "Marina Lopes", subtitle: "Te envio o briefing em 10min", time: "2m", badge: nil)
        drawNotificationCard(y: 316, kind: .chat, title: "Time · Acme", subtitle: "Lucas: revisei a PR, pode mergear", time: "8m", badge: "3")

        drawSectionTitle("FILAS DE PROCESSAMENTO", count: "3 ativas", y: 386)
        drawQueueCard(y: 412)

        drawText("⌘ Espaço · abrir Neo", in: NSRect(x: 24, y: bounds.height - 32, width: 150, height: 16), size: 11, weight: .medium, color: NSColor.white.withAlphaComponent(0.42), alignment: .left)
        drawKeycap("fn", in: NSRect(x: bounds.width - 78, y: bounds.height - 34, width: 25, height: 20))
        drawText("ditar", in: NSRect(x: bounds.width - 48, y: bounds.height - 30, width: 32, height: 14), size: 11, weight: .semibold, color: NSColor.white.withAlphaComponent(0.48), alignment: .left)
    }

    private func drawHeaderCard() {
        let card = NSRect(x: horizontalPadding, y: 30, width: bounds.width - horizontalPadding * 2, height: 72)
        fillRoundedCard(card, radius: 20, alpha: 0.40)
        let avatarRect = NSRect(x: card.minX + 18, y: card.minY + 18, width: 34, height: 34)
        drawBotIcon(in: avatarRect)

        let micSize: CGFloat = 45
        let mic = NSRect(x: card.maxX - micSize - 16, y: card.minY + (card.height - micSize) / 2, width: micSize, height: micSize)
        let micPath = NSBezierPath(ovalIn: mic)
        NSColor(calibratedRed: 0.37, green: 0.30, blue: 0.94, alpha: 1.0).setFill()
        micPath.fill()
        drawMicGlyph(in: mic, scale: 0.42)

        let textX = avatarRect.maxX + interItemSpacing
        let textMaxWidth = max(120, mic.minX - textX - 14)
        drawText("Olá, em que posso ajudar?", in: NSRect(x: textX, y: card.minY + 18, width: textMaxWidth, height: 20), size: 14, weight: .semibold, color: .white, alignment: .left)
        drawDottedLine(x: textX, y: card.minY + 43, dots: max(8, Int(min(14, (textMaxWidth / 12)))))
    }

    private func drawPromptCard() {
        let card = NSRect(x: horizontalPadding, y: 122, width: bounds.width - horizontalPadding * 2, height: 44)
        fillRoundedCard(card, radius: 14, alpha: 0.50)
        let searchRect = NSRect(x: card.minX + 16, y: card.minY + 13, width: 18, height: 18)
        drawSearchGlyph(in: searchRect)

        let keySize: CGFloat = 26
        let keysY = card.minY + (card.height - keySize) / 2
        let rightKeyRect = NSRect(x: card.maxX - keySize - 10, y: keysY, width: keySize, height: keySize)
        let leftKeyRect = NSRect(x: rightKeyRect.minX - keySize - 6, y: keysY, width: keySize, height: keySize)
        drawKeyIcon("↵", in: leftKeyRect)
        drawKeyIcon("↑", in: rightKeyRect)
    }

    private func drawSectionTitle(_ title: String, count: String, y: CGFloat) {
        drawText(title, in: NSRect(x: 30, y: y, width: 250, height: 18), size: 11, weight: .bold, color: NSColor.white.withAlphaComponent(0.56), alignment: .left)
        drawText(count, in: NSRect(x: bounds.width - 92, y: y, width: 64, height: 18), size: 11, weight: .bold, color: NSColor.white.withAlphaComponent(0.64), alignment: .right)
    }

    private enum NotificationKind {
        case spark
        case chat
    }

    private func drawNotificationCard(y: CGFloat, kind: NotificationKind, title: String, subtitle: String, time: String, badge: String?) {
        let card = NSRect(x: horizontalPadding, y: y, width: bounds.width - horizontalPadding * 2, height: 52)
        fillRoundedCard(card, radius: 13, alpha: 0.38)

        let iconRect = NSRect(x: card.minX + 14, y: y + 10, width: 30, height: 30)
        drawAppIcon(kind, in: iconRect)

        let textX = iconRect.maxX + 12
        let rightMetaWidth: CGFloat = 58
        let textWidth = max(110, card.maxX - textX - rightMetaWidth - 12)
        drawText(title, in: NSRect(x: textX, y: y + 10, width: textWidth, height: 17), size: 13, weight: .semibold, color: .white, alignment: .left)
        drawText(subtitle, in: NSRect(x: textX, y: y + 27, width: textWidth, height: 16), size: 11, weight: .medium, color: NSColor.white.withAlphaComponent(0.62), alignment: .left)
        drawText(time, in: NSRect(x: card.maxX - rightMetaWidth, y: y + 12, width: rightMetaWidth - 8, height: 16), size: 11, weight: .semibold, color: NSColor.white.withAlphaComponent(0.55), alignment: .right)

        if let badge {
            let badgeRect = NSRect(x: card.maxX - 28, y: y + 27, width: 18, height: 18)
            let badgePath = NSBezierPath(ovalIn: badgeRect)
            NSColor.systemGreen.setFill()
            badgePath.fill()
            drawText(badge, in: badgeRect.offsetBy(dx: 0, dy: 2.8), size: 10, weight: .bold, color: .white, alignment: .center)
        }
    }

    private func drawQueueCard(y: CGFloat) {
        let card = NSRect(x: horizontalPadding, y: y, width: bounds.width - horizontalPadding * 2, height: 126)
        fillRoundedCard(card, radius: 14, alpha: 0.38)

        drawQueueLine(y: y + 17, title: "Indexar Notion", pct: "72%", eta: "1m", widthFactor: 0.72)
        drawThinDivider(y: y + 53)
        drawQueueLine(y: y + 59, title: "Transcrição · call.mp3", pct: "34%", eta: "4m", widthFactor: 0.34)
        drawThinDivider(y: y + 95)
        drawQueueLine(y: y + 101, title: "Gerar 6 thumbs", pct: "12%", eta: "7m", widthFactor: 0.12)
    }

    private func drawQueueLine(y: CGFloat, title: String, pct: String, eta: String, widthFactor: CGFloat) {
        let cardMinX = horizontalPadding
        let cardMaxX = bounds.width - horizontalPadding
        drawPurpleDot(center: NSPoint(x: cardMinX + 16, y: y + 6))
        let labelX = cardMinX + 32
        let metaWidth: CGFloat = 78
        let labelWidth = max(120, cardMaxX - labelX - metaWidth - 12)
        drawText(title, in: NSRect(x: labelX, y: y - 3, width: labelWidth, height: 19), size: 13, weight: .semibold, color: .white, alignment: .left)
        drawText("\(pct)  ·  \(eta)", in: NSRect(x: cardMaxX - metaWidth, y: y - 2, width: metaWidth - 4, height: 18), size: 11, weight: .semibold, color: NSColor.white.withAlphaComponent(0.58), alignment: .right)

        let track = NSRect(x: cardMinX + 18, y: y + 22, width: (cardMaxX - cardMinX) - 36, height: 3)
        let trackPath = NSBezierPath(roundedRect: track, xRadius: 2, yRadius: 2)
        NSColor.white.withAlphaComponent(0.08).setFill()
        trackPath.fill()

        let fill = NSRect(x: track.minX, y: y + 22, width: track.width * widthFactor, height: 3)
        let fillPath = NSBezierPath(roundedRect: fill, xRadius: 2, yRadius: 2)
        NSColor(calibratedRed: 0.36, green: 0.46, blue: 1.0, alpha: 1.0).setFill()
        fillPath.fill()
    }

    private func fillRoundedCard(_ rect: NSRect, radius: CGFloat, alpha: CGFloat) {
        let path = NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)
        NSColor(calibratedWhite: 0.055, alpha: alpha).setFill()
        path.fill()
        NSColor.white.withAlphaComponent(0.095).setStroke()
        path.lineWidth = 1
        path.stroke()
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

        let outer = NSBezierPath(roundedRect: rect, xRadius: 15, yRadius: 15)
        NSColor(calibratedWhite: 0.16, alpha: 1.0).setFill()
        outer.fill()
        NSColor.white.withAlphaComponent(0.12).setStroke()
        outer.lineWidth = 1
        outer.stroke()

        let face = NSBezierPath(roundedRect: rect.insetBy(dx: 9, dy: 14), xRadius: 6, yRadius: 6)
        NSColor(calibratedWhite: 0.08, alpha: 1.0).setFill()
        face.fill()
        NSColor.white.withAlphaComponent(0.08).setStroke()
        face.stroke()

        drawGlowDot(center: NSPoint(x: rect.midX - 7, y: rect.midY + 2), radius: 3)
        drawGlowDot(center: NSPoint(x: rect.midX + 7, y: rect.midY + 2), radius: 3)
    }

    private func drawAppIcon(_ kind: NotificationKind, in rect: NSRect) {
        let path = NSBezierPath(roundedRect: rect, xRadius: 9, yRadius: 9)
        switch kind {
        case .spark:
            NSColor(calibratedRed: 0.36, green: 0.28, blue: 0.94, alpha: 1.0).setFill()
        case .chat:
            NSColor(calibratedRed: 0.12, green: 0.78, blue: 0.34, alpha: 1.0).setFill()
        }
        path.fill()

        switch kind {
        case .spark:
            drawSparkGlyph(in: rect)
        case .chat:
            drawChatGlyph(in: rect)
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

        let tail = NSBezierPath()
        tail.move(to: NSPoint(x: rect.midX - 3, y: rect.maxY - 12))
        tail.line(to: NSPoint(x: rect.midX - 9, y: rect.maxY - 7))
        tail.line(to: NSPoint(x: rect.midX, y: rect.maxY - 9))
        tail.close()
        tail.fill()

        NSColor(calibratedRed: 0.12, green: 0.78, blue: 0.34, alpha: 1.0).setFill()
        NSBezierPath(ovalIn: NSRect(x: rect.midX - 6, y: rect.midY - 2, width: 2.5, height: 2.5)).fill()
        NSBezierPath(ovalIn: NSRect(x: rect.midX, y: rect.midY + 1, width: 2.5, height: 2.5)).fill()
    }

    private func drawMicGlyph(in rect: NSRect, scale: CGFloat = 1.0) {
        let scaledWidth = rect.width * scale
        let scaledHeight = rect.height * scale
        let r = NSRect(
            x: rect.midX - scaledWidth / 2,
            y: rect.midY - scaledHeight / 2,
            width: scaledWidth,
            height: scaledHeight
        )
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

    private func drawPurpleDot(center: NSPoint) {
        NSColor(calibratedRed: 0.59, green: 0.47, blue: 1.0, alpha: 0.32).setFill()
        NSBezierPath(ovalIn: NSRect(x: center.x - 6, y: center.y - 6, width: 12, height: 12)).fill()
        NSColor(calibratedRed: 0.61, green: 0.50, blue: 1.0, alpha: 1.0).setFill()
        NSBezierPath(ovalIn: NSRect(x: center.x - 3, y: center.y - 3, width: 6, height: 6)).fill()
    }

    private func drawGlowDot(center: NSPoint, radius: CGFloat) {
        NSColor(calibratedRed: 0.61, green: 0.50, blue: 1.0, alpha: 0.34).setFill()
        NSBezierPath(ovalIn: NSRect(x: center.x - radius - 5, y: center.y - radius - 5, width: radius * 2 + 10, height: radius * 2 + 10)).fill()
        NSColor(calibratedRed: 0.72, green: 0.62, blue: 1.0, alpha: 1.0).setFill()
        NSBezierPath(ovalIn: NSRect(x: center.x - radius, y: center.y - radius, width: radius * 2, height: radius * 2)).fill()
    }

    private func drawThinDivider(y: CGFloat) {
        NSColor.white.withAlphaComponent(0.055).setStroke()
        let path = NSBezierPath()
        path.move(to: NSPoint(x: 28, y: y))
        path.line(to: NSPoint(x: bounds.width - 28, y: y))
        path.lineWidth = 1
        path.stroke()
    }

    private func drawAvatar(at rect: NSRect) {
        let path = NSBezierPath(roundedRect: rect, xRadius: 16, yRadius: 16)
        NSColor.systemIndigo.setFill()
        path.fill()
        drawText("✦", in: rect, size: 24, weight: .bold, color: .white, alignment: .center)
    }

    private func drawRobotIcon(at rect: NSRect) {
        let body = NSBezierPath(roundedRect: rect, xRadius: 10, yRadius: 10)
        NSColor.systemIndigo.setFill()
        body.fill()
        drawText("☻", in: rect.offsetBy(dx: 0, dy: 2), size: 18, weight: .bold, color: .white, alignment: .center)
    }

    private func drawStatusDot(center: NSPoint) {
        let radius: CGFloat = isThinking && pulsePhase ? 6 : 4
        let rect = NSRect(x: center.x - radius, y: center.y - radius, width: radius * 2, height: radius * 2)
        NSColor.systemGreen.withAlphaComponent(isThinking && pulsePhase ? 0.42 : 1).setFill()
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

    func controlTextDidEndEditing(_ obj: Notification) {
        let movement = (obj.userInfo?["NSTextMovement"] as? Int) ?? 0
        guard movement == NSReturnTextMovement else { return }
        startThinkingPulse()
    }

    private func startThinkingPulse() {
        isThinking = true
        pulseTimer?.invalidate()
        pulseTimer = Timer.scheduledTimer(withTimeInterval: 0.36, repeats: true) { [weak self] _ in
            self?.pulsePhase.toggle()
            self?.needsDisplay = true
        }
        RunLoop.main.add(pulseTimer!, forMode: .common)

        DispatchQueue.main.asyncAfter(deadline: .now() + 1.6) { [weak self] in
            self?.isThinking = false
            self?.pulsePhase = false
            self?.pulseTimer?.invalidate()
            self?.pulseTimer = nil
            self?.needsDisplay = true
        }
    }
}

final class NotchController {
    private let panel: NSPanel
    private let islandView: NeoIslandView

    private var state: IslandState = .idle
    private var hoverTimer: Timer?
    private var hoverBeganAt: TimeInterval?
    private var stableBeganAt: TimeInterval?
    private var lastPointer: NSPoint?

    private let idleSize = NSSize(width: 136, height: 30)
    private let peekSize = NSSize(width: 460, height: 46)
    private let expandedSize = NSSize(width: 560, height: 570)
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

        islandView = NeoIslandView(frame: NSRect(origin: .zero, size: idleSize))
        panel.contentView = islandView
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
    }

    func start() {
        apply(.idle, on: activeScreen() ?? NSScreen.main, animated: false)
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

        if isOverIsland || isOverNotch {
            updateIntent(pointer: pointer, now: now, on: screen)
        } else if state != .idle {
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
        if moved > movementTolerance {
            stableBeganAt = now
        }
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

        let size: NSSize
        switch next {
        case .idle:
            size = idleSize
        case .peek:
            size = peekSize
        case .expanded:
            size = expandedSize
        }

        let nextFrame = Self.frame(for: size, on: screen)
        islandView.frame = NSRect(origin: .zero, size: size)
        islandView.needsDisplay = true

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

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var controller: NotchController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        guard let screen = NSScreen.main else { return }
        let controller = NotchController(screen: screen)
        self.controller = controller
        controller.start()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()

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

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.masksToBounds = true

        promptField.placeholderString = "Pergunte ou peça algo ao Neo..."
        promptField.font = NSFont.systemFont(ofSize: 16, weight: .regular)
        promptField.focusRingType = .none
        promptField.isBordered = false
        promptField.drawsBackground = true
        promptField.backgroundColor = NSColor.white.withAlphaComponent(0.09)
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

    override func layout() {
        super.layout()
        promptField.frame = NSRect(x: 24, y: 104, width: bounds.width - 48, height: 42)
    }

    private func applyState() {
        promptField.isHidden = islandState != .expanded
        layer?.cornerRadius = islandState == .idle ? 19 : 30
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
        let radius = islandState == .idle ? bounds.height / 2 : 30
        let path = NSBezierPath(roundedRect: bounds.insetBy(dx: 1, dy: 1), xRadius: radius, yRadius: radius)
        NSColor.black.withAlphaComponent(0.91).setFill()
        path.fill()

        NSColor.white.withAlphaComponent(0.14).setStroke()
        path.lineWidth = 1
        path.stroke()
    }

    private func drawIdle() {
        let glint = NSBezierPath(roundedRect: bounds.insetBy(dx: 12, dy: 9), xRadius: 6, yRadius: 6)
        NSColor.white.withAlphaComponent(0.12).setFill()
        glint.fill()
    }

    private func drawPeek() {
        drawRobotIcon(at: NSRect(x: 24, y: 8, width: 30, height: 30))
        drawText("Neo", in: NSRect(x: 62, y: 10, width: 88, height: 22), size: 16, weight: .semibold, color: .white, alignment: .left)
        drawStatusDot(center: NSPoint(x: bounds.width - 88, y: 23))
        drawText("online", in: NSRect(x: bounds.width - 78, y: 10, width: 54, height: 22), size: 14, weight: .medium, color: NSColor.white.withAlphaComponent(0.72), alignment: .left)
    }

    private func drawExpanded() {
        drawAvatar(at: NSRect(x: 24, y: 44, width: 44, height: 44))
        drawText("Neo Assistant", in: NSRect(x: 82, y: 43, width: 240, height: 24), size: 19, weight: .semibold, color: .white, alignment: .left)
        drawText(isThinking ? "pensando..." : "pronto para ajudar", in: NSRect(x: 82, y: 67, width: 240, height: 20), size: 14, weight: .regular, color: NSColor.white.withAlphaComponent(0.7), alignment: .left)
        drawStatusDot(center: NSPoint(x: bounds.width - 32, y: 66))

        drawSegmentedControls()
        drawDivider(y: 206)
        drawText("RECENTES", in: NSRect(x: 24, y: 226, width: 120, height: 18), size: 12, weight: .bold, color: NSColor.white.withAlphaComponent(0.48), alignment: .left)
        drawText("Resumo do call de produto", in: NSRect(x: 32, y: 256, width: bounds.width - 120, height: 22), size: 16, weight: .medium, color: .white, alignment: .left)
        drawText("agora", in: NSRect(x: bounds.width - 86, y: 256, width: 54, height: 22), size: 14, weight: .regular, color: NSColor.white.withAlphaComponent(0.44), alignment: .right)
        drawText("Reescrever onboarding email", in: NSRect(x: 32, y: 292, width: bounds.width - 120, height: 22), size: 16, weight: .medium, color: .white, alignment: .left)
        drawText("1h", in: NSRect(x: bounds.width - 86, y: 292, width: 54, height: 22), size: 14, weight: .regular, color: NSColor.white.withAlphaComponent(0.44), alignment: .right)
        drawText("Gerar variantes de logo", in: NSRect(x: 32, y: 328, width: bounds.width - 120, height: 22), size: 16, weight: .medium, color: .white, alignment: .left)
        drawText("ontem", in: NSRect(x: bounds.width - 86, y: 328, width: 54, height: 22), size: 14, weight: .regular, color: NSColor.white.withAlphaComponent(0.44), alignment: .right)
        drawDivider(y: 368)
        drawText("⌘↵ enviar", in: NSRect(x: bounds.width - 116, y: bounds.height - 36, width: 88, height: 20), size: 13, weight: .medium, color: NSColor.white.withAlphaComponent(0.58), alignment: .right)
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

    private func drawSegmentedControls() {
        let labels = ["Llama 70B", "Haiku 4.5", "GPT-4o"]
        let gap: CGFloat = 10
        let width = (bounds.width - 48 - gap * 2) / 3
        for (index, label) in labels.enumerated() {
            let x = 24 + CGFloat(index) * (width + gap)
            let rect = NSRect(x: x, y: 162, width: width, height: 36)
            let path = NSBezierPath(roundedRect: rect, xRadius: 12, yRadius: 12)
            (index == 0 ? NSColor.systemIndigo.withAlphaComponent(0.48) : NSColor.white.withAlphaComponent(0.06)).setFill()
            path.fill()
            NSColor.white.withAlphaComponent(index == 0 ? 0.2 : 0.1).setStroke()
            path.lineWidth = 1
            path.stroke()
            drawText(label, in: rect, size: 14, weight: .semibold, color: index == 0 ? .white : NSColor.white.withAlphaComponent(0.66), alignment: .center)
        }
    }

    private func drawDivider(y: CGFloat) {
        NSColor.white.withAlphaComponent(0.08).setStroke()
        let path = NSBezierPath()
        path.move(to: NSPoint(x: 24, y: y))
        path.line(to: NSPoint(x: bounds.width - 24, y: y))
        path.lineWidth = 1
        path.stroke()
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
    private let expandedSize = NSSize(width: 640, height: 430)
    private let movementTolerance: CGFloat = 8
    private let peekIntentDelay: TimeInterval = 0.14
    private let expandIntentDelay: TimeInterval = 0.72

    init(screen: NSScreen) {
        let startFrame = Self.frame(for: idleSize, on: screen)
        panel = NSPanel(
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

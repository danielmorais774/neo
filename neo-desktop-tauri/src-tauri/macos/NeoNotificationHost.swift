import AppKit

final class NotificationContentView: NSView {
    var onHoverChanged: ((Bool) -> Void)?

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        addTrackingArea(NSTrackingArea(
            rect: bounds,
            options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
            owner: self,
            userInfo: nil
        ))
    }

    override func mouseEntered(with event: NSEvent) {
        onHoverChanged?(true)
    }

    override func mouseExited(with event: NSEvent) {
        onHoverChanged?(false)
    }
}

final class AppIconView: NSView {
    override func draw(_ dirtyRect: NSRect) {
        let path = NSBezierPath(roundedRect: bounds, xRadius: 12, yRadius: 12)
        NSColor(calibratedRed: 0.33, green: 0.27, blue: 0.96, alpha: 1).setFill()
        path.fill()

        let center = NSPoint(x: bounds.midX, y: bounds.midY)
        let star = NSBezierPath()
        star.move(to: NSPoint(x: center.x, y: center.y + 13))
        star.curve(to: NSPoint(x: center.x + 13, y: center.y), controlPoint1: NSPoint(x: center.x + 2, y: center.y + 4), controlPoint2: NSPoint(x: center.x + 9, y: center.y + 2))
        star.curve(to: NSPoint(x: center.x, y: center.y - 13), controlPoint1: NSPoint(x: center.x + 9, y: center.y - 2), controlPoint2: NSPoint(x: center.x + 2, y: center.y - 4))
        star.curve(to: NSPoint(x: center.x - 13, y: center.y), controlPoint1: NSPoint(x: center.x - 2, y: center.y - 4), controlPoint2: NSPoint(x: center.x - 9, y: center.y - 2))
        star.curve(to: NSPoint(x: center.x, y: center.y + 13), controlPoint1: NSPoint(x: center.x - 9, y: center.y + 2), controlPoint2: NSPoint(x: center.x - 2, y: center.y + 4))
        star.close()
        NSColor.white.setFill()
        star.fill()
    }
}

final class CloseButton: NSButton {
    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        title = ""
        isBordered = false
        bezelStyle = .regularSquare
        wantsLayer = true
        layer?.cornerRadius = frameRect.height / 2
        layer?.backgroundColor = NSColor.black.withAlphaComponent(0.48).cgColor
        layer?.borderColor = NSColor.white.withAlphaComponent(0.24).cgColor
        layer?.borderWidth = 1
        alphaValue = 0
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 13, weight: .medium),
            .foregroundColor: NSColor.white,
        ]
        let glyph = "×" as NSString
        let size = glyph.size(withAttributes: attrs)
        glyph.draw(
            at: NSPoint(x: (bounds.width - size.width) / 2, y: (bounds.height - size.height) / 2),
            withAttributes: attrs
        )
    }
}

final class GlassTextInput: NSView {
    let textField = NSTextField(frame: .zero)

    init(frame frameRect: NSRect, value: String, placeholder: String) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.cornerRadius = 10
        layer?.masksToBounds = true
        layer?.backgroundColor = NSColor.white.withAlphaComponent(0.09).cgColor
        layer?.borderColor = NSColor.white.withAlphaComponent(0.14).cgColor
        layer?.borderWidth = 1

        textField.stringValue = value
        textField.placeholderString = placeholder
        textField.font = .systemFont(ofSize: 13, weight: .regular)
        textField.textColor = .labelColor
        textField.focusRingType = .none
        textField.isBordered = false
        textField.drawsBackground = false
        textField.backgroundColor = .clear
        textField.frame = textRect(for: bounds)
        textField.autoresizingMask = [.width, .height]
        addSubview(textField)

        let sendButton = NSButton(title: "↑", target: nil, action: nil)
        sendButton.frame = NSRect(x: frameRect.width - 38, y: 5, width: 30, height: 30)
        sendButton.autoresizingMask = [.minXMargin]
        sendButton.bezelStyle = .regularSquare
        sendButton.isBordered = false
        sendButton.font = .systemFont(ofSize: 16, weight: .semibold)
        sendButton.contentTintColor = .white
        sendButton.wantsLayer = true
        sendButton.layer?.cornerRadius = 15
        sendButton.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.20).cgColor
        addSubview(sendButton)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    var stringValue: String {
        textField.stringValue
    }

    override func layout() {
        super.layout()
        textField.frame = textRect(for: bounds)
    }

    override func mouseDown(with event: NSEvent) {
        window?.makeFirstResponder(textField)
    }

    private func textRect(for rect: NSRect) -> NSRect {
        let font = textField.font ?? .systemFont(ofSize: 13, weight: .regular)
        let lineHeight = ceil(font.ascender - font.descender + font.leading)
        let y = floor((rect.height - lineHeight) / 2)
        return NSRect(x: 14, y: y, width: max(0, rect.width - 62), height: lineHeight)
    }
}

final class NotificationPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

final class NotificationHostApp: NSObject, NSApplicationDelegate {
    private var windows: [String: NSWindow] = [:]
    private var buttonActionData: [String: [String: Any]] = [:]
    private var fieldControls: [String: [[String: Any]]] = [:]
    private var dismissTimers: [String: DispatchWorkItem] = [:]

    func applicationDidFinishLaunching(_ notification: Notification) {
        sendEvent(["type": "ready"])
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.readLoop()
        }
    }

    private func readLoop() {
        while let line = readLine() {
            guard let data = line.data(using: .utf8) else { continue }
            guard
                let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                let type = root["type"] as? String
            else { continue }

            if type == "show", let notification = root["notification"] as? [String: Any] {
                DispatchQueue.main.async { [weak self] in
                    self?.show(notification: notification)
                }
            }
        }
    }

    private func show(notification: [String: Any]) {
        let notificationId = (notification["id"] as? String) ?? UUID().uuidString
        let title = (notification["title"] as? String) ?? "Nova notificação"
        let description = (notification["description"] as? String) ?? ""
        let actions = (notification["actions"] as? [[String: Any]]) ?? []
        let payload = (notification["payload"] as? [String: Any]) ?? [:]
        let fields = (notification["fields"] as? [[String: Any]]) ?? []
        let config = (notification["config"] as? [String: Any]) ?? [:]
        let durationMs = (config["duration_ms"] as? Int) ?? 0
        let persistent = (config["persistent"] as? Bool) ?? (durationMs == 0)
        let position = (config["position"] as? String) ?? "top_right"

        let width: CGFloat = 360
        let fieldRowHeight: CGFloat = 54
        let fieldsHeight: CGFloat = CGFloat(min(fields.count, 3)) * fieldRowHeight
        let height: CGFloat = actions.isEmpty ? (158 + fieldsHeight) : (170 + fieldsHeight)

        // buttonR: close button radius. The outer container is larger by buttonR on
        // left and top so the button can sit centered on the panel's top-left corner.
        let buttonR: CGFloat = 13
        let frame = windowFrame(width: width, height: height, buttonR: buttonR, position: position)

        let window = NotificationPanel(
            contentRect: frame,
            styleMask: [.borderless],
            backing: .buffered,
            defer: false
        )
        window.level = .floating
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = true
        window.hidesOnDeactivate = false
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]

        // outer: transparent tracking container — larger than panel to host the floating close button
        let outer = NotificationContentView(frame: NSRect(x: 0, y: 0, width: width + buttonR, height: height + buttonR))
        window.contentView = outer

        // content: the visible notification panel, offset right/down so the button can overhang
        let content = NSView(frame: NSRect(x: buttonR, y: 0, width: width, height: height))
        content.wantsLayer = true
        content.layer?.cornerRadius = 14
        content.layer?.masksToBounds = true
        outer.addSubview(content)

        let blur = NSVisualEffectView(frame: content.bounds)
        blur.autoresizingMask = [.width, .height]
        blur.material = .hudWindow
        blur.blendingMode = .behindWindow
        blur.state = .active
        blur.wantsLayer = true
        blur.layer?.cornerRadius = 14
        blur.layer?.masksToBounds = true
        content.addSubview(blur)

        let overlay = NSView(frame: content.bounds)
        overlay.autoresizingMask = [.width, .height]
        overlay.wantsLayer = true
        overlay.layer?.cornerRadius = 14
        overlay.layer?.masksToBounds = true
        overlay.layer?.backgroundColor = NSColor.black.withAlphaComponent(0.07).cgColor
        overlay.layer?.borderColor = NSColor.white.withAlphaComponent(0.18).cgColor
        overlay.layer?.borderWidth = 1
        content.addSubview(overlay)

        // Close button centered on the top-left corner of the panel (half outside)
        let closeButton = CloseButton(frame: NSRect(x: 0, y: height - buttonR, width: buttonR * 2, height: buttonR * 2))
        closeButton.target = self
        closeButton.action = #selector(handleClose(_:))
        closeButton.identifier = NSUserInterfaceItemIdentifier(rawValue: notificationId)
        outer.addSubview(closeButton)
        outer.onHoverChanged = { hovering in
            NSAnimationContext.runAnimationGroup { context in
                context.duration = 0.12
                closeButton.animator().alphaValue = hovering ? 1 : 0
            }
        }

        let icon = AppIconView(frame: NSRect(x: 16, y: height - 58, width: 38, height: 38))
        overlay.addSubview(icon)

        let appLabel = NSTextField(labelWithString: "Neo")
        appLabel.font = .systemFont(ofSize: 12, weight: .semibold)
        appLabel.textColor = .secondaryLabelColor
        appLabel.frame = NSRect(x: 66, y: height - 34, width: 140, height: 16)
        overlay.addSubview(appLabel)

        let nowLabel = NSTextField(labelWithString: "agora")
        nowLabel.font = .systemFont(ofSize: 12, weight: .semibold)
        nowLabel.textColor = .secondaryLabelColor
        nowLabel.alignment = .right
        nowLabel.frame = NSRect(x: width - 76, y: height - 34, width: 60, height: 16)
        overlay.addSubview(nowLabel)

        let titleLabel = NSTextField(labelWithString: title)
        titleLabel.font = .systemFont(ofSize: 14, weight: .bold)
        titleLabel.textColor = .labelColor
        titleLabel.frame = NSRect(x: 66, y: height - 61, width: width - 92, height: 20)
        overlay.addSubview(titleLabel)

        let bodyY = actions.isEmpty ? (58 + fieldsHeight) : (60 + fieldsHeight)
        let bodyLabel = NSTextField(frame: NSRect(x: 66, y: bodyY, width: width - 92, height: 34))
        bodyLabel.attributedStringValue = parseBodyText(description)
        bodyLabel.isEditable = false
        bodyLabel.isBordered = false
        bodyLabel.drawsBackground = false
        bodyLabel.backgroundColor = .clear
        bodyLabel.maximumNumberOfLines = 2
        overlay.addSubview(bodyLabel)

        var actionData: [[String: Any]] = []
        var currentFieldY = bodyY
        for field in fields.prefix(3) {
            currentFieldY -= fieldRowHeight
            let label = ((field["label"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let source = (field["source"] as? String) ?? label
            let value = (field["value"] as? String) ?? ""
            let fieldType = (field["field_type"] as? String) ?? "text"
            let optionsCsv = (field["options"] as? String) ?? ""
            let placeholder = (field["placeholder"] as? String) ?? ""
            let hasLabel = !label.isEmpty

            var inputX: CGFloat = 14
            var inputWidth: CGFloat = width - 28
            if hasLabel {
                let fieldLabel = NSTextField(labelWithString: label)
                fieldLabel.font = .systemFont(ofSize: 11, weight: .medium)
                fieldLabel.textColor = .secondaryLabelColor
                fieldLabel.frame = NSRect(x: 14, y: currentFieldY + 6, width: 95, height: 20)
                overlay.addSubview(fieldLabel)
                inputX = 112
                inputWidth = width - 126
            }

            if fieldType == "select" {
                let combo = NSComboBox(frame: NSRect(x: inputX, y: currentFieldY + 2, width: inputWidth, height: 40))
                let options = optionsCsv.split(separator: ",").map { String($0).trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
                combo.addItems(withObjectValues: options)
                combo.stringValue = value
                if !placeholder.isEmpty {
                    combo.placeholderString = placeholder
                }
                combo.font = .systemFont(ofSize: 13, weight: .regular)
                combo.wantsLayer = true
                combo.layer?.cornerRadius = 10
                combo.layer?.masksToBounds = true
                combo.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.12).cgColor
                combo.layer?.borderColor = NSColor.white.withAlphaComponent(0.16).cgColor
                combo.layer?.borderWidth = 1
                overlay.addSubview(combo)
                actionData.append([
                    "source": source,
                    "label": label,
                    "field_type": fieldType,
                    "value": value,
                    "control": combo
                ])
            } else {
                let input = GlassTextInput(
                    frame: NSRect(x: inputX, y: currentFieldY + 2, width: inputWidth, height: 40),
                    value: value,
                    placeholder: placeholder
                )
                overlay.addSubview(input)
                actionData.append([
                    "source": source,
                    "label": label,
                    "field_type": "text",
                    "value": value,
                    "control": input
                ])
            }
        }
        fieldControls[notificationId] = actionData

        if !actions.isEmpty {
            var x: CGFloat = 14
            let buttonY: CGFloat = 14
            let visibleActions = Array(actions.prefix(2))
            let buttonWidth = visibleActions.count == 1 ? (width - 28) : ((width - 42) / 2)
            for action in visibleActions {
                let label = (action["label"] as? String) ?? "Ação"
                let actionType = (action["action_type"] as? String) ?? "open"
                let actionPayload = (action["payload"] as? [String: Any]) ?? [:]
                let button = NSButton(title: label, target: self, action: #selector(handleAction(_:)))
                button.bezelStyle = .regularSquare
                button.isBordered = false
                button.font = .systemFont(ofSize: 14, weight: .semibold)
                button.contentTintColor = .white
                button.wantsLayer = true
                button.layer?.cornerRadius = 10
                button.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.12).cgColor
                button.layer?.borderColor = NSColor.white.withAlphaComponent(0.16).cgColor
                button.layer?.borderWidth = 1
                button.frame = NSRect(x: x, y: buttonY, width: buttonWidth, height: 36)
                let buttonId = UUID().uuidString
                button.identifier = NSUserInterfaceItemIdentifier(rawValue: buttonId)
                button.toolTip = actionType
                buttonActionData[buttonId] = [
                    "notification_id": notificationId,
                    "action_type": actionType,
                    "payload_base": merge(left: payload, right: actionPayload),
                ]
                overlay.addSubview(button)
                if visibleActions.count > 1 {
                    x += buttonWidth + 14
                }
            }
        }

        windows[notificationId]?.close()
        windows[notificationId] = window
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)

        if !persistent && durationMs > 0 {
            let item = DispatchWorkItem { [weak self] in
                DispatchQueue.main.async { self?.dismiss(notificationId: notificationId) }
            }
            dismissTimers[notificationId] = item
            DispatchQueue.global().asyncAfter(deadline: .now() + .milliseconds(durationMs), execute: item)
        }
    }

    @objc private func handleAction(_ sender: NSButton) {
        guard let buttonId = sender.identifier?.rawValue else { return }
        guard let data = buttonActionData.removeValue(forKey: buttonId) else { return }
        let notificationId = data["notification_id"] as? String ?? ""
        let payloadBase = data["payload_base"] as? [String: Any] ?? [:]
        let payload = merge(left: payloadBase, right: collectFieldPayload(notificationId: notificationId))
        sendEvent([
            "type": "action",
            "notification_id": notificationId,
            "action_type": data["action_type"] as? String ?? "",
            "payload": payload,
        ])
        dismiss(notificationId: notificationId)
    }

    @objc private func handleClose(_ sender: NSButton) {
        let notificationId = sender.identifier?.rawValue ?? ""
        if !notificationId.isEmpty {
            sendEvent(["type": "dismissed", "notification_id": notificationId])
            dismiss(notificationId: notificationId)
        }
    }

    private func dismiss(notificationId: String) {
        guard let window = windows.removeValue(forKey: notificationId) else { return }
        fieldControls.removeValue(forKey: notificationId)
        dismissTimers.removeValue(forKey: notificationId)?.cancel()

        let origin = window.frame.origin
        let slideDistance: CGFloat = window.frame.width + 32
        let screenMidX = NSScreen.main?.frame.midX ?? 0
        let dx = window.frame.midX >= screenMidX ? slideDistance : -slideDistance

        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.28
            ctx.timingFunction = CAMediaTimingFunction(name: .easeIn)
            window.animator().setFrame(
                NSRect(x: origin.x + dx, y: origin.y, width: window.frame.width, height: window.frame.height),
                display: true
            )
            window.animator().alphaValue = 0
        }, completionHandler: {
            window.close()
        })
    }

    private func collectFieldPayload(notificationId: String) -> [String: Any] {
        guard let fields = fieldControls[notificationId] else { return [:] }
        var fieldValues: [String: Any] = [:]
        var outFields: [[String: Any]] = []

        for field in fields {
            let source = (field["source"] as? String) ?? ""
            let label = (field["label"] as? String) ?? ""
            let fieldType = (field["field_type"] as? String) ?? "text"
            let initialValue = (field["value"] as? String) ?? ""
            var inputValue = initialValue

            if let input = field["control"] as? GlassTextInput {
                inputValue = input.stringValue
            } else if let input = field["control"] as? NSTextField {
                inputValue = input.stringValue
            } else if let combo = field["control"] as? NSComboBox {
                inputValue = combo.stringValue
            }

            if !source.isEmpty {
                fieldValues[source] = inputValue
            }

            outFields.append([
                "source": source,
                "label": label,
                "field_type": fieldType,
                "value": initialValue,
                "input_value": inputValue,
            ])
        }

        return [
            "fields": outFields,
            "field_values": fieldValues,
            "inputs": fieldValues,
        ]
    }

    private func windowFrame(width panelW: CGFloat, height panelH: CGFloat, buttonR: CGFloat, position: String) -> NSRect {
        let visible = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1200, height: 800)
        let margin: CGFloat = 16
        let outerW = panelW + buttonR
        let outerH = panelH + buttonR

        let x: CGFloat
        let y: CGFloat
        switch position {
        case "bottom_right":
            x = visible.maxX - panelW - margin - buttonR
            y = visible.minY + margin
        case "bottom_left":
            x = visible.minX + margin
            y = visible.minY + margin
        case "top_left":
            x = visible.minX + margin
            y = visible.maxY - panelH - margin
        case "center":
            x = visible.midX - outerW / 2
            y = visible.midY - outerH / 2
        default: // top_right
            x = visible.maxX - panelW - margin - buttonR
            y = visible.maxY - panelH - margin
        }
        return NSRect(x: x, y: y, width: outerW, height: outerH)
    }

    private func parseBodyText(_ text: String) -> NSAttributedString {
        let baseFont = NSFont.systemFont(ofSize: 12, weight: .regular)
        let monoFont = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        let result = NSMutableAttributedString()
        var remaining = text
        while !remaining.isEmpty {
            if let openRange = remaining.range(of: "`") {
                let before = String(remaining[..<openRange.lowerBound])
                if !before.isEmpty {
                    result.append(NSAttributedString(string: before, attributes: [
                        .foregroundColor: NSColor.secondaryLabelColor,
                        .font: baseFont,
                    ]))
                }
                let afterOpen = String(remaining[openRange.upperBound...])
                if let closeRange = afterOpen.range(of: "`") {
                    let code = String(afterOpen[..<closeRange.lowerBound])
                    result.append(NSAttributedString(string: code, attributes: [
                        .foregroundColor: NSColor.systemBlue,
                        .font: monoFont,
                    ]))
                    remaining = String(afterOpen[closeRange.upperBound...])
                } else {
                    result.append(NSAttributedString(string: "`" + afterOpen, attributes: [
                        .foregroundColor: NSColor.secondaryLabelColor,
                        .font: baseFont,
                    ]))
                    remaining = ""
                }
            } else {
                result.append(NSAttributedString(string: remaining, attributes: [
                    .foregroundColor: NSColor.secondaryLabelColor,
                    .font: baseFont,
                ]))
                remaining = ""
            }
        }
        return result
    }
}

private func merge(left: [String: Any], right: [String: Any]) -> [String: Any] {
    var out = left
    for (k, v) in right { out[k] = v }
    return out
}

private func sendEvent(_ event: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: event),
          let line = String(data: data, encoding: .utf8)
    else { return }
    fputs(line + "\n", stdout)
    fflush(stdout)
}

let app = NSApplication.shared
let delegate = NotificationHostApp()
app.setActivationPolicy(.accessory)
app.delegate = delegate
app.run()

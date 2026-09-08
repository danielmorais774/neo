import AppKit
import AVFoundation
import CoreGraphics
import CoreAudio
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
    // Fallback ate o Rust mandar o config por IPC (get_voice_config). neo-voice
    // roda no neo-host (LXC) da LAN; o Rust sobrescreve com NEO_VOICE_WS_URL.
    var wsBaseUrl: String = "ws://192.168.4.210:5002"
    var apiKey: String?
    var voice: String?
    var backend: String = "coqui"
    var language: String?
    /// Segundos de ociosidade ate o chip do Modo Live se fechar sozinho. Vem do
    /// Rust (NEO_NOTCH_IDLE_TIMEOUT_S); 0 = nunca fecha por inatividade.
    var idleTimeoutS: Double = 15
    /// Microfone escolhido pelo usuario no painel, por NOME (o mesmo string no
    /// CoreAudio, no AVCaptureDevice e no enumerateDevices do webview). nil/vazio
    /// = padrao do sistema. Toda captura TENTA este device e cai no padrao se ele
    /// sumiu ou nao entregar audio.
    var inputDeviceName: String?
}

// MARK: - Audio route

/// Por onde o som esta SAINDO agora, e o que isso implica para a captura.
///
/// Duas coisas que o macOS faz nas nossas costas e que degradavam o audio do
/// sistema inteiro enquanto o Neo escutava:
///
///  1. O Voice Processing I/O (o AEC da Apple, `setVoiceProcessingEnabled`) e
///     DUPLEX: para cancelar o eco ele abre tambem o dispositivo de SAIDA padrao
///     e o poe em modo "voice chat" -- taxa forcada e ducking do audio dos outros
///     apps. So vale a pena quando a saida e um ALTO-FALANTE, porque ai a musica
///     volta pelo ar e vaza no mic. Em FONE (Bluetooth ou P2) nao ha vazamento --
///     o som toca no seu ouvido -- e ligar o VPIO e so prejuizo.
///
///  2. Abrir o microfone de um fone Bluetooth derruba o dispositivo de A2DP
///     (estereo, alta fidelidade) para HFP/SCO (mono, ~16 kHz, som de telefone),
///     nos dois sentidos. Entao, quando o mic padrao e Bluetooth, capturamos pelo
///     mic INTERNO: o fone continua em A2DP e a musica nao vaza.
///
/// `NEO_AUDIO_AEC` = `auto` (default) | `on` | `off` forca a decisao quando o
/// transporte engana (ex.: caixa de som Bluetooth, onde a musica VAZA mas o
/// transporte parece fone).
///
/// O Rust tem a mesma leitura em `services/audio_route.rs` (para o sidecar da
/// wake word e para o webview). Sao processos diferentes: cada um resolve a rota
/// no instante em que abre o mic, que e a unica forma de nunca usar uma leitura
/// velha -- fone entra e sai a qualquer momento.
struct AudioRoute {
    /// Ligar o cancelamento de eco da Apple? Hoje so informativo no notch: a
    /// captura migrou p/ AVCaptureSession, que nao tem VPIO. Ver beginCapture.
    let aec: Bool
    /// `true` se o mic padrao do sistema e um fone Bluetooth -- nesse caso
    /// preferimos o mic INTERNO (abrir o mic do fone o derruba p/ HFP).
    let defaultInputIsBluetooth: Bool
    /// Nome do mic embutido, para casar com o `localizedName` do AVCaptureDevice.
    let builtinInputName: String
    /// Para o log: "builtin-speaker", "headphones", "bluetooth", "usb", ...
    let outputTransport: String

    static func resolve() -> AudioRoute {
        // AEC (VPIO) DESLIGADO por padrao. Motivo empirico (diag 2026-07-19): com
        // o sidecar da wake word capturando o mic o tempo todo, o VPIO ou entrega
        // 0 frames (perde a disputa) ou, quando entrega, manda audio MUDO (o AEC
        // cancela a propria voz do usuario) -> o VAD do neo-voice nunca ve fala.
        var aec = false
        switch ProcessInfo.processInfo.environment["NEO_AUDIO_AEC"]?.lowercased() {
        case "on", "1", "true": aec = true
        case "off", "0", "false": aec = false
        default: break
        }
        let builtin = Self.builtinInputID()
        return AudioRoute(
            aec: aec,
            defaultInputIsBluetooth: Self.defaultInputIsBluetooth(),
            builtinInputName: builtin != 0 ? (Self.deviceName(builtin) ?? "") : "",
            outputTransport: Self.outputTransport()
        )
    }

    /// Transporte da saida padrao agora (para diagnostico do playback do TTS).
    static func currentOutputTransport() -> String { Self.outputTransport() }

    // ---------------------------------------------------------------- CoreAudio

    /// `kAudioObjectSystemObject` chega como Int32 no Swift; a API quer AudioObjectID.
    private static let systemObject = AudioObjectID(kAudioObjectSystemObject)

    private static func address(
        _ selector: AudioObjectPropertySelector,
        _ scope: AudioObjectPropertyScope
    ) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(
            mSelector: selector,
            mScope: scope,
            mElement: kAudioObjectPropertyElementMain
        )
    }

    private static func u32(
        _ object: AudioObjectID,
        _ selector: AudioObjectPropertySelector,
        _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal
    ) -> UInt32? {
        var addr = address(selector, scope)
        var value: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        let status = AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &value)
        return status == noErr ? value : nil
    }

    private static func isBluetooth(_ device: AudioDeviceID) -> Bool {
        guard let transport = u32(device, kAudioDevicePropertyTransportType) else { return false }
        return transport == kAudioDeviceTransportTypeBluetooth
            || transport == kAudioDeviceTransportTypeBluetoothLE
    }

    private static func outputTransport() -> String {
        guard let output = u32(systemObject, kAudioHardwarePropertyDefaultOutputDevice),
              let transport = u32(output, kAudioDevicePropertyTransportType)
        else { return "unknown" }

        switch transport {
        case kAudioDeviceTransportTypeBuiltIn:
            // A saida embutida e duas coisas: o alto-falante ou o fone no P2. O
            // data source diferencia ('hdpn' = headphones).
            let source = u32(output, kAudioDevicePropertyDataSource, kAudioObjectPropertyScopeOutput)
            let headphones: UInt32 = 0x6864_706E // 'hdpn'
            return source == headphones ? "headphones" : "builtin-speaker"
        case kAudioDeviceTransportTypeBluetooth, kAudioDeviceTransportTypeBluetoothLE:
            return "bluetooth"
        case kAudioDeviceTransportTypeUSB:
            return "usb"
        case kAudioDeviceTransportTypeHDMI, kAudioDeviceTransportTypeDisplayPort:
            return "hdmi"
        case kAudioDeviceTransportTypeAirPlay:
            return "airplay"
        default:
            return "other"
        }
    }

    private static func defaultInputIsBluetooth() -> Bool {
        guard let input = u32(systemObject, kAudioHardwarePropertyDefaultInputDevice)
        else { return false }
        return isBluetooth(input)
    }

    /// Nome CoreAudio do device -- casa com o `localizedName` do AVCaptureDevice,
    /// que e como escolhemos o microfone (ver VoiceController.pickCaptureDevice).
    private static func deviceName(_ device: AudioDeviceID) -> String? {
        var addr = address(kAudioObjectPropertyName, kAudioObjectPropertyScopeGlobal)
        var cf: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        guard AudioObjectGetPropertyData(device, &addr, 0, nil, &size, &cf) == noErr,
              let value = cf?.takeRetainedValue() as String?
        else { return nil }
        return value
    }

    /// O mic embutido do Mac: transporte 'bltn' e com canais de entrada.
    private static func builtinInputID() -> AudioDeviceID {
        var addr = address(kAudioHardwarePropertyDevices, kAudioObjectPropertyScopeGlobal)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(systemObject, &addr, 0, nil, &size) == noErr,
              size > 0
        else { return 0 }

        let count = Int(size) / MemoryLayout<AudioDeviceID>.size
        var devices = [AudioDeviceID](repeating: 0, count: count)
        guard AudioObjectGetPropertyData(systemObject, &addr, 0, nil, &size, &devices) == noErr
        else { return 0 }

        for device in devices {
            guard u32(device, kAudioDevicePropertyTransportType) == kAudioDeviceTransportTypeBuiltIn,
                  hasInput(device)
            else { continue }
            return device
        }
        return 0
    }

    private static func hasInput(_ device: AudioDeviceID) -> Bool {
        var addr = address(kAudioDevicePropertyStreamConfiguration, kAudioObjectPropertyScopeInput)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(device, &addr, 0, nil, &size) == noErr, size > 0
        else { return false }

        let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: 16)
        defer { raw.deallocate() }
        guard AudioObjectGetPropertyData(device, &addr, 0, nil, &size, raw) == noErr else { return false }

        let list = raw.assumingMemoryBound(to: AudioBufferList.self)
        let buffers = UnsafeMutableAudioBufferListPointer(list)
        return buffers.contains { $0.mNumberChannels > 0 }
    }
}

// MARK: - Cyberpunk Design Tokens
//
// Shared palette/type recipe for the notch island + Live chip, ported from
// the "Cyberpunk" (glassdark) theme applied to the main window (see
// src/styles/themes.css, `[data-theme='glassdark']`) so all three surfaces
// read as one system: near-black red-tinted glass, angular 4px corners,
// monospace uppercase chrome labels, solid neon red on primary actions.
enum CP {
    static let red = NSColor(calibratedRed: 1.0, green: 0.176, blue: 0.239, alpha: 1.0)          // #ff2d3d
    static let redBright = NSColor(calibratedRed: 1.0, green: 0.271, blue: 0.325, alpha: 1.0)     // #ff4553 hover
    static let redInk = NSColor(calibratedRed: 0.039, green: 0.027, blue: 0.031, alpha: 1.0)      // #0a0708
    static let glassTint = NSColor(calibratedRed: 0.035, green: 0.027, blue: 0.035, alpha: 0.62)  // app-bg
    static let surface1 = NSColor(calibratedWhite: 1.0, alpha: 0.05)
    static let surfaceBorder = NSColor(calibratedRed: 1.0, green: 0.118, blue: 0.176, alpha: 0.16)
    static let accentBg = NSColor(calibratedRed: 1.0, green: 0.078, blue: 0.157, alpha: 0.12)     // gd-accent-bg
    static let accentBorder = NSColor(calibratedRed: 1.0, green: 0.118, blue: 0.176, alpha: 0.5)  // gd-accent-border
    static let hairline = NSColor(calibratedRed: 1.0, green: 0.118, blue: 0.176, alpha: 0.35)     // app-border
    static let textMuted = NSColor.white.withAlphaComponent(0.38)
    static let cardRadius: CGFloat = 4
    /// Preto do recorte da notch. A pilula do Modo Live precisa da MESMA cor
    /// da camera housing: com o vidro translucido dos outros estados, a "asa"
    /// aparece como um retangulo claro colado num buraco preto.
    static let notchBlack = NSColor(calibratedRed: 0.016, green: 0.012, blue: 0.016, alpha: 1.0)

    static func mono(_ size: CGFloat, _ weight: NSFont.Weight = .semibold) -> NSFont {
        NSFont.monospacedSystemFont(ofSize: size, weight: weight)
    }
}

/// Relogio e resposta de nivel COMPARTILHADOS pelas superficies do Modo Live:
/// a ilha (asas em volta do recorte), o chip logo abaixo da notch e o HUD que o
/// webview desenha dentro da janela (`LiveOverlayCyberpunk.css`).
///
/// Existe porque as tres rodavam ISOLADAS: cada uma com o proprio periodo de
/// animacao e -- pior -- com uma curva de VU diferente, entao a mesma voz mexia
/// as barras de cada superficie numa altura e num ritmo diferentes. Aqui os
/// numeros sao os keyframes do CSS, e o relogio e o de PAREDE (o mesmo do CSS):
/// contar quadros faz o nativo atrasar em relacao ao webview no primeiro frame
/// que cair.
///
/// Mexeu aqui? A curva de nivel tem um espelho em `src/lib/liveMotion.ts`.
enum LiveMotion {
    /// Periodos das barras do equalizador, em segundos (cyberBar1..5 no CSS).
    static let barPeriods: [Double] = [1.1, 0.9, 1.3, 1.0, 1.2]
    /// Piscada dos pontos de "processando" (cyberDotBlink1..3): mesmo periodo,
    /// cada ponto acendendo num instante diferente do ciclo.
    static let dotPeriod: Double = 1.2
    static let dotOffsets: [CGFloat] = [0.1, 0.5, 0.8]
    /// Constante de tempo do VU = `transition: transform 0.09s` do equalizador.
    static let levelSmoothing: Double = 0.09
    /// Entrada/saida do Modo Live = `cyberLiveIn 0.28s` do HUD do webview. As
    /// duas superficies tem de acabar de abrir no MESMO instante.
    static let enterDuration: Double = 0.28
    static let exitDuration: Double = 0.18

    /// 0...1 dentro de um ciclo de `period` segundos, ancorado no relogio.
    static func phase(_ period: Double, at time: Double = CACurrentMediaTime()) -> CGFloat {
        CGFloat(time.truncatingRemainder(dividingBy: period) / period)
    }

    /// Onda 0...1 equivalente a um keyframe `0%,100% -> 50%` com ease-in-out
    /// (que e exatamente meia cossenoide).
    static func wave(_ period: Double, at time: Double = CACurrentMediaTime()) -> CGFloat {
        0.5 - 0.5 * cos(phase(period, at: time) * 2 * .pi)
    }

    /// Alpha 0.2...1 de um ponto de "processando", com o pico curto do keyframe.
    static func blink(index: Int, at time: Double = CACurrentMediaTime()) -> CGFloat {
        let t = phase(dotPeriod, at: time)
        let offset = dotOffsets[index % dotOffsets.count]
        return 0.2 + 0.8 * pow(max(0, cos((t - offset) * 2 * .pi)), 6)
    }

    /// RMS do microfone -> nivel 0...1. Ganho 8, gama 0.65 e piso de ruido 0.18
    /// (fala normal tem RMS ~0.02-0.15, entao sem ganho a barra nao sai do lugar
    /// e sem piso o ruido de sala deixa o equalizador inflado parado).
    /// ESPELHO EXATO de `levelFromRms` em src/lib/liveMotion.ts.
    static func level(rms: Float) -> CGFloat {
        let curved = powf(min(1, max(0, rms) * 8), 0.65)
        let floorNoise: Float = 0.18
        return CGFloat(min(1, max(0, (curved - floorNoise) / (1 - floorNoise))))
    }
}

/// Faint red HUD grid, matching `.app::after` in the web theme — drawn crisp
/// directly over the glass rather than behind it.
func cpDrawGrid(in rect: NSRect, cell: CGFloat = 22) {
    CP.red.withAlphaComponent(0.05).setStroke()
    let path = NSBezierPath()
    path.lineWidth = 1
    var x = rect.minX
    while x <= rect.maxX {
        path.move(to: NSPoint(x: x.rounded(), y: rect.minY))
        path.line(to: NSPoint(x: x.rounded(), y: rect.maxY))
        x += cell
    }
    var y = rect.minY
    while y <= rect.maxY {
        path.move(to: NSPoint(x: rect.minX, y: y.rounded()))
        path.line(to: NSPoint(x: rect.maxX, y: y.rounded()))
        y += cell
    }
    path.stroke()
}

/// CRT scanline band sweeping top-to-bottom, matching `.app::before`
/// (`gdScan`) in the web theme. `phase` is 0...1, looped by the caller.
func cpDrawScanline(in rect: NSRect, phase: CGFloat, bandHeight: CGFloat = 64) {
    let travel = rect.height + bandHeight * 2
    let y = rect.minY - bandHeight + phase * travel
    let band = NSRect(x: rect.minX, y: y, width: rect.width, height: bandHeight)
    guard band.intersects(rect) else { return }
    NSGraphicsContext.saveGraphicsState()
    NSBezierPath(rect: rect).addClip()
    let gradient = NSGradient(colorsAndLocations:
        (CP.red.withAlphaComponent(0), CGFloat(0.0)),
        (CP.red.withAlphaComponent(0.09), CGFloat(0.5)),
        (CP.red.withAlphaComponent(0), CGFloat(1.0))
    )
    gradient?.draw(in: band, angle: 90)
    NSGraphicsContext.restoreGraphicsState()
}

/// Thin red accent hairline along the top edge, matching
/// `.window-titlebar::before`. Assumes an `isFlipped` view (y grows down).
func cpDrawTopHairline(in rect: NSRect) {
    let gradient = NSGradient(colorsAndLocations:
        (CP.red.withAlphaComponent(0), CGFloat(0.0)),
        (CP.red.withAlphaComponent(0.7), CGFloat(0.5)),
        (CP.red.withAlphaComponent(0), CGFloat(1.0))
    )
    gradient?.draw(in: NSRect(x: rect.minX, y: rect.minY, width: rect.width, height: 1), angle: 0)
}

/// Uppercase monospace chrome label — status readouts, tabs, keycaps — matching
/// the web theme's `text-transform: uppercase; font-family: var(--gd-mono)`.
func cpDrawLabel(_ text: String, in rect: NSRect, size: CGFloat, weight: NSFont.Weight = .bold, color: NSColor, alignment: NSTextAlignment = .left, tracking: CGFloat = 0.06) {
    let paragraph = NSMutableParagraphStyle()
    paragraph.alignment = alignment
    paragraph.lineBreakMode = .byTruncatingTail
    let attrs: [NSAttributedString.Key: Any] = [
        .font: CP.mono(size, weight),
        .foregroundColor: color,
        .paragraphStyle: paragraph,
        .kern: tracking * size
    ]
    text.uppercased().draw(in: rect, withAttributes: attrs)
}

// MARK: - Models

enum IslandState {
    case idle
    /// Modo Live ligado: a ilha vira uma pilula que ABRACA o recorte da notch,
    /// microfone numa asa e stop na outra. E o unico estado que aparece sozinho,
    /// sem ninguem passar o mouse -- por isso so tem dois alvos e nada mais.
    case live
    case peek
    case expanded
}

/// What the expanded stage area is currently showing.
/// O que a area central da ilha EXPANDIDA esta mostrando. Nao ha estado de
/// escuta: a ilha nao captura audio -- o microfone dela liga o Modo Live, que
/// tem as proprias superficies (asas + chip).
enum Activity {
    case none        // notifications list
    case thinking    // agent turn in flight
    case answering   // agent answered
}

struct NotchNotification {
    let id: String
    let kind: String          // "chat" | "spark"
    let title: String
    let subtitle: String
    let createdAt: TimeInterval
    let primaryAction: String? // action_type of the first action, if any
}

/// Recorte fisico da notch (a camera housing) de uma tela.
///
/// O macOS nao expoe a notch: expoe as duas areas UTILIZAVEIS da barra de menu,
/// uma de cada lado dela. O que sobra entre as duas E a notch. Tela sem notch
/// (monitor externo, Mac mais velho) devolve nil, e a pilula do Modo Live vira
/// uma barrinha comum, sem miolo cego.
enum NotchGeometry {
    static func size(on screen: NSScreen) -> NSSize? {
        guard #available(macOS 12.0, *),
              let left = screen.auxiliaryTopLeftArea,
              let right = screen.auxiliaryTopRightArea
        else { return nil }
        let width = screen.frame.width - left.width - right.width
        guard width > 1 else { return nil }
        return NSSize(width: width, height: max(left.height, right.height))
    }
}

// MARK: - Fullscreen detection

/// "Tem alguem em tela cheia nesta tela?" — a ilha flutua em `.statusBar` com
/// `.fullScreenAuxiliary`, entao por padrao ela fica POR CIMA de video em tela
/// cheia (YouTube, Netflix, player). Aqui decidimos quando sumir.
///
/// O sinal e a janela em si, nao "tem video tocando": procuramos uma janela de
/// OUTRO app, na camada normal (layer 0), cobrindo a tela inteira INCLUSIVE a
/// faixa do menu bar. Essa ultima condicao e o que separa tela cheia de janela
/// so maximizada — o macOS nao deixa uma janela normal invadir o menu bar, entao
/// a maximizada sempre para na altura dele.
enum FullscreenProbe {
    /// Retangulo da tela em coordenadas do CoreGraphics (origem no topo-esquerdo
    /// da tela zero, y crescendo pra BAIXO) — `CGWindowListCopyWindowInfo`
    /// devolve bounds nesse sistema, e o AppKit usa o oposto.
    private static func cgRect(of screen: NSScreen) -> CGRect {
        let frame = screen.frame
        guard let zero = NSScreen.screens.first else { return frame }
        return CGRect(
            x: frame.origin.x,
            y: zero.frame.maxY - frame.maxY,
            width: frame.width,
            height: frame.height
        )
    }

    static func isCovering(_ screen: NSScreen) -> Bool {
        let target = cgRect(of: screen)

        // Num espaco de tela cheia o menu bar nao fica reservado, entao
        // visibleFrame encosta no topo do frame. Isso libera a variante "ajustar
        // abaixo da camera" dos MacBooks com notch, em que a janela comeca
        // ABAIXO do notch e, so pelos bounds, seria identica a uma maximizada.
        let menuBarFree = screen.visibleFrame.maxY >= screen.frame.maxY - 1
        var topSlack: CGFloat = 1
        if menuBarFree {
            if #available(macOS 12.0, *) {
                topSlack = max(topSlack, screen.safeAreaInsets.top)
            }
        }

        // `.optionOnScreenOnly` ja limpa janelas minimizadas/de outro espaco.
        // Nada aqui depende de kCGWindowName, que e o unico campo que exigiria
        // permissao de Gravacao de Tela.
        guard let windows = CGWindowListCopyWindowInfo(
            [.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID
        ) as? [[String: Any]] else { return false }

        let selfPID = Int(ProcessInfo.processInfo.processIdentifier)

        for window in windows {
            guard (window[kCGWindowLayer as String] as? Int) == 0 else { continue }
            guard (window[kCGWindowOwnerPID as String] as? Int) != selfPID else { continue }
            if let alpha = window[kCGWindowAlpha as String] as? Double, alpha <= 0.01 { continue }
            guard let raw = window[kCGWindowBounds as String] as? [String: Any],
                  let bounds = CGRect(dictionaryRepresentation: raw as CFDictionary)
            else { continue }

            let slack: CGFloat = 2
            let coversWidth = bounds.minX <= target.minX + slack && bounds.maxX >= target.maxX - slack
            let reachesBottom = bounds.maxY >= target.maxY - slack
            let coversTopStrip = bounds.minY <= target.minY + topSlack
            if coversWidth && reachesBottom && coversTopStrip { return true }
        }
        return false
    }
}

// MARK: - Voice (native STT + TTS)

/// Captures the microphone, streams PCM int16 16k to neo-voice `/ws/panel`
/// (STT), and plays back TTS PCM from `/ws/tts`. Half-duplex: the mic is muted
/// while Neo speaks. Mirrors the protocol in liveVoiceService.ts.
// NSObject: o delegate da AVCaptureSession (AVCaptureAudioDataOutputSampleBufferDelegate)
// exige NSObjectProtocol, que o Swift so da por heranca.
final class VoiceController: NSObject {
    var cfg = VoiceConfig()

    var onPartial: ((String) -> Void)?
    /// (texto corrigido, transcricao crua). O `raw` vem do neo-voice quando o
    /// corretor de ASR mexeu no texto (stt_text.py) — segue p/ o metadata do turno.
    var onFinal: ((String, String?) -> Void)?
    var onError: ((String) -> Void)?
    var onListeningChanged: ((Bool) -> Void)?
    var onSpeakingChanged: ((Bool) -> Void)?
    var onLevel: ((CGFloat) -> Void)?

    private let session = URLSession(configuration: .default)

    // STT
    private var sttTask: URLSessionWebSocketTask?
    // Captura por AVCaptureSession, nao por AVAudioEngine: o inputNode do
    // AVAudioEngine NAO troca de dispositivo. `setDeviceID` e aceito (o device
    // realmente muda) mas o no continua reportando o formato do device antigo e
    // nunca converge, entao o tap nasce com formato errado e entrega ZERO frames
    // -- era o "Sem audio do microfone" com fone Bluetooth. A AVCaptureSession
    // abre o device escolhido de verdade, sem mexer no padrao do sistema.
    private let captureSession = AVCaptureSession()
    private let captureQueue = DispatchQueue(label: "com.neo.notch.capture")
    private var captureOutput: AVCaptureAudioDataOutput?
    private var converter: AVAudioConverter?
    private var outFormat: AVAudioFormat?
    /// Formato real entregue pelo device, descoberto no PRIMEIRO buffer. O
    /// conversor p/ 16k e criado ai -- nunca a partir de um formato "declarado",
    /// que foi exatamente a armadilha do AVAudioEngine.
    private var inFormat: AVAudioFormat?
    private var micMuted = false
    private(set) var listening = false
    private var framesReceived = 0
    // Diagnóstico do porquê "capta mas nao transcreve": quantos frames o tap
    // entregou (framesReceived) vs quantos foram DE FATO enviados ao WS
    // (framesSent). Divergência = audio capturado mas nao transmitido.
    private var framesSent = 0
    private var diagAec = false
    private var diagInSR: Double = 0
    private var diagPeakRms: Float = 0 // maior RMS visto -> distingue sinal real de audio mudo
    private var diagDevice = ""        // microfone realmente aberto
    /// Segunda tentativa: o device preferido nao entregou audio, entao esta
    /// captura ignora a preferencia e usa o padrao do sistema. Resetado a cada
    /// nova sessao de escuta.
    private var deviceFallback = false

    // TTS
    private var ttsTask: URLSessionWebSocketTask?
    private let playEngine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private var playerFormat: AVAudioFormat?
    private var pendingBuffers = 0
    private var ttsDone = false
    private(set) var speaking = false
    // Diagnóstico do playback do TTS: chunks de PCM recebidos, se o playEngine
    // chegou a tocar, e o erro do start() (hoje engolido por um catch silencioso).
    private var diagTtsChunks = 0
    private var diagPlayStarted = false
    private var diagTtsErr = ""

    // ------------------------------------------------------------------ STT
    func toggleListening() {
        if listening { stopListening() } else { requestAndStart() }
    }

    private func requestAndStart() {
        deviceFallback = false // sessao nova sempre tenta o device preferido
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
        logErr("[NeoNotch] STT WS conectando: \(url.absoluteString)")
        receiveSTT()

        // Device a usar, resolvido AGORA (o fone entra e sai o tempo todo):
        // 1) o que o usuario escolheu no painel (cfg.inputDeviceName);
        // 2) sumiu/nao achou -> mic INTERNO quando o padrao e Bluetooth (abrir o
        //    mic do fone o derruba de A2DP p/ HFP e a musica vira som de telefone);
        // 3) senao -> padrao do sistema.
        let route = AudioRoute.resolve()
        diagAec = false // AVCaptureSession nao tem Voice Processing (ver AudioRoute)
        let device = pickCaptureDevice(route: route)
        guard let device else {
            onError?("Nenhum microfone disponível.")
            cleanupCapture()
            return
        }
        diagDevice = device.localizedName
        logErr("[NeoNotch] mic: '\(device.localizedName)' (escolhido='\(cfg.inputDeviceName ?? "")' "
               + "saida=\(route.outputTransport) padraoEhBT=\(route.defaultInputIsBluetooth) "
               + "fallback=\(deviceFallback))")

        captureSession.beginConfiguration()
        for existing in captureSession.inputs { captureSession.removeInput(existing) }
        for existing in captureSession.outputs { captureSession.removeOutput(existing) }
        do {
            let deviceInput = try AVCaptureDeviceInput(device: device)
            guard captureSession.canAddInput(deviceInput) else {
                captureSession.commitConfiguration()
                onError?("Microfone '\(device.localizedName)' indisponível.")
                cleanupCapture()
                return
            }
            captureSession.addInput(deviceInput)
        } catch {
            captureSession.commitConfiguration()
            onError?("Falha ao abrir o microfone: \(error.localizedDescription)")
            cleanupCapture()
            return
        }

        let output = AVCaptureAudioDataOutput()
        output.setSampleBufferDelegate(self, queue: captureQueue)
        guard captureSession.canAddOutput(output) else {
            captureSession.commitConfiguration()
            onError?("Não foi possível ler o microfone.")
            cleanupCapture()
            return
        }
        captureSession.addOutput(output)
        captureOutput = output
        captureSession.commitConfiguration()

        // NOTA: nao ha mais Voice Processing I/O (AEC) aqui. Ele so existe no
        // inputNode do AVAudioEngine, que tivemos de abandonar porque nao troca de
        // dispositivo (ver `captureSession`). Na pratica nao se perdeu nada: o AEC
        // ja vinha DESLIGADO por padrao -- com o sidecar da wake word segurando o
        // mic, o VPIO entregava 0 frames ou audio mudo. `NEO_AUDIO_AEC=on` agora
        // so afeta o webview, que continua no getUserMedia.

        guard let target = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: 16000,
            channels: 1,
            interleaved: true
        ) else {
            onError?("Formato de áudio inválido.")
            cleanupCapture()
            return
        }
        outFormat = target
        // `converter`/`inFormat` ficam nil de proposito: sao criados no primeiro
        // sample buffer, com o formato REAL do device (ver makeConverter).
        converter = nil
        inFormat = nil
        diagInSR = 0

        do {
            framesReceived = 0
            framesSent = 0
            diagPeakRms = 0
            captureSession.startRunning()
            listening = true
            micMuted = false
            onListeningChanged?(true)
            // Diagnostic: if the tap never fires, the mic isn't actually feeding
            // us audio (almost always the TCC permission for this bare binary).
            // Emitimos um `notch_diag` (o Rust loga no Neo Desktop.log) em dois
            // instantes: 1.6s (saude inicial) e 5s (depois do usuario ja ter
            // falado) — para distinguir 0-frames vs capta-mas-nao-envia.
            for delay in [1.6, 5.0] {
                DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
                    guard let self, self.listening else { return }
                    emitEvent([
                        "type": "notch_diag",
                        "at": delay,
                        "framesReceived": self.framesReceived,
                        "framesSent": self.framesSent,
                        "micMuted": self.micMuted,
                        "converterNil": self.converter == nil,
                        "taskNil": self.sttTask == nil,
                        "aec": self.diagAec,
                        "inSampleRate": self.diagInSR,
                        "peakRms": self.diagPeakRms,
                        "device": self.diagDevice,
                    ])
                }
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.6) { [weak self] in
                guard let self, self.listening else { return }
                guard self.framesReceived == 0 else { return }
                if !self.deviceFallback {
                    // O device escolhido abriu mas nao entrega audio (fone BT em
                    // A2DP nao tem mic, device ocupado, ...). Ultima tentativa no
                    // PADRAO DO SISTEMA -- ter audio vale mais que ter o device
                    // preferido. Isto e o que faltava antes: o retry so mexia no
                    // AEC (ja desligado), entao a falha era permanente.
                    logErr("[NeoNotch] 0 frames em '\(self.diagDevice)' — tentando o padrao do sistema")
                    self.deviceFallback = true
                    self.cleanupCapture()
                    self.beginCapture()
                } else {
                    logErr("[NeoNotch] 0 frames tambem no padrao do sistema")
                    self.onError?("Sem áudio do microfone — nenhuma entrada está capturando.")
                }
            }
        }
    }

    /// Microfone a abrir nesta captura. A escolha do usuario ganha; se ela sumiu
    /// (ou ja falhou uma vez, `deviceFallback`), cai na heuristica antiga e por
    /// fim no padrao do sistema.
    private func pickCaptureDevice(route: AudioRoute) -> AVCaptureDevice? {
        let available = AVCaptureDevice.DiscoverySession(
            deviceTypes: [.microphone, .external],
            mediaType: .audio,
            position: .unspecified
        ).devices

        if !deviceFallback, let wanted = cfg.inputDeviceName, !wanted.isEmpty {
            if let match = available.first(where: { $0.localizedName == wanted }) {
                return match
            }
            logErr("[NeoNotch] microfone escolhido '\(wanted)' nao esta disponivel — usando outro")
        }

        // Sem escolha (ou escolha ausente): so evitamos o caso nocivo -- mic de
        // fone Bluetooth, que derruba o aparelho de A2DP p/ HFP.
        if !deviceFallback, route.defaultInputIsBluetooth,
           let builtin = available.first(where: { $0.localizedName == route.builtinInputName }) {
            return builtin
        }

        return AVCaptureDevice.default(for: .audio) ?? available.first
    }

    /// Conversor p/ 16 kHz int16 criado com o formato REAL do primeiro buffer.
    private func makeConverter(from format: AVAudioFormat) {
        guard let target = outFormat else { return }
        inFormat = format
        diagInSR = format.sampleRate
        let conv = AVAudioConverter(from: format, to: target)
        // Reamostragem HW(48k)->16k com qualidade maxima (menos aliasing).
        conv?.sampleRateConverterQuality = AVAudioQuality.max.rawValue
        converter = conv
        logErr("[NeoNotch] captura ativa: \(format.sampleRate) Hz, \(format.channelCount) ch")
    }

    /// Converte o CMSampleBuffer da AVCaptureSession no AVAudioPCMBuffer que o
    /// resto do pipeline (VU, conversor 16k, envio ao WS) ja consumia do
    /// AVAudioEngine -- assim so a FONTE mudou, o processamento continua igual.
    fileprivate func handle(sampleBuffer: CMSampleBuffer) {
        guard let desc = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(desc)
        else { return }

        // O formato real so e conhecido agora; o conversor nasce aqui.
        if converter == nil || inFormat?.sampleRate != asbd.pointee.mSampleRate {
            guard let fmt = AVAudioFormat(streamDescription: asbd) else { return }
            makeConverter(from: fmt)
        }
        guard let fmt = inFormat else { return }

        let frames = AVAudioFrameCount(CMSampleBufferGetNumSamples(sampleBuffer))
        guard frames > 0,
              let pcm = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: frames)
        else { return }
        pcm.frameLength = frames

        // Copia as amostras para dentro do AVAudioPCMBuffer.
        var blockBuffer: CMBlockBuffer?
        var abl = AudioBufferList()
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: &abl,
            bufferListSize: MemoryLayout<AudioBufferList>.size,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
            blockBufferOut: &blockBuffer
        )
        guard status == noErr else { return }
        _ = blockBuffer // mantido vivo enquanto copiamos

        let src = UnsafeMutableAudioBufferListPointer(&abl)
        let dst = UnsafeMutableAudioBufferListPointer(pcm.mutableAudioBufferList)
        for i in 0..<min(src.count, dst.count) {
            guard let from = src[i].mData, let to = dst[i].mData else { continue }
            memcpy(to, from, Int(min(src[i].mDataByteSize, dst[i].mDataByteSize)))
        }

        processCaptured(pcm)
    }

    private func processCaptured(_ buffer: AVAudioPCMBuffer) {
        framesReceived += 1
        // VU meter from the raw float input. A curva mora em LiveMotion, nao aqui:
        // ela e a MESMA do webview, senao a mesma voz mexe as barras da ilha e as
        // do HUD do app em alturas diferentes.
        if let ch = buffer.floatChannelData, buffer.frameLength > 0 {
            let n = Int(buffer.frameLength)
            var sum: Float = 0
            let p = ch[0]
            for i in 0..<n { sum += p[i] * p[i] }
            let rms = sqrtf(sum / Float(n))
            if rms > diagPeakRms { diagPeakRms = rms }
            let level = LiveMotion.level(rms: rms)
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
        framesSent += 1
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
            case .failure(let error):
                // Antes: `break` silencioso -> "capta mas nao transcreve" sem
                // pista nenhuma. Agora logamos o motivo (recusa/queda do WS).
                logErr("[NeoNotch] STT WS caiu: \(error.localizedDescription)")
            }
        }
    }

    private func handleSttJSON(_ s: String) {
        guard let data = s.data(using: .utf8),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = root["type"] as? String else { return }
        let text = (root["text"] as? String) ?? ""
        let raw = root["raw_text"] as? String
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            if type == "partial" {
                self.onPartial?(text)
            } else if type == "final" {
                // Final VAZIO e proposital: o neo-voice descartou o enunciado (eco
                // do wake word). Nao abre turno — o guard de vazio ja cobre.
                let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
                if !trimmed.isEmpty { self.onFinal?(trimmed, raw) }
            }
        }
    }

    func stopListening() {
        cleanupCapture()
        listening = false
        onListeningChanged?(false)
    }

    private func cleanupCapture() {
        if captureSession.isRunning { captureSession.stopRunning() }
        captureOutput?.setSampleBufferDelegate(nil, queue: nil)
        captureOutput = nil
        captureSession.beginConfiguration()
        for input in captureSession.inputs { captureSession.removeInput(input) }
        for output in captureSession.outputs { captureSession.removeOutput(output) }
        captureSession.commitConfiguration()
        sttTask?.cancel(with: .goingAway, reason: nil)
        sttTask = nil
        converter = nil
        inFormat = nil
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
        diagTtsChunks = 0
        diagPlayStarted = false
        diagTtsErr = ""
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
                // O servidor FECHA o /ws/tts logo apos o `done` -> isso cai aqui.
                // NAO parar o player agora: os buffers ja agendados ainda estao
                // tocando. Marca fim de stream e deixa drenar (o callback
                // .dataPlayedBack chama maybeFinishSpeaking quando o ultimo acaba).
                DispatchQueue.main.async { [weak self] in
                    guard let self else { return }
                    self.ttsDone = true
                    self.maybeFinishSpeaking()
                }
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
            diagPlayStarted = true
        } catch {
            diagTtsErr = error.localizedDescription
            logErr("[NeoNotch] TTS playEngine.start() FALHOU: \(error.localizedDescription)")
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
            self.diagTtsChunks += 1
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
        emitEvent([
            "type": "notch_tts_diag",
            "chunks": diagTtsChunks,
            "playStarted": diagPlayStarted,
            "engineRunning": playEngine.isRunning,
            "playerPlaying": player.isPlaying,
            "err": diagTtsErr,
            "outTransport": AudioRoute.currentOutputTransport(),
        ])
        ttsTask?.cancel(with: .goingAway, reason: nil)
        ttsTask = nil
        if player.isPlaying { player.stop() }
        if playEngine.isRunning { playEngine.stop() }
        // Zera o formato: paramos o engine acima, entao a proxima fala precisa
        // passar pelo caminho completo do setupPlayer (reconecta + start + play).
        // Sem isso, o setupPlayer faz early-return por "formato ja existe" e os
        // buffers da 2a fala vao para um engine parado = silencio (Bug B).
        playerFormat = nil
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

// Recebe o audio da AVCaptureSession (fila `captureQueue`) e entrega ao mesmo
// pipeline de sempre. Ver VoiceController.handle(sampleBuffer:).
extension VoiceController: AVCaptureAudioDataOutputSampleBufferDelegate {
    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        handle(sampleBuffer: sampleBuffer)
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

    // ------------------------------------------------------------ Modo Live
    /// Sub-estado da sessao Live (listening | thinking | speaking). Mexe so no
    /// medidor da asa esquerda: o mic e o stop ficam onde estao, sempre.
    var liveVoiceState: String = "listening" { didSet { needsDisplay = true } }
    /// Nivel do microfone (0...1), a ~30 Hz. NAO pede redesenho: o proprio timer
    /// de animacao ja redesenha enquanto a pilula esta aberta.
    var liveLevel: CGFloat = 0
    /// Largura do recorte = o miolo CEGO da pilula, onde nada e desenhado porque
    /// fica atras da camera. Ver NotchGeometry.
    var notchCoreWidth: CGFloat = 0
    /// Ponteiro sobre o stop. Vem do `tick` do controller (que ja le o mouse a
    /// 40 Hz) em vez de uma tracking area, que teria de ser refeita a cada quadro
    /// da animacao de abertura.
    var liveHover = false { didSet { if oldValue != liveHover { needsDisplay = true } } }
    /// Botao de stop das asas.
    var onLiveStop: (() -> Void)?

    /// 0...1 de quanto o CONTEUDO das asas ja apareceu. A moldura (o painel) tem
    /// a mola dela; isto e o segundo tempo -- os icones saem de tras da notch
    /// depois que ela ja comecou a abrir.
    private var liveReveal: CGFloat = 0
    private var liveRevealTarget: CGFloat = 0
    /// Nivel suavizado, so para o medidor nao tremer.
    private var liveLevelSmooth: CGFloat = 0
    private var stopHitRect: NSRect = .zero

    private let promptField = NSTextField(frame: .zero)
    private let horizontalPadding: CGFloat = 24

    private var pulsePhase: CGFloat = 0
    /// 0...1 sweep position of the CRT scanline (expanded state only).
    private var scanPhase: CGFloat = 0
    private var pulseTimer: Timer?

    // Hit-test rects (refreshed every draw, expanded state only)
    private var micHitRect: NSRect = .zero
    private var notifHitRects: [(NSRect, String, String?)] = []

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true

        let placeholderAttrs: [NSAttributedString.Key: Any] = [
            .font: CP.mono(13, .regular),
            .foregroundColor: NSColor.white.withAlphaComponent(0.38)
        ]
        promptField.placeholderAttributedString = NSAttributedString(
            string: "Pergunte ou peça algo ao Neo...",
            attributes: placeholderAttrs
        )
        promptField.font = CP.mono(13, .regular)
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
        let promptCardY: CGFloat = 136
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
            self.scanPhase += 0.0037   // ~9s per sweep, matching gdScan
            if self.scanPhase > 1 { self.scanPhase -= 1 }
            if self.islandState == .live {
                // Mola criticamente amortecida: converge em ~0.3 s sem passar do
                // ponto. O overshoot fica todo na moldura, que e quem "estica".
                // Rampa linear aqui + o ease cubico no desenho = exatamente a
                // duracao do `cyberLiveIn` do HUD do webview.
                let dt = 1.0 / 30.0
                let opening = self.liveRevealTarget > self.liveReveal
                let step = CGFloat(dt / (opening ? LiveMotion.enterDuration : LiveMotion.exitDuration))
                self.liveReveal = opening
                    ? min(self.liveRevealTarget, self.liveReveal + step)
                    : max(self.liveRevealTarget, self.liveReveal - step)
                // Suavizacao do VU pela CONSTANTE DE TEMPO do CSS, nao por um fator
                // por quadro: assim ela nao muda se a taxa de redesenho mudar.
                let alpha = CGFloat(1 - exp(-dt / LiveMotion.levelSmoothing))
                self.liveLevelSmooth += (self.liveLevel - self.liveLevelSmooth) * alpha
                self.needsDisplay = true
            } else if self.islandState == .expanded {
                self.needsDisplay = true
            }
        }
        RunLoop.main.add(timer, forMode: .common)
        pulseTimer = timer
    }

    func stopPulse() {
        pulseTimer?.invalidate()
        pulseTimer = nil
    }

    /// Reinicia a revelacao do conteudo das asas (chamado ao ABRIR o Modo Live).
    func beginLiveReveal() {
        liveReveal = 0
        liveRevealTarget = 1
        liveLevelSmooth = 0
        needsDisplay = true
    }

    /// Apaga o conteudo das asas antes de a moldura encolher.
    func endLiveReveal() {
        liveRevealTarget = 0
        needsDisplay = true
    }

    /// O stop e o UNICO alvo clicavel da pilula (o mic e indicador). Ponto em
    /// coordenadas da view.
    func isOverLiveStop(_ point: NSPoint) -> Bool {
        stopHitRect != .zero && stopHitRect.insetBy(dx: -6, dy: -6).contains(point)
    }

    // -------------------------------------------------------------- drawing
    override func draw(_ dirtyRect: NSRect) {
        // Near-black red-tinted glass over the blur, matching --app-bg in the
        // web theme. Rounding is handled by the container layer's mask.
        CP.glassTint.setFill()
        bounds.fill()

        switch islandState {
        case .idle: drawIdle()
        case .live: drawLive()
        case .peek: drawPeek()
        case .expanded: drawExpanded()
        }

        cpDrawTopHairline(in: bounds)
    }

    private func drawIdle() {
        // Slim HUD strip — a tiny presence indicator inside the bar.
        let bar = NSBezierPath(roundedRect: bounds.insetBy(dx: 14, dy: 11), xRadius: CP.cardRadius, yRadius: CP.cardRadius)
        CP.surface1.setFill()
        bar.fill()
        drawGlowDot(center: NSPoint(x: bounds.midX, y: bounds.midY), radius: 2.4)
    }

    /// Pilula do Modo Live: miolo cego (atras da camera), microfone + medidor na
    /// asa esquerda, stop na direita. Tudo entra deslizando de tras do recorte --
    /// `liveReveal` e o segundo tempo da animacao, depois da moldura.
    private func drawLive() {
        // Mesma cor do recorte: as asas tem de parecer a PROPRIA notch esticando.
        CP.notchBlack.setFill()
        bounds.fill()

        let wing = (bounds.width - notchCoreWidth) / 2
        guard wing > 10 else { stopHitRect = .zero; return }

        let reveal = max(0, min(1, liveReveal))
        let eased = 1 - pow(1 - reveal, 3)      // ease-out cubico
        let slide = (1 - eased) * 22            // sai de tras da notch
        let centerY = bounds.midY

        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current?.cgContext.setAlpha(eased)

        // ---- asa esquerda: microfone + medidor (grupo centrado na asa)
        let groupWidth: CGFloat = 42
        let groupX = wing / 2 - groupWidth / 2 - slide
        // -10.2 e nao -9: o glifo tem haste e base embaixo, entao o centro optico
        // dele fica ~1.2 pt abaixo do centro da caixa.
        let mic = NSRect(x: groupX, y: centerY - 10.2, width: 18, height: 18)
        if liveVoiceState == "listening" {
            // O halo respira com a voz: e o feedback de "estou te ouvindo" que
            // se le de longe, sem texto nenhum.
            let glow = 3 + liveLevelSmooth * 6
            CP.red.withAlphaComponent(0.10 + 0.24 * min(1, liveLevelSmooth)).setFill()
            NSBezierPath(ovalIn: mic.insetBy(dx: -glow, dy: -glow)).fill()
        }
        drawMicGlyph(in: mic, scale: 1.0, color: liveVoiceState == "thinking"
            ? CP.red.withAlphaComponent(0.55) : CP.red)
        drawLiveMeter(x: groupX + 24, centerY: centerY, width: 15, height: 16)

        // ---- asa direita: stop
        let side: CGFloat = 20
        let stop = NSRect(x: bounds.maxX - wing / 2 - side / 2 + slide,
                          y: centerY - side / 2, width: side, height: side)
        stopHitRect = stop
        if liveHover {
            CP.red.withAlphaComponent(0.26).setFill()
            NSBezierPath(roundedRect: stop.insetBy(dx: -5, dy: -5),
                         xRadius: CP.cardRadius + 3, yRadius: CP.cardRadius + 3).fill()
        }
        let stopPath = NSBezierPath(roundedRect: stop, xRadius: CP.cardRadius, yRadius: CP.cardRadius)
        (liveHover ? CP.redBright : CP.red).setFill()
        stopPath.fill()
        CP.redInk.setFill()
        NSBezierPath(roundedRect: stop.insetBy(dx: 6.5, dy: 6.5), xRadius: 1, yRadius: 1).fill()

        NSGraphicsContext.restoreGraphicsState()
    }

    /// Tres barrinhas ao lado do mic: seguem a voz enquanto escuta e pulsam
    /// sozinhas enquanto o Neo pensa/fala. E o que deixa a pilula viva sem
    /// gastar largura com texto.
    private func drawLiveMeter(x: CGFloat, centerY: CGFloat, width: CGFloat, height: CGFloat) {
        let bars = 3
        let gap: CGFloat = 4
        let barWidth = (width - CGFloat(bars - 1) * gap) / CGFloat(bars)
        let now = CACurrentMediaTime()
        for index in 0..<bars {
            // Mesmo periodo por barra que o equalizador do HUD (cyberBar1..3).
            let wave = LiveMotion.wave(LiveMotion.barPeriods[index], at: now)
            let amount: CGFloat
            switch liveVoiceState {
            case "thinking": amount = 0.24 + 0.34 * wave
            case "speaking": amount = 0.28 + 0.72 * wave
            default:
                // O jitter por barra e o que faz o medidor parecer audio, e nao
                // tres retangulos subindo juntos.
                amount = max(0.38, min(1, liveLevelSmooth * (1.3 + 0.6 * wave)))
            }
            let barHeight = max(3, height * amount)
            let rect = NSRect(x: x + CGFloat(index) * (barWidth + gap),
                              y: centerY - barHeight / 2, width: barWidth, height: barHeight)
            CP.red.withAlphaComponent(0.5 + 0.5 * amount).setFill()
            NSBezierPath(roundedRect: rect, xRadius: 1, yRadius: 1).fill()
        }
    }

    private func drawPeek() {
        drawBotIcon(in: NSRect(x: 22, y: 9, width: 28, height: 28))
        cpDrawLabel("Neo", in: NSRect(x: 60, y: 10, width: 90, height: 22), size: 15, weight: .bold, color: .white, tracking: 0.08)
        let dotX = bounds.width - 92
        drawStatusDot(center: NSPoint(x: dotX, y: 23))
        cpDrawLabel("online", in: NSRect(x: dotX + 12, y: 11, width: 64, height: 22), size: 11, weight: .bold, color: CP.red, tracking: 0.08)
    }

    private func drawExpanded() {
        notifHitRects.removeAll()

        // Ambient HUD grid + CRT scanline, matching the main window's glassdark
        // shell (.app::after / .app::before) — the expanded island is the only
        // surface big enough to read it.
        cpDrawGrid(in: bounds)
        cpDrawScanline(in: bounds, phase: scanPhase)

        drawHeaderCard()
        drawPromptCard()

        let stageTop: CGFloat = 202
        let stageBottom = bounds.height - 44
        let stage = NSRect(x: horizontalPadding, y: stageTop, width: bounds.width - horizontalPadding * 2, height: stageBottom - stageTop)

        switch activity {
        case .thinking:
            drawVoiceStage(in: stage, title: "Pensando…", body: caption)
        case .answering:
            drawVoiceStage(in: stage, title: "Neo respondeu", body: caption)
        case .none:
            drawNotifications(in: stage)
        }

        if let err = errorText {
            cpDrawLabel("⚠ \(err)", in: NSRect(x: horizontalPadding, y: bounds.height - 60, width: bounds.width - horizontalPadding * 2, height: 18), size: 11, weight: .bold, color: CP.red, tracking: 0)
        }

        drawText("⌘ Espaço · abrir Neo", in: NSRect(x: 24, y: bounds.height - 30, width: 160, height: 16), size: 11, weight: .medium, color: NSColor.white.withAlphaComponent(0.42), alignment: .left)
        drawKeycap("fn", in: NSRect(x: bounds.width - 78, y: bounds.height - 32, width: 25, height: 20))
        cpDrawLabel("ditar", in: NSRect(x: bounds.width - 48, y: bounds.height - 28, width: 32, height: 14), size: 10, weight: .bold, color: NSColor.white.withAlphaComponent(0.48), tracking: 0.05)
    }

    private func drawHeaderCard() {
        let card = NSRect(x: horizontalPadding, y: 44, width: bounds.width - horizontalPadding * 2, height: 72)
        fillRoundedCard(card)
        let avatarRect = NSRect(x: card.minX + 18, y: card.minY + 18, width: 34, height: 34)
        drawBotIcon(in: avatarRect)

        // Botao de microfone = LIGAR O MODO LIVE (btn-send: vermelho neon solido).
        // Nao tem estado ligado/desligado: assim que o Live sobe, a ilha vira as
        // asas em volta do recorte e este cartao sai de cena.
        let micSize: CGFloat = 46
        let mic = NSRect(x: card.maxX - micSize - 16, y: card.minY + (card.height - micSize) / 2, width: micSize, height: micSize)
        micHitRect = mic
        let micPath = NSBezierPath(roundedRect: mic, xRadius: CP.cardRadius, yRadius: CP.cardRadius)
        CP.red.setFill()
        micPath.fill()
        drawMicGlyph(in: mic, scale: 0.34, color: CP.redInk)

        let textX = avatarRect.maxX + 22
        let textMaxWidth = max(120, mic.minX - textX - 14)
        let greeting: String
        switch activity {
        case .thinking: greeting = "Só um instante…"
        case .answering: greeting = "Pronto!"
        case .none: greeting = "Olá, em que posso ajudar?"
        }
        drawText(greeting, in: NSRect(x: textX, y: card.minY + 18, width: textMaxWidth, height: 20), size: 14, weight: .semibold, color: .white, alignment: .left)

        drawDottedLine(x: textX, y: card.minY + 43, dots: max(8, Int(min(14, (textMaxWidth / 12)))))
    }

    private func drawPromptCard() {
        let card = NSRect(x: horizontalPadding, y: 136, width: bounds.width - horizontalPadding * 2, height: 46)
        fillRoundedCard(card)
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
        cpDrawLabel("Notificações", in: NSRect(x: stage.minX + 6, y: stage.minY, width: 250, height: 18), size: 11, weight: .bold, color: NSColor.white.withAlphaComponent(0.56), tracking: 0.08)
        cpDrawLabel("\(notifications.count)", in: NSRect(x: stage.maxX - 64, y: stage.minY, width: 58, height: 18), size: 11, weight: .bold, color: CP.red, alignment: .right, tracking: 0.04)

        if notifications.isEmpty {
            let empty = NSRect(x: stage.minX, y: stage.minY + 30, width: stage.width, height: 64)
            fillRoundedCard(empty)
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
        fillRoundedCard(card)
        let iconRect = NSRect(x: card.minX + 14, y: card.minY + 11, width: 30, height: 30)
        drawAppIcon(n.kind, in: iconRect)

        let textX = iconRect.maxX + 12
        let rightMetaWidth: CGFloat = 58
        let textWidth = max(110, card.maxX - textX - rightMetaWidth - 12)
        drawText(n.title, in: NSRect(x: textX, y: card.minY + 10, width: textWidth, height: 17), size: 13, weight: .semibold, color: .white, alignment: .left)
        drawText(n.subtitle, in: NSRect(x: textX, y: card.minY + 27, width: textWidth, height: 16), size: 11, weight: .medium, color: NSColor.white.withAlphaComponent(0.62), alignment: .left)
        cpDrawLabel(relativeTime(n.createdAt), in: NSRect(x: card.maxX - rightMetaWidth, y: card.minY + 12, width: rightMetaWidth - 10, height: 16), size: 10, weight: .semibold, color: NSColor.white.withAlphaComponent(0.55), alignment: .right, tracking: 0.03)
    }

    private func drawVoiceStage(in stage: NSRect, title: String, body: String) {
        let card = NSRect(x: stage.minX, y: stage.minY, width: stage.width, height: min(stage.height, 220))
        fillRoundedCard(card)

        // animated accent dot — a single neon red across all activity states.
        let dotAlpha = activity == .answering ? 1.0 : (0.55 + 0.45 * (0.5 + 0.5 * sin(pulsePhase)))
        CP.red.withAlphaComponent(dotAlpha).setFill()
        NSBezierPath(ovalIn: NSRect(x: card.minX + 18, y: card.minY + 20, width: 10, height: 10)).fill()
        cpDrawLabel(title, in: NSRect(x: card.minX + 36, y: card.minY + 15, width: card.width - 52, height: 20), size: 12, weight: .bold, color: NSColor.white.withAlphaComponent(0.85), tracking: 0.06)

        let bodyRect = NSRect(x: card.minX + 18, y: card.minY + 44, width: card.width - 36, height: card.height - 58)
        if body.isEmpty && activity == .thinking {
            drawThinkingDots(at: NSPoint(x: card.minX + 22, y: card.minY + 58))
        } else {
            drawWrapped(body, in: bodyRect, size: 15, weight: .regular, color: NSColor.white.withAlphaComponent(0.9))
        }
    }

    // ----------------------------------------------------------- hit-test
    override func mouseDown(with event: NSEvent) {
        if islandState == .live {
            // A pilula fica POR CIMA da barra de menu: fora do stop ela nao pode
            // fazer nada, so engolir o clique.
            if isOverLiveStop(convert(event.locationInWindow, from: nil)) { onLiveStop?() }
            return
        }
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

    /// Angular frosted card, matching the web theme's flat surface recipe
    /// (surface-1 fill + surface-border hairline, radius 4).
    private func fillRoundedCard(_ rect: NSRect) {
        let path = NSBezierPath(roundedRect: rect, xRadius: CP.cardRadius, yRadius: CP.cardRadius)
        CP.surface1.setFill()
        path.fill()
        CP.surfaceBorder.setStroke()
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
        let now = CACurrentMediaTime()
        for i in 0..<3 {
            CP.red.withAlphaComponent(LiveMotion.blink(index: i, at: now)).setFill()
            NSBezierPath(ovalIn: NSRect(x: origin.x + CGFloat(i) * 14, y: origin.y, width: 7, height: 7)).fill()
        }
    }

    /// Squared HUD avatar — red rim + red glyph, matching `.header-avatar`.
    private func drawBotIcon(in rect: NSRect) {
        CP.red.withAlphaComponent(0.15).setFill()
        NSBezierPath(ovalIn: rect.insetBy(dx: -9, dy: -9)).fill()

        let antenna = NSBezierPath()
        antenna.move(to: NSPoint(x: rect.midX, y: rect.minY - 11))
        antenna.line(to: NSPoint(x: rect.midX, y: rect.minY))
        CP.red.setStroke()
        antenna.lineWidth = 2
        antenna.stroke()
        CP.red.setFill()
        NSBezierPath(ovalIn: NSRect(x: rect.midX - 3.5, y: rect.minY - 15, width: 7, height: 7)).fill()

        let outer = NSBezierPath(roundedRect: rect, xRadius: CP.cardRadius, yRadius: CP.cardRadius)
        CP.accentBg.setFill()
        outer.fill()
        CP.accentBorder.setStroke()
        outer.lineWidth = 1
        outer.stroke()

        let face = NSBezierPath(roundedRect: rect.insetBy(dx: 8, dy: 12), xRadius: 2, yRadius: 2)
        NSColor(calibratedWhite: 0.05, alpha: 1.0).setFill()
        face.fill()

        drawGlowDot(center: NSPoint(x: rect.midX - 7, y: rect.midY + 2), radius: 3)
        drawGlowDot(center: NSPoint(x: rect.midX + 7, y: rect.midY + 2), radius: 3)
    }

    /// Squared app icon — chat = solid red fill, spark = outline (differentiate
    /// by fill vs. outline, staying inside the mono-red palette).
    private func drawAppIcon(_ kind: String, in rect: NSRect) {
        let path = NSBezierPath(roundedRect: rect, xRadius: CP.cardRadius, yRadius: CP.cardRadius)
        if kind == "chat" {
            CP.red.setFill()
            path.fill()
            drawChatGlyph(in: rect)
        } else {
            CP.surface1.setFill()
            path.fill()
            CP.accentBorder.setStroke()
            path.lineWidth = 1
            path.stroke()
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
        CP.red.setFill()
        path.fill()
    }

    private func drawChatGlyph(in rect: NSRect) {
        let bubble = NSBezierPath(roundedRect: rect.insetBy(dx: 8, dy: 8), xRadius: 2, yRadius: 2)
        CP.redInk.setFill()
        bubble.fill()
        CP.red.setFill()
        NSBezierPath(ovalIn: NSRect(x: rect.midX - 6, y: rect.midY - 2, width: 2.5, height: 2.5)).fill()
        NSBezierPath(ovalIn: NSRect(x: rect.midX, y: rect.midY + 1, width: 2.5, height: 2.5)).fill()
    }

    private func drawMicGlyph(in rect: NSRect, scale: CGFloat = 1.0, color: NSColor = .white) {
        let scaledWidth = rect.width * scale
        let scaledHeight = rect.height * scale
        let r = NSRect(x: rect.midX - scaledWidth / 2, y: rect.midY - scaledHeight / 2, width: scaledWidth, height: scaledHeight)
        let s = min(scaledWidth, scaledHeight) / 20
        color.setStroke()
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
        let path = NSBezierPath(roundedRect: rect, xRadius: 3, yRadius: 3)
        CP.surface1.setFill()
        path.fill()
        CP.surfaceBorder.setStroke()
        path.lineWidth = 1
        path.stroke()
        drawText(text, in: rect.offsetBy(dx: 0, dy: 5), size: 14, weight: .bold, color: .white, alignment: .center)
    }

    private func drawKeycap(_ text: String, in rect: NSRect) {
        let path = NSBezierPath(roundedRect: rect, xRadius: 3, yRadius: 3)
        CP.surface1.setFill()
        path.fill()
        CP.surfaceBorder.setStroke()
        path.lineWidth = 1
        path.stroke()
        cpDrawLabel(text, in: rect.offsetBy(dx: 0, dy: 4), size: 11, weight: .bold, color: NSColor.white.withAlphaComponent(0.80), alignment: .center, tracking: 0.04)
    }

    private func drawDottedLine(x: CGFloat, y: CGFloat, dots: Int) {
        CP.red.withAlphaComponent(0.6).setFill()
        for index in 0..<dots {
            let dotX = x + CGFloat(index) * 6
            NSBezierPath(ovalIn: NSRect(x: dotX, y: y, width: 2.5, height: 2.5)).fill()
        }
    }

    private func drawGlowDot(center: NSPoint, radius: CGFloat) {
        CP.red.withAlphaComponent(0.34).setFill()
        NSBezierPath(ovalIn: NSRect(x: center.x - radius - 5, y: center.y - radius - 5, width: radius * 2 + 10, height: radius * 2 + 10)).fill()
        CP.red.setFill()
        NSBezierPath(ovalIn: NSRect(x: center.x - radius, y: center.y - radius, width: radius * 2, height: radius * 2)).fill()
    }

    /// Solid red status LED with a soft glow, matching `.header-status.connected`.
    private func drawStatusDot(center: NSPoint) {
        let radius: CGFloat = 4
        let rect = NSRect(x: center.x - radius, y: center.y - radius, width: radius * 2, height: radius * 2)
        CP.red.withAlphaComponent(0.35).setFill()
        NSBezierPath(ovalIn: rect.insetBy(dx: -3, dy: -3)).fill()
        CP.red.setFill()
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

    /// Prompt DIGITADO na ilha, para o agente completo. A ilha nao captura mais
    /// audio: o microfone dela virou o botao de ligar o Modo Live, que e quem tem
    /// a sessao de voz (e a tolerancia a erro de ASR no neo-core).
    var onPrompt: ((String) -> Void)?
    /// Stop das asas do Modo Live (encerra a sessao, venha ela do chip nativo
    /// ou do overlay dentro da janela).
    var onLiveStop: (() -> Void)?

    private var state: IslandState = .idle
    private var hoverTimer: Timer?
    private var hoverBeganAt: TimeInterval?
    private var stableBeganAt: TimeInterval?
    private var lastPointer: NSPoint?

    /// Ilha recolhida porque tem alguem em tela cheia (ver FullscreenProbe).
    private var hiddenForFullscreen = false
    private var lastFullscreenPoll: TimeInterval = 0

    /// Quem esta pedindo a pilula de Modo Live: "native" (sessao do chip) e/ou
    /// "app" (overlay dentro da janela). Ela so se recolhe quando os DOIS soltam,
    /// senao fechar uma sessao apagaria os controles da outra.
    private var liveSources: Set<String> = []

    private let idleSize = NSSize(width: 138, height: 30)
    private let peekSize = NSSize(width: 460, height: 46)
    private let expandedSize = NSSize(width: 560, height: 538)
    private let movementTolerance: CGFloat = 8
    private let peekIntentDelay: TimeInterval = 0.14
    private let expandIntentDelay: TimeInterval = 0.72
    /// Varrer a lista de janelas custa caro pros 40 Hz do `tick`, e entrar/sair
    /// de tela cheia nao e algo que precise de resposta em milissegundos.
    private let fullscreenPollInterval: TimeInterval = 0.4
    /// Quanto cada asa avanca para fora do recorte. 62 pt cabem icone + medidor
    /// sem invadir a area onde a barra de menu costuma ter conteudo.
    private let liveWingWidth: CGFloat = 62
    /// A ilha e angular (4 px) por design; a pilula do Live e a excecao, porque
    /// esta imitando HARDWARE: as quinas de baixo copiam a curva do recorte.
    private let liveCornerRadius: CGFloat = 11

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
        container.layer?.cornerRadius = CP.cardRadius
        container.layer?.borderWidth = 1
        container.layer?.borderColor = CP.hairline.cgColor

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

        wireIsland()
    }

    private func wireIsland() {
        islandView.onSubmitPrompt = { [weak self] text in
            guard let self else { return }
            self.islandView.errorText = nil
            self.islandView.caption = ""
            self.islandView.activity = .thinking
            self.onPrompt?(text)
        }
        islandView.onMicTapped = {
            // O mic da ilha LIGA o Modo Live -- nao dita mais um prompt avulso.
            // Quem decide ONDE o Live abre (modal na janela ou chip nativo) e o
            // Rust, pela mesma regra da wake word; ver `live_request` no
            // notch_host.rs. A ilha nao se recolhe aqui: quem a substitui pelas
            // asas e o `setLive` que volta pelo IPC.
            emitEvent(["type": "live_request"])
        }
        islandView.onLiveStop = { [weak self] in
            self?.onLiveStop?()
        }
        islandView.onNotificationTapped = { id, action in
            if let action {
                emitEvent(["type": "notification_action", "notification_id": id, "action_type": action])
            } else {
                emitEvent(["type": "notification_dismiss", "notification_id": id])
            }
        }
    }

    /// Interrupcao por voz ("pare") na superficie da ILHA: cancela o turno que
    /// um prompt DIGITADO deixou em voo. Devolve false quando nao havia nada a
    /// parar (ai quem trata e a sessao Live).
    func interruptTurn() -> Bool {
        var acted = false
        if islandView.activity == .thinking {
            emitEvent(["type": "live_abort"]) // cancels the in-flight agent turn
            islandView.activity = .none
            acted = true
        }
        return acted
    }

    // ------------------------------------------------------------- Modo Live
    /// Liga/desliga a pilula de Modo Live. Duas fontes acendem a MESMA pilula --
    /// a sessao nativa do chip e o Modo Live aberto dentro da janela -- porque o
    /// usuario ve uma notch so.
    func setLive(_ on: Bool, source: String, state liveState: String? = nil) {
        if let liveState, !liveState.isEmpty, liveState != "idle" {
            islandView.liveVoiceState = liveState
        }
        let wasLive = !liveSources.isEmpty
        if on { liveSources.insert(source) } else { liveSources.remove(source) }
        let isLive = !liveSources.isEmpty
        guard wasLive != isLive, let screen = activeScreen() else { return }
        if isLive {
            enterLive(on: screen)
        } else {
            exitLive(on: screen)
        }
    }

    /// Nivel do microfone da sessao Live, para o medidor da asa esquerda.
    func setLiveLevel(_ level: CGFloat) {
        islandView.liveLevel = level
    }

    /// A pilula NASCE do tamanho do recorte -- invisivel, escondida atras da
    /// camera -- e so entao estica para os lados. E isso que da a leitura de "a
    /// notch cresceu", em vez de "uma janelinha apareceu no topo da tela".
    private func enterLive(on screen: NSScreen) {
        resetIntent()
        let geometry = liveGeometry(on: screen)
        state = .live
        islandView.islandState = .live
        islandView.activity = .none
        islandView.caption = ""
        islandView.errorText = nil
        islandView.notchCoreWidth = geometry.core.width
        islandView.liveHover = false
        islandView.beginLiveReveal()
        applyCorners(for: .live)
        panel.setFrame(Self.frame(for: geometry.core, on: screen), display: true)
        panel.orderFrontRegardless()
        NSAnimationContext.runAnimationGroup { context in
            context.duration = LiveMotion.enterDuration
            // Passa um pouco do ponto e volta: e a mola que faz a abertura
            // parecer material, e nao um corte de tamanho.
            context.timingFunction = CAMediaTimingFunction(controlPoints: 0.18, 1.5, 0.32, 1)
            panel.animator().setFrame(Self.frame(for: geometry.full, on: screen), display: true)
        }
    }

    /// Recolhe pela mesma porta: o conteudo apaga primeiro, a moldura encolhe ate
    /// o recorte e so entao a ilha volta a ser a pilula ociosa.
    private func exitLive(on screen: NSScreen) {
        islandView.endLiveReveal()
        islandView.liveHover = false
        let core = liveGeometry(on: screen).core
        NSAnimationContext.runAnimationGroup({ context in
            context.duration = LiveMotion.exitDuration
            context.timingFunction = CAMediaTimingFunction(controlPoints: 0.4, 0, 0.9, 0.4)
            panel.animator().setFrame(Self.frame(for: core, on: screen), display: true)
        }, completionHandler: { [weak self] in
            guard let self, self.liveSources.isEmpty else { return }  // reabriu no meio
            self.apply(.idle, on: self.activeScreen() ?? screen, animated: false)
        })
    }

    /// (miolo, pilula inteira) do Modo Live nesta tela. O miolo tem a largura
    /// EXATA do recorte, e por isso e o quadro zero da animacao.
    private func liveGeometry(on screen: NSScreen) -> (core: NSSize, full: NSSize) {
        if let notch = NotchGeometry.size(on: screen) {
            let height = max(idleSize.height, notch.height)
            return (NSSize(width: notch.width, height: height),
                    NSSize(width: notch.width + liveWingWidth * 2, height: height))
        }
        // Sem notch nao ha o que abracar: a pilula nasce de um ponto no centro e
        // as duas asas ficam lado a lado, com uma folga no meio.
        let height = idleSize.height + 2
        let gap: CGFloat = 26
        return (NSSize(width: gap, height: height),
                NSSize(width: gap + liveWingWidth * 2, height: height))
    }

    /// Hover do stop sem tracking area: a view e flipped, entao o y local conta do
    /// topo do painel para baixo.
    private func updateLiveHover(pointer: NSPoint) {
        let frame = panel.frame
        let local = NSPoint(x: pointer.x - frame.minX, y: frame.maxY - pointer.y)
        islandView.liveHover = islandView.isOverLiveStop(local)
    }

    // ----------------------------------------------------------------- IPC
    func addNotification(_ n: NotchNotification) {
        islandView.addNotification(n)
    }

    /// Resposta de um prompt digitado: a ilha MOSTRA, quem fala e o Modo Live.
    func agentComplete(text: String) {
        islandView.caption = text
        islandView.activity = .answering
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

        // Keep the island open while a voice/agent turn is alive even if the
        // pointer drifts away.
        // O Modo Live segura a ilha aberta: as asas sao o unico jeito de ver que o
        // mic esta ligado e de desliga-lo. O turno digitado tambem segura, senao a
        // resposta some do meio da tela.
        let busy = islandView.activity == .thinking || !liveSources.isEmpty

        if now - lastFullscreenPoll >= fullscreenPollInterval {
            lastFullscreenPoll = now
            updateFullscreenHiding(on: screen, busy: busy)
        }
        // Recolhida: nada de hover/expandir. O turno em voo continua vivo (o
        // audio nao para), so a apresentacao e que sai da frente.
        if hiddenForFullscreen { return }

        // Modo Live: a pilula E o controle. Se ela expandisse por hover, o caminho
        // do mouse ate o stop derrubaria o proprio botao debaixo do cursor.
        if state == .live {
            updateLiveHover(pointer: pointer)
            return
        }

        let interactiveFrame = panel.frame.insetBy(dx: state == .idle ? 8 : 14, dy: state == .idle ? 8 : 12)
        let isOverIsland = interactiveFrame.contains(pointer)
        let isOverNotch = notchZone(on: screen).contains(pointer)

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

    /// Some/volta conforme alguem entra ou sai de tela cheia NA TELA DA ILHA
    /// (`activeScreen` segue o ponteiro: video em tela cheia no monitor de fora
    /// nao esconde a ilha no monitor onde voce esta).
    private func updateFullscreenHiding(on screen: NSScreen, busy: Bool) {
        // `busy` segura o recolhimento: se o usuario mandou um prompt e so
        // depois foi pra tela cheia, engolir a ilha sumiria com a resposta.
        // Assim que o turno assenta, o proximo poll recolhe.
        let shouldHide = FullscreenProbe.isCovering(screen) && !busy
        guard shouldHide != hiddenForFullscreen else { return }
        hiddenForFullscreen = shouldHide

        if shouldHide {
            resetIntent()
            // Volta pra pilula ANTES de sair de cena: reaparecer expandida, com
            // o texto de um turno velho, nao faz sentido.
            apply(.idle, on: screen, animated: false)
            islandView.stopPulse()
            panel.orderOut(nil)
        } else {
            // Reposiciona antes de mostrar: enquanto recolhida o `tick` sai
            // cedo, entao o painel ficou parado na tela de onde ela sumiu.
            islandView.startPulse()
            if liveSources.isEmpty {
                apply(.idle, on: screen, animated: false)
            } else {
                // Voltar como pilula ociosa apagaria o mic e o stop de uma sessao
                // Live que continua correndo: reabre pela mesma animacao.
                enterLive(on: screen)
            }
            panel.orderFrontRegardless()
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
        switch next {
        case .idle:
            size = idleSize
        case .live:
            size = liveGeometry(on: screen).full
        case .peek:
            size = peekSize
        case .expanded:
            size = expandedSize
        }

        applyCorners(for: next)

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

    /// Quinas: HUD angular (4 px) em todos os estados menos o Live, onde a ilha
    /// imita o recorte -- topo reto (encosta na borda da tela) e as duas quinas
    /// de baixo com a curva do hardware.
    private func applyCorners(for state: IslandState) {
        let radius = state == .live ? liveCornerRadius : CP.cardRadius
        let bottomOnly: CACornerMask = [.layerMinXMinYCorner, .layerMaxXMinYCorner]
        let all: CACornerMask = [.layerMinXMinYCorner, .layerMaxXMinYCorner,
                                 .layerMinXMaxYCorner, .layerMaxXMaxYCorner]
        let mask = state == .live ? bottomOnly : all
        container.layer?.cornerRadius = radius
        container.layer?.maskedCorners = mask
        blur.layer?.cornerRadius = radius
        blur.layer?.maskedCorners = mask
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
    private var progressPhase: CGFloat = 0
    /// 0...1 sweep position of the CRT scanline (speaking card only — the
    /// only state tall enough to read it).
    private var scanPhase: CGFloat = 0
    private var timer: Timer?
    private var closeHitRect: NSRect = .zero
    private var abortHitRect: NSRect = .zero

    override var isFlipped: Bool { true }

    func startAnimating() {
        if timer != nil { return }
        let t = Timer(timeInterval: 1.0 / 30.0, repeats: true) { [weak self] _ in
            guard let self else { return }
            self.progressPhase += 0.018
            if self.progressPhase > 1 { self.progressPhase -= 1 }
            self.scanPhase += 0.0037
            if self.scanPhase > 1 { self.scanPhase -= 1 }
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
        // Near-black red-tinted glass over the blur, matching --app-bg.
        CP.glassTint.setFill()
        bounds.fill()
        abortHitRect = .zero
        closeHitRect = .zero
        switch liveState {
        case "listening": drawListening()
        case "thinking": drawThinking()
        case "speaking": drawSpeaking()
        default: break
        }
        cpDrawTopHairline(in: bounds)
    }

    // listening pill
    private func drawListening() {
        let avatar = NSRect(x: 13, y: bounds.midY - 15, width: 30, height: 30)
        drawNeoFace(in: avatar, glow: true)

        let leftmost = drawControls(centerY: bounds.midY, rightEdge: bounds.maxX - 15, showAbort: false)

        let waveX = avatar.maxX + 13
        let waveW = leftmost - 11 - waveX
        drawWaveform(x: waveX, y: bounds.midY - 8, width: waveW, height: 16)
    }

    // thinking / transition card
    private func drawThinking() {
        let avatar = NSRect(x: 14, y: bounds.midY - 16, width: 32, height: 32)
        drawNeoFace(in: avatar, glow: true)

        _ = drawControls(centerY: 19, rightEdge: bounds.maxX - 15, showAbort: true)

        let textX = avatar.maxX + 12
        cpDrawLabel("Deixa comigo", in: NSRect(x: textX, y: bounds.minY + 14, width: 200, height: 18), size: 12, weight: .bold, color: .white, tracking: 0.05)
        drawThinkingDots(at: NSPoint(x: textX + 104, y: bounds.minY + 21))

        // indeterminate progress bar, matching `.progress-bar`'s red gradient.
        let track = NSRect(x: textX, y: bounds.maxY - 20, width: bounds.maxX - 18 - textX, height: 3)
        CP.surface1.setFill()
        NSBezierPath(roundedRect: track, xRadius: 1.5, yRadius: 1.5).fill()
        let segW = track.width * 0.32
        let travel = track.width + segW
        let x = track.minX - segW + travel * progressPhase
        let clampedX = max(track.minX, x)
        let clampedMax = min(track.maxX, x + segW)
        if clampedMax > clampedX {
            let seg = NSRect(x: clampedX, y: track.minY, width: clampedMax - clampedX, height: 3)
            CP.red.setFill()
            NSBezierPath(roundedRect: seg, xRadius: 1.5, yRadius: 1.5).fill()
        }
    }

    // speaking / response card
    private func drawSpeaking() {
        // Ambient HUD grid + scanline — this is the tallest, most card-like
        // state, the same threshold used by the expanded island.
        cpDrawGrid(in: bounds)
        cpDrawScanline(in: bounds, phase: scanPhase)

        let avatar = NSRect(x: 15, y: 13, width: 26, height: 26)
        drawNeoFace(in: avatar, glow: false)
        cpDrawLabel("Neo", in: NSRect(x: avatar.maxX + 9, y: 15, width: 48, height: 18), size: 12, weight: .bold, color: .white, tracking: 0.08)
        drawMiniBars(x: avatar.maxX + 44, y: 20, active: true)

        let leftmost = drawControls(centerY: 24, rightEdge: bounds.maxX - 15, showAbort: true)
        // "⌥ falar" hint, left of the controls
        cpDrawLabel("⌥ falar", in: NSRect(x: leftmost - 66, y: 17, width: 56, height: 16), size: 10, weight: .bold, color: NSColor.white.withAlphaComponent(0.5), alignment: .right, tracking: 0.04)

        let body = NSRect(x: 18, y: 48, width: bounds.width - 36, height: bounds.height - 58)
        let p = NSMutableParagraphStyle()
        p.lineBreakMode = .byWordWrapping
        p.lineSpacing = 3
        (spoken as NSString).draw(in: body, withAttributes: [
            .font: NSFont.systemFont(ofSize: 13.5, weight: .regular),
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
    /// Squared, matching `.window-control` / `.window-control-close`.
    @discardableResult
    private func drawControls(centerY: CGFloat, rightEdge: CGFloat, showAbort: Bool) -> CGFloat {
        let closeR: CGFloat = 7
        let close = NSRect(x: rightEdge - closeR * 2, y: centerY - closeR, width: closeR * 2, height: closeR * 2)
        closeHitRect = close
        CP.red.setFill()
        NSBezierPath(roundedRect: close, xRadius: 2, yRadius: 2).fill()

        guard showAbort else { return close.minX }

        let aR: CGFloat = 9
        let abort = NSRect(x: close.minX - 12 - aR * 2, y: centerY - aR, width: aR * 2, height: aR * 2)
        abortHitRect = abort
        CP.surface1.setFill()
        let abortPath = NSBezierPath(roundedRect: abort, xRadius: 3, yRadius: 3)
        abortPath.fill()
        CP.surfaceBorder.setStroke()
        abortPath.lineWidth = 1
        abortPath.stroke()
        // stop square
        let sq = abort.insetBy(dx: aR * 0.62, dy: aR * 0.62)
        NSColor.white.withAlphaComponent(0.85).setFill()
        NSBezierPath(roundedRect: sq, xRadius: 1, yRadius: 1).fill()
        return abort.minX
    }

    /// Squared HUD face — red rim + red glowing "eyes", matching the notch
    /// island's bot icon / `.message.assistant .message-avatar`.
    private func drawNeoFace(in rect: NSRect, glow: Bool) {
        if glow {
            CP.red.withAlphaComponent(0.20).setFill()
            NSBezierPath(ovalIn: rect.insetBy(dx: -7, dy: -7)).fill()
        }
        CP.accentBg.setFill()
        NSBezierPath(roundedRect: rect, xRadius: CP.cardRadius, yRadius: CP.cardRadius).fill()
        CP.accentBorder.setStroke()
        let ring = NSBezierPath(roundedRect: rect.insetBy(dx: 0.5, dy: 0.5), xRadius: CP.cardRadius, yRadius: CP.cardRadius)
        ring.lineWidth = 1
        ring.stroke()

        let eyeR = rect.width * 0.085
        let eyeY = rect.midY - eyeR
        let dx = rect.width * 0.17
        for ex in [rect.midX - dx, rect.midX + dx] {
            CP.red.setFill()
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
            CP.red.withAlphaComponent(0.55 + 0.45 * v).setFill()
            NSBezierPath(roundedRect: rect, xRadius: 1, yRadius: 1).fill()
        }
    }

    private func drawMiniBars(x: CGFloat, y: CGFloat, active: Bool) {
        let now = CACurrentMediaTime()
        for i in 0..<4 {
            // Mesmo relogio e mesmos periodos do medidor da ilha logo acima: o
            // chip fica colado nela, e fora de fase os dois brigavam na tela.
            let h = active ? 4 + LiveMotion.wave(LiveMotion.barPeriods[i], at: now) * 9 : 5
            let rect = NSRect(x: x + CGFloat(i) * 5, y: y + (12 - h) / 2, width: 3, height: h)
            CP.red.withAlphaComponent(0.9).setFill()
            NSBezierPath(roundedRect: rect, xRadius: 1, yRadius: 1).fill()
        }
    }

    private func drawThinkingDots(at origin: NSPoint) {
        let now = CACurrentMediaTime()
        for i in 0..<3 {
            CP.red.withAlphaComponent(LiveMotion.blink(index: i, at: now)).setFill()
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
        container.layer?.cornerRadius = CP.cardRadius
        container.layer?.borderWidth = 1
        container.layer?.borderColor = CP.hairline.cgColor

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
        let radius: CGFloat = CP.cardRadius   // angular HUD corners in every state
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
                // Mesma duracao da abertura da ilha e do HUD do webview.
                context.duration = LiveMotion.enterDuration
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
            return NSSize(width: 268, height: 52)
        case "thinking":
            return NSSize(width: 360, height: 64)
        case "speaking":
            let width: CGFloat = 440
            let bodyWidth = width - 36
            let attr = NSAttributedString(string: spoken.isEmpty ? " " : spoken, attributes: [
                .font: NSFont.systemFont(ofSize: 13.5, weight: .regular)
            ])
            let bounding = attr.boundingRect(with: NSSize(width: bodyWidth, height: 600), options: [.usesLineFragmentOrigin, .usesFontLeading])
            let textHeight = min(180, max(20, ceil(bounding.height)))
            return NSSize(width: width, height: 48 + textHeight + 16)
        default:
            return NSSize(width: 268, height: 52)
        }
    }
}

/// Orchestrates hands-free Live Mode natively: owns its own VoiceController so
/// it keeps capturing even when the main app window is minimized. Drives the
/// LiveChip and routes the conversation (STT → agent prompt → answer + TTS).
final class LiveSession {
    private let chip: LiveChipController
    private let voice = VoiceController()
    /// (texto, transcricao crua) — a sessao Live SEMPRE vem do microfone.
    var onPrompt: ((String, String?) -> Void)?
    /// Espelha a sessao na ILHA (as asas em volta do recorte): (ativa, estado).
    /// O chip mostra a CONVERSA; a ilha mostra os CONTROLES.
    var onStateChanged: ((Bool, String) -> Void)?
    /// Nivel do microfone para o medidor das asas.
    var onLevel: ((CGFloat) -> Void)?

    private var active = false
    /// The answer card lingers after TTS ends; it only collapses back to
    /// "listening" once the user actually speaks again (a partial arrives).
    private var showingAnswer = false
    /// True between sending a prompt and the agent answering — used by the
    /// "pare" interrupt to know there's an in-flight turn worth cancelling.
    private var thinking = false
    /// Auto-encerramento por inatividade: se acionar o Modo Live e NAO falar
    /// nada, o chip se fecha sozinho depois de `idleTimeout`. So corre no estado
    /// de escuta ocioso; e pausado enquanto o Neo pensa/fala. Ver armIdleTimer.
    private var idleTimer: DispatchWorkItem?
    /// Vem do VoiceConfig (Rust, NEO_NOTCH_IDLE_TIMEOUT_S). O default aqui so
    /// vale ate o primeiro `config` chegar pelo IPC. 0 = nunca fecha sozinho.
    private var idleTimeout: TimeInterval = 15
    /// Teto de seguranca p/ os estados OCUPADOS (pensando/falando), onde o timer
    /// de ociosidade nao corre. Sem ele, um turno que trava — engine de TTS que
    /// para de mandar audio no meio, agente que nunca responde — deixava o chip
    /// aberto e o mic pausado P/ SEMPRE, porque `onSpeakingChanged(false)` nunca
    /// chegava para rearmar o timer. Generoso de proposito: uma resposta longa de
    /// verdade passa de 2 minutos de fala (ja medimos 166s), entao isto e um
    /// backstop contra travamento, nao um limite de duracao de resposta.
    private let stuckTimeout: TimeInterval = 300

    init(chip: LiveChipController) {
        self.chip = chip
        wire()
    }

    func applyVoiceConfig(_ cfg: VoiceConfig) {
        voice.cfg = cfg
        idleTimeout = cfg.idleTimeoutS
        // config pode chegar com uma sessao ja aberta: re-arma no novo prazo em
        // vez de esperar o proximo evento (senao a sessao atual fica no antigo).
        if active && idleTimer != nil { armIdleTimer() }
    }
    var isActive: Bool { active }

    func start() {
        if active { return }
        active = true
        showingAnswer = false
        thinking = false
        present("listening")
        voice.toggleListening()
        armIdleTimer()
    }

    func stop() {
        if !active { return }
        active = false
        showingAnswer = false
        thinking = false
        cancelIdleTimer()
        voice.dispose()
        chip.hideChip()
        onStateChanged?(false, "idle")
    }

    /// Toda transicao de estado passa por aqui: o chip conta a conversa, a ilha
    /// acende os controles. Um lugar so para os dois nunca divergirem.
    private func present(_ state: String, partial: String = "", spoken: String = "") {
        chip.update(active: true, state: state, partial: partial, spoken: spoken, level: 0)
        onStateChanged?(true, state)
    }

    /// (Re)arma o fecha-sozinho: dispara em `idleTimeout` se nada o rearmar.
    /// Chamado ao entrar/permanecer em escuta ociosa (start, cada parcial, fim
    /// da fala do Neo). A saida e a MESMA do X do chip (emit live_close + stop).
    private func armIdleTimer() {
        guard idleTimeout > 0 else { cancelIdleTimer(); return } // 0 = desligado
        armClose(after: idleTimeout, reason: "inatividade (%.0fs sem fala)")
    }

    /// Enquanto o Neo pensa ou fala nao e "sem ouvir nada", entao o timer de
    /// ociosidade nao vale — mas alguma coisa tem de correr, senao um turno
    /// travado prende a sessao para sempre. Ver `stuckTimeout`.
    private func armStuckTimer() {
        armClose(after: stuckTimeout, reason: "turno travado (%.0fs no mesmo estado)")
    }

    private func armClose(after seconds: TimeInterval, reason: String) {
        cancelIdleTimer()
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.active else { return }
            NSLog("[NeoNotch] Live fechado por " + reason, seconds)
            emitEvent(["type": "live_close"])
            self.stop()
        }
        idleTimer = work
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds, execute: work)
    }

    private func cancelIdleTimer() {
        idleTimer?.cancel()
        idleTimer = nil
    }

    /// Abort just the current turn (stop speaking/agent) and keep listening.
    func abort() {
        guard active else { return }
        voice.cancelSpeak()
        showingAnswer = false
        thinking = false
        present("listening")
        armIdleTimer() // voltou a escutar ocioso -> conta o tempo de novo
        emitEvent(["type": "live_abort"])
    }

    /// Voice-triggered interrupt ("pare"): only act when there's something to
    /// stop (Neo is speaking or still thinking), otherwise leave listening
    /// as-is. Returns false when it didn't act (so the caller can offer the
    /// interrupt to the island surface instead).
    @discardableResult
    func interrupt() -> Bool {
        guard active, voice.speaking || thinking else { return false }
        abort()
        return true
    }

    func agentComplete(text: String, voice voiceOverride: [String: Any]?) {
        guard active else { return }
        showingAnswer = true
        thinking = false
        armStuckTimer() // vai falar: ociosidade nao vale, mas o teto de travamento sim
        present("speaking", spoken: text)
        let v = voiceOverride?["voice"] as? String
        let lang = voiceOverride?["language"] as? String
        let eng = voiceOverride?["engine"] as? String
        voice.speak(text: text, voice: v, language: lang, backend: eng)
    }

    func agentError(_ message: String) {
        guard active else { return }
        showingAnswer = true
        thinking = false
        armIdleTimer() // erro mostrado, sem fala a caminho -> fecha se ficar ocioso
        present("speaking", spoken: "⚠ " + message)
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
                self.present("listening")
            }
        }
        voice.onPartial = { [weak self] text in
            guard let self, self.active else { return }
            // User is speaking again → dismiss the lingering answer.
            self.showingAnswer = false
            // So rearma ESCUTANDO: um parcial atrasado (audio ja em transito no
            // neo-voice) chegando durante a fala trocaria o teto de travamento
            // pelo prazo curto de ociosidade e cortaria uma resposta longa.
            if !self.thinking && !self.voice.speaking { self.armIdleTimer() }
            self.present("listening", partial: text)
        }
        voice.onFinal = { [weak self] text, raw in
            guard let self, self.active else { return }
            self.showingAnswer = false
            self.thinking = true
            self.armStuckTimer() // vai pensar: idem — agente mudo nao prende a sessao
            self.present("thinking")
            self.onPrompt?(text, raw)
        }
        voice.onSpeakingChanged = { [weak self] on in
            // The answer card is KEPT when playback ends (no UI change here);
            // we only feed the wake-word anti-echo gate.
            emitEvent(["type": "tts_state", "source": "live", "speaking": on])
            // Falando -> so o teto de travamento; parou -> volta a contar ociosidade.
            if on { self?.armStuckTimer() } else { self?.armIdleTimer() }
        }
        voice.onLevel = { [weak self] level in
            guard let self, self.active, !self.showingAnswer else { return }
            self.chip.setLevel(level)
            self.onLevel?(level)
        }
        voice.onError = { [weak self] msg in
            guard let self, self.active else { return }
            self.showingAnswer = true
            self.present("speaking", spoken: "⚠ " + msg)
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
        controller.onPrompt = { [weak self] text in
            // Prompt digitado na ilha: nunca e transcricao, entao nao leva `source`.
            self?.submitPrompt(text, fromLive: false, fromVoice: false)
        }
        controller.onLiveStop = { [weak self] in
            // Mesma saida do X do chip: encerra a sessao NATIVA e avisa o webview
            // (o Rust reemite `live-close-requested`), porque o Modo Live pode ter
            // sido aberto la dentro -- e ai a ilha e o unico controle na tela.
            emitEvent(["type": "live_close"])
            self?.liveSession?.stop()
            self?.controller?.setLive(false, source: "app")
        }

        let chip = LiveChipController()
        liveChip = chip
        let session = LiveSession(chip: chip)
        session.onPrompt = { [weak self] text, raw in
            // sessao Live: sempre microfone
            self?.submitPrompt(text, fromLive: true, fromVoice: true, asrRaw: raw)
        }
        session.onStateChanged = { [weak self] active, state in
            self?.controller?.setLive(active, source: "native", state: state)
        }
        session.onLevel = { [weak self] level in
            self?.controller?.setLiveLevel(level)
        }
        liveSession = session

        emitEvent(["type": "ready"])
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.readLoop()
        }
    }

    private func submitPrompt(_ text: String, fromLive: Bool, fromVoice: Bool,
                              asrRaw: String? = nil) {
        pendingFromLive = fromLive
        // `source` marca a ENTRADA como transcricao de audio: o notch tambem tem
        // campo de texto, e so o turno que saiu do microfone ganha a tolerancia a
        // erro de ASR no neo-core (voice-input-normalization-plan.md §3).
        var event: [String: Any] = ["type": "prompt", "text": text]
        if fromVoice {
            event["source"] = "voice"
            if let raw = asrRaw, !raw.isEmpty { event["raw_text"] = raw }
        }
        emitEvent(event)
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
                if let t = v["idleTimeoutS"] as? Double { cfg.idleTimeoutS = max(0, t) }
                cfg.inputDeviceName = (v["inputDeviceName"] as? String)
                    .flatMap { $0.isEmpty ? nil : $0 }
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
                controller.agentComplete(text: text)
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
        case "live_ui":
            // Modo Live aberto DENTRO do app (overlay do webview). A sessao nao e
            // nossa, mas a notch e a mesma: acende as asas para o mic e o stop
            // existirem tambem quando a janela e que esta ouvindo.
            controller.setLive((root["active"] as? Bool) ?? false,
                               source: "app",
                               state: root["state"] as? String)
            // O nivel vem do MICROFONE DO WEBVIEW (a captura e dele nessa
            // sessao). Sem isto o medidor da ilha ficava parado no piso
            // enquanto o equalizador do app dancava com a voz.
            if let level = root["level"] as? Double {
                controller.setLiveLevel(CGFloat(max(0, min(1, level))))
            }
        case "live_interrupt":
            // "pare": the Live chip has priority; when it has nothing to stop,
            // the island surface (typed/mic prompt being spoken or thought)
            // takes the interrupt instead.
            if liveSession?.interrupt() != true {
                _ = controller.interruptTurn()
            }
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

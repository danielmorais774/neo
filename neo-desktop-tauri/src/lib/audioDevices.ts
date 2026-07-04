// audioDevices — seleção persistida (localStorage) do dispositivo de ENTRADA
// (microfone) e de SAÍDA (alto-falante) de áudio, compartilhada entre o
// SettingsModal (onde o usuário escolhe) e o liveVoiceService / wakeRecorder
// (que capturam e reproduzem no Modo Live), sem passar por React.
//
// '' (string vazia / chave ausente) = "Padrão do sistema": deixa o navegador
// decidir. É o comportamento antigo, então nada muda até o usuário escolher.

const INPUT_KEY = 'neo.audio.inputDeviceId';
const OUTPUT_KEY = 'neo.audio.outputDeviceId';
// Rótulo do alto-falante escolhido pelo picker nativo (selectAudioOutput), que
// pode não aparecer em enumerateDevices — guardamos p/ exibir no dropdown.
const OUTPUT_LABEL_KEY = 'neo.audio.outputLabel';

/** Disparado quando a seleção muda, p/ os painéis abertos sincronizarem. */
export const AUDIO_DEVICES_EVENT = 'neo-audio-devices-changed';

export function getInputDeviceId(): string {
  return localStorage.getItem(INPUT_KEY) || '';
}

export function getOutputDeviceId(): string {
  return localStorage.getItem(OUTPUT_KEY) || '';
}

export function setInputDeviceId(id: string): void {
  if (id) localStorage.setItem(INPUT_KEY, id);
  else localStorage.removeItem(INPUT_KEY);
  window.dispatchEvent(new Event(AUDIO_DEVICES_EVENT));
}

export function setOutputDeviceId(id: string, label?: string): void {
  if (id) localStorage.setItem(OUTPUT_KEY, id);
  else localStorage.removeItem(OUTPUT_KEY);
  if (label) localStorage.setItem(OUTPUT_LABEL_KEY, label);
  else localStorage.removeItem(OUTPUT_LABEL_KEY);
  window.dispatchEvent(new Event(AUDIO_DEVICES_EVENT));
}

export function getOutputLabel(): string {
  return localStorage.getItem(OUTPUT_LABEL_KEY) || '';
}

/**
 * Devolve as constraints de áudio p/ getUserMedia respeitando o mic escolhido.
 * Passa o resto das opções (channelCount, echoCancellation, …) intocado.
 */
export function micConstraints(base: MediaTrackConstraints = {}): MediaTrackConstraints {
  const id = getInputDeviceId();
  return id ? { ...base, deviceId: { exact: id } } : base;
}

type SinkCapableAudioContext = AudioContext & {
  setSinkId?: (id: string) => Promise<void>;
};
type SinkCapableMediaElement = HTMLAudioElement & {
  setSinkId?: (id: string) => Promise<void>;
};
type OutputPickerMediaDevices = MediaDevices & {
  selectAudioOutput?: () => Promise<MediaDeviceInfo>;
};

function audioContextCanRoute(): boolean {
  return typeof (AudioContext.prototype as SinkCapableAudioContext).setSinkId === 'function';
}

function mediaElementCanRoute(): boolean {
  try {
    return typeof (document.createElement('audio') as SinkCapableMediaElement).setSinkId === 'function';
  } catch {
    return false;
  }
}

/**
 * true se este webview sabe rotear a saída para um dispositivo específico.
 * Chromium expõe AudioContext.setSinkId; o WebKit (WKWebView do macOS) expõe
 * setSinkId no elemento <audio> — cobrimos os dois.
 */
export function outputRoutingSupported(): boolean {
  return audioContextCanRoute() || mediaElementCanRoute();
}

/** true se há o picker nativo de saída (WebKit), útil quando enumerateDevices
 *  não lista os alto-falantes. */
export function nativeOutputPickerSupported(): boolean {
  return typeof (navigator.mediaDevices as OutputPickerMediaDevices)?.selectAudioOutput === 'function';
}

/** Abre o picker nativo de saída e persiste o device escolhido. Devolve o
 *  MediaDeviceInfo (ou null se cancelado / indisponível). */
export async function pickOutputDeviceNative(): Promise<MediaDeviceInfo | null> {
  const md = navigator.mediaDevices as OutputPickerMediaDevices;
  if (typeof md?.selectAudioOutput !== 'function') return null;
  try {
    const device = await md.selectAudioOutput();
    setOutputDeviceId(device.deviceId, device.label);
    return device;
  } catch {
    return null; // usuário cancelou
  }
}

/**
 * Roteia a saída de TTS de um AudioContext para o alto-falante escolhido.
 *
 * Devolve o AudioNode onde as fontes de áudio devem se conectar: o destino do
 * próprio ctx (padrão do sistema ou quando o Chromium roteia via
 * AudioContext.setSinkId) ou um MediaStreamDestination cujo stream toca por um
 * <audio> com setSinkId (caminho do WebKit). Chame dispose() ao encerrar a fala.
 *
 * Tudo é best-effort: qualquer falha cai no destino padrão para nunca derrubar
 * a fala do Neo.
 */
export class OutputRouter {
  private el: SinkCapableMediaElement | null = null;

  async attach(ctx: AudioContext): Promise<AudioNode> {
    const id = getOutputDeviceId();
    if (!id) return ctx.destination; // padrão do sistema: não mexe

    // 1) Chromium: setSinkId direto no AudioContext, sem elemento extra.
    const sinkCtx = ctx as SinkCapableAudioContext;
    if (typeof sinkCtx.setSinkId === 'function') {
      try {
        await sinkCtx.setSinkId(id);
        return ctx.destination;
      } catch (e) {
        console.warn('[audioDevices] AudioContext.setSinkId falhou:', e);
      }
    }

    // 2) WebKit: roteia o áudio por um <audio> que suporta setSinkId.
    const el = document.createElement('audio') as SinkCapableMediaElement;
    if (typeof el.setSinkId === 'function') {
      try {
        const dest = ctx.createMediaStreamDestination();
        el.autoplay = true; // ajuda o WebKit a não bloquear o play() do stream
        el.srcObject = dest.stream;
        await el.setSinkId(id);
        await el.play();
        this.el = el;
        return dest;
      } catch (e) {
        console.warn('[audioDevices] <audio>.setSinkId falhou:', e);
      }
    }

    return ctx.destination; // sem suporte: saída padrão
  }

  dispose(): void {
    if (!this.el) return;
    try {
      this.el.pause();
    } catch {
      /* noop */
    }
    this.el.srcObject = null;
    this.el = null;
  }
}

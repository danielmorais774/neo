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
 * Política de captura para a rota de áudio atual, resolvida pelo Rust
 * (services/audio_route.rs) via CoreAudio. Ver `getMicStream`.
 */
export interface AudioRoute {
  /** Pedir `echoCancellation` ao getUserMedia? */
  aec: boolean;
  /** 'builtin-speaker' | 'headphones' | 'bluetooth' | 'usb' | … */
  outputTransport: string;
  /** Nome CoreAudio do mic interno — casa com o `label` do enumerateDevices. */
  builtinInputName: string;
  defaultInputIsBluetooth: boolean;
  reason: string;
}

const FALLBACK_ROUTE: AudioRoute = {
  aec: true,
  outputTransport: 'unknown',
  builtinInputName: '',
  defaultInputIsBluetooth: false,
  reason: 'rota indisponível; mantendo o comportamento padrão',
};

/** Resolve a rota AGORA — o fone entra e sai o tempo todo, cache aqui mente. */
export async function getAudioRoute(): Promise<AudioRoute> {
  try {
    return (await window.electronAPI?.audioRoute?.()) ?? FALLBACK_ROUTE;
  } catch {
    return FALLBACK_ROUTE;
  }
}

/**
 * Mic a usar: a escolha explícita do usuário ganha sempre. Sem escolha, se o mic
 * padrão do sistema for um fone Bluetooth, preferimos o mic INTERNO — abrir o mic
 * do fone derruba o dispositivo de A2DP p/ HFP e a música vira som de telefone.
 * O casamento é pelo nome, que o WebKit expõe igual ao CoreAudio.
 */
async function preferredInputId(route: AudioRoute): Promise<string> {
  const chosen = getInputDeviceId();
  if (chosen) return chosen;

  // Sem escolha local, mas pode haver uma escolha COMPARTILHADA (gravada no Rust
  // por nome, que é o que o notch nativo também lê). Isso mantém as três
  // superfícies no mesmo microfone mesmo que este webview seja novo — o
  // localStorage é por origem e não sobrevive a tudo.
  try {
    const byName = await window.electronAPI?.getAudioInput?.();
    if (byName) {
      const id = await deviceIdForName(byName);
      if (id) return id;
    }
  } catch {
    /* segue para a heurística abaixo */
  }

  if (!route.defaultInputIsBluetooth || !route.builtinInputName) return '';
  return (await deviceIdForName(route.builtinInputName)) || '';
}

/** deviceId do input cujo `label` bate com `name` ('' se não achar). Sem
 *  permissão de mic os labels vêm vazios, então cai no padrão do sistema. */
async function deviceIdForName(name: string): Promise<string> {
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const match = devices.find((d) => d.kind === 'audioinput' && d.label === name);
    return match?.deviceId || '';
  } catch {
    return '';
  }
}

/**
 * Abre o microfone seguindo a rota de áudio atual.
 *
 * `echoCancellation` não é mais fixo: no WebKit ele liga o mesmo Voice Processing
 * I/O do nativo, que é duplex — abre o dispositivo de SAÍDA padrão junto e faz
 * ducking do áudio dos outros apps. Só vale quando a música volta pelo ar e vaza
 * no mic (saída em alto-falante); em fone é degradar a música de graça.
 */
export async function getMicStream(): Promise<MediaStream> {
  const route = await getAudioRoute();
  const base: MediaTrackConstraints = {
    channelCount: 1,
    echoCancellation: route.aec,
    noiseSuppression: route.aec,
    autoGainControl: true,
  };
  const id = await preferredInputId(route);
  if (!id) return navigator.mediaDevices.getUserMedia({ audio: base });

  try {
    return await navigator.mediaDevices.getUserMedia({
      audio: { ...base, deviceId: { exact: id } },
    });
  } catch (e) {
    // OverconstrainedError: o device sumiu (fone desconectado) ou o id salgado do
    // WebKit rodou. Cair no mic padrão é melhor que derrubar o Modo Live com um
    // "permissão negada" que não tem nada a ver com permissão.
    if (e instanceof Error && e.name !== 'OverconstrainedError') throw e;
    console.warn('[audioDevices] mic escolhido indisponível; usando o padrão do sistema');
    return navigator.mediaDevices.getUserMedia({ audio: base });
  }
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

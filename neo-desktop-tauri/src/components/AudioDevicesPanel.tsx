// AudioDevicesPanel — escolha do dispositivo de ENTRADA (microfone) e de SAÍDA
// (alto-falante) usados pelo Modo Live (STT/TTS). A seleção é persistida em
// localStorage (lib/audioDevices) e lida pelo liveVoiceService na captura e na
// reprodução — sem passar por props.
//
// enumerateDevices() só revela os NOMES dos dispositivos depois que a permissão
// de microfone é concedida; enquanto isso os rótulos vêm vazios. Por isso há um
// botão "Permitir microfone" que pede a permissão uma vez e recarrega a lista.

import { useCallback, useEffect, useState } from 'react';
import GlassSelect, { type GlassSelectOption } from './GlassSelect';
import {
  AUDIO_DEVICES_EVENT,
  getInputDeviceId,
  getOutputDeviceId,
  getOutputLabel,
  nativeOutputPickerSupported,
  outputRoutingSupported,
  pickOutputDeviceNative,
  setInputDeviceId,
  setOutputDeviceId,
} from '../lib/audioDevices';
import './AudioDevicesPanel.css';

const DEFAULT_OPTION: GlassSelectOption<string> = {
  value: '',
  label: 'Padrão do sistema',
  description: 'Deixa o sistema operacional decidir',
};

function toOptions(devices: MediaDeviceInfo[], fallback: string): GlassSelectOption<string>[] {
  return [
    DEFAULT_OPTION,
    ...devices.map((d, i) => ({
      value: d.deviceId,
      label: d.label || `${fallback} ${i + 1}`,
    })),
  ];
}

export default function AudioDevicesPanel() {
  const [inputs, setInputs] = useState<MediaDeviceInfo[]>([]);
  const [outputs, setOutputs] = useState<MediaDeviceInfo[]>([]);
  const [inputId, setInputIdState] = useState<string>(getInputDeviceId);
  const [outputId, setOutputIdState] = useState<string>(getOutputDeviceId);
  const [needsPermission, setNeedsPermission] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const outputSupported = outputRoutingSupported();
  const nativePicker = nativeOutputPickerSupported();
  const canEnumerate = typeof navigator.mediaDevices?.enumerateDevices === 'function';

  // setState fica DENTRO do callback do .then (não no corpo síncrono), que é o
  // padrão aceito para reagir a um sistema externo — evita o aviso
  // "set-state-in-effect" quando o effect chama refresh() na montagem.
  const refresh = useCallback((): Promise<void> => {
    if (!navigator.mediaDevices?.enumerateDevices) return Promise.resolve();
    return navigator.mediaDevices.enumerateDevices().then((devices) => {
      const isReal = (d: MediaDeviceInfo) => d.deviceId !== 'default' && d.deviceId !== 'communications';
      const ins = devices.filter((d) => d.kind === 'audioinput' && isReal(d));
      const outs = devices.filter((d) => d.kind === 'audiooutput' && isReal(d));
      setInputs(ins);
      setOutputs(outs);
      // Rótulo vazio em algum input => permissão de mic ainda não concedida.
      setNeedsPermission(ins.length > 0 && ins.some((d) => !d.label));
    });
  }, []);

  useEffect(() => {
    void refresh();
    const onDeviceChange = () => void refresh();
    const onSelectionChange = () => {
      setInputIdState(getInputDeviceId());
      setOutputIdState(getOutputDeviceId());
    };
    navigator.mediaDevices?.addEventListener?.('devicechange', onDeviceChange);
    window.addEventListener(AUDIO_DEVICES_EVENT, onSelectionChange);
    return () => {
      navigator.mediaDevices?.removeEventListener?.('devicechange', onDeviceChange);
      window.removeEventListener(AUDIO_DEVICES_EVENT, onSelectionChange);
    };
  }, [refresh]);

  const grantPermission = useCallback(async () => {
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((t) => t.stop()); // só queríamos destravar os rótulos
      await refresh();
    } catch {
      setError('Permissão de microfone negada — não dá pra listar os nomes.');
    }
  }, [refresh]);

  const onPickInput = useCallback(
    (value: string) => {
      setInputDeviceId(value);
      setInputIdState(value);
      // O webview escolhe por deviceId (id salgado do WebKit); o notch nativo e o
      // sidecar da wake word não entendem esse id. Persistimos também o NOME no
      // Rust, que é a chave comum aos três processos — sem isso, escolher o mic
      // aqui não teria efeito nenhum no chip do notch.
      const name = value ? inputs.find((d) => d.deviceId === value)?.label || '' : '';
      void window.electronAPI?.setAudioInput?.(name || null).catch((e: unknown) => {
        console.warn('[AudioDevicesPanel] não consegui salvar o mic no Rust:', e);
      });
    },
    [inputs],
  );

  const onPickOutput = useCallback((value: string) => {
    setOutputDeviceId(value); // sem label: limpa o rótulo do picker nativo
    setOutputIdState(value);
  }, []);

  const onPickOutputNative = useCallback(async () => {
    const device = await pickOutputDeviceNative();
    if (device) {
      setOutputIdState(device.deviceId);
      await refresh();
    }
  }, [refresh]);

  // Alto-falante escolhido pelo picker nativo pode não estar em enumerateDevices;
  // injeta uma opção com o rótulo salvo p/ o dropdown exibir a seleção correta.
  const outputOptions = toOptions(outputs, 'Saída');
  if (outputId && !outputOptions.some((o) => o.value === outputId)) {
    outputOptions.push({ value: outputId, label: getOutputLabel() || 'Saída selecionada' });
  }

  if (!canEnumerate) {
    return <p className="ad-hint ad-warn">Este ambiente não expõe dispositivos de áudio.</p>;
  }

  return (
    <div className="ad-wrap">
      <div className="ad-row">
        <div className="ad-meta">
          <strong>Entrada (microfone)</strong>
          <small>Usado para ouvir você no Modo Live</small>
        </div>
        <GlassSelect
          ariaLabel="Dispositivo de entrada de áudio"
          value={inputId}
          options={toOptions(inputs, 'Microfone')}
          onChange={onPickInput}
        />
      </div>

      <div className="ad-row">
        <div className="ad-meta">
          <strong>Saída (alto-falante)</strong>
          <small>Onde o Neo fala as respostas</small>
        </div>
        {outputs.length === 0 && nativePicker ? (
          <button type="button" className="ad-grant ad-inline" onClick={() => void onPickOutputNative()}>
            {outputId ? getOutputLabel() || 'Saída selecionada' : 'Escolher saída…'}
          </button>
        ) : (
          <GlassSelect
            ariaLabel="Dispositivo de saída de áudio"
            value={outputId}
            options={outputOptions}
            onChange={onPickOutput}
            disabled={!outputSupported}
          />
        )}
      </div>

      {!outputSupported && (
        <p className="ad-hint ad-warn">
          Este webview não permite escolher a saída de áudio; o Neo usa o alto-falante padrão do
          sistema.
        </p>
      )}

      {needsPermission && (
        <button type="button" className="ad-grant" onClick={() => void grantPermission()}>
          Permitir microfone para ver os nomes
        </button>
      )}

      {error && <p className="ad-hint ad-warn">{error}</p>}
    </div>
  );
}

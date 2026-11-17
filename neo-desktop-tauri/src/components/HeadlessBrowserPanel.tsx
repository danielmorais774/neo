// HeadlessBrowserPanel — tetos do navegador headless LOCAL (services/headless.rs).
//
// Por que existe: o navegador do SERVIDOR sai por IP de datacenter na França e
// a web brasileira recusa (medido em 2026-09-20 — o CDN do portal de dados devolve 403 para
// o ASN inteiro, Google e DuckDuckGo servem CAPTCHA). O headless roda na máquina
// DELE, com o mesmo IP residencial do navegador visível, sem tomar a tela.
//
// A conta de RAM é paga pela máquina dele, então os tetos moram aqui. O número
// exibido é `phys_footprint` da árvore de processos — NÃO soma de RSS, que
// conta os frameworks compartilhados uma vez por processo e superestima ~4×
// (medido: 2.227 MB somando RSS contra ~300 MB no maior processo).

import { useCallback, useEffect, useRef, useState } from 'react';
import type { HeadlessLimits, HeadlessStats } from '../lib/tauriApi';
import { save as persistLimits } from '../lib/headlessLimits';
import './HeadlessBrowserPanel.css';

const POLL_MS = 4000;

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export default function HeadlessBrowserPanel() {
  const [limits, setLimits] = useState<HeadlessLimits | null>(null);
  const [stats, setStats] = useState<HeadlessStats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const montado = useRef(true);

  const carregar = useCallback(async () => {
    try {
      const [l, s] = await Promise.all([
        window.electronAPI.headlessGetLimits(),
        window.electronAPI.headlessStats(),
      ]);
      if (!montado.current) return;
      setLimits(l);
      setStats(s);
      setError(null);
    } catch (e) {
      if (montado.current) setError(errMsg(e));
    }
  }, []);

  useEffect(() => {
    montado.current = true;
    void carregar();
    // Só faz sentido acompanhar enquanto a tela está aberta: o número que
    // interessa (RAM) muda com o uso, não com o tempo.
    const t = setInterval(() => void carregar(), POLL_MS);
    return () => {
      montado.current = false;
      clearInterval(t);
    };
  }, [carregar]);

  const aplicar = useCallback(
    async (patch: Record<string, unknown>) => {
      setSaving(true);
      try {
        const novo = await window.electronAPI.headlessSetLimits(patch as never);
        if (montado.current) {
          setLimits(novo);
          setError(null);
        }
        // Guarda DEPOIS do Rust aceitar: o backend normaliza (piso do TTL,
        // perfil desconhecido), e salvar o pedido cru guardaria um valor que
        // nunca foi aplicado.
        persistLimits(novo);
      } catch (e) {
        if (montado.current) setError(errMsg(e));
      } finally {
        if (montado.current) setSaving(false);
      }
    },
    [],
  );

  const desligar = useCallback(async () => {
    try {
      await window.electronAPI.headlessShutdown();
      await carregar();
    } catch (e) {
      setError(errMsg(e));
    }
  }, [carregar]);

  if (!limits) {
    return <div className="headless-panel headless-muted">Carregando…</div>;
  }

  if (!limits.available) {
    return (
      <div className="headless-panel">
        <div className="headless-warn">
          Nenhum Chrome, Brave, Chromium ou Edge encontrado nesta máquina. O
          navegador headless precisa de um deles — ele reusa o que já está
          instalado, sem baixar nada.
        </div>
      </div>
    );
  }

  const ramPct =
    stats?.running && limits.max_ram_mb > 0 && stats.ram_mb != null
      ? Math.min(100, Math.round((stats.ram_mb / limits.max_ram_mb) * 100))
      : 0;

  return (
    <div className="headless-panel">
      {error && <div className="headless-warn">{error}</div>}

      <div className="headless-status">
        {stats?.running ? (
          <>
            <span className="headless-dot headless-dot-on" />
            <div className="headless-status-text">
              <strong>{stats.tabs ?? 0} aba(s) abertas</strong>
              <small>
                {stats.ram_mb} MB
                {limits.max_ram_mb > 0 ? ` de ${limits.max_ram_mb} MB` : ' (sem teto)'}
                {stats.browser ? ` · ${stats.browser.split('/').pop()}` : ''}
              </small>
            </div>
            <button type="button" className="headless-btn" onClick={() => void desligar()}>
              Encerrar
            </button>
          </>
        ) : (
          <>
            <span className="headless-dot" />
            <div className="headless-status-text">
              <strong>Parado</strong>
              <small>Sobe sozinho quando o Neo precisar</small>
            </div>
          </>
        )}
      </div>

      {stats?.running && limits.max_ram_mb > 0 && (
        <div className="headless-bar" aria-hidden>
          <div
            className={`headless-bar-fill${ramPct > 85 ? ' headless-bar-hot' : ''}`}
            style={{ width: `${ramPct}%` }}
          />
        </div>
      )}

      <label className="headless-row">
        <span>
          Abas em paralelo
          <small>0 = sem limite nosso</small>
        </span>
        <input
          type="number"
          min={0}
          max={20}
          value={limits.max_tabs}
          disabled={saving}
          onChange={(e) => void aplicar({ maxTabs: Math.max(0, Number(e.target.value) || 0) })}
        />
      </label>

      <label className="headless-row">
        <span>
          Teto de memória (MB)
          <small>0 = sem teto nosso; o sistema continua sendo a rede de segurança</small>
        </span>
        <input
          type="number"
          min={0}
          step={100}
          value={limits.max_ram_mb}
          disabled={saving}
          onChange={(e) => void aplicar({ maxRamMb: Math.max(0, Number(e.target.value) || 0) })}
        />
      </label>

      <label className="headless-row">
        <span>
          Fechar aba ociosa após (s)
          <small>Fechar aba não devolve memória na hora — por isso o teto acima também existe</small>
        </span>
        <input
          type="number"
          min={0}
          step={30}
          value={limits.idle_ttl_s}
          disabled={saving}
          onChange={(e) => void aplicar({ idleTtlS: Math.max(0, Number(e.target.value) || 0) })}
        />
      </label>

      <label className="headless-row">
        <span>
          Perfil
          <small>
            &quot;Logado&quot; deixa o Neo navegar com a SUA sessão nos sites — poderoso e
            arriscado
          </small>
        </span>
        <select
          value={limits.profile}
          disabled={saving}
          onChange={(e) => void aplicar({ profile: e.target.value })}
        >
          <option value="dedicado">Dedicado (limpo, sem sync)</option>
          <option value="logado">Logado (sua sessão)</option>
        </select>
      </label>
    </div>
  );
}

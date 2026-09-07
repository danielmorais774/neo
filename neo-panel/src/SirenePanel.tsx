import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Screen from './ui/Screen';
import Icon from './ui/Icon';
import './SirenePanel.css';

/*
 * Sirene ONVIF — tela dedicada ao ESP32 (contrato em API.md, na raiz do repo).
 *
 * Esta tela NÃO passa pelo neo-core: ele roda na VPS e não enxerga a LAN. Quem
 * fala com o ESP32 é a máquina que abriu o painel, que está na mesma rede.
 *
 * Duas rotas, porque o firmware não coopera com CORS (medido: GET responde 200
 * sem Access-Control-Allow-Origin, OPTIONS dá 404 — o browser bloquearia tudo):
 *
 *  - em DEV, via `/sirene/<ip>/...`, o proxy do vite.config.ts. Mesma origem,
 *    CORS deixa de existir, funciona sem tocar no firmware.
 *  - no BUILD de produção, direto em `http://<ip>/...` — e aí o firmware
 *    precisa mandar os cabeçalhos (a tela diz isso quando a chamada falha).
 *
 * O IP mora num campo + localStorage, e não em env, porque é DHCP. E toda
 * requisição leva AbortController: ESP32 desligado não recusa conexão, só
 * emudece — sem timeout a tela fica pendurada pra sempre.
 */

const STORAGE_KEY = 'neo.sirene.ip';
const DEFAULT_IP = '192.168.4.85';
const POLL_MS = 2000;
const TIMEOUT_MS = 4000;
const MAX_SLOTS = 8;

/** Tamanho do buffer circular no firmware (RAM, ~2.4 KB) — não vem pela API. */
const EVENT_CAPACITY = 200;
const EVENT_RANGES = [
  { label: '6 h', hours: 6 },
  { label: '24 h', hours: 24 },
  { label: '7 dias', hours: 168 },
  { label: 'Tudo', hours: 0 },
];

// ── Contrato do ESP32 ────────────────────────────────────────

/** Comando manual: enquanto != 'auto', a detecção automática fica suspensa. */
type ManualOverride = 'auto' | 'forced_on' | 'forced_off';

/**
 * Silêncio escalonado: o firmware aumenta o cooldown a cada disparo seguido
 * (padrão 5 s → 5 min → 2 h) pra que um falso positivo teimoso — sombra, galho,
 * inseto na lente — não toque a noite inteira. Ausente em firmware antigo.
 */
interface CooldownState {
  /** Disparos seguidos. Zera sozinho após `streakResetMs` sem disparo novo. */
  triggerStreak: number;
  /** Detecções engolidas pelo silêncio — o que teria disparado e não disparou. */
  suppressedCount: number;
  /** Quanto falta do silêncio atual; 0 fora do cooldown. */
  remainingMs: number;
}

interface SireneStatus {
  armed: boolean;
  motionActive: boolean;
  peopleConfirmed: boolean;
  peopleBurst: boolean;
  sirenState: 'idle' | 'ringing' | 'cooldown' | string;
  /** Ausente no firmware anterior às rotas /api/siren/* — a tela trata como "não suportado". */
  manualOverride?: ManualOverride | string;
  wifiRssi: number;
  utcTime: string;
  /** De onde veio o relógio. Ausente no firmware que só sincronizava pela câmera. */
  timeSource?: string;
  /** Ausente no firmware anterior ao silêncio escalonado. */
  cooldown?: CooldownState;
}

interface PeopleCfg {
  confirmCount: number;
  confirmWindowMs: number;
  burstCount: number;
  burstWindowMs: number;
}

interface Slot {
  days: number;
  startHour: number;
  startMinute: number;
  endHour: number;
  endMinute: number;
}

interface ScheduleCfg {
  enabled: boolean;
  utcOffsetMinutes: number;
  slots: Slot[];
}

/** A escada do silêncio, persistida na flash junto com o resto de /api/config. */
interface CooldownCfg {
  /** Silêncio depois do 1º disparo da sequência. */
  baseMs: number;
  /** Silêncio do 2º disparo em diante, até a escalada. */
  stepMs: number;
  /** A partir deste disparo da sequência vale `longMs`. */
  escalateAfter: number;
  /** O teto — é ele que emudece a sirene por horas. */
  longMs: number;
  /** Tempo sem disparo que faz a sequência voltar a zero. */
  streakResetMs: number;
}

interface SireneConfig {
  alarmEnabled: boolean;
  people: PeopleCfg;
  /** Ausente em firmware anterior ao silêncio escalonado. */
  cooldown?: CooldownCfg;
  schedule: ScheduleCfg;
}

interface SireneEvent {
  startUtc: string;
  durationMs: number;
  /** Qual caminho fechou o disparo. `both` = os dois ativos no mesmo instante. */
  trigger: 'confirm' | 'burst' | 'both' | string;
  /** Estado dos flags no instante do disparo. */
  motion: boolean;
  confirm: boolean;
  burst: boolean;
}

interface EventsResponse {
  count: number;
  periodHours: number;
  events: SireneEvent[];
}

// ── Transporte ───────────────────────────────────────────────

const normalizeIp = (raw: string) =>
  raw.trim().replace(/^https?:\/\//i, '').replace(/\/+$/, '');

const VIA_PROXY = import.meta.env.DEV;

const endpoint = (ip: string, path: string) =>
  VIA_PROXY ? `/sirene/${normalizeIp(ip)}${path}` : `http://${normalizeIp(ip)}${path}`;

/** Traduz a falha crua do fetch no que provavelmente aconteceu de verdade. */
const describe = (e: unknown): string => {
  if (e instanceof DOMException && e.name === 'AbortError') {
    return `sem resposta em ${TIMEOUT_MS / 1000}s — ESP32 desligado, fora da rede ou IP errado`;
  }
  if (e instanceof TypeError) {
    return VIA_PROXY
      ? 'o dev server do painel não respondeu — ele está no ar?'
      : 'não foi possível falar com o ESP32 — ele está offline, o IP mudou (DHCP), ou o firmware não está mandando os cabeçalhos de CORS';
  }
  return e instanceof Error ? e.message : String(e);
};

async function req<T>(ip: string, path: string, init?: RequestInit): Promise<T> {
  const ctrl = new AbortController();
  const timer = window.setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(endpoint(ip, path), { ...init, signal: ctrl.signal });
    if (!res.ok) {
      // O proxy do vite devolve {"error": "..."} — é mais útil que "HTTP 502".
      const detail = await res.json().catch(() => null);
      const msg = detail && typeof detail === 'object' && 'error' in detail ? String(detail.error) : '';
      throw new Error(msg || `HTTP ${res.status} ${res.statusText}`.trim());
    }
    return (await res.json()) as T;
  } finally {
    window.clearTimeout(timer);
  }
}

const postConfig = (ip: string, patch: Record<string, unknown>) =>
  req<SireneConfig>(ip, '/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });

/**
 * Comando manual: POST /api/siren/{on,off,auto}, sem corpo, devolvendo
 * `{ manualOverride }`. Nada aqui é gravado na flash — reboot volta pra 'auto'.
 */
const postSiren = (ip: string, action: OverrideAction) =>
  req<{ manualOverride?: ManualOverride | string }>(ip, `/api/siren/${action}`, { method: 'POST' });

/**
 * Saída manual do silêncio escalonado: POST /api/siren/clear-cooldown, sem
 * corpo. Zera a escada e, se a sirene estiver em `cooldown`, devolve pra `idle`
 * na hora. É o único jeito de encurtar as 2 h sem esperar ou reiniciar a placa.
 */
const postClearCooldown = (ip: string) =>
  req<{ triggerStreak?: number; suppressedCount?: number; sirenState?: string }>(
    ip,
    '/api/siren/clear-cooldown',
    { method: 'POST' },
  );

// ── Dias da semana (bitmask) ─────────────────────────────────

const DAYS = [
  { bit: 1, short: 'D', full: 'Domingo' },
  { bit: 2, short: 'S', full: 'Segunda' },
  { bit: 4, short: 'T', full: 'Terça' },
  { bit: 8, short: 'Q', full: 'Quarta' },
  { bit: 16, short: 'Q', full: 'Quinta' },
  { bit: 32, short: 'S', full: 'Sexta' },
  { bit: 64, short: 'S', full: 'Sábado' },
];

const DAY_PRESETS = [
  { label: 'Todos', value: 127 },
  { label: 'Seg–Sex', value: 62 },
  { label: 'Fim de semana', value: 65 },
];

const describeDays = (days: number): string => {
  if (days === 0) return 'nenhum dia (slot inativo)';
  if (days === 127) return 'todos os dias';
  if (days === 62) return 'seg a sex';
  if (days === 65) return 'fim de semana';
  return DAYS.filter((d) => (days & d.bit) !== 0)
    .map((d) => d.full.slice(0, 3).toLowerCase())
    .join(', ');
};

// ── Horários ─────────────────────────────────────────────────

const pad = (n: number) => String(n).padStart(2, '0');
const toTime = (h: number, m: number) => `${pad(h)}:${pad(m)}`;
const fromTime = (v: string): { h: number; m: number } => {
  const [h, m] = v.split(':');
  return { h: Math.min(23, Math.max(0, Number(h) || 0)), m: Math.min(59, Math.max(0, Number(m) || 0)) };
};

/** Regra do firmware: start == end cobre o dia inteiro; start > end cruza a meia-noite. */
const describeWindow = (s: Slot): string => {
  const start = toTime(s.startHour, s.startMinute);
  const end = toTime(s.endHour, s.endMinute);
  if (start === end) return 'dia inteiro (24h)';
  const crosses = s.startHour * 60 + s.startMinute > s.endHour * 60 + s.endMinute;
  return `${start} → ${end}${crosses ? ' (cruza a meia-noite)' : ''}`;
};

const OFFSETS = [
  { value: -180, label: 'BRT — Brasília (UTC−3)' },
  { value: -120, label: 'BRT verão / Fernando de Noronha (UTC−2)' },
  { value: -240, label: 'Amazonas (UTC−4)' },
  { value: -300, label: 'Acre (UTC−5)' },
  { value: 0, label: 'UTC' },
];

const SIREN_STATE: Record<string, { label: string; tone: 'idle' | 'hot' | 'warm' }> = {
  idle: { label: 'Em repouso', tone: 'idle' },
  ringing: { label: 'TOCANDO', tone: 'hot' },
  cooldown: { label: 'Cooldown', tone: 'warm' },
};

// ── Comando manual ───────────────────────────────────────────

type OverrideAction = 'on' | 'off' | 'auto';

/** Estado -> como mostrar. `mod` casa com as variantes de .sirene-override-badge. */
const OVERRIDE_STATE: Record<string, { label: string; mod: string; note: string }> = {
  auto: {
    label: 'AUTOMÁTICO',
    mod: '',
    note: 'A sirene obedece à detecção, à agenda e à chave geral do alarme.',
  },
  forced_on: {
    label: 'FORÇADA LIGADA',
    mod: ' is-on',
    note: 'A sirene está tocando por comando manual. Detecção, agenda e alarme estão suspensos até voltar ao automático.',
  },
  forced_off: {
    label: 'TRAVADA DESLIGADA',
    mod: ' is-off',
    note: 'A sirene não vai tocar — nem com pessoa detectada. O alarme está efetivamente mudo até voltar ao automático.',
  },
};

/** Fallback se o firmware responder 200 com corpo vazio/inesperado. */
const OVERRIDE_RESULT: Record<OverrideAction, ManualOverride> = {
  on: 'forced_on',
  off: 'forced_off',
  auto: 'auto',
};

const OVERRIDE_DONE: Record<OverrideAction, string> = {
  on: 'Sirene ligada por comando manual.',
  off: 'Sirene desligada e travada — não dispara mais até voltar ao automático.',
  auto: 'Controle devolvido à detecção automática.',
};

// ── Silêncio escalonado ──────────────────────────────────────

/**
 * Duração legível pra configuração e contagem regressiva. É separada de
 * `formatDuration` (que é dos disparos, e nunca passa de 5 min) porque aqui os
 * valores vão a horas — "7200000 ms" e "120 min" não se leem.
 */
const humanMs = (ms: number): string => {
  if (!Number.isFinite(ms) || ms < 0) return '—';
  // Zero sai como '0 s' (e não como "sem silêncio") porque este texto entra no
  // meio de frases — "silencia por sem silêncio" é o que sairia.
  if (ms === 0) return '0 s';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const total = Math.round(ms / 1000);
  if (total < 60) return `${total} s`;
  const min = Math.floor(total / 60);
  const restS = total % 60;
  if (min < 60) return restS ? `${min} min ${restS} s` : `${min} min`;
  const h = Math.floor(min / 60);
  const restMin = min % 60;
  return restMin ? `${h} h ${restMin} min` : `${h} h`;
};

/**
 * A escada em linguagem de gente, do jeito que ela cai PRA ESTES valores — o
 * firmware paga `baseMs` no 1º disparo, `stepMs` do 2º em diante e `longMs` a
 * partir de `escalateAfter`. Com o padrão (escalateAfter=3) isso é exatamente
 * 5 s → 5 min → 2 h; com outros valores as faixas mudam, e a tela tem de
 * mostrar a escada real e não a do texto.
 */
const ladderRows = (c: CooldownCfg): { when: string; ms: number; from: number; to: number }[] => {
  if (c.escalateAfter <= 1) {
    return [{ when: 'todo disparo', ms: c.longMs, from: 1, to: Infinity }];
  }
  const rows = [{ when: '1º disparo', ms: c.baseMs, from: 1, to: 1 }];
  if (c.escalateAfter > 2) {
    rows.push({
      when: c.escalateAfter === 3 ? '2º disparo' : `2º ao ${c.escalateAfter - 1}º disparo`,
      ms: c.stepMs,
      from: 2,
      to: c.escalateAfter - 1,
    });
  }
  rows.push({ when: `${c.escalateAfter}º disparo em diante`, ms: c.longMs, from: c.escalateAfter, to: Infinity });
  return rows;
};

/** Quanto o PRÓXIMO disparo vai pagar de silêncio. */
const cooldownFor = (c: CooldownCfg, streak: number): number => {
  if (streak >= c.escalateAfter) return c.longMs;
  if (streak >= 2) return c.stepMs;
  return c.baseMs;
};

const COOLDOWN_FIELDS: { key: keyof CooldownCfg; label: string; hint: string; unit: 'ms' | 'n' }[] = [
  { key: 'baseMs', label: '1º disparo (ms)', hint: 'silêncio depois do primeiro disparo da sequência', unit: 'ms' },
  { key: 'stepMs', label: 'Disparos seguintes (ms)', hint: 'silêncio do 2º disparo em diante, antes da escalada', unit: 'ms' },
  { key: 'escalateAfter', label: 'Escalar a partir do disparo nº', hint: 'deste disparo em diante vale o silêncio longo', unit: 'n' },
  { key: 'longMs', label: 'Silêncio longo (ms)', hint: 'o teto — é ele que emudece a sirene por horas', unit: 'ms' },
  { key: 'streakResetMs', label: 'Zerar contagem após (ms)', hint: 'tempo sem disparo que faz a sequência voltar a zero', unit: 'ms' },
];

const suppressedLabel = (n: number): string =>
  n === 1 ? '1 detecção engolida' : `${n} detecções engolidas`;

/** Fonte do relógio do device (`timeSource`), novo em /api/status. */
const TIME_SOURCE: Record<string, string> = {
  ntp: 'NTP',
  camera: 'câmera (ONVIF)',
  none: 'sem sincronia',
};

/** Qual caminho fechou o disparo, em /api/events. */
const TRIGGER_LABEL: Record<string, string> = {
  confirm: 'CONFIRM',
  burst: 'BURST',
  both: 'AMBOS',
};

// ── Histórico de disparos ────────────────────────────────────

const formatDuration = (ms: number): string => {
  if (!Number.isFinite(ms) || ms < 0) return '—';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1).replace('.', ',')} s`;
  const min = Math.floor(s / 60);
  // floor (e não round) no resto: arredondar produz "1 min 60 s" em 119,7 s.
  const rest = Math.floor(s % 60);
  return `${min} min${rest ? ` ${rest} s` : ''}`;
};

const relative = (ms: number): string => {
  if (ms < -60_000) return 'no futuro (relógios fora de sincronia)';
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return 'agora mesmo';
  const min = Math.floor(s / 60);
  if (min < 60) return `há ${min} min`;
  const h = Math.floor(min / 60);
  if (h < 24) return `há ${h} h`;
  const d = Math.floor(h / 24);
  return `há ${d} dia${d > 1 ? 's' : ''}`;
};

const parseUtc = (iso: string): number => {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? 0 : t;
};

/**
 * O relógio do ESP32 vem da câmera: sem sync ele nasce em 1970, e aí "há 56
 * anos" só confunde — melhor dizer que o horário não vale.
 */
const describeWhen = (iso: string): { abs: string; rel: string } => {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return { abs: iso || '—', rel: 'horário ilegível' };
  const d = new Date(t);
  if (d.getUTCFullYear() < 2020) return { abs: iso, rel: 'relógio do device não sincronizado' };
  return {
    abs: d.toLocaleString('pt-BR', {
      day: '2-digit', month: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    }),
    rel: relative(Date.now() - t),
  };
};

const rssiLabel = (dbm: number): string => {
  if (dbm >= -50) return 'excelente';
  if (dbm >= -60) return 'bom';
  if (dbm >= -70) return 'ok';
  return 'fraco';
};

const NEW_SLOT: Slot = { days: 127, startHour: 22, startMinute: 0, endHour: 7, endMinute: 0 };

// ── Peças de UI ──────────────────────────────────────────────

const Led = ({ on, label, tone = 'violet' }: { on: boolean; label: string; tone?: 'violet' | 'rose' | 'green' }) => (
  <div className={`sirene-led${on ? ' is-on' : ''} tone-${tone}`}>
    <span className="sirene-led-dot" />
    <span>{label}</span>
  </div>
);

// ── Tela ─────────────────────────────────────────────────────

const SirenePanel = () => {
  const [ipInput, setIpInput] = useState(() => localStorage.getItem(STORAGE_KEY) ?? DEFAULT_IP);
  const [ip, setIp] = useState(() => localStorage.getItem(STORAGE_KEY) ?? DEFAULT_IP);

  const [status, setStatus] = useState<SireneStatus | null>(null);
  const [statusErr, setStatusErr] = useState<string | null>(null);
  const [live, setLive] = useState(true);

  const [cfg, setCfg] = useState<SireneConfig | null>(null);
  const [cfgErr, setCfgErr] = useState<string | null>(null);
  const [loadingCfg, setLoadingCfg] = useState(false);

  const [alarmSaving, setAlarmSaving] = useState(false);

  const [overrideBusy, setOverrideBusy] = useState<OverrideAction | null>(null);
  const [overrideErr, setOverrideErr] = useState<string | null>(null);
  const [overrideOk, setOverrideOk] = useState<string | null>(null);
  // Ligar a sirene faz barulho de verdade na casa: pede confirmação antes.
  const [armingOn, setArmingOn] = useState(false);

  // People fica como string porque o campo precisa poder ficar vazio enquanto
  // se digita; a validação de faixa acontece no submit.
  const [people, setPeople] = useState<Record<keyof PeopleCfg, string>>({
    confirmCount: '', confirmWindowMs: '', burstCount: '', burstWindowMs: '',
  });
  const [peopleSaving, setPeopleSaving] = useState(false);
  const [peopleErr, setPeopleErr] = useState<string | null>(null);
  const [peopleOk, setPeopleOk] = useState<string | null>(null);

  // Silêncio escalonado: o botão que encerra o silêncio agora, e a escada
  // (persistida) que define quanto ele dura. Como em `people`, os campos são
  // string pra poderem ficar vazios enquanto se digita.
  const [clearBusy, setClearBusy] = useState(false);
  const [clearErr, setClearErr] = useState<string | null>(null);
  const [clearOk, setClearOk] = useState<string | null>(null);

  const [cool, setCool] = useState<Record<keyof CooldownCfg, string>>({
    baseMs: '', stepMs: '', escalateAfter: '', longMs: '', streakResetMs: '',
  });
  const [coolSaving, setCoolSaving] = useState(false);
  const [coolErr, setCoolErr] = useState<string | null>(null);
  const [coolOk, setCoolOk] = useState<string | null>(null);

  const [events, setEvents] = useState<SireneEvent[] | null>(null);
  const [eventsErr, setEventsErr] = useState<string | null>(null);
  const [loadingEvents, setLoadingEvents] = useState(false);
  const [eventHours, setEventHours] = useState(24);

  const [sched, setSched] = useState<ScheduleCfg | null>(null);
  const [schedSaving, setSchedSaving] = useState(false);
  const [schedErr, setSchedErr] = useState<string | null>(null);
  const [schedOk, setSchedOk] = useState<string | null>(null);

  // Só é problema quando a tela chama o ESP32 direto: pelo proxy quem faz o
  // HTTP é o dev server, não o browser.
  const insecure = window.location.protocol === 'https:' && !VIA_PROXY;

  /** Espelha a resposta do ESP32 nos rascunhos — a fonte da verdade é o device. */
  const adopt = useCallback((c: SireneConfig) => {
    setCfg(c);
    setPeople({
      confirmCount: String(c.people?.confirmCount ?? 3),
      confirmWindowMs: String(c.people?.confirmWindowMs ?? 8000),
      burstCount: String(c.people?.burstCount ?? 5),
      burstWindowMs: String(c.people?.burstWindowMs ?? 3000),
    });
    setCool({
      baseMs: String(c.cooldown?.baseMs ?? 5000),
      stepMs: String(c.cooldown?.stepMs ?? 300000),
      escalateAfter: String(c.cooldown?.escalateAfter ?? 3),
      longMs: String(c.cooldown?.longMs ?? 7200000),
      streakResetMs: String(c.cooldown?.streakResetMs ?? 10800000),
    });
    setSched({
      enabled: c.schedule?.enabled ?? false,
      utcOffsetMinutes: c.schedule?.utcOffsetMinutes ?? -180,
      slots: (c.schedule?.slots ?? []).map((s) => ({ ...s })),
    });
  }, []);

  const loadConfig = useCallback(async () => {
    setLoadingCfg(true);
    setCfgErr(null);
    try {
      adopt(await req<SireneConfig>(ip, '/api/config'));
    } catch (e) {
      setCfg(null);
      setSched(null);
      setCfgErr(describe(e));
    } finally {
      setLoadingCfg(false);
    }
  }, [ip, adopt]);

  useEffect(() => { void loadConfig(); }, [loadConfig]);

  const loadEvents = useCallback(async () => {
    setLoadingEvents(true);
    setEventsErr(null);
    try {
      const r = await req<EventsResponse>(ip, `/api/events?hours=${eventHours}`);
      setEvents(Array.isArray(r?.events) ? r.events : []);
    } catch (e) {
      setEvents(null);
      setEventsErr(describe(e));
    } finally {
      setLoadingEvents(false);
    }
  }, [ip, eventHours]);

  useEffect(() => { void loadEvents(); }, [loadEvents]);

  // Um disparo só entra no log quando a sirene PARA de tocar (é o fim que fecha
  // a duração). Então o momento de recarregar é a saída de `ringing` — quem já
  // está com a tela aberta vê o disparo aparecer sem clicar em nada.
  const wasRinging = useRef(false);
  useEffect(() => {
    const st = status?.sirenState;
    if (!st) return; // polling pausado ou sem contato não é fim de toque
    const ringing = st === 'ringing';
    if (wasRinging.current && !ringing) void loadEvents();
    wasRinging.current = ringing;
  }, [status?.sirenState, loadEvents]);

  // Polling do status: cadeia de setTimeout (não setInterval) pra uma resposta
  // lenta não empilhar requisição em cima de requisição.
  useEffect(() => {
    if (!live) return;
    let cancelled = false;
    let timer = 0;

    const tick = async () => {
      try {
        const s = await req<SireneStatus>(ip, '/api/status');
        if (cancelled) return;
        setStatus(s);
        setStatusErr(null);
      } catch (e) {
        if (cancelled) return;
        setStatus(null);
        setStatusErr(describe(e));
      }
      if (!cancelled) timer = window.setTimeout(() => void tick(), POLL_MS);
    };

    void tick();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [ip, live]);

  const connect = () => {
    const next = normalizeIp(ipInput);
    if (!next) return;
    localStorage.setItem(STORAGE_KEY, next);
    setIpInput(next);
    setIp(next);
    setStatus(null);
    setStatusErr(null);
  };

  const toggleAlarm = async () => {
    if (!cfg) return;
    setAlarmSaving(true);
    setCfgErr(null);
    try {
      adopt(await postConfig(ip, { alarmEnabled: !cfg.alarmEnabled }));
    } catch (e) {
      setCfgErr(describe(e));
    } finally {
      setAlarmSaving(false);
    }
  };

  const sendOverride = async (action: OverrideAction) => {
    setOverrideBusy(action);
    setOverrideErr(null);
    setOverrideOk(null);
    try {
      const r = await postSiren(ip, action);
      // O polling só refletiria isso no próximo tick (até 2 s). Antecipar aqui
      // evita o clique que parece não ter feito nada — e o tick seguinte
      // corrige, porque a fonte da verdade continua sendo /api/status.
      const next = r?.manualOverride ?? OVERRIDE_RESULT[action];
      setStatus((s) => (s ? { ...s, manualOverride: next } : s));
      setOverrideOk(OVERRIDE_DONE[action]);
    } catch (e) {
      setOverrideErr(describe(e));
    } finally {
      setOverrideBusy(null);
      setArmingOn(false);
    }
  };

  /**
   * POST /api/siren/clear-cooldown. O efeito é antecipado na tela pelo mesmo
   * motivo do override: o polling só traria isso no próximo tick (até 2 s), e
   * um clique que parece não ter feito nada é o pior desfecho justamente aqui,
   * onde quem clica normalmente está com pressa.
   */
  const clearCooldown = async () => {
    const before = status?.cooldown?.suppressedCount ?? 0;
    setClearBusy(true);
    setClearErr(null);
    setClearOk(null);
    try {
      const r = await postClearCooldown(ip);
      setStatus((st) =>
        st
          ? {
              ...st,
              sirenState: r?.sirenState ?? (st.sirenState === 'cooldown' ? 'idle' : st.sirenState),
              cooldown: {
                triggerStreak: r?.triggerStreak ?? 0,
                suppressedCount: r?.suppressedCount ?? 0,
                remainingMs: 0,
              },
            }
          : st,
      );
      const base = cfg?.cooldown?.baseMs;
      setClearOk(
        `Silêncio encerrado e contagem zerada${base !== undefined ? ` — o próximo disparo volta a valer ${humanMs(base)}` : ''}.` +
          (before > 0 ? ` Antes disso, ${suppressedLabel(before)}.` : ''),
      );
    } catch (e) {
      setClearErr(describe(e));
    } finally {
      setClearBusy(false);
    }
  };

  const coolDirty = useMemo(() => {
    if (!cfg?.cooldown) return false;
    return (Object.keys(cool) as (keyof CooldownCfg)[]).some(
      (k) => cool[k] !== String(cfg.cooldown?.[k] ?? ''),
    );
  }, [cool, cfg]);

  const saveCool = async () => {
    const parsed = {
      baseMs: Number(cool.baseMs),
      stepMs: Number(cool.stepMs),
      escalateAfter: Number(cool.escalateAfter),
      longMs: Number(cool.longMs),
      streakResetMs: Number(cool.streakResetMs),
    } as CooldownCfg;

    // O firmware não documenta as faixas destes campos (ao contrário de
    // `people`), então aqui só bloqueia o que é estruturalmente impossível — o
    // resto vira aviso, e quem clampa é o device.
    const problems: string[] = [];
    for (const f of COOLDOWN_FIELDS) {
      const v = parsed[f.key];
      if (!Number.isInteger(v) || v < 0) problems.push(`${f.label} deve ser um inteiro ≥ 0`);
    }
    if (Number.isInteger(parsed.escalateAfter) && parsed.escalateAfter < 1)
      problems.push('a escalada tem de começar no disparo 1 ou depois');

    if (problems.length) {
      setCoolErr(problems.join(' · '));
      setCoolOk(null);
      return;
    }

    setCoolSaving(true);
    setCoolErr(null);
    setCoolOk(null);
    try {
      adopt(await postConfig(ip, { cooldown: parsed }));
      setCoolOk(
        `Escada salva na flash do ESP32 — silêncio longo de ${humanMs(parsed.longMs)} a partir do ${parsed.escalateAfter}º disparo seguido.`,
      );
    } catch (e) {
      setCoolErr(describe(e));
    } finally {
      setCoolSaving(false);
    }
  };

  const peopleDirty = useMemo(() => {
    if (!cfg) return false;
    return (Object.keys(people) as (keyof PeopleCfg)[]).some(
      (k) => people[k] !== String(cfg.people?.[k] ?? ''),
    );
  }, [people, cfg]);

  const savePeople = async () => {
    const n = (v: string) => Number(v);
    const parsed: PeopleCfg = {
      confirmCount: n(people.confirmCount),
      confirmWindowMs: n(people.confirmWindowMs),
      burstCount: n(people.burstCount),
      burstWindowMs: n(people.burstWindowMs),
    };

    // Faixas de API.md — o firmware clampa, mas errar em silêncio é pior.
    const problems: string[] = [];
    const int = (v: number) => Number.isInteger(v);
    if (!int(parsed.confirmCount) || parsed.confirmCount < 1 || parsed.confirmCount > 16)
      problems.push('confirmCount deve ser um inteiro de 1 a 16');
    if (!int(parsed.burstCount) || parsed.burstCount < 1 || parsed.burstCount > 16)
      problems.push('burstCount deve ser um inteiro de 1 a 16');
    if (!int(parsed.confirmWindowMs) || parsed.confirmWindowMs < 500)
      problems.push('confirmWindowMs deve ser um inteiro ≥ 500');
    if (!int(parsed.burstWindowMs) || parsed.burstWindowMs < 500)
      problems.push('burstWindowMs deve ser um inteiro ≥ 500');

    if (problems.length) {
      setPeopleErr(problems.join(' · '));
      setPeopleOk(null);
      return;
    }

    setPeopleSaving(true);
    setPeopleErr(null);
    setPeopleOk(null);
    try {
      adopt(await postConfig(ip, { people: parsed }));
      setPeopleOk('Limiares salvos na flash do ESP32.');
    } catch (e) {
      setPeopleErr(describe(e));
    } finally {
      setPeopleSaving(false);
    }
  };

  const patchSlot = (i: number, patch: Partial<Slot>) => {
    setSched((s) => (s ? { ...s, slots: s.slots.map((sl, j) => (j === i ? { ...sl, ...patch } : sl)) } : s));
    setSchedOk(null);
  };

  const saveSchedule = async () => {
    if (!sched) return;
    setSchedSaving(true);
    setSchedErr(null);
    setSchedOk(null);
    try {
      adopt(await postConfig(ip, {
        schedule: {
          enabled: sched.enabled,
          utcOffsetMinutes: sched.utcOffsetMinutes,
          slots: sched.slots,
        },
      }));
      setSchedOk(
        sched.slots.length === 0
          ? 'Agenda salva sem nenhum slot — com a agenda ligada, o alarme nunca fica ativo.'
          : `Agenda salva com ${sched.slots.length} slot${sched.slots.length > 1 ? 's' : ''}.`,
      );
    } catch (e) {
      setSchedErr(describe(e));
    } finally {
      setSchedSaving(false);
    }
  };

  // A ordem do buffer circular não é contrato — ordena aqui, mais recente primeiro.
  const orderedEvents = useMemo(
    () => (events ? [...events].sort((a, b) => parseUtc(b.startUtc) - parseUtc(a.startUtc)) : []),
    [events],
  );

  const eventTotalMs = useMemo(
    () => orderedEvents.reduce((acc, e) => acc + (Number(e.durationMs) || 0), 0),
    [orderedEvents],
  );

  const st = status ? (SIREN_STATE[status.sirenState] ?? { label: status.sirenState, tone: 'idle' as const }) : null;
  const offsetKnown = sched ? OFFSETS.some((o) => o.value === sched.utcOffsetMinutes) : true;

  // O campo só existe no firmware com as rotas /api/siren/*. Ausência é sinal
  // de firmware antigo — mas os botões seguem clicáveis, e o 404 explica.
  const overrideUnsupported = !!status && status.manualOverride === undefined;
  const override = status?.manualOverride;
  const ov = override
    ? OVERRIDE_STATE[override] ?? {
        label: String(override).toUpperCase(),
        mod: ' is-off',
        note: 'Valor de manualOverride que esta tela não conhece — trate como não-automático.',
      }
    : null;

  // Mesma lógica do override: campo ausente é firmware anterior ao recurso, e
  // não "zero". Confundir os dois faria a tela mentir dizendo "0 disparos".
  const cd = status?.cooldown;
  const cooldownUnsupported = !!status && cd === undefined;
  const muted = !!cd && cd.remainingMs > 0;
  const nothingToClear =
    !!cd && cd.triggerStreak === 0 && cd.suppressedCount === 0 && cd.remainingMs === 0 &&
    status?.sirenState !== 'cooldown';

  // A escada mostrada segue o RASCUNHO, não o que está gravado: mexer no campo
  // e ver a escada mudar é o que torna esses cinco números legíveis. Rascunho a
  // meio caminho (campo vazio ou inválido) cai pro que veio do device.
  const ladderCfg = useMemo<CooldownCfg | null>(() => {
    const parsed = {
      baseMs: Number(cool.baseMs),
      stepMs: Number(cool.stepMs),
      escalateAfter: Number(cool.escalateAfter),
      longMs: Number(cool.longMs),
      streakResetMs: Number(cool.streakResetMs),
    } as CooldownCfg;
    const usable = COOLDOWN_FIELDS.every(
      (f) => cool[f.key].trim() !== '' && Number.isInteger(parsed[f.key]) && parsed[f.key] >= 0,
    );
    return usable ? parsed : cfg?.cooldown ?? null;
  }, [cool, cfg]);

  const rows = ladderCfg ? ladderRows(ladderCfg) : [];
  // O disparo que vem é o próximo da sequência — é ele que a escada vai cobrar.
  const nextStreak = (cd?.triggerStreak ?? 0) + 1;

  /** Combinações que o firmware aceita mas que quase certamente não é o que se quis. */
  const coolWarn = useMemo(() => {
    if (!ladderCfg) return null;
    if (ladderCfg.longMs === 0)
      return 'Silêncio longo em 0 desliga a escalada: cada disparo pode ser seguido do próximo na hora.';
    if (ladderCfg.longMs < ladderCfg.stepMs)
      return 'A escada desce — o silêncio longo é menor que o dos disparos seguintes, o que torna a insistência mais permissiva em vez de menos.';
    if (ladderCfg.streakResetMs > 0 && ladderCfg.streakResetMs <= ladderCfg.longMs)
      return 'A contagem zera antes de o silêncio longo acabar: a sequência nunca chega a passar da escalada.';
    return null;
  }, [ladderCfg]);

  return (
    <Screen
      icon="siren"
      title="Sirene"
      actions={
        <div className="sirene-topbar-actions">
          <button
            className={`neo-toggle ${live ? 'is-on' : 'is-off'}`}
            onClick={() => setLive((v) => !v)}
            title="Consulta /api/status a cada 2s"
          >
            {live ? '● AO VIVO' : '○ PAUSADO'}
          </button>
          <button className="neo-btn-ghost" onClick={() => void loadConfig()} disabled={loadingCfg}>
            <Icon name="refresh" size={14} />
            {loadingCfg ? 'Lendo...' : 'Recarregar'}
          </button>
        </div>
      }
    >
      <div className="neo-content sirene-screen">
        {insecure && (
          <div className="neo-warn is-amber" style={{ marginBottom: 28 }}>
            <Icon name="warn" size={14} />
            O painel está em HTTPS e o ESP32 só fala HTTP — o browser vai bloquear as
            requisições (mixed content). Abra o painel por <strong>http://</strong> na LAN.
          </div>
        )}

        {/* ── Conexão ── */}
        <div className="neo-section">
          <h2 className="neo-h2">Dispositivo</h2>
          <p className="neo-desc">
            O IP vem do DHCP do roteador e aparece no monitor serial no boot do ESP32
            (<em>Wi-Fi ok — IP …</em>). O browser fala <strong>direto</strong> com ele na LAN, sem
            passar pelo neo-core — então este PC precisa estar na mesma rede.
          </p>

          <div className="neo-card">
            <div className="sirene-connect">
              <div className="neo-field" style={{ flex: 1, marginBottom: 0 }}>
                <label className="neo-field-label">IP na rede local</label>
                <input
                  className="neo-input"
                  value={ipInput}
                  onChange={(e) => setIpInput(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') connect(); }}
                  placeholder={DEFAULT_IP}
                  spellCheck={false}
                />
              </div>
              <button className="neo-btn-primary" onClick={connect} disabled={!normalizeIp(ipInput)}>
                Conectar
              </button>
            </div>

            <div className="neo-meta">
              Base: <span className="neo-meta-value">http://{ip}</span> · o IP fica salvo neste
              navegador.
              <br />
              Transporte:{' '}
              <span className="neo-meta-value">
                {VIA_PROXY ? 'proxy do dev server (/sirene) — sem CORS' : 'direto do browser pro ESP32'}
              </span>
            </div>

            {statusErr && (
              <div className="neo-warn" style={{ marginTop: 18 }}>
                <Icon name="warn" size={14} />
                <span>
                  <strong>Sem contato:</strong> {statusErr}.
                  {!VIA_PROXY && (
                    <>
                      <br />
                      Se o ESP32 estiver ligado e o IP certo, o que falta é o firmware devolver{' '}
                      <code>Access-Control-Allow-Origin: *</code> e responder o preflight{' '}
                      <code>OPTIONS</code> com 200 — sem isso o browser bloqueia a chamada mesmo com
                      o device respondendo.
                    </>
                  )}
                </span>
              </div>
            )}
          </div>
        </div>

        {/* ── Estado ao vivo ── */}
        <div className="neo-section">
          <h2 className="neo-h2">Estado ao vivo</h2>
          <p className="neo-desc">
            Leitura de <code>GET /api/status</code> a cada 2 s. <strong>Armado</strong> já leva a
            agenda em conta: fora dos slots ele fica desarmado mesmo com o alarme habilitado. O que
            ele <em>não</em> leva em conta é o silêncio escalonado — dá pra estar armado e mudo ao
            mesmo tempo, e é por isso que existe a pílula <strong>SILENCIADA</strong>.
          </p>

          <div className="neo-card">
            {!status ? (
              <div className="neo-muted">{live ? 'Aguardando resposta do ESP32...' : 'Polling pausado.'}</div>
            ) : (
              <>
                <div className="sirene-state-head">
                  <div className={`sirene-siren tone-${st?.tone}`}>
                    <span className="sirene-siren-label">Sirene</span>
                    <span className="sirene-siren-value">{st?.label}</span>
                  </div>
                  <div className="sirene-state-pills">
                    {/* Armado e mudo convivem: `armed` não conta o cooldown. */}
                    {cd && cd.remainingMs > 0 && (
                      <span
                        className="sirene-mute-badge"
                        title="Silêncio escalonado em curso — a sirene não dispara até acabar"
                      >
                        SILENCIADA · {humanMs(cd.remainingMs)}
                      </span>
                    )}
                    {/* Override só aparece quando é exceção: 'auto' é o normal. */}
                    {ov && override !== 'auto' && (
                      <span className={`sirene-override-badge${ov.mod}`} title={ov.note}>
                        {ov.label}
                      </span>
                    )}
                    <div className={`sirene-armed ${status.armed ? 'is-armed' : ''}`}>
                      {status.armed ? 'ARMADO' : 'DESARMADO'}
                    </div>
                  </div>
                </div>

                <div className="sirene-leds">
                  <Led on={status.motionActive} label="Movimento" tone="violet" />
                  <Led on={status.peopleConfirmed} label="Pessoa · CONFIRM" tone="rose" />
                  <Led on={status.peopleBurst} label="Pessoa · BURST" tone="rose" />
                </div>

                <div className="neo-meta">
                  Wi-Fi: <span className="neo-meta-value">{status.wifiRssi} dBm ({rssiLabel(status.wifiRssi)})</span>
                  {' · '}
                  Relógio do device: <span className="neo-meta-value">{status.utcTime}</span>
                  {' · '}
                  local: <span className="neo-meta-value">
                    {(() => {
                      const d = new Date(status.utcTime);
                      return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString('pt-BR');
                    })()}
                  </span>
                  {status.timeSource && (
                    <>
                      {' · '}
                      fonte:{' '}
                      <span className="neo-meta-value">
                        {TIME_SOURCE[status.timeSource] ?? status.timeSource}
                      </span>
                    </>
                  )}
                </div>
              </>
            )}
          </div>
        </div>

        {/* ── Silêncio escalonado ── */}
        <div className="neo-section">
          <h2 className="neo-h2">Silêncio escalonado</h2>
          <p className="neo-desc">
            A cada disparo seguido o firmware aumenta o silêncio obrigatório. É o que impede um
            falso positivo teimoso — uma sombra, um galho, um inseto na lente — de tocar a noite
            inteira. O preço é que, enquanto o silêncio longo corre, a sirene fica muda{' '}
            <strong>pra todo mundo</strong>, inclusive pra um intruso de verdade: sem{' '}
            <code>POST /api/siren/clear-cooldown</code> só restaria esperar as horas passarem ou
            reiniciar a placa.
          </p>

          <div className="neo-card">
            {cooldownUnsupported && (
              <div className="neo-warn is-amber" style={{ marginBottom: 16 }}>
                <Icon name="warn" size={14} />
                O <code>/api/status</code> deste ESP32 não traz o bloco <code>cooldown</code> —
                sinal de firmware anterior ao silêncio escalonado. O botão abaixo deve responder{' '}
                <strong>404</strong> até você regravar o device.
              </div>
            )}

            {!status ? (
              <div className="neo-muted">
                {live ? 'Aguardando resposta do ESP32...' : 'Polling pausado.'}
              </div>
            ) : cd ? (
              <>
                <div className="sirene-cool-stats">
                  <div className={`sirene-cool-stat${cd.triggerStreak > 0 ? ' is-hot' : ''}`}>
                    <span className="sirene-cool-label">Disparos seguidos</span>
                    <span className="sirene-cool-value">{cd.triggerStreak}</span>
                  </div>
                  <div className={`sirene-cool-stat${cd.suppressedCount > 0 ? ' is-hot' : ''}`}>
                    <span className="sirene-cool-label">Detecções engolidas</span>
                    <span className="sirene-cool-value">{cd.suppressedCount}</span>
                  </div>
                  <div className={`sirene-cool-stat${muted ? ' is-hot' : ''}`}>
                    <span className="sirene-cool-label">Silêncio restante</span>
                    <span className="sirene-cool-value">{muted ? humanMs(cd.remainingMs) : '—'}</span>
                  </div>
                </div>

                {muted && (
                  <div className="neo-warn" style={{ marginTop: 18 }}>
                    <Icon name="warn" size={14} />
                    <span>
                      <strong>A sirene está muda agora.</strong> Faltam{' '}
                      {humanMs(cd.remainingMs)} de silêncio
                      {cd.suppressedCount > 0 && <> — {suppressedLabel(cd.suppressedCount)} desde o último disparo</>}.
                      Nada dispara até acabar, nem detecção de pessoa.
                    </span>
                  </div>
                )}

                <div className="sirene-cmd" style={{ marginTop: 18 }}>
                  <button
                    className="neo-btn-primary"
                    onClick={() => void clearCooldown()}
                    disabled={clearBusy || nothingToClear}
                    title={
                      nothingToClear
                        ? 'Nada a encerrar: a contagem já está zerada e a sirene não está em cooldown'
                        : 'POST /api/siren/clear-cooldown'
                    }
                  >
                    {clearBusy ? 'Encerrando...' : 'Encerrar silêncio agora'}
                  </button>
                  <span className="neo-note">
                    {nothingToClear
                      ? 'Contagem zerada — o próximo disparo já é o primeiro da sequência.'
                      : ladderCfg
                        ? `Zera a sequência: o próximo disparo volta a valer ${humanMs(ladderCfg.baseMs)}.`
                        : 'Zera a sequência e devolve a sirene ao silêncio curto.'}
                  </span>
                </div>

                {ladderCfg && !nothingToClear && (
                  <div className="neo-meta">
                    Sem encerrar, o próximo disparo silencia por{' '}
                    <span className="neo-meta-value">{humanMs(cooldownFor(ladderCfg, nextStreak))}</span>
                    {' · '}
                    a contagem zera sozinha após{' '}
                    <span className="neo-meta-value">{humanMs(ladderCfg.streakResetMs)}</span> sem
                    disparo novo
                  </div>
                )}

                {!live && (
                  <div className="neo-meta-faint" style={{ marginTop: 10 }}>
                    Polling pausado — estes números são do último tick e podem estar velhos.
                  </div>
                )}
              </>
            ) : (
              <div className="neo-muted">Este firmware não reporta o estado do silêncio.</div>
            )}

            {clearErr && (
              <div className="neo-error">
                Erro: {clearErr}
                {clearErr.includes('404') && (
                  <>
                    {' '}— 404 aqui é firmware antigo: <code>/api/siren/clear-cooldown</code> só
                    existe depois de gravar a versão com o silêncio escalonado.
                  </>
                )}
              </div>
            )}
            {clearOk && <div className="neo-ok">✓ {clearOk}</div>}
          </div>

          <div className="neo-card">
            <h3 className="neo-h3">A escada</h3>
            {!cfg ? (
              <div className="neo-muted">Sem configuração carregada.</div>
            ) : !cfg.cooldown ? (
              <div className="neo-warn is-amber">
                <Icon name="warn" size={14} />O <code>GET /api/config</code> deste ESP32 não traz o
                bloco <code>cooldown</code> — a escada é fixa no firmware e não dá pra editar por
                aqui.
              </div>
            ) : (
              <>
                <p className="neo-desc" style={{ marginTop: 0 }}>
                  Estes valores são gravados na flash (NVS) e sobrevivem a reboot — ao contrário da
                  contagem, que vive só em RAM. A escada abaixo é a que vale{' '}
                  <strong>para os valores nos campos</strong>, e acompanha o que você digita.
                </p>

                <div className="sirene-ladder">
                  {rows.map((r) => {
                    const isNext = !!cd && nextStreak >= r.from && nextStreak <= r.to;
                    return (
                      <div className={`sirene-ladder-row${isNext ? ' is-next' : ''}`} key={r.when}>
                        <span className="sirene-ladder-when">{r.when}</span>
                        <span className="sirene-ladder-ms">{humanMs(r.ms)} de silêncio</span>
                        {isNext && <span className="sirene-ladder-tag">é o próximo</span>}
                      </div>
                    );
                  })}
                </div>

                {coolWarn && (
                  <div className="neo-warn is-amber" style={{ marginTop: 16 }}>
                    <Icon name="warn" size={14} />
                    {coolWarn}
                  </div>
                )}

                <div className="sirene-grid" style={{ marginTop: 20 }}>
                  {COOLDOWN_FIELDS.map((f) => (
                    <div className="neo-field" key={f.key}>
                      <label className="neo-field-label">{f.label}</label>
                      <input
                        className="neo-input"
                        type="number"
                        min={f.unit === 'n' ? 1 : 0}
                        step={f.unit === 'ms' ? 1000 : 1}
                        value={cool[f.key]}
                        onChange={(e) => {
                          setCool((c) => ({ ...c, [f.key]: e.target.value }));
                          setCoolOk(null);
                        }}
                      />
                      <div className="neo-note" style={{ marginTop: 6 }}>
                        {f.unit === 'ms' && (
                          <strong>
                            {cool[f.key].trim() === '' ? '—' : humanMs(Number(cool[f.key]))}
                            {' · '}
                          </strong>
                        )}
                        {f.hint}
                      </div>
                    </div>
                  ))}
                </div>

                <div className="neo-actions">
                  <button
                    className="neo-btn-primary"
                    onClick={() => void saveCool()}
                    disabled={coolSaving || !coolDirty}
                  >
                    {coolSaving ? 'Salvando...' : 'Salvar escada'}
                  </button>
                  {coolDirty && !coolSaving && <span className="neo-note">Alteração não salva</span>}
                </div>

                {coolErr && <div className="neo-error">Erro: {coolErr}</div>}
                {coolOk && <div className="neo-ok">✓ {coolOk}</div>}
              </>
            )}
          </div>
        </div>

        {/* ── Comando manual ── */}
        <div className="neo-section">
          <h2 className="neo-h2">Comando manual</h2>
          <p className="neo-desc">
            <code>POST /api/siren/on</code>, <code>/off</code> e <code>/auto</code> passam por cima
            da detecção, da agenda e da chave geral. Enquanto houver override, a lógica automática
            fica <strong>suspensa</strong>. Nada disso vai pra flash: se o ESP32 reiniciar, ele
            volta sozinho para o automático.
          </p>

          <div className="neo-card">
            <div className="sirene-override">
              <div className="sirene-override-label">
                Controle:{' '}
                <span className={`sirene-override-badge${ov?.mod ?? ' is-unknown'}`}>
                  {ov?.label ?? (overrideUnsupported ? 'NÃO REPORTADO' : 'sem leitura')}
                </span>
              </div>
              <div className="neo-note" style={{ flex: 1, minWidth: 220 }}>
                {ov?.note ?? (live ? 'Aguardando /api/status.' : 'Polling pausado — o estado abaixo pode estar velho.')}
              </div>
            </div>

            {overrideUnsupported && (
              <div className="neo-warn is-amber" style={{ marginBottom: 16 }}>
                <Icon name="warn" size={14} />
                O <code>/api/status</code> deste ESP32 não traz <code>manualOverride</code> — sinal
                de firmware anterior ao comando manual. Os botões abaixo devem responder{' '}
                <strong>404</strong> até você regravar o device.
              </div>
            )}

            {override === 'forced_off' && (
              <div className="neo-warn" style={{ marginBottom: 16 }}>
                <Icon name="warn" size={14} />
                <span>
                  <strong>O alarme está mudo.</strong> Com <code>forced_off</code> a sirene não toca
                  nem com pessoa confirmada — e isso <em>não</em> aparece na chave geral do alarme,
                  que continua como está. Volte para o automático quando terminar.
                </span>
              </div>
            )}

            {armingOn ? (
              <div className="sirene-confirm">
                <Icon name="warn" size={14} />
                <span>
                  Isso liga a sirene <strong>de verdade</strong>, agora, e ela só para quando você
                  mandar desligar ou voltar ao automático. Confirma?
                </span>
                <div className="sirene-cmd" style={{ marginLeft: 'auto' }}>
                  <button
                    className="neo-btn-danger"
                    onClick={() => void sendOverride('on')}
                    disabled={overrideBusy !== null}
                  >
                    {overrideBusy === 'on' ? 'Ligando...' : 'Sim, ligar'}
                  </button>
                  <button className="neo-btn-ghost" onClick={() => setArmingOn(false)}>
                    Cancelar
                  </button>
                </div>
              </div>
            ) : (
              <div className="sirene-cmd">
                <button
                  className="neo-btn-danger"
                  onClick={() => { setArmingOn(true); setOverrideErr(null); setOverrideOk(null); }}
                  disabled={overrideBusy !== null}
                  title="POST /api/siren/on"
                >
                  Ligar sirene agora
                </button>
                <button
                  className="neo-btn-ghost"
                  onClick={() => void sendOverride('off')}
                  disabled={overrideBusy !== null}
                  title="POST /api/siren/off — para a sirene e trava o disparo automático"
                >
                  {overrideBusy === 'off' ? 'Desligando...' : 'Desligar e travar'}
                </button>
                <button
                  className="neo-btn-primary"
                  onClick={() => void sendOverride('auto')}
                  disabled={overrideBusy !== null || override === 'auto'}
                  title="POST /api/siren/auto — devolve o controle à detecção"
                >
                  {overrideBusy === 'auto' ? 'Voltando...' : 'Voltar ao automático'}
                </button>
              </div>
            )}

            {overrideErr && (
              <div className="neo-error">
                Erro: {overrideErr}
                {overrideErr.includes('404') && (
                  <>
                    {' '}— 404 aqui é firmware antigo: as rotas <code>/api/siren/*</code> só existem
                    depois de gravar a versão com o comando manual.
                  </>
                )}
              </div>
            )}
            {overrideOk && <div className="neo-ok">✓ {overrideOk}</div>}
          </div>
        </div>

        {/* ── Histórico de disparos ── */}
        <div className="neo-section">
          <h2 className="neo-h2">Histórico de disparos</h2>
          <p className="neo-desc">
            <code>GET /api/events?hours=N</code> — o firmware guarda os últimos{' '}
            {EVENT_CAPACITY} disparos num buffer em RAM, então a lista{' '}
            <strong>se perde no reboot</strong>. Um disparo só é registrado quando a sirene{' '}
            <strong>para</strong> de tocar: é o fim do toque que fecha a duração.
          </p>

          <div className="neo-card">
            <div className="sirene-events-bar">
              <div className="sirene-days" style={{ marginBottom: 0 }}>
                {EVENT_RANGES.map((r) => (
                  <button
                    key={r.hours}
                    className={`sirene-preset${eventHours === r.hours ? ' is-on' : ''}`}
                    onClick={() => setEventHours(r.hours)}
                    title={r.hours === 0 ? 'hours=0 — devolve o buffer inteiro' : `hours=${r.hours}`}
                  >
                    {r.label}
                  </button>
                ))}
              </div>
              <button className="neo-btn-ghost" onClick={() => void loadEvents()} disabled={loadingEvents}>
                <Icon name="refresh" size={14} />
                {loadingEvents ? 'Lendo...' : 'Atualizar'}
              </button>
            </div>

            {eventsErr ? (
              <div className="neo-warn">
                <Icon name="warn" size={14} />
                <span>
                  Não foi possível ler o histórico: {eventsErr}.
                  {eventsErr.includes('404') && (
                    <>
                      <br />
                      404 aqui é firmware antigo: <code>/api/events</code> só existe depois de
                      gravar a versão com o log de eventos.
                    </>
                  )}
                </span>
              </div>
            ) : !events ? (
              <div className="neo-muted">Lendo histórico...</div>
            ) : orderedEvents.length === 0 ? (
              <div className="neo-muted">
                Nenhum disparo {eventHours === 0 ? 'no buffer' : `nas últimas ${eventHours} h`}.
                {status?.sirenState === 'ringing' &&
                  ' A sirene está tocando agora — o registro entra quando ela parar.'}
              </div>
            ) : (
              <>
                <div className="sirene-events">
                  {orderedEvents.map((ev, i) => {
                    const when = describeWhen(ev.startUtc);
                    // 'both' pinta como burst (é o caminho mais quente) e já diz
                    // tudo no rótulo — as tags de "também" seriam redundantes.
                    const hot = ev.trigger === 'burst' || ev.trigger === 'both';
                    return (
                      <div className="sirene-event" key={`${ev.startUtc}-${i}`}>
                        <div className="sirene-event-when">
                          <span className="sirene-event-time">{when.abs}</span>
                          <span className="sirene-event-ago">{when.rel}</span>
                        </div>
                        <div className="sirene-event-dur">{formatDuration(Number(ev.durationMs))}</div>
                        <div className="sirene-event-tags">
                          <span className={`sirene-path-tag${hot ? ' is-burst' : ''}`}>
                            {TRIGGER_LABEL[ev.trigger] ?? String(ev.trigger || '?').toUpperCase()}
                          </span>
                          {ev.motion && <span className="sirene-event-flag is-motion">+ movimento</span>}
                          {ev.trigger === 'burst' && ev.confirm && (
                            <span className="sirene-event-flag">confirm também</span>
                          )}
                          {ev.trigger === 'confirm' && ev.burst && (
                            <span className="sirene-event-flag">burst também</span>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>

                <div className="neo-meta">
                  <span className="neo-meta-value">{orderedEvents.length}</span> disparo
                  {orderedEvents.length > 1 ? 's' : ''}{' '}
                  {eventHours === 0 ? 'no buffer' : `nas últimas ${eventHours} h`}
                  {' · '}
                  tempo total tocando:{' '}
                  <span className="neo-meta-value">{formatDuration(eventTotalMs)}</span>
                  {' · '}
                  último: <span className="neo-meta-value">{describeWhen(orderedEvents[0].startUtc).rel}</span>
                  {orderedEvents.length >= EVENT_CAPACITY && (
                    <div className="neo-meta-faint">
                      O buffer está cheio ({EVENT_CAPACITY} eventos) — o que for mais antigo que o
                      último item da lista já foi descartado.
                    </div>
                  )}
                </div>
              </>
            )}
          </div>
        </div>

        {/* ── Alarme ── */}
        <div className="neo-section">
          <h2 className="neo-h2">Alarme</h2>
          <p className="neo-desc">
            Chave geral. Desligado, o alarme <strong>nunca</strong> dispara — independente de
            agenda ou de detecção. A configuração é gravada na flash (NVS) e sobrevive a reboot.
          </p>

          <div className="neo-card">
            {!cfg ? (
              <div className="neo-warn">
                <Icon name="warn" size={14} />
                {cfgErr ? `Não foi possível ler a configuração: ${cfgErr}` : 'Lendo configuração...'}
              </div>
            ) : (
              <div className="neo-toggle-row">
                <div className="neo-toggle-label">
                  Alarme:{' '}
                  <span className={cfg.alarmEnabled ? 'neo-state-on' : 'neo-state-off'}>
                    {cfg.alarmEnabled ? 'HABILITADO' : 'DESABILITADO'}
                  </span>
                  {cfg.alarmEnabled && cfg.schedule?.enabled && status && !status.armed && (
                    <span className="neo-state-live">● fora da agenda agora</span>
                  )}
                  {/* Esta chave é persistida; o override não. Quem manda agora é o override. */}
                  {override === 'forced_off' && (
                    <span className="neo-state-live">● irrelevante: sirene travada em forced_off</span>
                  )}
                  {override === 'forced_on' && (
                    <span className="neo-state-live">● irrelevante: sirene forçada em forced_on</span>
                  )}
                </div>
                <button
                  className={`neo-toggle ${cfg.alarmEnabled ? 'is-on' : 'is-off'}`}
                  onClick={() => void toggleAlarm()}
                  disabled={alarmSaving}
                >
                  {alarmSaving ? '...' : cfg.alarmEnabled ? '● LIGADO' : '○ DESLIGADO'}
                </button>
              </div>
            )}
            {cfg && cfgErr && <div className="neo-error">Erro: {cfgErr}</div>}
          </div>
        </div>

        {/* ── Detecção ── */}
        <div className="neo-section">
          <h2 className="neo-h2">Detecção de pessoa</h2>
          <p className="neo-desc">
            O disparo só vem de eventos <code>IsPeople</code> da câmera — <code>IsMotion</code>{' '}
            sozinho é ignorado. Há dois caminhos independentes: <strong>CONFIRM</strong> (precisa de
            movimento simultâneo) e <strong>BURST</strong> (dispara sozinho, na rajada). Contagens
            maiores e janelas menores = menos falso positivo.
          </p>

          <div className="neo-card">
            {!cfg ? (
              <div className="neo-muted">Sem configuração carregada.</div>
            ) : (
              <>
                <div className="sirene-path">
                  <div className="sirene-path-tag">CONFIRM</div>
                  <div className="neo-note">
                    {people.confirmCount || '?'} eventos <code>IsPeople</code> em{' '}
                    {people.confirmWindowMs || '?'} ms <strong>+ movimento simultâneo</strong> → dispara
                  </div>
                </div>
                <div className="sirene-grid">
                  <div className="neo-field">
                    <label className="neo-field-label">Eventos (1–16)</label>
                    <input
                      className="neo-input" type="number" min={1} max={16}
                      value={people.confirmCount}
                      onChange={(e) => { setPeople((p) => ({ ...p, confirmCount: e.target.value })); setPeopleOk(null); }}
                    />
                  </div>
                  <div className="neo-field">
                    <label className="neo-field-label">Janela (ms, ≥ 500)</label>
                    <input
                      className="neo-input" type="number" min={500} step={500}
                      value={people.confirmWindowMs}
                      onChange={(e) => { setPeople((p) => ({ ...p, confirmWindowMs: e.target.value })); setPeopleOk(null); }}
                    />
                  </div>
                </div>

                <div className="sirene-path" style={{ marginTop: 6 }}>
                  <div className="sirene-path-tag is-burst">BURST</div>
                  <div className="neo-note">
                    {people.burstCount || '?'} eventos <code>IsPeople</code> em{' '}
                    {people.burstWindowMs || '?'} ms → dispara sozinho
                  </div>
                </div>
                <div className="sirene-grid">
                  <div className="neo-field">
                    <label className="neo-field-label">Eventos (1–16)</label>
                    <input
                      className="neo-input" type="number" min={1} max={16}
                      value={people.burstCount}
                      onChange={(e) => { setPeople((p) => ({ ...p, burstCount: e.target.value })); setPeopleOk(null); }}
                    />
                  </div>
                  <div className="neo-field">
                    <label className="neo-field-label">Janela (ms, ≥ 500)</label>
                    <input
                      className="neo-input" type="number" min={500} step={500}
                      value={people.burstWindowMs}
                      onChange={(e) => { setPeople((p) => ({ ...p, burstWindowMs: e.target.value })); setPeopleOk(null); }}
                    />
                  </div>
                </div>

                <div className="neo-actions">
                  <button className="neo-btn-primary" onClick={() => void savePeople()} disabled={peopleSaving || !peopleDirty}>
                    {peopleSaving ? 'Salvando...' : 'Salvar limiares'}
                  </button>
                  {peopleDirty && !peopleSaving && <span className="neo-note">Alteração não salva</span>}
                </div>

                {peopleErr && <div className="neo-error">Erro: {peopleErr}</div>}
                {peopleOk && <div className="neo-ok">✓ {peopleOk}</div>}
              </>
            )}
          </div>
        </div>

        {/* ── Agenda ── */}
        <div className="neo-section">
          <h2 className="neo-h2">Agenda de monitoramento</h2>
          <p className="neo-desc">
            Com a agenda <strong>desligada</strong>, o alarme fica ativo o tempo todo (desde que
            habilitado). Ligada, ele só arma dentro dos slots — e basta <strong>um</strong> slot
            valer no momento. Máximo de {MAX_SLOTS} slots; salvar substitui a lista inteira.
          </p>

          <div className="neo-card">
            {!sched ? (
              <div className="neo-muted">Sem configuração carregada.</div>
            ) : (
              <>
                <div className="neo-toggle-row" style={{ marginBottom: 22 }}>
                  <div className="neo-toggle-label">
                    Agenda:{' '}
                    <span className={sched.enabled ? 'neo-state-on' : 'neo-state-off'}>
                      {sched.enabled ? 'ATIVA' : 'IGNORADA'}
                    </span>
                  </div>
                  <button
                    className={`neo-toggle ${sched.enabled ? 'is-on' : 'is-off'}`}
                    onClick={() => { setSched({ ...sched, enabled: !sched.enabled }); setSchedOk(null); }}
                  >
                    {sched.enabled ? '● ATIVA' : '○ IGNORADA'}
                  </button>
                </div>

                <div className="neo-field">
                  <label className="neo-field-label">Fuso horário do device</label>
                  <div className="neo-select-wrap">
                    <select
                      className="neo-select"
                      value={sched.utcOffsetMinutes}
                      onChange={(e) => { setSched({ ...sched, utcOffsetMinutes: Number(e.target.value) }); setSchedOk(null); }}
                    >
                      {!offsetKnown && (
                        <option value={sched.utcOffsetMinutes}>
                          Personalizado ({sched.utcOffsetMinutes} min)
                        </option>
                      )}
                      {OFFSETS.map((o) => (
                        <option key={o.value} value={o.value}>{o.label}</option>
                      ))}
                    </select>
                    <span className="neo-chevron">▼</span>
                  </div>
                  <div className="neo-note" style={{ marginTop: 8 }}>
                    O ESP32 sincroniza o relógio em UTC pela câmera; o offset é o que converte os
                    horários abaixo (que são <strong>locais</strong>) pra hora do device.
                  </div>
                </div>

                <div className="sirene-slots">
                  {sched.slots.length === 0 && (
                    <div className="neo-warn is-amber">
                      <Icon name="warn" size={14} />
                      Nenhum slot. Com a agenda ativa, isso significa que o alarme{' '}
                      <strong>nunca</strong> dispara.
                    </div>
                  )}

                  {sched.slots.map((slot, i) => (
                    <div className="sirene-slot" key={i}>
                      <div className="sirene-slot-head">
                        <span className="sirene-slot-n">Slot {i + 1}</span>
                        <button
                          className="sirene-slot-del"
                          onClick={() => {
                            setSched({ ...sched, slots: sched.slots.filter((_, j) => j !== i) });
                            setSchedOk(null);
                          }}
                          title="Remover slot"
                        >
                          Remover
                        </button>
                      </div>

                      <div className="sirene-days">
                        {DAYS.map((d) => (
                          <button
                            key={d.bit}
                            className={`sirene-day${(slot.days & d.bit) !== 0 ? ' is-on' : ''}`}
                            title={d.full}
                            onClick={() => patchSlot(i, { days: slot.days ^ d.bit })}
                          >
                            {d.short}
                          </button>
                        ))}
                        <span className="sirene-day-sep" />
                        {DAY_PRESETS.map((p) => (
                          <button
                            key={p.label}
                            className={`sirene-preset${slot.days === p.value ? ' is-on' : ''}`}
                            onClick={() => patchSlot(i, { days: p.value })}
                          >
                            {p.label}
                          </button>
                        ))}
                      </div>

                      <div className="sirene-times">
                        <div className="neo-field" style={{ marginBottom: 0 }}>
                          <label className="neo-field-label">Início</label>
                          <input
                            className="neo-input" type="time"
                            value={toTime(slot.startHour, slot.startMinute)}
                            onChange={(e) => {
                              const { h, m } = fromTime(e.target.value);
                              patchSlot(i, { startHour: h, startMinute: m });
                            }}
                          />
                        </div>
                        <div className="neo-field" style={{ marginBottom: 0 }}>
                          <label className="neo-field-label">Fim</label>
                          <input
                            className="neo-input" type="time"
                            value={toTime(slot.endHour, slot.endMinute)}
                            onChange={(e) => {
                              const { h, m } = fromTime(e.target.value);
                              patchSlot(i, { endHour: h, endMinute: m });
                            }}
                          />
                        </div>
                      </div>

                      <div className="neo-meta" style={{ marginTop: 12 }}>
                        <span className="neo-meta-value">{describeDays(slot.days)}</span>, {describeWindow(slot)}
                        <span className="neo-meta-faint">days = {slot.days}</span>
                      </div>
                    </div>
                  ))}
                </div>

                <div className="neo-actions" style={{ marginTop: 18 }}>
                  <button
                    className="neo-btn-ghost"
                    onClick={() => { setSched({ ...sched, slots: [...sched.slots, { ...NEW_SLOT }] }); setSchedOk(null); }}
                    disabled={sched.slots.length >= MAX_SLOTS}
                    title={sched.slots.length >= MAX_SLOTS ? `Máximo de ${MAX_SLOTS} slots` : undefined}
                  >
                    + Adicionar slot
                  </button>
                  <button className="neo-btn-primary" onClick={() => void saveSchedule()} disabled={schedSaving}>
                    {schedSaving ? 'Salvando...' : 'Salvar agenda'}
                  </button>
                </div>

                {schedErr && <div className="neo-error">Erro: {schedErr}</div>}
                {schedOk && <div className="neo-ok">✓ {schedOk}</div>}
              </>
            )}
          </div>
        </div>

        {/* ── Referência ── */}
        <div className="neo-section">
          <h2 className="neo-h2">Como a sirene se comporta</h2>
          <p className="neo-desc">
            Os tempos do <em>toque</em> são compilados no firmware e não vêm pela API — estão aqui
            só como referência do que esperar depois do disparo. O silêncio que vem <em>depois</em>
            do toque é a única parte configurável, e mora na seção acima.
          </p>
          <div className="neo-card">
            <ul className="sirene-ref">
              <li>O relé <strong>segue ativo</strong> enquanto continuarem chegando eventos de pessoa ou movimento.</li>
              <li>Desliga após <strong>10 s</strong> (<code>SIREN_MIN_ON_MS</code>) sem evento novo.</li>
              <li>Teto absoluto de <strong>5 min</strong> tocando (<code>SIREN_MAX_ON_MS</code>).</li>
              <li>
                Aí entra o <strong>silêncio escalonado</strong>: não redispara antes de ele acabar,
                e ele cresce a cada disparo seguido. Diferente dos itens acima, esse é configurável
                (<code>cooldown</code> em <code>/api/config</code>) e pode ser encerrado na mão.
              </li>
              <li>
                Nada disso vale sob <strong>comando manual</strong>: em <code>forced_on</code> a
                sirene ignora os tempos e só para por comando, e em <code>forced_off</code> ela não
                liga. O override <strong>não sobrevive a reboot</strong>.
              </li>
            </ul>
          </div>
        </div>
      </div>
    </Screen>
  );
};

export default SirenePanel;

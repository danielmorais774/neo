import express from 'express';
import pino from 'pino';
import wppconnect from '@wppconnect-team/wppconnect';
import { mkdirSync, existsSync, rmSync } from 'fs';
import { join } from 'path';
import { execSync } from 'child_process';

const logger = pino({
  level: process.env.LOG_LEVEL || 'info',
  transport: process.env.NODE_ENV !== 'production'
    ? { target: 'pino-pretty', options: { colorize: true } }
    : undefined,
});

const PORT = parseInt(process.env.PORT || '3030', 10);
const TOKEN_DIR = process.env.TOKEN_DIR || '/app/auth/venom';
const SESSION = process.env.SESSION_NAME || 'neo';
const NEO_CORE_WEBHOOK_URL = process.env.NEO_CORE_WEBHOOK_URL || 'http://neo-core:8000';
const NEO_CORE_WEBHOOK_SECRET = process.env.WHATSAPP_WEBHOOK_SECRET || 'dev-secret';
const CHROMIUM_PATH = process.env.PUPPETEER_EXECUTABLE_PATH || '/usr/bin/chromium';

mkdirSync(TOKEN_DIR, { recursive: true });

const AUTH_SUCCESS_STATES = new Set([
  'isLogged', 'inChat', 'qrReadSuccess', 'chatsAvailable', 'successChat',
]);
// notLogged always means "needs QR" — safe to downgrade from any state.
// The transient inChat→notLogged sequence during QR boot is handled correctly
// because catchQR fires after and sets 'qr_ready'.
const DISCONNECTED_STATES = new Set([
  'notLogged', 'browserClose', 'qrReadFail', 'autocloseCalled',
  'desconnectedMobile', 'serverClose', 'deviceNotConnected', 'noOpenBrowser',
]);

let _client = null;
let _currentQr = null;
let _qrAttempts = 0;
let _connectionState = 'starting';
let _starting = false;
let _offlineTimer = null;

// Keep the linked WhatsApp Web session reported as offline/unavailable.
// WhatsApp silences push notifications on the PHONE whenever a linked device is
// "online/active" (it assumes you'll read on the computer). Since this runs in a
// headless Chrome that is always "focused", WhatsApp thinks the user is actively
// on web and delivers phone notifications silently. Marking presence unavailable
// makes the phone notify normally again. The socket stays connected either way,
// so we still receive messages via onMessage.
async function markWebOffline(client) {
  try {
    if (typeof client.setOnlinePresence === 'function') {
      await client.setOnlinePresence(false);
    } else if (typeof client.sendPresenceUnavailable === 'function') {
      await client.sendPresenceUnavailable();
    }
  } catch (e) {
    logger.warn({ err: e.message }, 'markWebOffline failed');
  }
}

// WA-JS re-broadcasts "available" presence on its own, so re-assert offline on a
// timer while connected.
function startOfflineKeepAlive(client) {
  if (_offlineTimer) clearInterval(_offlineTimer);
  markWebOffline(client);
  _offlineTimer = setInterval(() => {
    if (_client && _connectionState === 'connected') markWebOffline(_client);
  }, 30000);
}

async function emitWebhook(payload) {
  try {
    const res = await fetch(`${NEO_CORE_WEBHOOK_URL}/api/v1/whatsapp/webhook`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Webhook-Secret': NEO_CORE_WEBHOOK_SECRET,
      },
      body: JSON.stringify(payload),
    });
    if (!res.ok) logger.warn({ status: res.status, payload }, 'Webhook delivery failed');
  } catch (err) {
    logger.error({ err: err.message }, 'Webhook emit error');
  }
}

function normalizeQr(raw) {
  if (!raw) return null;
  if (typeof raw !== 'string') return null;
  return raw.startsWith('data:') ? raw : `data:image/png;base64,${raw}`;
}

// wppconnect.create() failing used to be terminal: the catch below set
// _connectionState='error' and nothing ever called startClient() again — it
// runs only on boot and on POST /qr/refresh. On 2026-09-16 an auto-close threw
// during init and the gateway sat idle for 8 days answering 503 on /chats while
// `docker ps` still said "Up". Retry with backoff so a transient Chromium or
// phone-sync failure heals on its own.
const RETRY_BASE_MS = 30000;
const RETRY_MAX_MS = 15 * 60 * 1000;
const RETRY_AFTER_DEAD_MS = 5 * 60 * 1000;
// Waiting on a QR scan is not a failure — only these mean nobody is coming back.
const DEAD_STATES = new Set(['error', 'disconnected']);

let _retryTimer = null;
let _retryAttempt = 0;
let _deadSince = null;

function scheduleRestart(reason) {
  if (_retryTimer || _starting) return;
  const delay = Math.min(RETRY_BASE_MS * 2 ** _retryAttempt, RETRY_MAX_MS);
  _retryAttempt += 1;
  logger.warn({ reason, delayMs: delay, attempt: _retryAttempt }, 'Scheduling wppconnect restart');
  _retryTimer = setTimeout(() => {
    _retryTimer = null;
    startClient();
  }, delay);
}

// The client can also go quiet without throwing (onStateChange DISCONNECTED,
// statusFind browserClose). Those never reach the catch, so poll for a state
// that stayed dead and hand it to the same backoff.
function startWatchdog() {
  setInterval(() => {
    if (_starting || _retryTimer) return;
    if (!DEAD_STATES.has(_connectionState)) {
      _deadSince = null;
      if (_connectionState === 'connected') _retryAttempt = 0;
      return;
    }
    if (_deadSince === null) {
      _deadSince = Date.now();
      return;
    }
    if (Date.now() - _deadSince >= RETRY_AFTER_DEAD_MS) {
      _deadSince = null;
      scheduleRestart(`state '${_connectionState}' held for ${RETRY_AFTER_DEAD_MS / 60000}min`);
    }
  }, 60000);
}

async function startClient() {
  if (_starting) {
    logger.warn('startClient called while already starting; ignoring');
    return;
  }
  _starting = true;
  _connectionState = 'starting';
  _currentQr = null;
  logger.info({ TOKEN_DIR, SESSION }, 'Starting wppconnect...');

  // Chromium leaves Singleton* lock files when a container dies hard.
  // Use find -delete so it handles symlinks and nested paths reliably.
  try {
    execSync(
      `find /app/auth -name "Singleton*" -delete -print && find /app/auth -name "DevToolsActivePort*" -delete -print`,
      { stdio: 'pipe', timeout: 5000 },
    );
    logger.info('Stale Chromium locks cleared');
  } catch (e) {
    logger.warn({ err: e.message }, 'Lock cleanup warning (non-fatal)');
  }

  try {
    const client = await wppconnect.create({
      session: SESSION,
      folderNameToken: TOKEN_DIR,
      headless: 'new',
      devtools: false,
      useChrome: false,
      debug: false,
      disableSpins: true,
      disableWelcome: true,
      logQR: false,
      autoClose: 0,
      // `autoClose: 0` only disables the QR-phase timer. After the QR phase
      // wppconnect arms a SECOND one — host.layer.js does
      // `startAutoClose(this.options.deviceSyncTimeout)` (default 180000) and
      // startAutoClose only checks `time > 0`, so it ignores autoClose. When
      // the phone takes longer than that to sync it fires "Auto Close Called",
      // the browser closes and the session is gone. That killed the session on
      // 2026-09-16 and it stayed dead for 8 days. 0 disables it too.
      deviceSyncTimeout: 0,
      updatesLog: false,
      browserArgs: [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--disable-gpu',
        '--no-zygote',
        '--no-first-run',
      ],
      puppeteerOptions: {
        executablePath: CHROMIUM_PATH,
      },
      browserPathExecutable: CHROMIUM_PATH,
      catchQR: (base64Qr, _ascii, attempts) => {
        _currentQr = normalizeQr(base64Qr);
        _qrAttempts = attempts;
        _connectionState = 'qr_ready';
        logger.info({ attempts }, 'QR captured');
      },
      statusFind: (statusSession, session) => {
        logger.info({ statusSession, session }, 'Status change');
        if (AUTH_SUCCESS_STATES.has(statusSession)) {
          _connectionState = 'connected';
          _currentQr = null;
        } else if (DISCONNECTED_STATES.has(statusSession)) {
          _connectionState = 'disconnected';
        }
      },
    });

    _client = client;
    logger.info('wppconnect client ready');

    // If statusFind already set connected, we're good; otherwise check via client.
    if (_connectionState !== 'connected') {
      try {
        const isConn = await client.isConnected();
        if (isConn) { _connectionState = 'connected'; _currentQr = null; }
        logger.info({ isConn, state: _connectionState }, 'Post-create connection check');
      } catch (e) {
        logger.warn({ err: e.message }, 'isConnected check failed');
      }
    }

    // Keep phone notifications loud: report this web session as offline.
    startOfflineKeepAlive(client);

    client.onStateChange((state) => {
      logger.info({ state }, 'WA state change');
      // wppconnect emits: CONNECTED, OPENING, PAIRING, UNPAIRED, CONFLICT, TIMEOUT, etc.
      if (['CONNECTED', 'MAIN'].includes(state)) {
        _connectionState = 'connected';
        _currentQr = null;
        startOfflineKeepAlive(client);
      } else if (['CONFLICT', 'UNPAIRED', 'UNLAUNCHED'].includes(state)) {
        client.useHere().catch((err) => logger.warn({ err: err.message }, 'useHere failed'));
      } else if (['DISCONNECTED', 'TIMEOUT', 'UNPAIRED_IDLE'].includes(state)) {
        _connectionState = 'disconnected';
      }
    });

    client.onMessage(async (message) => {
      if (message.fromMe) return;
      if (!message.body && !message.caption) return;
      await emitWebhook({
        jid: message.from,
        text: message.body || message.caption || '',
        message_id: message.id,
        timestamp: message.timestamp,
      });
    });
  } catch (err) {
    logger.error({ err: err.message, stack: err.stack }, 'wppconnect init failed');
    _connectionState = 'error';
    // Drop the half-open client so the next attempt starts from a clean
    // Chromium (startClient clears the Singleton locks it leaves behind).
    if (_client) {
      try { await _client.close(); } catch {}
      _client = null;
    }
  } finally {
    _starting = false;
    if (_connectionState === 'error') scheduleRestart('init failed');
  }
}

const app = express();
app.use(express.json({ limit: '2mb' }));

app.get('/health', (_req, res) => {
  res.json({
    status: 'ok',
    connected: _connectionState === 'connected',
    state: _connectionState,
  });
});

app.get('/qr', (_req, res) => {
  res.json({
    qr: _currentQr,
    state: _connectionState,
    attempts: _qrAttempts,
  });
});

app.post('/qr/refresh', async (_req, res) => {
  logger.info('QR refresh requested — wiping tokens and restarting');
  if (_client) {
    try { await _client.logout(); } catch (e) { logger.warn({ err: e.message }, 'logout failed'); }
    try { await _client.close(); } catch (e) { logger.warn({ err: e.message }, 'close failed'); }
  }
  _client = null;
  _currentQr = null;
  _connectionState = 'starting';

  const tokenFolder = join(TOKEN_DIR, SESSION);
  if (existsSync(tokenFolder)) {
    try { rmSync(tokenFolder, { recursive: true, force: true }); }
    catch (e) { logger.warn({ err: e.message }, 'Token cleanup warning'); }
  }

  startClient();
  res.json({ ok: true });
});

function ensureReady(res) {
  if (!_client || _connectionState !== 'connected') {
    res.status(503).json({ error: 'Client not connected', state: _connectionState });
    return false;
  }
  return true;
}

function toJid(value) {
  if (!value) return null;
  return value.includes('@') ? value : `${value.replace(/\D/g, '')}@c.us`;
}

// Os dois números vêm só do ambiente (docker-compose → .env). Sem padrão no
// código: um clone mal configurado não pode mandar mensagem para ninguém.
function requiredPhone(name) {
  const digits = (process.env[name] || '').replace(/\D/g, '');
  if (!digits) {
    console.error(`[neo-whatsapp] defina ${name} no .env (só dígitos, com DDI)`);
    process.exit(1);
  }
  return digits;
}

// Único número com quem o Neo conversa.
const ALLOWED_PHONE = requiredPhone('ALLOWED_WHATSAPP_PHONE');

// Destino fixo do endpoint /notify.
const NOTIFY_PHONE = requiredPhone('NOTIFY_WHATSAPP_PHONE');

function isAllowedJid(jid) {
  if (!jid) return false;
  const digits = jid.replace(/\D/g, '');
  return digits === ALLOWED_PHONE || digits === `+${ALLOWED_PHONE}`;
}

app.post('/send', async (req, res) => {
  const { jid, text } = req.body || {};
  if (!jid || !text) return res.status(400).json({ error: 'jid and text required' });
  if (!ensureReady(res)) return;
  logger.info({ jid, digits: jid.replace(/\D/g, ''), allowed: ALLOWED_PHONE }, 'DEBUG send attempt');
  if (!isAllowedJid(jid)) {
    logger.warn({ jid }, 'Wholesale dev block: rejected send to non-allowed number');
    return res.status(403).json({ error: `Envio bloqueado: número ${jid} não autorizado (permitido: ${ALLOWED_PHONE})` });
  }
  try {
    const result = await _client.sendText(toJid(jid), text);
    res.json({ message_id: result?.id || result?.to?._serialized || 'sent' });
  } catch (err) {
    logger.error({ err: err.message, jid }, 'sendText failed');
    res.status(500).json({ error: err.message });
  }
});

// POST /notify — sends the request body's text to the single fixed contact
// (NOTIFY_PHONE) and nobody else. Body: { text }.
app.post('/notify', async (req, res) => {
  const { text } = req.body || {};
  if (!text) return res.status(400).json({ error: 'text required' });
  if (!ensureReady(res)) return;
  try {
    // Resolve the real serialized JID (handles the BR 9th-digit quirk),
    // falling back to the naive conversion if lookup fails.
    let target = toJid(NOTIFY_PHONE);
    try {
      const status = await _client.checkNumberStatus(target);
      if (status?.id?._serialized) target = status.id._serialized;
    } catch (e) {
      logger.warn({ err: e.message }, 'notify: checkNumberStatus failed, using raw jid');
    }
    const result = await _client.sendText(target, text);
    res.json({ jid: target, message_id: result?.id || result?.to?._serialized || 'sent' });
  } catch (err) {
    logger.error({ err: err.message }, 'notify sendText failed');
    res.status(500).json({ error: err.message });
  }
});

app.post('/lookup', async (req, res) => {
  const { phone_e164 } = req.body || {};
  if (!phone_e164) return res.status(400).json({ error: 'phone_e164 required' });
  if (!ensureReady(res)) return;
  try {
    const target = toJid(phone_e164);
    const status = await _client.checkNumberStatus(target);
    res.json({
      jid: status?.id?._serialized || target,
      exists: !!status?.numberExists,
    });
  } catch (err) {
    logger.error({ err: err.message }, 'lookup failed');
    res.status(500).json({ error: err.message });
  }
});

app.get('/chats', async (_req, res) => {
  if (!ensureReady(res)) return;

  // Wait for WhatsApp Web to fully populate the in-memory chat store.
  // The session may be CONNECTED and receiving messages via websocket,
  // but the local ChatStore list may still be empty. Poll until it has data.
  let storeReady = false;
  let attempts = 0;
  for (let i = 0; i < 6; i++) {
    try {
      const info = await _client.page.evaluate(() => {
        try {
          const W = window.WPP?.chat;
          const S = window.Store?.Chat;
          const wppChats = W?.list ? W.list({}) : null;
          return {
            wpp_count: Array.isArray(wppChats) ? wppChats.length : -1,
            store_count: S?.getModelsArray ? S.getModelsArray().length : -1,
            store_ready: !!(S?.getModelsArray && S.getModelsArray().length > 0),
            wpp_ready: !!(Array.isArray(wppChats) && wppChats.length > 0),
          };
        } catch (e) { return { error: e.message }; }
      });
      logger.info({ attempt: i, info }, 'Store readiness check');
      if (info.store_ready || info.wpp_ready) { storeReady = true; break; }
    } catch (e) { logger.warn({ err: e.message, attempt: i }, 'Store check failed'); }
    if (i < 5) await new Promise(r => setTimeout(r, 5000));
  }

  let raw = null;

  try {
    raw = await Promise.race([
      _client.listChats(),
      new Promise((_, reject) => setTimeout(() => reject(new Error('listChats timeout')), 15000)),
    ]);
    if (!Array.isArray(raw)) raw = null;
  } catch (_) {}

  if (raw === null || raw.length === 0) {
    try {
      raw = await Promise.race([
        _client.getAllChats(),
        new Promise((_, reject) => setTimeout(() => reject(new Error('getAllChats timeout')), 15000)),
      ]);
    } catch (_) {}
  }

  // Direct Store query as last resort
  if (!Array.isArray(raw) || raw.length === 0) {
    try {
      const storeChats = await _client.page.evaluate(() => {
        try {
          const W = window.WPP?.chat;
          if (W?.list) {
            const all = W.list({});
            if (Array.isArray(all) && all.length > 0) {
              return all.map(c => ({ id: c.id?._serialized || c.id, name: c.name || c.formattedTitle || c.id?.user, unreadCount: c.unreadCount || 0 }));
            }
          }
          const S = window.Store?.Chat;
          if (S?.getModelsArray) {
            return S.getModelsArray().map(c => ({ id: c.id?._serialized || c.id, name: c.name || c.formattedTitle || c.id?.user, unreadCount: c.unreadCount || 0 }));
          }
          return [];
        } catch (e) { return { error: e.message }; }
      });
      if (Array.isArray(storeChats) && storeChats.length > 0) raw = storeChats;
      logger.info({ raw_len: Array.isArray(raw) ? raw.length : 'not_array' }, 'Store direct result');
    } catch (e) {
      logger.warn({ err: e.message }, 'Store direct query failed');
    }
  }

  res.json({
    chats: Array.isArray(raw)
      ? raw
          .filter(c => c.id && (c.id.user || c.id._serialized || (typeof c.id === 'string' && c.id.includes('@'))))
          .map(c => ({
            jid: c.id?._serialized || c.id,
            name: c.name || c.formattedTitle || c.contact?.pushname || c.id?.user || (typeof c.id === 'string' ? c.id.split('@')[0] : 'Unknown'),
            unread: c.unreadCount || 0,
          }))
      : [],
  });
});

function foldAccents(str) {
  return (str || '')
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase();
}

app.get('/contacts', async (req, res) => {
  if (!ensureReady(res)) return;
  const q = (req.query.q || '').toString().trim();
  const limit = Math.min(parseInt(req.query.limit, 10) || 500, 2000);
  const onlyMine = (req.query.only_saved || '').toString() === '1';

  try {
    let contacts = [];
    try {
      contacts = (await _client.getAllContacts()) || [];
    } catch (e) {
      logger.warn({ err: e.message }, 'getAllContacts failed, falling back to chats');
    }

    const normalized = contacts
      .map(c => {
        const id = c.id?._serialized || c.id || null;
        if (!id || (typeof id === 'string' && !id.includes('@'))) return null;
        const name = c.name || c.formattedName || c.pushname || c.shortName || c.verifiedName || null;
        const user = c.id?.user || (typeof id === 'string' ? id.split('@')[0] : null);
        return {
          jid: id,
          name: name || (user ? `+${user}` : id),
          phone_e164: user && /^\d+$/.test(user) ? `+${user}` : null,
          is_my_contact: !!c.isMyContact,
          is_wa_contact: c.isWAContact !== false,
          is_group: typeof id === 'string' && id.endsWith('@g.us'),
        };
      })
      .filter(c => c && !c.is_group && c.is_wa_contact);

    let filtered = onlyMine ? normalized.filter(c => c.is_my_contact) : normalized;

    if (q) {
      const needle = foldAccents(q);
      const stop = new Set(['minha','meu','para','pra','pro','o','a','do','da','no','na','de']);
      const tokens = needle.split(/\s+/).filter(t => t && !stop.has(t));
      filtered = filtered
        .map(c => {
          const hayName = foldAccents(c.name || '');
          const hayPhone = foldAccents(c.phone_e164 || '');
          let score = 0;
          for (const t of tokens) {
            if (!t) continue;
            if (hayName === t) score += 100;
            else if (hayName.startsWith(t)) score += 60;
            else if (hayName.includes(` ${t}`) || hayName.includes(t)) score += 30;
            if (hayPhone.includes(t)) score += 20;
          }
          if (c.is_my_contact) score += 5;
          return { ...c, _score: score };
        })
        .filter(c => c._score > 0)
        .sort((a, b) => b._score - a._score);
    }

    res.json({ contacts: filtered.slice(0, limit) });
  } catch (err) {
    logger.error({ err: err.message }, '/contacts failed');
    res.status(500).json({ error: err.message });
  }
});

app.get('/chats/:jid/messages', async (req, res) => {
  if (!ensureReady(res)) return;
  const target = toJid(decodeURIComponent(req.params.jid));
  const limit = Math.min(parseInt(req.query.limit, 10) || 100, 500);
  const before = req.query.before || null;

  try {
    let msgs = [];

    if (before) {
      // Cursor-based: load messages older than a given message id
      const older = await _client.page.evaluate((chatId, beforeMsgId) => {
        try {
          const msg = window.Store.Msg.get(beforeMsgId);
          if (!msg) return { msgs: [], hasMore: false };
          const results = window.Store.MessageCollection.query({ before: msg });
          return {
            msgs: (results || []).map(m => ({
              id: m.id?._serialized || m.id,
              from: m.from?._serialized || m.from,
              body: m.body || m.caption || '',
              timestamp: m.timestamp || m.t,
              fromMe: m.fromMe || false,
            })),
            hasMore: (results || []).length > 0,
          };
        } catch (e) { return { msgs: [], hasMore: false }; }
      }, target, before);
      msgs = older?.msgs || [];
    } else {
      // Fresh load: try loadAndGetAllMessagesInChat first
      // It returns newest-first — reverse to get oldest-first (correct chronological order)
      try {
        msgs = (await _client.loadAndGetAllMessagesInChat(target, true, false)) || [];
      } catch (_) {
        try {
          msgs = (await _client.getAllMessagesInChat(target, true, false)) || [];
        } catch (_) {}
      }
      if (msgs.length > 0) msgs.reverse();
    }

    // Ensure chronological order for all fresh-load paths (some WA sessions return newest-first)
    if (!before) {
      msgs.sort((a, b) => a.timestamp - b.timestamp);
    }

    // If we still have room, pad via Store cursor in a loop
    while (msgs.length < limit) {
      const oldest = msgs[0];
      if (!oldest?.id) break;

      const older = await _client.page.evaluate((chatId, oldestMsgId) => {
        try {
          const msg = window.Store.Msg.get(oldestMsgId);
          if (!msg) return { msgs: [], hasMore: false };
          const results = window.Store.MessageCollection.query({ before: msg });
          return {
            msgs: (results || []).map(m => ({
              id: m.id?._serialized || m.id,
              from: m.from?._serialized || m.from,
              body: m.body || m.caption || '',
              timestamp: m.timestamp || m.t,
              fromMe: m.fromMe || false,
            })),
            hasMore: (results || []).length > 0,
          };
        } catch (e) {
          return { msgs: [], hasMore: false };
        }
      }, target, oldest.id);

      if (!older?.msgs?.length) break;
      msgs = older.msgs.reverse().concat(msgs);
    }

    res.json({
      messages: msgs.slice(-limit).map(m => ({
        id: m.id?._serialized || m.id,
        from: m.from?._serialized || m.from,
        body: m.body || m.caption || '',
        caption: m.caption || null,
        mediaType: (m.type && ['image','video','audio','document','ptt','sticker','vcard'].includes(m.type)) ? m.type : null,
        mimeType: m.mimetype || null,
        fileName: m.filename || null,
        timestamp: m.timestamp || m.t,
        fromMe: m.fromMe || false,
      })),
    });
  } catch (err) {
    logger.error({ err: err.message }, 'get messages failed');
    res.status(500).json({ error: err.message });
  }
});

// GET /chats/:jid/media/:msgId — download media for a message, returns { url }
// msgId comes double-encoded from the panel via encodeURIComponent, so decode it
app.get('/chats/:jid/media/:msgId', async (req, res) => {
  if (!ensureReady(res)) return;
  const msgId = decodeURIComponent(req.params.msgId);
  try {
    const url = await _client.downloadMedia(msgId);
    res.json({ url });
  } catch (err) {
    logger.error({ err: err.message, msgId }, 'downloadMedia failed');
    res.status(500).json({ error: err.message });
  }
});

app.listen(PORT, () => {
  logger.info({ port: PORT, tokenDir: TOKEN_DIR }, 'HTTP server listening');
  startClient();
  startWatchdog();
});

async function shutdown(signal) {
  logger.info({ signal }, 'Shutting down');
  if (_client) {
    try { await _client.close(); } catch {}
  }
  process.exit(0);
}
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));

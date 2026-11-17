const NATIVE_HOST_NAME = "com.ifuryst.open_browser_use.extension";
const NATIVE_HOST_STATUS_KEY = "OPEN_BROWSER_USE_NATIVE_HOST_STATUS";
const SESSION_STATE_KEY = "OPEN_BROWSER_USE_SESSION_STATE";
const RECONNECT_ALARM_NAME = "open-browser-use-native-reconnect";
const HEARTBEAT_ALARM_NAME = "open-browser-use-heartbeat";
const DEFAULT_CDP_TIMEOUT_MS = 10_000;
const CURSOR_ARRIVAL_TIMEOUT_MS = 1_000;
const MAX_USER_TABS = 1000;
const DEFAULT_SESSION_GROUP_TITLE = "Task - OBU";
const DELIVERABLE_GROUP_TITLE = "✅ Open Browser Use";
const LEGACY_SESSION_GROUP_TITLE_PATTERN = /^Open Browser Use [0-9a-f]{8}$/i;

class JsonRpcPeer {
  constructor(transport, handlers) {
    this.transport = transport;
    this.handlers = handlers;
    this.transport.setMessageCallback((message) => {
      void this.handleMessage(message);
    });
  }

  async handleMessage(message) {
    if (!message || typeof message !== "object" || typeof message.method !== "string") {
      return;
    }
    const id = message.id;
    try {
      const handler = this.handlers[message.method];
      if (typeof handler !== "function") {
        throw new Error(`No handler registered for method: ${message.method}`);
      }
      const result = await handler.call(this.handlers, message.params ?? {});
      if (id !== undefined) {
        this.transport.sendMessage({ jsonrpc: "2.0", id, result: result ?? {} });
      }
    } catch (error) {
      if (id !== undefined) {
        this.transport.sendMessage({
          jsonrpc: "2.0",
          id,
          error: {
            code: -32000,
            message: error instanceof Error ? error.message : String(error)
          }
        });
      }
    }
  }

  sendNotification(method, params) {
    this.transport.sendMessage({ jsonrpc: "2.0", method, params });
  }
}

class NativeTransport {
  constructor(hostName) {
    this.hostName = hostName;
    this.port = null;
    this.messageCallback = null;
    this.reconnectAttempt = 0;
    this.status = {
      hostName: this.hostName,
      lastChecked: Date.now(),
      reconnectAttempt: this.reconnectAttempt,
      state: "disconnected"
    };
    this.connect();
    chrome.alarms.onAlarm.addListener((alarm) => {
      if (alarm.name === RECONNECT_ALARM_NAME && !this.port) {
        this.connect();
      }
    });
  }

  setMessageCallback(callback) {
    this.messageCallback = callback;
  }

  sendMessage(message) {
    if (!this.port) {
      this.scheduleReconnect("native host disconnected");
      throw new Error("Native host is disconnected");
    }
    this.port.postMessage(message);
  }

  connect() {
    if (this.port) {
      return;
    }
    try {
      const port = chrome.runtime.connectNative(this.hostName);
      this.port = port;
      this.reconnectAttempt = 0;
      this.setStatus({ state: "connected" });
      port.onMessage.addListener((message) => {
        this.messageCallback?.(message);
      });
      port.onDisconnect.addListener(() => {
        this.port = null;
        this.scheduleReconnect(chrome.runtime.lastError?.message ?? "native host disconnected");
      });
    } catch (error) {
      this.port = null;
      this.scheduleReconnect(error instanceof Error ? error.message : String(error));
    }
  }

  scheduleReconnect(error) {
    this.reconnectAttempt += 1;
    this.setStatus({
      state: "reconnecting",
      error,
      nextRetryMs: 5000,
      reconnectAttempt: this.reconnectAttempt
    });
    chrome.alarms.create(RECONNECT_ALARM_NAME, { periodInMinutes: 0.1 }).catch(() => {});
  }

  setStatus(status) {
    this.status = {
      hostName: this.hostName,
      lastChecked: Date.now(),
      reconnectAttempt: this.reconnectAttempt,
      ...status
    };
    chrome.storage.local
      .set({
        [NATIVE_HOST_STATUS_KEY]: this.status
      })
      .catch(() => {});
  }

  getStatus() {
    return this.status;
  }
}

class SessionStore {
  constructor() {
    this.state = { sessions: {}, deliverableGroupId: null };
    this.ready = this.load();
  }

  async load() {
    const stored = await chrome.storage.local.get(SESSION_STATE_KEY);
    const value = stored[SESSION_STATE_KEY];
    if (value && typeof value === "object") {
      this.state = {
        sessions: value.sessions && typeof value.sessions === "object" ? value.sessions : {},
        deliverableGroupId:
          typeof value.deliverableGroupId === "number" ? value.deliverableGroupId : null
      };
      let migrated = false;
      for (const session of Object.values(this.state.sessions)) {
        if (
          session &&
          typeof session === "object" &&
          typeof session.title === "string" &&
          LEGACY_SESSION_GROUP_TITLE_PATTERN.test(session.title)
        ) {
          session.title = DEFAULT_SESSION_GROUP_TITLE;
          migrated = true;
        }
      }
      if (migrated) {
        await this.save();
      }
      if (typeof this.state.deliverableGroupId === "number") {
        await chrome.tabGroups
          .update(this.state.deliverableGroupId, { title: DELIVERABLE_GROUP_TITLE })
          .catch(() => {});
      }
    }
  }

  async save() {
    await chrome.storage.local.set({ [SESSION_STATE_KEY]: this.state });
  }

  async getSession(sessionId) {
    await this.ready;
    const existing = this.state.sessions[sessionId];
    if (existing && typeof existing === "object") {
      return existing;
    }
    const created = {
      chromeGroupId: null,
      title: DEFAULT_SESSION_GROUP_TITLE,
      activeTabId: null,
      tabOrigins: {}
    };
    this.state.sessions[sessionId] = created;
    await this.save();
    return created;
  }

  async removeSession(sessionId) {
    await this.ready;
    delete this.state.sessions[sessionId];
    await this.save();
  }

  async findSessionByGroup(groupId) {
    await this.ready;
    for (const [sessionId, session] of Object.entries(this.state.sessions)) {
      if (session.chromeGroupId === groupId) {
        return { sessionId, session };
      }
    }
    return null;
  }
}

class BrowserBackend {
  constructor() {
    this.store = new SessionStore();
    this.attachedTabs = new Set();
    this.activeTabsBySession = new Map();
    this.cursorByTabId = new Map();
    this.downloadFilenamesById = new Map();
    this.downloadUrlsById = new Map();
    this.downloadsById = new Map();
    this.downloadWaiters = new Set();
    this.downloadChangeListeners = new Set();
    this.cursorArrivalWaitersByKey = new Map();
    this.fileChoosersById = new Map();
    this.fileChooserWaitersByTabId = new Map();
    this.nextCursorMoveSequence = 1;
    chrome.debugger.onDetach.addListener((source) => {
      if (typeof source.tabId === "number") {
        this.attachedTabs.delete(source.tabId);
      }
    });
  }

  ping() {
    return "pong";
  }

  async getInfo() {
    let { extensionInstanceId } = await chrome.storage.local.get("extensionInstanceId");
    if (typeof extensionInstanceId !== "string") {
      extensionInstanceId = crypto.randomUUID();
      await chrome.storage.local.set({ extensionInstanceId });
    }
    return {
      name: "Open Browser Use Chrome",
      version: chrome.runtime.getManifest().version,
      type: "extension",
      metadata: {
        extensionId: chrome.runtime.id,
        extensionInstanceId,
        nativeHostName: NATIVE_HOST_NAME
      }
    };
  }

  async createTab(params) {
    const session = await this.requireSession(params);
    const chromeTab = await createBackgroundTab();
    await this.ensureSessionGroup(session.sessionId, chromeTab.id, "agent");
    await this.setSessionActiveTab(session.sessionId, chromeTab.id);
    return { ...toBrowserTab(chromeTab), active: true };
  }

  async getTabs(params) {
    const session = await this.requireSession(params);
    const tabs = await this.getSessionTabs(session.sessionId);
    return await this.withLogicalActive(session.sessionId, tabs.map(toBrowserTab));
  }

  async getUserTabs(params) {
    await this.requireSession(params);
    const tabs = (await chrome.tabs.query({}))
      .filter(hasTabId)
      .sort(compareLastAccessed)
      .slice(0, MAX_USER_TABS);
    const groupTitles = await readGroupTitles(tabs);
    return tabs.map((tab) => toUserTab(tab, groupTitles));
  }

  async getUserHistory(params) {
    await this.requireSession(params);
    const query = typeof params.query === "string" ? params.query : "";
    const maxResults =
      Number.isInteger(params.limit) && params.limit > 0 ? params.limit : 100;
    const search = { text: query, maxResults };
    if (typeof params.from === "string") {
      search.startTime = parseDate(params.from, "from");
    }
    if (typeof params.to === "string") {
      search.endTime = parseDate(params.to, "to");
    }
    const results = await chrome.history.search(search);
    return results.flatMap((item) => {
      if (typeof item.url !== "string" || typeof item.lastVisitTime !== "number") {
        return [];
      }
      return [
        {
          url: item.url,
          ...(item.title ? { title: item.title } : {}),
          dateVisited: new Date(item.lastVisitTime).toISOString()
        }
      ];
    });
  }

  async claimUserTab(params) {
    const session = await this.requireSession(params);
    const tabId = requireTabId(params, "claimUserTab");
    const tab = await chrome.tabs.get(tabId);
    if (!hasTabId(tab)) {
      throw new Error(`Chrome tab ${tabId} has no id`);
    }
    if (tab.url?.startsWith("chrome://")) {
      throw new Error(`Chrome internal tab ${tabId} cannot be claimed`);
    }
    if (typeof tab.groupId === "number" && tab.groupId !== -1) {
      const owner = await this.store.findSessionByGroup(tab.groupId);
      if (owner && owner.sessionId !== session.sessionId) {
        throw new Error(`Tab ${tabId} is already part of browser session ${owner.sessionId}`);
      }
    }
    await this.ensureSessionGroup(session.sessionId, tab.id, "user");
    await this.setSessionActiveTab(session.sessionId, tab.id);
    return { ...toBrowserTab(tab), active: true };
  }

  async finalizeTabs(params) {
    const session = await this.requireSession(params);
    if (!Array.isArray(params.keep)) {
      throw new Error("finalizeTabs requires keep array");
    }
    const tabs = await this.getSessionTabs(session.sessionId);
    const knownTabIds = new Set(tabs.filter(hasTabId).map((tab) => tab.id));
    const keep = new Map();
    for (const entry of params.keep) {
      if (!entry || typeof entry !== "object") {
        throw new Error("finalizeTabs received invalid tab entry");
      }
      const tabId = requireTabId(entry, "finalizeTabs");
      if (!knownTabIds.has(tabId)) {
        throw new Error(`finalizeTabs cannot keep unknown tab ${tabId}`);
      }
      if (keep.has(tabId)) {
        throw new Error(`finalizeTabs received duplicate tab ${tabId}`);
      }
      const status = entry.status;
      if (status !== "handoff" && status !== "deliverable") {
        throw new Error(`finalizeTabs received invalid status ${String(status)}`);
      }
      keep.set(tabId, status);
    }
    const sessionState = await this.store.getSession(session.sessionId);
    const agentTabsToClose = [];
    const userTabsToRelease = [];
    const deliverableTabs = [];
    const handoffTabs = [];
    for (const tab of tabs) {
      if (!hasTabId(tab)) {
        continue;
      }
      const keptStatus = keep.get(tab.id);
      if (keptStatus === "handoff") {
        handoffTabs.push(tab.id);
        continue;
      }
      if (keptStatus === "deliverable") {
        deliverableTabs.push(tab.id);
        continue;
      }
      if (sessionState.tabOrigins[String(tab.id)] === "agent") {
        agentTabsToClose.push(tab.id);
      } else {
        userTabsToRelease.push(tab.id);
      }
    }
    // Detach every disposition, including handoff tabs we keep alive: leaving a
    // tab attached keeps Chrome's "started debugging this browser" banner up
    // after the turn ends. The debugger re-attaches lazily on the next CDP call
    // (the CLI runner and background handlers attach on demand), so dropping it
    // here is loss-free while clearing the banner.
    await this.detachMany([
      ...agentTabsToClose,
      ...userTabsToRelease,
      ...deliverableTabs,
      ...handoffTabs
    ]);
    if (agentTabsToClose.length > 0) {
      await chrome.tabs.remove(agentTabsToClose.length === 1 ? agentTabsToClose[0] : agentTabsToClose);
    }
    if (userTabsToRelease.length > 0 && chrome.tabs.ungroup) {
      await chrome.tabs.ungroup(userTabsToRelease.length === 1 ? userTabsToRelease[0] : userTabsToRelease);
    }
    if (deliverableTabs.length > 0) {
      await this.moveToDeliverables(deliverableTabs);
    }
    for (const tabId of [...agentTabsToClose, ...userTabsToRelease, ...deliverableTabs]) {
      delete sessionState.tabOrigins[String(tabId)];
      this.cursorByTabId.delete(tabId);
    }
    if (handoffTabs.length > 0) {
      sessionState.activeTabId = handoffTabs.includes(sessionState.activeTabId)
        ? sessionState.activeTabId
        : handoffTabs[0];
      this.activeTabsBySession.set(session.sessionId, sessionState.activeTabId);
      await this.store.save();
    } else {
      this.activeTabsBySession.delete(session.sessionId);
      await this.store.removeSession(session.sessionId);
    }
  }

  async nameSession(params) {
    const session = await this.requireSession(params);
    const name = typeof params.name === "string" && params.name.trim() ? params.name.trim() : DEFAULT_SESSION_GROUP_TITLE;
    const sessionState = await this.store.getSession(session.sessionId);
    sessionState.title = name;
    await this.store.save();
    if (typeof sessionState.chromeGroupId === "number") {
      await chrome.tabGroups.update(sessionState.chromeGroupId, { title: name }).catch(() => {});
    }
  }

  async attach(params) {
    await this.requireSessionTab(params, "attach");
    const tabId = requireTabId(params, "attach");
    if (!this.attachedTabs.has(tabId)) {
      try {
        await chrome.debugger.attach({ tabId }, "1.3");
      } catch (error) {
        if (!String(error?.message ?? error).includes("Another debugger")) {
          throw error;
        }
      }
      this.attachedTabs.add(tabId);
    }
  }

  async detach(params) {
    await this.requireSessionTab(params, "detach");
    const tabId = requireTabId(params, "detach");
    await this.detachTab(tabId);
  }

  async executeCdp(params) {
    await this.requireSession(params);
    const target = params.target && typeof params.target === "object" ? params.target : {};
    const tabId = target.tabId;
    if (typeof tabId === "number") {
      await this.requireSessionTab({ ...params, tabId }, "executeCdp");
      if (!this.attachedTabs.has(tabId)) {
        throw new Error("Debugger unattached");
      }
    }
    const timeoutMs =
      typeof params.timeoutMs === "number" && params.timeoutMs > 0
        ? params.timeoutMs
        : DEFAULT_CDP_TIMEOUT_MS;
    return await withTimeout(timeoutMs, async () => {
      if (params.method === "Target.getTargets") {
        return { targetInfos: await chrome.debugger.getTargets() };
      }
      if (typeof params.method !== "string") {
        throw new Error("executeCdp requires method");
      }
      return await chrome.debugger.sendCommand(target, params.method, params.commandParams ?? {});
    });
  }

  async moveMouse(params) {
    const session = await this.requireSession(params);
    await this.requireSessionTab(params, "moveMouse");
    const tabId = requireTabId(params, "moveMouse");
    if (!Number.isFinite(params.x) || !Number.isFinite(params.y)) {
      throw new Error("moveMouse requires finite x and y");
    }
    const cursorReady = await this.ensureCursorContentScript(session.sessionId, tabId);
    if (!cursorReady) {
      throw new Error(`Cannot inject cursor content script into tab ${tabId}`);
    }
    const moveSequence = this.nextCursorMoveSequence;
    this.nextCursorMoveSequence += 1;
    const cursor = {
      moveSequence,
      visible: true,
      x: params.x,
      y: params.y
    };
    this.cursorByTabId.set(tabId, cursor);
    const arrivalWaiter =
      params.waitForArrival === false
        ? null
        : this.createCursorArrivalWaiter(params.session_id, params.turn_id, moveSequence);
    try {
      await chrome.tabs.sendMessage(tabId, {
        type: "OPEN_BROWSER_USE_CURSOR",
        sessionId: params.session_id,
        turnId: params.turn_id,
        moveSequence,
        x: cursor.x,
        y: cursor.y,
        visible: cursor.visible
      });
      await arrivalWaiter?.promise;
    } catch (error) {
      arrivalWaiter?.cancel();
      throw error;
    }
  }

  async waitForFileChooser(params) {
    await this.requireSessionTab(params, "waitForFileChooser");
    const tabId = requireTabId(params, "waitForFileChooser");
    if (!this.attachedTabs.has(tabId)) {
      await this.attach(params);
    }
    const timeoutMs =
      typeof params.timeoutMs === "number" && params.timeoutMs > 0
        ? params.timeoutMs
        : DEFAULT_CDP_TIMEOUT_MS;
    const waitForEvent = this.waitForFileChooserEvent(tabId);
    try {
      await chrome.debugger.sendCommand({ tabId }, "Page.setInterceptFileChooserDialog", {
        enabled: true
      });
      return await withTimeout(timeoutMs, () => waitForEvent);
    } finally {
      this.fileChooserWaitersByTabId.delete(tabId);
      await chrome.debugger
        .sendCommand({ tabId }, "Page.setInterceptFileChooserDialog", { enabled: false })
        .catch(() => {});
    }
  }

  async setFileChooserFiles(params) {
    await this.requireSession(params);
    if (typeof params.fileChooserId !== "string" || params.fileChooserId === "") {
      throw new Error("setFileChooserFiles requires fileChooserId");
    }
    const chooser = this.fileChoosersById.get(params.fileChooserId);
    if (!chooser) {
      throw new Error(`Unknown file chooser id "${params.fileChooserId}"`);
    }
    await this.requireSessionTab({ ...params, tabId: chooser.tabId }, "setFileChooserFiles");
    if (!Array.isArray(params.files) || params.files.length === 0) {
      throw new Error("setFileChooserFiles requires at least one file");
    }
    const files = params.files.map((file) => {
      if (typeof file !== "string" || file === "") {
        throw new Error("setFileChooserFiles files must be non-empty strings");
      }
      return file;
    });
    if (!chooser.isMultiple && files.length > 1) {
      throw new Error("File chooser does not accept multiple files");
    }
    await chrome.debugger.sendCommand({ tabId: chooser.tabId }, "DOM.setFileInputFiles", {
      backendNodeId: chooser.backendNodeId,
      files
    });
    this.fileChoosersById.delete(params.fileChooserId);
  }

  async waitForDownload(params) {
    await this.requireSessionTab(params, "waitForDownload");
    const timeoutMs =
      typeof params.timeoutMs === "number" && params.timeoutMs > 0
        ? params.timeoutMs
        : DEFAULT_CDP_TIMEOUT_MS;
    const download = await this.waitForDownloadChange(
      (change) =>
        change.status === "started" ||
        change.status === "in_progress" ||
        change.status === "complete",
      timeoutMs,
      "Timed out waiting for download."
    );
    return { downloadId: download.id };
  }

  async downloadPath(params) {
    await this.requireSession(params);
    if (typeof params.downloadId !== "string" || params.downloadId === "") {
      throw new Error("downloadPath requires downloadId");
    }
    const current = this.downloadsById.get(params.downloadId);
    if (current?.status === "complete") {
      return { path: current.filename ?? null };
    }
    if (current?.status === "failed" || current?.status === "canceled") {
      return { path: null };
    }
    const timeoutMs =
      typeof params.timeoutMs === "number" && params.timeoutMs > 0
        ? params.timeoutMs
        : DEFAULT_CDP_TIMEOUT_MS;
    const download = await this.waitForDownloadChange(
      (change) =>
        change.id === params.downloadId &&
        (change.status === "complete" || change.status === "failed" || change.status === "canceled"),
      timeoutMs,
      `Timed out waiting for download ${params.downloadId}.`
    );
    return { path: download.status === "complete" ? download.filename ?? null : null };
  }

  async readClipboardText(params) {
    await this.requireSessionTab(params, "readClipboardText");
    const tabId = requireTabId(params, "readClipboardText");
    if (!this.attachedTabs.has(tabId)) {
      await this.attach(params);
    }
    const text = await this.evaluateJavascript(tabId, "navigator.clipboard.readText()", true);
    return { text: typeof text === "string" ? text : "" };
  }

  async writeClipboardText(params) {
    await this.requireSessionTab(params, "writeClipboardText");
    const tabId = requireTabId(params, "writeClipboardText");
    if (typeof params.text !== "string") {
      throw new Error("writeClipboardText requires text");
    }
    if (!this.attachedTabs.has(tabId)) {
      await this.attach(params);
    }
    await this.evaluateJavascript(tabId, `navigator.clipboard.writeText(${JSON.stringify(params.text)})`, true);
  }

  async readClipboard(params) {
    await this.requireSessionTab(params, "readClipboard");
    const tabId = requireTabId(params, "readClipboard");
    if (!this.attachedTabs.has(tabId)) {
      await this.attach(params);
    }
    const items = await this.evaluateJavascript(
      tabId,
      `((async () => {
        const clipboardItems = await navigator.clipboard.read();
        return await Promise.all(clipboardItems.map(async (item) => {
          const entries = await Promise.all(item.types.map(async (type) => {
            const blob = await item.getType(type);
            if (type.startsWith("text/")) {
              return { mime_type: type, text: await blob.text() };
            }
            const base64 = await new Promise((resolve, reject) => {
              const reader = new FileReader();
              reader.onerror = () => reject(reader.error);
              reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
              reader.readAsDataURL(blob);
            });
            return { mime_type: type, base64 };
          }));
          return { entries, presentation_style: item.presentationStyle ?? "unspecified" };
        }));
      })())`,
      true
    );
    return { items: Array.isArray(items) ? items : [] };
  }

  async writeClipboard(params) {
    await this.requireSessionTab(params, "writeClipboard");
    const tabId = requireTabId(params, "writeClipboard");
    if (!Array.isArray(params.items)) {
      throw new Error("writeClipboard requires items");
    }
    if (!this.attachedTabs.has(tabId)) {
      await this.attach(params);
    }
    await this.evaluateJavascript(
      tabId,
      `((async (items) => {
        const clipboardItems = items.map((item) => {
          const entries = {};
          for (const entry of item.entries ?? []) {
            const mime = entry.mime_type;
            if (!mime) continue;
            if (entry.text !== undefined) {
              entries[mime] = new Blob([entry.text], { type: mime });
            } else if (entry.base64 !== undefined) {
              const binary = atob(entry.base64);
              const bytes = new Uint8Array(binary.length);
              for (let index = 0; index < binary.length; index += 1) {
                bytes[index] = binary.charCodeAt(index);
              }
              entries[mime] = new Blob([bytes], { type: mime });
            }
          }
          return new ClipboardItem(entries, { presentationStyle: item.presentation_style ?? "unspecified" });
        });
        await navigator.clipboard.write(clipboardItems);
      })(${JSON.stringify(params.items)}))`,
      true
    );
  }

  async turnEnded(params) {
    const session = await this.requireSession(params);
    const tabs = await this.getSessionTabs(session.sessionId);
    await this.detachMany(tabs.filter(hasTabId).map((tab) => tab.id));
    this.activeTabsBySession.delete(session.sessionId);
    for (const tab of tabs) {
      if (hasTabId(tab)) {
        this.cursorByTabId.delete(tab.id);
      }
    }
    const sessionState = await this.store.getSession(session.sessionId);
    sessionState.activeTabId = null;
    await this.store.save();
    await Promise.allSettled(tabs.filter(hasTabId).map((tab) => this.publishCursorState(tab.id)));
  }

  async executeUnhandledCommand(params) {
    throw new Error(`Open Browser Use Chrome does not support command "${params.type}"`);
  }

  addDownloadChangeListener(listener) {
    this.downloadChangeListeners.add(listener);
    return () => this.downloadChangeListeners.delete(listener);
  }

  handleDownloadCreated(item) {
    if (!this.isBrowserControlActive() || !Number.isInteger(item?.id)) {
      return;
    }
    const filename = typeof item.filename === "string" ? item.filename : "";
    const url =
      typeof item.finalUrl === "string" ? item.finalUrl : typeof item.url === "string" ? item.url : "";
    this.downloadFilenamesById.set(item.id, filename);
    this.downloadUrlsById.set(item.id, url);
    this.emitDownloadChange({
      id: String(item.id),
      filename,
      url,
      status: "started"
    });
  }

  handleDownloadChanged(delta) {
    if (!Number.isInteger(delta?.id)) {
      return;
    }
    const filename =
      typeof delta.filename?.current === "string"
        ? delta.filename.current
        : this.downloadFilenamesById.get(delta.id);
    const url = this.downloadUrlsById.get(delta.id);
    if (typeof filename !== "string" || typeof url !== "string") {
      return;
    }
    this.downloadFilenamesById.set(delta.id, filename);
    const status = downloadStatus(delta);
    if (!status) {
      return;
    }
    this.emitDownloadChange({
      id: String(delta.id),
      filename,
      url,
      status
    });
    if (status === "complete" || status === "failed" || status === "canceled") {
      this.downloadFilenamesById.delete(delta.id);
      this.downloadUrlsById.delete(delta.id);
    }
  }

  notifyCursorArrived(params) {
    const moveSequence = params?.moveSequence;
    if (!Number.isInteger(moveSequence)) {
      return false;
    }
    const waiter = this.cursorArrivalWaitersByKey.get(cursorArrivalKey(params.sessionId, params.turnId, moveSequence));
    waiter?.();
    return Boolean(waiter);
  }

  handleCdpEvent(source, method, params) {
    if (method !== "Page.fileChooserOpened" || typeof source?.tabId !== "number") {
      return;
    }
    const waiter = this.fileChooserWaitersByTabId.get(source.tabId);
    waiter?.resolve(params ?? {});
  }

  waitForFileChooserEvent(tabId) {
    if (this.fileChooserWaitersByTabId.has(tabId)) {
      throw new Error(`Already waiting for file chooser in tab ${tabId}`);
    }
    return new Promise((resolve, reject) => {
      this.fileChooserWaitersByTabId.set(tabId, {
        resolve: (event) => {
          this.fileChooserWaitersByTabId.delete(tabId);
          if (!Number.isInteger(event.backendNodeId)) {
            reject(new Error("File chooser event did not include backendNodeId"));
            return;
          }
          const fileChooserId = crypto.randomUUID();
          const chooser = {
            tabId,
            backendNodeId: event.backendNodeId,
            isMultiple: event.mode === "selectMultiple"
          };
          this.fileChoosersById.set(fileChooserId, chooser);
          resolve({
            fileChooserId,
            isMultiple: chooser.isMultiple
          });
        }
      });
    });
  }

  readCursorOverlayState(tabId) {
    for (const [sessionId, activeTabId] of this.activeTabsBySession) {
      if (activeTabId === tabId) {
        return {
          cursor: this.cursorByTabId.get(tabId) ?? null,
          isVisible: true,
          sessionId,
          turnId: null
        };
      }
    }
    return {
      cursor: null,
      isVisible: false,
      sessionId: null,
      turnId: null
    };
  }

  isBrowserControlActive() {
    return this.activeTabsBySession.size > 0;
  }

  createCursorArrivalWaiter(sessionId, turnId, moveSequence) {
    const key = cursorArrivalKey(sessionId, turnId, moveSequence);
    let timeoutId;
    let resolvePromise;
    const resolve = () => {
      if (timeoutId !== undefined) {
        clearTimeout(timeoutId);
      }
      this.cursorArrivalWaitersByKey.delete(key);
      resolvePromise?.();
    };
    const promise = new Promise((resolveInner) => {
      resolvePromise = resolveInner;
      timeoutId = setTimeout(resolve, CURSOR_ARRIVAL_TIMEOUT_MS);
      this.cursorArrivalWaitersByKey.set(key, resolve);
    });
    return { promise, cancel: resolve };
  }

  emitDownloadChange(change) {
    this.downloadsById.set(change.id, {
      ...(this.downloadsById.get(change.id) ?? {}),
      ...change
    });
    for (const waiter of [...this.downloadWaiters]) {
      if (waiter.predicate(change)) {
        waiter.resolve(change);
      }
    }
    for (const listener of this.downloadChangeListeners) {
      listener(change);
    }
  }

  waitForDownloadChange(predicate, timeoutMs, timeoutMessage) {
    for (const change of this.downloadsById.values()) {
      if (predicate(change)) {
        return Promise.resolve(change);
      }
    }
    return new Promise((resolve, reject) => {
      const waiter = {
        predicate,
        resolve: (change) => {
          cleanup();
          resolve(change);
        }
      };
      const cleanup = () => {
        clearTimeout(timeoutId);
        this.downloadWaiters.delete(waiter);
      };
      const timeoutId = setTimeout(() => {
        cleanup();
        reject(new Error(timeoutMessage));
      }, timeoutMs);
      this.downloadWaiters.add(waiter);
    });
  }

  async evaluateJavascript(tabId, expression, awaitPromise) {
    const result = await chrome.debugger.sendCommand({ tabId }, "Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise
    });
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.text ?? "JavaScript evaluation failed");
    }
    return result.result?.value;
  }

  async ensureSessionGroup(sessionId, tabId, origin) {
    const session = await this.store.getSession(sessionId);
    let groupId = session.chromeGroupId;
    if (typeof groupId === "number") {
      try {
        await chrome.tabGroups.get(groupId);
        await chrome.tabs.group({ groupId, tabIds: [tabId] });
      } catch {
        groupId = null;
      }
    }
    if (typeof groupId !== "number") {
      groupId = await chrome.tabs.group({ tabIds: [tabId] });
      session.chromeGroupId = groupId;
    }
    session.tabOrigins[String(tabId)] = origin;
    await this.store.save();
    await chrome.tabGroups.update(groupId, {
      title: session.title,
      color: "blue",
      collapsed: false
    });
  }

  async setSessionActiveTab(sessionId, tabId) {
    const session = await this.store.getSession(sessionId);
    session.activeTabId = tabId;
    this.activeTabsBySession.set(sessionId, tabId);
    await this.store.save();
    await this.publishCursorState(tabId);
  }

  async getSessionTabs(sessionId) {
    const session = await this.store.getSession(sessionId);
    if (typeof session.chromeGroupId !== "number") {
      return [];
    }
    try {
      return (await chrome.tabs.query({ groupId: session.chromeGroupId })).filter(
        (tab) => hasTabId(tab) && !tab.url?.startsWith("chrome://")
      );
    } catch {
      await this.store.removeSession(sessionId);
      return [];
    }
  }

  async requireSession(params) {
    if (!params || typeof params !== "object") {
      throw new Error("Missing browser session params");
    }
    if (typeof params.session_id !== "string") {
      throw new Error("Missing required browser session_id");
    }
    if (typeof params.turn_id !== "string") {
      throw new Error("Missing required browser turn_id");
    }
    await this.store.getSession(params.session_id);
    return { sessionId: params.session_id, turnId: params.turn_id };
  }

  async requireSessionTab(params, command) {
    const session = await this.requireSession(params);
    const tabId = requireTabId(params, command);
    const tabs = await this.getSessionTabs(session.sessionId);
    if (!tabs.some((tab) => tab.id === tabId)) {
      throw new Error(`Tab ${tabId} is not part of browser session ${session.sessionId}`);
    }
  }

  async withLogicalActive(sessionId, tabs) {
    if (tabs.length === 0) {
      return tabs;
    }
    const session = await this.store.getSession(sessionId);
    let activeTabId = this.activeTabsBySession.get(sessionId) ?? session.activeTabId;
    if (!tabs.some((tab) => tab.id === activeTabId)) {
      activeTabId = tabs.find((tab) => tab.active)?.id ?? tabs[0].id;
      await this.setSessionActiveTab(sessionId, activeTabId);
    } else if (!this.activeTabsBySession.has(sessionId)) {
      this.activeTabsBySession.set(sessionId, activeTabId);
    }
    return tabs.map((tab) => ({ ...tab, active: tab.id === activeTabId }));
  }

  async moveToDeliverables(tabIds) {
    if (!Array.isArray(tabIds) || tabIds.length === 0) {
      return;
    }

    const tabRecords = await Promise.all(
      tabIds.map((id) => chrome.tabs.get(id).catch(() => null))
    );
    const tabsByWindow = new Map();
    for (const tab of tabRecords) {
      if (!tab || typeof tab.id !== "number" || typeof tab.windowId !== "number") continue;
      const arr = tabsByWindow.get(tab.windowId) ?? [];
      arr.push(tab.id);
      tabsByWindow.set(tab.windowId, arr);
    }
    if (tabsByWindow.size === 0) {
      return;
    }

    const storedGroupId = this.store.state.deliverableGroupId;
    let lastGroupId = null;

    for (const [windowId, windowTabIds] of tabsByWindow) {
      const existingGroups = await chrome.tabGroups
        .query({ windowId, title: DELIVERABLE_GROUP_TITLE })
        .catch(() => []);

      let primaryGroupId = null;
      if (existingGroups.length > 0) {
        const preferred =
          existingGroups.find((g) => g.id === storedGroupId) ??
          existingGroups.reduce((min, g) => (g.id < min.id ? g : min), existingGroups[0]);
        primaryGroupId = preferred.id;

        for (const group of existingGroups) {
          if (group.id === primaryGroupId) continue;
          const dupTabs = await chrome.tabs.query({ groupId: group.id }).catch(() => []);
          const dupTabIds = dupTabs
            .map((t) => t.id)
            .filter((id) => typeof id === "number");
          if (dupTabIds.length > 0) {
            await chrome.tabs
              .group({ groupId: primaryGroupId, tabIds: dupTabIds })
              .catch(() => {});
          }
        }
      }

      if (typeof primaryGroupId === "number") {
        try {
          await chrome.tabs.group({ groupId: primaryGroupId, tabIds: windowTabIds });
        } catch {
          primaryGroupId = null;
        }
      }

      if (typeof primaryGroupId !== "number") {
        primaryGroupId = await chrome.tabs.group({ tabIds: windowTabIds });
      }

      await chrome.tabGroups
        .update(primaryGroupId, {
          title: DELIVERABLE_GROUP_TITLE,
          color: "green",
          collapsed: false
        })
        .catch(() => {});

      lastGroupId = primaryGroupId;
    }

    if (typeof lastGroupId === "number") {
      this.store.state.deliverableGroupId = lastGroupId;
      await this.store.save();
    }
  }

  async detachMany(tabIds) {
    await Promise.allSettled(tabIds.map((tabId) => this.detachTab(tabId)));
  }

  async detachTab(tabId) {
    try {
      await chrome.debugger.detach({ tabId });
    } finally {
      this.attachedTabs.delete(tabId);
      for (const [fileChooserId, chooser] of this.fileChoosersById) {
        if (chooser.tabId === tabId) {
          this.fileChoosersById.delete(fileChooserId);
        }
      }
      this.cursorByTabId.delete(tabId);
    }
  }

  async ensureCursorContentScript(sessionId, tabId) {
    try {
      const response = await chrome.tabs.sendMessage(tabId, { type: "OPEN_BROWSER_USE_PING" });
      if (response?.ok) {
        return true;
      }
    } catch {}
    try {
      await chrome.scripting.executeScript({
        target: { tabId },
        files: ["content-cursor.js"]
      });
      return true;
    } catch {
      return false;
    }
  }

  async publishCursorState(tabId) {
    if (!(await this.ensureCursorContentScript(null, tabId))) {
      return false;
    }
    try {
      const response = await chrome.tabs.sendMessage(tabId, {
        type: "OPEN_BROWSER_USE_CURSOR_STATE",
        state: this.readCursorOverlayState(tabId)
      });
      return response?.ok === true;
    } catch {
      return false;
    }
  }
}

function hasTabId(tab) {
  return typeof tab?.id === "number";
}

function toBrowserTab(tab) {
  return {
    id: tab.id,
    title: tab.title,
    active: tab.active,
    url: tab.url
  };
}

function toUserTab(tab, groupTitles) {
  const lastAccessed =
    typeof tab.lastAccessed === "number" && Number.isFinite(tab.lastAccessed)
      ? new Date(tab.lastAccessed).toISOString()
      : undefined;
  const groupTitle =
    typeof tab.groupId === "number" && tab.groupId !== -1 ? groupTitles.get(tab.groupId) : undefined;
  return {
    id: tab.id,
    ...(tab.title ? { title: tab.title } : {}),
    ...(tab.url ? { url: tab.url } : {}),
    ...(lastAccessed ? { lastOpened: lastAccessed } : {}),
    ...(groupTitle ? { tabGroup: groupTitle } : {})
  };
}

function requireTabId(params, command) {
  if (!Number.isInteger(params.tabId)) {
    throw new Error(`${command} requires an integer tabId`);
  }
  return params.tabId;
}

async function createBackgroundTab() {
  const windowId = await chooseWindowId();
  if (typeof windowId === "number") {
    const tab = await chrome.tabs.create({ active: false, url: "about:blank", windowId });
    if (hasTabId(tab)) {
      return tab;
    }
  }
  const win = await chrome.windows.create({
    focused: false,
    type: "normal",
    url: "about:blank"
  });
  const tab = win.tabs?.find(hasTabId);
  if (!tab) {
    throw new Error("Created Chrome window has no tab");
  }
  return tab;
}

async function chooseWindowId() {
  const windows = await chrome.windows.getAll({ windowTypes: ["normal"] });
  const focused = windows.find((win) => win.focused && typeof win.id === "number");
  return focused?.id ?? windows.find((win) => typeof win.id === "number")?.id ?? null;
}

async function readGroupTitles(tabs) {
  const ids = new Set(
    tabs
      .map((tab) => tab.groupId)
      .filter((groupId) => typeof groupId === "number" && groupId !== -1)
  );
  const entries = await Promise.all(
    [...ids].map(async (groupId) => {
      try {
        const group = await chrome.tabGroups.get(groupId);
        return group.title ? [groupId, group.title] : null;
      } catch {
        return null;
      }
    })
  );
  return new Map(entries.filter(Boolean));
}

function compareLastAccessed(left, right) {
  const byLastAccessed = (right.lastAccessed ?? 0) - (left.lastAccessed ?? 0);
  if (byLastAccessed !== 0) {
    return byLastAccessed;
  }
  const byWindow = (left.windowId ?? 0) - (right.windowId ?? 0);
  return byWindow !== 0 ? byWindow : (left.index ?? 0) - (right.index ?? 0);
}

function parseDate(value, field) {
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) {
    throw new Error(`getUserHistory requires ${field} to be a valid date`);
  }
  return timestamp;
}

function downloadStatus(delta) {
  switch (delta.state?.current) {
    case "complete":
      return "complete";
    case "interrupted":
      return delta.error?.current === "USER_CANCELED" ? "canceled" : "failed";
    case "in_progress":
      return "in_progress";
    default:
      return undefined;
  }
}

function cursorArrivalKey(sessionId, turnId, moveSequence) {
  return `${sessionId}:${turnId}:${moveSequence}`;
}

async function withTimeout(timeoutMs, fn) {
  let timeout;
  try {
    return await Promise.race([
      fn(),
      new Promise((_, reject) => {
        timeout = setTimeout(() => {
          reject(new Error(`Timed out after ${timeoutMs}ms waiting for CDP command.`));
        }, timeoutMs);
      })
    ]);
  } finally {
    clearTimeout(timeout);
  }
}

function startHeartbeat(peer, backend) {
  chrome.alarms.create(HEARTBEAT_ALARM_NAME, { periodInMinutes: 0.5 }).catch(() => {});
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === HEARTBEAT_ALARM_NAME) {
      safeSendNotification(peer, "heartbeat", { at: new Date().toISOString() });
      void backend.getInfo().catch(() => {});
    }
  });
}

chrome.runtime.onInstalled.addListener(async () => {
  const { extensionInstanceId } = await chrome.storage.local.get("extensionInstanceId");
  if (typeof extensionInstanceId !== "string") {
    await chrome.storage.local.set({ extensionInstanceId: crypto.randomUUID() });
  }
});

const backend = new BrowserBackend();
const transport = new NativeTransport(NATIVE_HOST_NAME);
const peer = new JsonRpcPeer(transport, backend);
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "GET_NATIVE_HOST_STATUS") {
    const status = transport.getStatus();
    sendResponse({ ok: status.state === "connected", status, error: status.error });
    return true;
  }
  if (message?.type === "GET_OPEN_BROWSER_USE_CURSOR_STATE") {
    const tabId = typeof sender.tab?.id === "number" ? sender.tab.id : -1;
    sendResponse({ ok: true, state: backend.readCursorOverlayState(tabId) });
    return true;
  }
  if (message?.type === "OPEN_BROWSER_USE_CURSOR_ARRIVED") {
    sendResponse({ ok: backend.notifyCursorArrived(message) });
    return true;
  }
  return false;
});
chrome.debugger.onEvent.addListener((source, method, params) => {
  backend.handleCdpEvent(source, method, params);
  safeSendNotification(peer, "onCDPEvent", { source, method, params });
});
backend.addDownloadChangeListener((change) => {
  safeSendNotification(peer, "onDownloadChange", change);
});
chrome.downloads.onCreated.addListener((item) => {
  backend.handleDownloadCreated(item);
});
chrome.downloads.onChanged.addListener((delta) => {
  backend.handleDownloadChanged(delta);
});
startHeartbeat(peer, backend);

function safeSendNotification(peer, method, params) {
  try {
    peer.sendNotification(method, params);
  } catch {}
}

// ─────────────────────────────────────────────────────────────────────────────
// NEO — adições do fork (neo-browser-local). Tudo abaixo desta linha é nosso;
// nada acima foi alterado, para o merge com o upstream continuar trivial.
// Ver: neo-core/docs/browser-local-plan.md §3.1
// ─────────────────────────────────────────────────────────────────────────────

const NEO_OVERLAY_KEY = "neoOverlayByTab";
const NEO_BADGE_ALARM = "neo-badge";

/** Estado do painel por aba (inclusive a linha do tempo). chrome.storage.session
 *  sobrevive ao worker dormir. */
async function neoReadOverlayState() {
  try {
    const stored = await chrome.storage.session.get(NEO_OVERLAY_KEY);
    return stored?.[NEO_OVERLAY_KEY] ?? {};
  } catch {
    return {};
  }
}

async function neoWriteOverlayState(byTab) {
  try {
    await chrome.storage.session.set({ [NEO_OVERLAY_KEY]: byTab });
  } catch {
    /* noop */
  }
}

const NEO_MAX_ITENS = 60;

async function neoSetOverlay(tabId, patch) {
  const byTab = await neoReadOverlayState();
  const next = { ...(byTab[tabId] ?? { active: false, stopped: false, items: [] }), ...patch };
  // A linha do tempo vem inteira do content script a cada evento. O teto é
  // repetido aqui porque o que entra no storage.session é ISTO, e quota
  // estourada derruba a persistência da faixa toda (silenciosamente).
  if (Array.isArray(next.items) && next.items.length > NEO_MAX_ITENS) {
    next.items = next.items.slice(-NEO_MAX_ITENS);
  }
  if (next.active === false && next.stopped !== true) {
    delete byTab[tabId];
  } else {
    byTab[tabId] = next;
  }
  await neoWriteOverlayState(byTab);
  return next;
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const tabId = typeof sender.tab?.id === "number" ? sender.tab.id : -1;

  // O painel acabou de subir numa aba (ou depois de navegar) e quer saber se
  // esta aba está sob controle do Neo — e com qual linha do tempo.
  if (message?.type === "NEO_OVERLAY_QUERY") {
    neoReadOverlayState().then((byTab) => {
      sendResponse({ state: byTab[tabId] ?? { active: false, stopped: false, items: [] } });
    });
    return true;
  }

  // O painel aplicou um estado que veio do mundo principal (CDP) — persiste,
  // para a próxima navegação re-montar a linha do tempo igual.
  if (message?.type === "NEO_OVERLAY_SET") {
    // A pergunta do painel só nasce pelo `neoPergunta` (canal do desktop): o
    // estado que o content script manda é derivado de `postMessage`, que a
    // própria página consegue imitar — então ele nunca escreve a pergunta.
    const { pergunta: _naoDaqui, rascunho: _nemIsto, ...estado } = message.state ?? {};
    void neoSetOverlay(tabId, estado);
    sendResponse({ ok: true });
    return true;
  }

  // Botão PARE. O atributo data-neo-stop já foi posto pelo content script (é o
  // freio confiável); esta notificação é só o caminho rápido até o neo-desktop.
  if (message?.type === "NEO_STOP") {
    neoSetOverlay(tabId, { stopped: true }).then(() => {
      safeSendNotification(peer, "neo.stop", { tabId, at: new Date().toISOString() });
    });
    sendResponse({ ok: true });
    return true;
  }
  return false;
});

// Aba fechada: some com o estado, senão um tabId reciclado herda a faixa.
chrome.tabs.onRemoved.addListener((tabId) => {
  void neoReadOverlayState().then((byTab) => {
    if (byTab[tabId]) {
      delete byTab[tabId];
      return neoWriteOverlayState(byTab);
    }
  });
});

/** Badge: verde quando o host nativo está conectado, cinza quando não. */
function neoUpdateBadge() {
  try {
    const connected = transport.getStatus()?.state === "connected";
    chrome.action.setBadgeText({ text: connected ? "●" : "○" });
    chrome.action.setBadgeBackgroundColor({ color: connected ? "#22c55e" : "#6b7280" });
  } catch {
    /* action pode não estar pronta durante o spin-up do worker */
  }
}

chrome.alarms.create(NEO_BADGE_ALARM, { periodInMinutes: 0.4 }).catch(() => {});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === NEO_BADGE_ALARM) neoUpdateBadge();
});
neoUpdateBadge();

// ─────────────────────────────────────────────────────────────────────────────
// NEO — `searchDownloads`: a barra de download do NAVEGADOR como fonte.
//
// O `browser.downloads` do desktop lia a PASTA (`read_dir` em ~/Downloads) e
// tirava conclusão do nome do arquivo: sufixo `.crdownload` = baixando, sem
// sufixo = pronto. Isso não distingue as duas coisas que mais importam depois
// de um clique em "Baixar":
//
//   (a) o clique não virou download nenhum  → tentar de novo é CERTO;
//   (b) o servidor ainda está preparando/zipando → tentar de novo DUPLICA.
//
// Nos dois casos a pasta está igual: vazia. Foi assim que o agente cancelou o
// próprio download para "tentar de novo" (job 2c8cc496) e baixou o mesmo
// arquivo várias vezes (jobs 6142ed04 e 0f87265f, 5 cópias de um APK de 52 MB).
//
// `chrome.downloads.search` responde as duas: o item EXISTE desde o instante em
// que o Chrome aceita o download (bem antes do primeiro byte no disco), e traz
// `state`/`error`/`paused`/`bytesReceived` — ou seja, "começou" vira EVENTO em
// vez de palpite, e "falhou" passa a existir como estado, com motivo.
//
// Fica no apêndice (e no prototype) de propósito: UPSTREAM.md manda não tocar em
// nada acima da linha do fork, para o merge com o open-browser-use seguir trivial.
// O dispatch do JsonRpcPeer é `this.handlers[method]`, busca comum de
// propriedade — método no prototype resolve igual.
// ─────────────────────────────────────────────────────────────────────────────

/** Só o nome do arquivo, sem a pasta. `filename` do Chrome é caminho ABSOLUTO. */
function neoBasename(filename) {
  const path = typeof filename === "string" ? filename : "";
  const cut = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return cut >= 0 ? path.slice(cut + 1) : path;
}

/**
 * Um item do `chrome.downloads` no formato que o neo-core entende.
 *
 * `incomplete` continua existindo com o MESMO sentido de antes (o disco ainda
 * está enchendo) para o lado de lá não precisar de dois caminhos de leitura —
 * só que agora vem do estado real e não do sufixo do nome.
 */
function neoDownloadItem(item, nowMs) {
  const startedMs = Date.parse(item.startTime ?? "");
  const state = typeof item.state === "string" ? item.state : "in_progress";
  return {
    download_id: String(item.id),
    name: neoBasename(item.filename),
    path: typeof item.filename === "string" ? item.filename : "",
    url: item.finalUrl || item.url || "",
    state,
    error: typeof item.error === "string" ? item.error : "",
    paused: item.paused === true,
    bytes: Number(item.bytesReceived) || 0,
    bytes_received: Number(item.bytesReceived) || 0,
    total_bytes: Number(item.totalBytes) || 0,
    // `exists` só é confiável depois de `complete`; antes disso o Chrome deixa
    // em true por padrão.
    exists: item.exists !== false,
    // "o arquivo no disco ainda NÃO é o final" — vale para `in_progress` e
    // também para `interrupted`: download que morreu no meio deixa um parcial,
    // e chamar isso de pronto seria o falso sucesso de novo.
    incomplete: state !== "complete",
    started_ms: Number.isNaN(startedMs) ? 0 : startedMs,
    seconds_ago: Number.isNaN(startedMs) ? 0 : Math.max(0, Math.round((nowMs - startedMs) / 1000))
  };
}

/**
 * Downloads que o NAVEGADOR conhece.
 *
 * `since_s` corta pelo início do download (não pelo mtime do arquivo, que é o
 * que a leitura de pasta usava e que muda quando o arquivo termina). `ids`
 * consulta itens específicos — é o que o acompanhamento usa depois de já saber
 * qual download é o seu, sem depender de casar por nome.
 */
BrowserBackend.prototype.searchDownloads = async function searchDownloads(params) {
  const since = Number(params?.since_s) > 0 ? Number(params.since_s) : 900;
  const limit = Number(params?.limit) > 0 ? Math.min(Number(params.limit), 50) : 25;
  const ids = Array.isArray(params?.ids)
    ? params.ids.map((id) => Number(id)).filter((id) => Number.isInteger(id))
    : [];

  const nowMs = Date.now();
  const query = ids.length
    ? { id: ids[0] }
    : { startedAfter: new Date(nowMs - since * 1000).toISOString(), limit, orderBy: ["-startTime"] };

  // Vários ids exigem uma busca por id (a API não aceita lista) — barato, são
  // poucos, e evita trazer a pasta inteira só para achar dois.
  const raw = ids.length
    ? (await Promise.all(ids.map((id) => chrome.downloads.search({ id })))).flat()
    : await chrome.downloads.search(query);

  const items = raw
    .filter((item) => item && Number.isInteger(item.id))
    .map((item) => neoDownloadItem(item, nowMs))
    .sort((a, b) => b.started_ms - a.started_ms);

  return { ok: true, count: items.length, items };
};

// ─────────────────────────────────────────────────────────────────────────────
// NEO — SUPERFÍCIE DO NEO-CORE (neo-core/docs/browser-surface-plan.md)
//
// Esta extensão sempre falou só com o host nativo (`obu`), que fala com o
// neo-desktop-tauri, que fala com o core. Ou seja: ela nunca teve identidade
// própria no Neo — a "conta" dela era a do Mac, por transitividade, e não havia
// onde colar credencial nenhuma.
//
// Aqui ela ganha um canal PRÓPRIO com o core, autenticado por um token de
// device, exatamente como um aparelho pareado. O que isso muda:
//
//   • ela existe no registro de dispositivos, com nome, e é revogável sozinha;
//   • o core sabe que ela está viva sem depender do desktop estar aberto;
//   • fica endereçável por nome ("o navegador do macbook"), de qualquer canal —
//     inclusive do celular, que é o caso que hoje cai no navegador do SERVIDOR.
//
// **O pareamento é por CÓDIGO**, digitado no popup, e não por um token fixo
// colado. O código é de uso único e morre em 5 min: se vazar, não vale nada. Um
// token colado à mão é uma credencial que precisa ser vista, copiada e guardada
// — e credencial que se vê acaba em screenshot (queimou em 2026-09-27).
//
// O código sai do PAINEL (Dispositivos → gerar código). Não precisa do
// neo-desktop-tauri para nada: é o caminho de quem não tem desktop na máquina.
// A contrapartida é que a superfície nasce sem `host_device_id` — então batize-a
// com a máquina no nome ("Edge do macbook"), que é o que mantém o endereçamento
// por máquina funcionando.
//
// **Ação de navegação continua com o desktop.** Esta superfície declara só o que
// sabe fazer, e o core recusa o resto com mensagem própria em vez de tentar e
// falhar (§13.3 do plano). Quando as derivadas do core existirem, ela passa a
// declarar `eval`/`cdp` — junto com a implementação, nunca antes.
// ─────────────────────────────────────────────────────────────────────────────

const NEO_SURF_KEYS = ["neoSurfToken", "neoSurfDeviceId", "neoCoreUrl", "neoSurfReportTabs"];
const NEO_SURF_CORE_PADRAO = "http://127.0.0.1:8000";
const NEO_SURF_RECONNECT_MIN_MS = 1000;
const NEO_SURF_RECONNECT_MAX_MS = 15000;

const neoSurf = { ws: null, atraso: NEO_SURF_RECONNECT_MIN_MS, erro: null };

async function neoSurfEstado() {
  try {
    const g = await chrome.storage.local.get(NEO_SURF_KEYS);
    return {
      token: typeof g.neoSurfToken === "string" ? g.neoSurfToken : "",
      deviceId: typeof g.neoSurfDeviceId === "string" ? g.neoSurfDeviceId : "",
      // Configurável desde o dia um: hoje o core mora atrás do TÚNEL na máquina,
      // e apontar para uma entrada pública depois tem que ser config, não
      // reescrita.
      coreUrl: (typeof g.neoCoreUrl === "string" && g.neoCoreUrl) || NEO_SURF_CORE_PADRAO,
      // Reportar abas é OPT-IN. Esta extensão enxerga `<all_urls>`: mandar todas
      // as abas ao core seria mandar a navegação inteira para a VPS sem escolha.
      // Parear dá IDENTIDADE; reportar é outra decisão, e é sua.
      reportarAbas: g.neoSurfReportTabs === true,
    };
  } catch {
    return { token: "", deviceId: "", coreUrl: NEO_SURF_CORE_PADRAO, reportarAbas: false };
  }
}

function neoNomeDoNavegador() {
  const ua = navigator.userAgent || "";
  if (/Edg\//.test(ua)) return "Edge";
  if (/OPR\//.test(ua)) return "Opera";
  if (/Brave/.test(ua)) return "Brave";
  if (/Chrome\//.test(ua)) return "Chrome";
  return "Navegador";
}

/**
 * O `connect-src` do manifest é quem decide com quem esta extensão consegue
 * falar, e ele é ESTÁTICO — o campo "Avançado" do popup não o alcança. Sem esta
 * checagem, apontar o core para um endereço fora da lista dá exatamente o mesmo
 * "Failed to fetch" de túnel fora do ar, que é o erro que não ensina nada.
 * Mantenha em sincronia com `content_security_policy.extension_pages`.
 */
function neoSurfForaDoAlcance(base) {
  let u;
  try {
    u = new URL(base);
  } catch {
    return `endereço do neo-core inválido: "${base}"`;
  }
  const local =
    u.hostname === "127.0.0.1" || u.hostname === "localhost" || u.hostname === "[::1]";
  if (u.protocol === "https:") return null;
  if (u.protocol === "http:" && local) return null;
  return (
    `o manifest só deixa esta extensão alcançar https:// ou o túnel local ` +
    `(127.0.0.1/localhost) — "${base}" não passa no connect-src.`
  );
}

/** Troca o código de 6 dígitos por um token de device. */
async function neoSurfParear({ code, coreUrl, nome }) {
  const base = (coreUrl || NEO_SURF_CORE_PADRAO).replace(/\/$/, "");
  const { deviceId: existente, reportarAbas } = await neoSurfEstado();
  // Um device_id estável por perfil: re-parear ROTACIONA o token no core em vez
  // de criar uma linha nova a cada tentativa.
  const deviceId = existente || crypto.randomUUID();

  const bloqueio = neoSurfForaDoAlcance(base);
  if (bloqueio) throw new Error(bloqueio);

  const pedido = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      code: String(code || "").trim(),
      device_id: deviceId,
      device_type: "neo-browser-ext",
      model: neoNomeDoNavegador(),
      os_platform: navigator.platform || "unknown",
      os_version: "",
      client_version: chrome.runtime.getManifest().version,
      display_name: (nome || "").trim() || neoNomeDoNavegador(),
      capabilities: {
        browser_local: true,
        browser_headless: false,
        // Declarar MENOS é o erro barato: o core trata quem não declara como a
        // mais pobre, e prometer o que não se faz é o modo de falha que a
        // declaração existe para matar.
        primitives: { tabs: reportarAbas, eval: false, cdp: false },
      },
    }),
  };

  // `fetch` só levanta por REDE — status nenhum chega aqui. As duas causas
  // reais são o túnel fora do ar e o `connect-src`, e "Failed to fetch" não
  // distingue as duas. Dizer o endereço e onde olhar é a diferença entre um
  // conserto de um minuto e uma caçada.
  let res;
  try {
    res = await fetch(`${base}/api/v1/devices/pair`, pedido);
  } catch (e) {
    throw new Error(
      `não alcancei o neo-core em ${base} (${e?.message || e}). ` +
        `O túnel da :8000 está de pé? Se o console do service worker ` +
        `(edge://extensions → "service worker") disser "Refused to connect", ` +
        `o bloqueio é o connect-src do manifest.`,
    );
  }

  if (!res.ok) {
    const corpo = await res.json().catch(() => ({}));
    const motivo =
      res.status === 403
        ? "código inválido ou expirado (eles valem 5 minutos e só uma vez)"
        : corpo?.detail || `o neo-core respondeu ${res.status}`;
    throw new Error(motivo);
  }

  const data = await res.json();
  await chrome.storage.local.set({
    neoSurfToken: data.device_token || "",
    neoSurfDeviceId: data.device_id || deviceId,
    neoCoreUrl: base,
  });
  neoSurf.erro = null;
  neoSurfConectar();
  return {
    userId: data.user_id,
    host: data.host_device_id,
    nome: data.display_name,
  };
}

async function neoSurfDesparear() {
  await chrome.storage.local.remove(["neoSurfToken", "neoSurfDeviceId"]);
  try {
    if (neoSurf.ws) neoSurf.ws.close();
  } catch {}
  neoSurf.ws = null;
  neoSurf.erro = null;
}

function neoSurfAberta() {
  return !!neoSurf.ws && neoSurf.ws.readyState === WebSocket.OPEN;
}

async function neoSurfConectar() {
  if (neoSurf.ws && (neoSurf.ws.readyState === WebSocket.OPEN || neoSurf.ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  const { token, deviceId, coreUrl, reportarAbas } = await neoSurfEstado();
  if (!token || !deviceId) return; // não pareada: silêncio, não erro

  const bloqueio = neoSurfForaDoAlcance(coreUrl);
  if (bloqueio) {
    // Sem reagendar: reconectar não conserta connect-src, e o keepalive de 0,4
    // min já volta aqui se o endereço mudar. Laço apertado e mudo, não.
    neoSurf.erro = bloqueio;
    return;
  }

  const base = coreUrl.replace(/^http/, "ws").replace(/\/$/, "");
  const url =
    `${base}/api/v1/ws/devices/control/${encodeURIComponent(deviceId)}` +
    `?token=${encodeURIComponent(token)}`;
  try {
    neoSurf.ws = new WebSocket(url);
  } catch (e) {
    // Rede fora do ar NÃO levanta aqui (vira `onerror`/`onclose`): se levantou,
    // o endereço foi recusado antes de sair — normalmente o `connect-src`.
    neoSurf.erro = `não consegui abrir o canal com ${coreUrl}: ${e?.message || e}`;
    neoSurfAgendarReconexao();
    return;
  }

  neoSurf.ws.onopen = () => {
    neoSurf.atraso = NEO_SURF_RECONNECT_MIN_MS;
    neoSurf.erro = null;
    // `hello` primeiro: é ele que diz ao core o que esta superfície sabe fazer.
    neoSurf.ws.send(
      JSON.stringify({
        type: "hello",
        surface: {
          provider: "extension",
          engine: neoNomeDoNavegador(),
          primitives: { tabs: reportarAbas, eval: false, cdp: false, screenshot: false },
        },
      }),
    );
    if (reportarAbas) neoSurfEmpurrarAbas();
  };

  neoSurf.ws.onmessage = (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    // Envelope do canal: {id, action, args}. Nada do vocabulário `browser.*` é
    // implementado por aqui ainda — quem dirige a aba continua sendo o caminho
    // desktop → obu → CDP. Responder com o motivo é honesto; o core já recusa
    // antes, pela declaração, mas a rede de segurança vale.
    if (msg && msg.id) {
      neoSurf.ws.send(
        JSON.stringify({
          id: msg.id,
          ok: false,
          error:
            "esta superfície ainda não executa ação de navegação — o Neo Desktop precisa estar aberto nesta máquina",
        }),
      );
    }
  };

  neoSurf.ws.onclose = (ev) => {
    neoSurf.ws = null;
    // 4401 = o core não reconhece mais o token (device removido no painel).
    // Insistir seria reconexão infinita; apagar o estado é o certo.
    if (ev && ev.code === 4401) {
      chrome.storage.local.remove(["neoSurfToken", "neoSurfDeviceId"]);
      neoSurf.erro = "o Neo removeu este navegador — pareie de novo";
      return;
    }
    neoSurfAgendarReconexao();
  };

  neoSurf.ws.onerror = () => {
    try {
      neoSurf.ws.close();
    } catch {}
  };
}

function neoSurfAgendarReconexao() {
  setTimeout(neoSurfConectar, neoSurf.atraso);
  neoSurf.atraso = Math.min(neoSurf.atraso * 2, NEO_SURF_RECONNECT_MAX_MS);
}

/** Snapshot de abas — só quando VOCÊ ligou (ver `reportarAbas`). */
async function neoSurfEmpurrarAbas() {
  if (!neoSurfAberta()) return;
  const { reportarAbas } = await neoSurfEstado();
  if (!reportarAbas) return;
  try {
    const abas = await chrome.tabs.query({});
    neoSurf.ws.send(
      JSON.stringify({
        type: "tabs",
        tabs: abas
          .filter((t) => t.url && /^https?:/.test(t.url))
          .map((t) => ({
            tabId: t.id,
            url: t.url,
            title: t.title || "",
            active: !!t.active,
            audible: !!t.audible,
          })),
      }),
    );
  } catch {}
}

// Keepalive: o service worker MV3 é efêmero e leva o WebSocket junto no reciclo.
// `.catch` como os outros alarmes deste arquivo: em MV3 `create` devolve
// Promise, e rejeição não tratada derruba o worker.
chrome.alarms.create("neo-surface-keepalive", { periodInMinutes: 0.4 }).catch(() => {});
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name !== "neo-surface-keepalive") return;
  if (!neoSurfAberta()) neoSurfConectar();
  else neoSurfEmpurrarAbas();
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "NEO_SURFACE_STATUS") {
    neoSurfEstado().then((s) =>
      sendResponse({
        pareada: !!s.token,
        conectada: neoSurfAberta(),
        coreUrl: s.coreUrl,
        reportarAbas: s.reportarAbas,
        erro: neoSurf.erro,
      }),
    );
    return true;
  }
  if (message?.type === "NEO_SURFACE_PAIR") {
    neoSurfParear(message)
      .then((r) => sendResponse({ ok: true, ...r }))
      .catch((e) => sendResponse({ ok: false, erro: e?.message || String(e) }));
    return true;
  }
  if (message?.type === "NEO_SURFACE_UNPAIR") {
    neoSurfDesparear().then(() => sendResponse({ ok: true }));
    return true;
  }
  if (message?.type === "NEO_SURFACE_SET_TABS") {
    chrome.storage.local.set({ neoSurfReportTabs: !!message.ligado }).then(() => {
      // A declaração muda: reconecta para mandar um `hello` novo, senão o core
      // segue achando que esta superfície não reporta (ou que reporta).
      try {
        if (neoSurf.ws) neoSurf.ws.close();
      } catch {}
      neoSurf.ws = null;
      neoSurfConectar();
      sendResponse({ ok: true });
    });
    return true;
  }
  return false;
});

neoSurfConectar();

// ─────────────────────────────────────────────────────────────────────────────
// Neo — apêndice (3º): as abas que a PÁGINA abre (docs/browser-multi-aba-plan.md).
//
// Job 3512cdd2 (2026-11-02): o clique no anúncio do AliExpress abriu o produto
// numa aba nova, o Edge foi para ela, e o agente seguiu na aba da busca vendo
// "clique ok, a tela não mudou". Ninguém aqui sabia que a aba existia: o único
// `onCreated` era o de downloads, e o upstream só aceita CDP em aba do GRUPO da
// sessão (`requireSessionTab`).
//
// Três peças, todas fora do bloco do upstream:
//   1. `chrome.tabs.onCreated` com `openerTabId` de uma aba da sessão → a nova
//      entra no grupo, com origem `site` e quem a abriu;
//   2. `neoTakeNewTabs` / `neoSessionTabs` / `neoCloseTab` para o desktop;
//   3. `requireSessionTab` aceita aba de origem `site` fora do grupo — popup com
//      `width/height` abre numa janela `popup`, e tab group não existe lá.
// ─────────────────────────────────────────────────────────────────────────────

/** Filhas ainda não entregues ao desktop, por aba que as abriu. Em memória de
 * propósito: é uma conversa de segundos entre o clique e o `neoTakeNewTabs`. */
const NEO_ABAS_NOVAS = new Map();

async function neoAbaCriada(tab) {
  if (!hasTabId(tab) || typeof tab.openerTabId !== "number") {
    return;
  }
  const origem = await chrome.tabs.get(tab.openerTabId).catch(() => null);
  if (!origem || typeof origem.groupId !== "number" || origem.groupId === -1) {
    return; // aba aberta por aba que não é do Neo: não é da nossa conta
  }
  const dono = await backend.store.findSessionByGroup(origem.groupId);
  if (!dono) {
    return;
  }
  if (tab.groupId !== origem.groupId) {
    // Falha em janela `popup` (sem tab groups) — a origem `site` cobre.
    await chrome.tabs.group({ groupId: origem.groupId, tabIds: [tab.id] }).catch(() => {});
  }
  dono.session.tabOrigins[String(tab.id)] = "site";
  dono.session.tabOpeners = dono.session.tabOpeners || {};
  dono.session.tabOpeners[String(tab.id)] = tab.openerTabId;
  await backend.store.save();
  const fila = NEO_ABAS_NOVAS.get(tab.openerTabId) || [];
  fila.push(tab.id);
  NEO_ABAS_NOVAS.set(tab.openerTabId, fila);
}

chrome.tabs.onCreated.addListener((tab) => {
  neoAbaCriada(tab).catch(() => {});
});

chrome.tabs.onRemoved.addListener((tabId) => {
  for (const [opener, fila] of NEO_ABAS_NOVAS) {
    const resto = fila.filter((id) => id !== tabId);
    if (resto.length) NEO_ABAS_NOVAS.set(opener, resto);
    else NEO_ABAS_NOVAS.delete(opener);
  }
});

function neoAbaParaFora(tab, sessao) {
  return {
    id: tab.id,
    url: tab.url || tab.pendingUrl || "",
    title: tab.title || "",
    active: !!tab.active,
    origin: sessao.tabOrigins?.[String(tab.id)] || "",
    openerTabId: sessao.tabOpeners?.[String(tab.id)] ?? null,
  };
}

/** As filhas de `openerTabId` que nasceram desde a última pergunta. Entrega cada
 * uma UMA vez: é o que o desktop chama logo depois do clique. */
BrowserBackend.prototype.neoTakeNewTabs = async function neoTakeNewTabs(params) {
  const sessao = await this.requireSession(params);
  const opener = Number(params?.openerTabId);
  if (!Number.isInteger(opener)) {
    throw new Error("neoTakeNewTabs requires an integer openerTabId");
  }
  const estado = await this.store.getSession(sessao.sessionId);
  const ids = NEO_ABAS_NOVAS.get(opener) || [];
  NEO_ABAS_NOVAS.delete(opener);
  const saida = [];
  for (const id of ids) {
    const tab = await chrome.tabs.get(id).catch(() => null);
    if (tab) saida.push(neoAbaParaFora(tab, estado)); // fechou antes de ser vista: some
  }
  return saida;
};

/** Todas as abas da sessão, com origem e quem abriu — inclusive popup fora do grupo. */
BrowserBackend.prototype.neoSessionTabs = async function neoSessionTabs(params) {
  const sessao = await this.requireSession(params);
  const estado = await this.store.getSession(sessao.sessionId);
  const doGrupo = await this.getSessionTabs(sessao.sessionId);
  const vistas = new Set(doGrupo.map((t) => t.id));
  const fora = [];
  let mudou = false;
  for (const [id, origem] of Object.entries(estado.tabOrigins || {})) {
    if (origem !== "site" || vistas.has(Number(id))) continue;
    const tab = await chrome.tabs.get(Number(id)).catch(() => null);
    if (tab) {
      fora.push(tab);
    } else {
      delete estado.tabOrigins[id];
      mudou = true;
    }
  }
  if (mudou) await this.store.save();
  return [...doGrupo, ...fora].map((t) => neoAbaParaFora(t, estado));
};

/** Fecha uma aba que o Neo ou o site abriu. A do usuário, nunca. */
BrowserBackend.prototype.neoCloseTab = async function neoCloseTab(params) {
  const sessao = await this.requireSession(params);
  const tabId = requireTabId(params, "neoCloseTab");
  const estado = await this.store.getSession(sessao.sessionId);
  const origem = estado.tabOrigins?.[String(tabId)];
  if (origem !== "agent" && origem !== "site") {
    throw new Error(`a aba ${tabId} não foi aberta pelo Neo nem pela página — não fecho`);
  }
  await this.detachTab(tabId).catch(() => {});
  await chrome.tabs.remove(tabId).catch(() => {});
  delete estado.tabOrigins[String(tabId)];
  if (estado.tabOpeners) delete estado.tabOpeners[String(tabId)];
  await this.store.save();
  return { ok: true };
};

const neoRequireSessionTabUpstream = BrowserBackend.prototype.requireSessionTab;
BrowserBackend.prototype.requireSessionTab = async function requireSessionTab(params, command) {
  try {
    return await neoRequireSessionTabUpstream.call(this, params, command);
  } catch (erro) {
    const sessao = await this.requireSession(params);
    const estado = await this.store.getSession(sessao.sessionId);
    if (estado.tabOrigins?.[String(params?.tabId)] === "site") {
      return; // popup aberto pela página da sessão, numa janela sem grupo
    }
    throw erro;
  }
};

/** Reivindicar de novo (retomada de tarefa) não rebaixa a origem: uma aba que a
 * página ou o Neo abriu continua fechável pelo Neo. O upstream grava `user` em
 * toda aba reivindicada. */
const neoClaimUserTabUpstream = BrowserBackend.prototype.claimUserTab;
BrowserBackend.prototype.claimUserTab = async function claimUserTab(params) {
  const sessao = await this.requireSession(params);
  const estado = await this.store.getSession(sessao.sessionId);
  const antes = estado.tabOrigins?.[String(params?.tabId)];
  const resultado = await neoClaimUserTabUpstream.call(this, params);
  if (antes === "site" || antes === "agent") {
    estado.tabOrigins[String(params.tabId)] = antes;
    await this.store.save();
  }
  return resultado;
};


// ─────────────────────────────────────────────────────────────────────────────
// Neo — apêndice (4º): a CONVERSA (neo-core/docs/browser-conversa-plan.md).
//
//   • `neoPergunta` — o desktop abre (e fecha) a pergunta do painel por AQUI, e
//     não pelo `postMessage` da faixa. A página consegue imitar o `postMessage`;
//     uma pergunta falsa com botão "Sim" desenhada ao lado da verdadeira ("posso
//     excluir a conta?") viraria autorização forjada. O que vem por aqui chega
//     ao content script por `chrome.tabs.sendMessage`, que a página não alcança.
//   • `NEO_MSG` — o que o Daniel escreveu (ou o botão que clicou) no painel vira
//     a notificação `neo.msg` para o desktop. A `opcao` só passa adiante se for
//     um botão da pergunta que o Neo fez de verdade, com o mesmo texto.
//   • `neo.detach` — por que o navegador soltou o debugger da aba
//     (`target_closed`, `canceled_by_user`). Antes "Debugger unattached" chegava
//     sem motivo (sessão 2bd028fb, três jobs).
// ─────────────────────────────────────────────────────────────────────────────

const NEO_MSG_MAX = 1500;

BrowserBackend.prototype.neoPergunta = async function neoPergunta(params) {
  const tabId = params?.tabId;
  if (!Number.isInteger(tabId)) throw new Error("neoPergunta requires an integer tabId");
  // A conferência antes do Salvar é a pergunta mais longa (um campo por linha,
  // com o valor inteiro); o core já corta em `[navegador.conversa]
  // pergunta_painel_max`. Em 1500 aqui, a apresentação saía pela metade.
  const texto = typeof params?.texto === "string" ? params.texto.trim().slice(0, 8000) : "";
  const opcoes = Array.isArray(params?.opcoes)
    ? params.opcoes
        .filter((o) => typeof o === "string" && o.trim())
        .map((o) => o.trim().slice(0, 80))
        .slice(0, 6)
    : [];
  const pergunta = texto ? { texto, opcoes, at: Date.now() } : null;
  // `active: true` junto: a pergunta só existe com tarefa rodando, e o estado
  // pode ainda não ter chegado do content script (é assíncrono) — sem isto o
  // `neoSetOverlay` apagaria a aba "sem tarefa" levando a pergunta junto.
  await neoSetOverlay(tabId, pergunta ? { pergunta, active: true } : { pergunta: null });
  try {
    await chrome.tabs.sendMessage(tabId, { type: "NEO_PERGUNTA", pergunta });
  } catch {
    /* aba recarregando: a re-montagem do painel traz a pergunta pelo QUERY */
  }
  return { ok: true };
};

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type !== "NEO_MSG") return false;
  const tabId = typeof sender.tab?.id === "number" ? sender.tab.id : -1;
  const texto = typeof message.texto === "string" ? message.texto.trim().slice(0, NEO_MSG_MAX) : "";
  if (tabId < 0 || !texto) {
    sendResponse({ ok: false });
    return true;
  }
  neoReadOverlayState().then((byTab) => {
    const estado = byTab[tabId];
    // Só aba com tarefa do Neo rodando: fora disso não há quem leia.
    if (!estado || estado.active !== true) {
      sendResponse({ ok: false, motivo: "esta aba não tem tarefa do Neo rodando" });
      return;
    }
    const params = { tabId, texto, at: new Date().toISOString() };
    const pergunta = estado.pergunta;
    const i = message.opcao;
    if (
      pergunta &&
      Number.isInteger(i) &&
      i >= 0 &&
      i < (pergunta.opcoes || []).length &&
      pergunta.opcoes[i] === texto
    ) {
      params.opcao = i;
    }
    safeSendNotification(peer, "neo.msg", params);
    sendResponse({ ok: true });
  });
  return true;
});

// O rascunho do campo do painel, por aba: a página que navega (o "Avançar" do
// Google Forms recarrega) remontava o painel com o campo vazio. Fica no
// storage.session (memória, some ao fechar o navegador) e volta pelo QUERY.
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type !== "NEO_RASCUNHO") return false;
  const tabId = typeof sender.tab?.id === "number" ? sender.tab.id : -1;
  const texto = typeof message.texto === "string" ? message.texto.slice(0, NEO_MSG_MAX) : "";
  if (tabId < 0) {
    sendResponse({ ok: false });
    return true;
  }
  neoReadOverlayState().then((byTab) => {
    if (!byTab[tabId]) {
      sendResponse({ ok: false });
      return;
    }
    neoSetOverlay(tabId, { rascunho: texto.trim() ? { texto, at: Date.now() } : null }).then(() =>
      sendResponse({ ok: true }),
    );
  });
  return true;
});

chrome.debugger.onDetach.addListener((source, reason) => {
  if (typeof source?.tabId !== "number") return;
  safeSendNotification(peer, "neo.detach", {
    tabId: source.tabId,
    reason: String(reason ?? ""),
    at: new Date().toISOString(),
  });
});

/**
 * Tira o foco do campo da página e fecha o menu que outra extensão abriu nele.
 *
 * Gerenciador de senhas põe o menu dele (uma página `chrome-extension://` de
 * OUTRA extensão) dentro da aba quando um campo ganha foco, e o navegador então
 * solta o debugger e recusa religar ("Cannot access a chrome-extension:// URL of
 * different extension"). Sessões 98c8bac7 e 42a4ffd4: o Neo tinha que pedir ao
 * Daniel para fechar o menu. Aqui vai pelo `chrome.scripting`, que não depende
 * do debugger: blur no campo, Esc, e remove o iframe de outra extensão que
 * estiver ao alcance (o de shadow root fechado some sozinho com o blur).
 */
BrowserBackend.prototype.neoSoltarMenu = async function neoSoltarMenu(params) {
  const tabId = params?.tabId;
  if (!Number.isInteger(tabId)) throw new Error("neoSoltarMenu requires an integer tabId");
  const [saida] = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      const ativo = document.activeElement;
      let blur = false;
      if (ativo && ativo !== document.body && typeof ativo.blur === "function") {
        ativo.blur();
        blur = true;
      }
      const esc = { key: "Escape", code: "Escape", keyCode: 27, bubbles: true };
      document.dispatchEvent(new KeyboardEvent("keydown", esc));
      document.dispatchEvent(new KeyboardEvent("keyup", esc));
      const nosso = `chrome-extension://${chrome.runtime.id}/`;
      let removidos = 0;
      for (const f of document.querySelectorAll("iframe")) {
        const src = String(f.src || "");
        if (src.startsWith("chrome-extension://") && !src.startsWith(nosso)) {
          f.remove();
          removidos += 1;
        }
      }
      return { blur, removidos };
    },
  });
  return { ok: true, ...(saida?.result || {}) };
};

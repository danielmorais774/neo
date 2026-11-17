const REPO_URL = "https://github.com/iFurySt/open-browser-use";
const REFRESH_INTERVAL_MS = 5000;

const statusPill = document.getElementById("status-pill");
const statusLabel = document.getElementById("status-label");
const statusMessage = document.getElementById("status-message");
const hostName = document.getElementById("host-name");
const lastChecked = document.getElementById("last-checked");
const errorMessage = document.getElementById("error-message");
const repoLink = document.getElementById("repo-link");
const extensionVersion = document.getElementById("extension-version");
const platformLabel = document.getElementById("platform-label");
const cliCommands = document.getElementById("cli-commands");
const chromeApi = globalThis.chrome;

renderExtensionVersion();
renderInstallCommands();
void refreshStatus();
setInterval(() => {
  void refreshStatus();
}, REFRESH_INTERVAL_MS);

repoLink?.addEventListener("click", (event) => {
  if (!chromeApi?.tabs?.create) {
    return;
  }
  event.preventDefault();
  void chromeApi.tabs.create({ url: REPO_URL });
});

async function refreshStatus() {
  if (!chromeApi?.runtime?.sendMessage) {
    renderStatus(undefined);
    return;
  }
  try {
    const response = await chromeApi.runtime.sendMessage({ type: "GET_NATIVE_HOST_STATUS" });
    renderStatus(response?.status);
  } catch {
    if (!chromeApi?.storage?.local?.get) {
      renderStatus(undefined);
      return;
    }
    const value = await chromeApi.storage.local.get("OPEN_BROWSER_USE_NATIVE_HOST_STATUS");
    renderStatus(value.OPEN_BROWSER_USE_NATIVE_HOST_STATUS);
  }
}

function renderStatus(status) {
  const state = normalizeState(status?.state);
  const detail = statusDetails(state, status);
  statusPill.dataset.state = state;
  statusLabel.textContent = detail.label;
  statusMessage.textContent = detail.message;
  hostName.textContent = status?.hostName ?? "com.ifuryst.open_browser_use.extension";
  lastChecked.textContent = formatLastChecked(status?.lastChecked);

  const error = typeof status?.error === "string" ? status.error.trim() : "";
  if (error) {
    errorMessage.hidden = false;
    errorMessage.textContent = error;
  } else {
    errorMessage.hidden = true;
    errorMessage.textContent = "";
  }
}

function renderExtensionVersion() {
  const version = chromeApi?.runtime?.getManifest?.().version;
  if (typeof version === "string" && version.trim() !== "") {
    extensionVersion.textContent = `v${version}`;
  }
}

function renderInstallCommands() {
  const platform = detectPlatform();
  platformLabel.textContent = platform.label;
  cliCommands.replaceChildren(
    ...platform.commands.map((group) => {
      const wrapper = document.createElement("div");
      wrapper.className = "command-group";

      const source = document.createElement("span");
      source.className = "command-source";
      source.textContent = group.label;

      const command = document.createElement("code");
      command.className = "command-line";
      command.textContent = group.command;

      wrapper.append(source, command);
      return wrapper;
    })
  );
}

function detectPlatform() {
  const rawPlatform =
    globalThis.navigator?.userAgentData?.platform ??
    globalThis.navigator?.platform ??
    globalThis.navigator?.userAgent ??
    "";
  const platform = String(rawPlatform).toLowerCase();

  if (platform.includes("mac")) {
    return {
      label: "macOS",
      commands: [
        {
          label: "npm",
          command: "npm install -g open-browser-use && open-browser-use setup"
        },
        {
          label: "Homebrew",
          command: "brew install iFurySt/open-browser-use/open-browser-use && open-browser-use setup"
        }
      ]
    };
  }

  if (platform.includes("win")) {
    return {
      label: "Windows",
      commands: [
        {
          label: "npm",
          command: "npm install -g open-browser-use && open-browser-use setup"
        }
      ]
    };
  }

  if (platform.includes("linux") || platform.includes("x11")) {
    return {
      label: "Linux",
      commands: [
        {
          label: "npm",
          command: "npm install -g open-browser-use && open-browser-use setup"
        }
      ]
    };
  }

  return {
    label: "This OS",
    commands: [
      {
        label: "npm",
        command: "npm install -g open-browser-use && open-browser-use setup"
      }
    ]
  };
}

function normalizeState(state) {
  if (state === "connected" || state === "reconnecting" || state === "disconnected") {
    return state;
  }
  return "unknown";
}

function statusDetails(state, status) {
  if (state === "connected") {
    return {
      label: "Connected",
      message: "Native host is connected and ready."
    };
  }
  if (state === "reconnecting") {
    const attempt =
      Number.isInteger(status?.reconnectAttempt) && status.reconnectAttempt > 0
        ? ` Attempt ${status.reconnectAttempt}.`
        : "";
    return {
      label: "Reconnecting",
      message: `Native host is reconnecting.${attempt}`
    };
  }
  if (state === "disconnected") {
    return {
      label: "Disconnected",
      message: "Native host is not connected."
    };
  }
  return {
    label: "Unknown",
    message: "Native host status is unavailable."
  };
}

function formatLastChecked(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "Unknown";
  }
  return new Date(value).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  });
}

// ── Inspetor do Neo ─────────────────────────────────────────────────  [FORK]
//
// Manda o content script desenhar o inventário na aba ativa. O popup não sabe
// mapear nada: ele só pede, e mostra o resumo do que voltou.

const mapToggle = document.getElementById("map-toggle");
const mapRefresh = document.getElementById("map-refresh");
const mapSummary = document.getElementById("map-summary");
const mapCount = document.getElementById("map-count");

let mapAtivo = false;

function renderMapa(resposta) {
  if (!resposta) {
    mapSummary.hidden = false;
    mapSummary.textContent =
      "Esta aba não aceita o inspetor. Páginas do próprio navegador " +
      "(chrome://, edge://, a loja de extensões) bloqueiam content scripts — " +
      "abra um site normal e tente de novo.";
    mapCount.textContent = "—";
    return;
  }
  if (resposta.ok === false) {
    mapSummary.hidden = false;
    mapSummary.textContent = `Não deu para mapear: ${resposta.erro || "erro desconhecido"}`;
    return;
  }

  mapAtivo = resposta.ativo === true;
  mapToggle.textContent = mapAtivo ? "Limpar marcações" : "Mapear elementos";
  mapRefresh.hidden = !mapAtivo;

  if (!mapAtivo) {
    mapSummary.hidden = true;
    mapCount.textContent = "—";
    return;
  }

  mapCount.textContent = `${resposta.total} elementos`;
  const origens = Object.entries(resposta.porOrigem || {})
    .map(([chave, n]) => `${chave} ${n}`)
    .join(" · ");
  const linhas = [`${resposta.total} elementos em ${resposta.ms} ms — ${origens}`];
  if (resposta.cobertos) {
    linhas.push(`${resposta.cobertos} com algo na frente (não dá para clicar)`);
  }
  // O que a allowlist antiga NÃO via. É a linha que responde "a correção pegou
  // neste site?" sem precisar ler diário nenhum.
  if (resposta.amostraNova?.length) {
    linhas.push("", "Só aparecem desde a correção:", ...resposta.amostraNova);
  } else {
    linhas.push("", "Nesta página o markup canônico já bastava.");
  }
  mapSummary.hidden = false;
  mapSummary.textContent = linhas.join("\n");
}

// O inspetor NÃO é carregado em toda página: são 33 KB de mapeador para uma
// ferramenta usada de vez em quando. Ele é injetado sob demanda — o que também
// resolve a aba que já estava aberta quando a extensão foi recarregada, caso em
// que o content script registrado no manifest simplesmente não está lá.
async function enviar(tabId, modo) {
  try {
    return await chromeApi.tabs.sendMessage(tabId, { type: "NEO_MAP", modo });
  } catch {
    // Rejeita quando não há NENHUM content script na aba. `undefined` (sem
    // exceção) é outro caso: há content script, mas não o nosso.
    return undefined;
  }
}

async function injetar(tabId) {
  await chromeApi.scripting.executeScript({
    target: { tabId },
    files: ["neo-map.generated.js", "content-inspect.js"],
  });
}

async function pedirMapa(modo) {
  if (!chromeApi?.tabs) return;
  try {
    const [aba] = await chromeApi.tabs.query({ active: true, currentWindow: true });
    if (!aba?.id) return renderMapa(null);

    let resposta = await enviar(aba.id, modo);
    if (resposta === undefined) {
      // Ainda não injetado nesta aba. Injetar é idempotente (o content script
      // tem guarda de instalação), então tentar de novo é seguro.
      await injetar(aba.id);
      resposta = await enviar(aba.id, modo);
    }
    renderMapa(resposta ?? null);
  } catch {
    // `executeScript` levanta em páginas onde a extensão não pode entrar:
    // chrome://, edge://, a loja, PDFs. Aí sim é limitação do navegador.
    renderMapa(null);
  }
}

mapToggle?.addEventListener("click", () => void pedirMapa("toggle"));
mapRefresh?.addEventListener("click", () => void pedirMapa("refresh"));

// ── Conta do Neo: parear esta superfície ────────────────────────────────────
//
// O campo é de CÓDIGO (6 dígitos, do painel), não de token. Ver a nota no
// apêndice do background.js: código é de uso único e expira em 5 min, então
// vazar não custa nada — token colado à mão precisa ser visto, copiado e
// guardado, e credencial que se vê acaba em screenshot.

const surfPill = document.getElementById("surf-pill");
const surfMsg = document.getElementById("surf-msg");
const surfForm = document.getElementById("surf-form");
const surfCode = document.getElementById("surf-code");
const surfName = document.getElementById("surf-name");
const surfCore = document.getElementById("surf-core");
const surfTabs = document.getElementById("surf-tabs");
const surfPair = document.getElementById("surf-pair");
const surfUnpair = document.getElementById("surf-unpair");

function surfDizer(texto, tipo) {
  if (!surfMsg) return;
  surfMsg.hidden = !texto;
  surfMsg.textContent = texto || "";
  surfMsg.style.color =
    tipo === "erro" ? "#f87171" : tipo === "ok" ? "#4ade80" : "";
}

function surfPintar(s) {
  if (!surfPill) return;
  // Três estados, três ações diferentes — juntar "pareado" com "conectado"
  // esconderia justamente o caso "credencial boa, core fora do ar".
  surfPill.textContent = s.conectada ? "conectado" : s.pareada ? "pareado" : "não pareado";
  surfForm.hidden = !!s.pareada;
  surfUnpair.hidden = !s.pareada;
  if (surfCore && document.activeElement !== surfCore) surfCore.value = s.coreUrl || "";
  if (surfTabs) surfTabs.checked = !!s.reportarAbas;
  if (s.erro) surfDizer(s.erro, "erro");
  else if (s.pareada && !s.conectada) {
    surfDizer("Pareado, mas sem canal — o neo-core está acessível? (túnel)", null);
  } else if (s.conectada) {
    surfDizer("", null);
  }
}

function surfAtualizar() {
  try {
    chromeApi?.runtime?.sendMessage({ type: "NEO_SURFACE_STATUS" }, (s) => {
      if (chromeApi.runtime.lastError || !s) return;
      surfPintar(s);
    });
  } catch {
    /* worker reciclando */
  }
}

surfPair?.addEventListener("click", () => {
  const code = (surfCode.value || "").replace(/\D/g, "");
  if (code.length !== 6) {
    surfDizer("O código tem 6 dígitos — gere um no painel (Dispositivos).", "erro");
    return;
  }
  surfDizer("Pareando…", null);
  chromeApi.runtime.sendMessage(
    {
      type: "NEO_SURFACE_PAIR",
      code,
      nome: surfName.value,
      coreUrl: surfCore?.value,
    },
    (r) => {
      if (chromeApi.runtime.lastError || !r) return surfDizer("O worker não respondeu.", "erro");
      if (!r.ok) return surfDizer(r.erro || "não consegui parear", "erro");
      surfCode.value = "";
      surfDizer(
        r.host
          ? `Pareado e vinculado à máquina. Nome: ${r.nome}`
          : `Pareado como "${r.nome}". Sem vínculo de máquina — o nome é o que endereça.`,
        "ok",
      );
      setTimeout(surfAtualizar, 800);
    },
  );
});

surfUnpair?.addEventListener("click", () => {
  chromeApi.runtime.sendMessage({ type: "NEO_SURFACE_UNPAIR" }, () => {
    surfDizer("Despareado.", null);
    setTimeout(surfAtualizar, 300);
  });
});

surfTabs?.addEventListener("change", () => {
  chromeApi.runtime.sendMessage(
    { type: "NEO_SURFACE_SET_TABS", ligado: surfTabs.checked },
    () => setTimeout(surfAtualizar, 400),
  );
});

surfAtualizar();
setInterval(surfAtualizar, 2500);

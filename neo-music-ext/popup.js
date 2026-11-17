// Popup UI for the Neo Music Bridge: shows the connection status to the Neo
// Desktop app and the music tabs currently being reported. Queries the background
// service worker via runtime messaging and auto-refreshes.

function mmss(sec) {
  if (typeof sec !== "number" || !isFinite(sec) || sec < 0) return "";
  const t = Math.floor(sec);
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = t % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return (h ? h + ":" : "") + mm + ":" + String(s).padStart(2, "0");
}

// Resumo dos sinais de UMA aba, na mesma leitura que o Neo faz.
function signals(t) {
  const out = [];
  if (t.hasMedia === false) out.push("sem vídeo aberto");
  else if (t.paused === true) out.push("pausado");
  else if (t.paused === false) out.push("tocando");
  if (t.adShowing) out.push("anúncio");
  if (t.contentType === "music") out.push("música");
  else if (t.contentType === "video") out.push("vídeo");
  if (t.channel) out.push("canal: " + t.channel);
  if (t.live) out.push("ao vivo");
  else if (typeof t.duration === "number") {
    const pos = mmss(t.currentTime);
    const dur = mmss(t.duration);
    if (pos && dur) out.push(pos + "/" + dur);
  }
  if (t.audible) out.push("som na guia");
  else if (t.muted) out.push("guia muda");
  else if (t.pageMuted) out.push("player mudo");
  if (t.kind && t.kind !== "watch") out.push(t.kind);
  if (t.probe && t.probe !== "ok") out.push("sonda: " + t.probe);
  return out.join(" · ");
}

function render(status) {
  const dot = document.getElementById("dot");
  const label = document.getElementById("label");
  const sub = document.getElementById("sub");
  const tabsEl = document.getElementById("tabs");
  const empty = document.getElementById("empty");
  const port = document.getElementById("port");

  dot.className = "dot";
  if (status.connected) {
    dot.classList.add("on");
    label.textContent = "Conectado ao Neo Desktop";
    sub.textContent = "ponte ativa";
  } else if (status.connecting) {
    dot.classList.add("connecting");
    label.textContent = "Conectando…";
    sub.textContent = "tentando alcançar o Neo Desktop";
  } else {
    dot.classList.add("off");
    label.textContent = "Desconectado";
    // A ponte agora exige credencial, e "desconectado" tem três causas
    // diferentes que pedem ações diferentes. Dizer qual é poupa a caça ao erro.
    sub.textContent =
      status.motivo === "sem_token"
        ? "falta a credencial — cole o token abaixo"
        : status.motivo === "recusado"
          ? "conexão recusada — token errado, ou o Neo Desktop está fechado"
          : "o Neo Desktop está aberto?";
  }

  // Estado da credencial, sem nunca exibir o valor guardado.
  const tokenHint = document.getElementById("tokenHint");
  if (tokenHint && !tokenHint.dataset.dirty) {
    if (status.connected) {
      tokenHint.className = "token-hint ok";
      tokenHint.textContent = "✓ credencial aceita pela ponte";
    } else if (status.hasToken) {
      tokenHint.className = "token-hint";
      tokenHint.textContent = "Token salvo. Se segue recusando, copie de novo no Neo Desktop.";
    } else {
      tokenHint.className = "token-hint err";
      tokenHint.textContent =
        'Sem token. Aba Dispositivos do Neo Desktop → cartão "Extensão de música" → Copiar.';
    }
  }
  if (status.wsUrl) port.textContent = status.wsUrl;

  // Fila do Neo em execução: mostra a fase do item e o código do despacho — é
  // o mesmo código que aparece na linha da fila no painel, então dá para casar
  // "o que estou ouvindo" com "o que o Neo acha que está tocando".
  const controlled = document.getElementById("controlled");
  const controlledSub = document.getElementById("controlledSub");
  if (controlled) {
    const w = status.watch;
    controlled.hidden = !w;
    if (w) {
      const fase =
        {
          armada: "aguardando o player começar",
          tocando: "tocando",
          terminando: "terminando — já pedi a próxima",
          terminou: "faixa encerrada",
        }[w.fase] || w.fase;
      controlledSub.textContent =
        fase + (w.codigo ? " · código " + w.codigo : "") + (w.videoId ? " · " + w.videoId : "");
    }
  }

  const tabs = Array.isArray(status.tabs) ? status.tabs : [];
  tabsEl.textContent = "";
  empty.hidden = tabs.length > 0;
  for (const t of tabs) {
    const li = document.createElement("li");
    li.className = "tab";

    const site = document.createElement("span");
    site.className = "site " + (t.site || "");
    site.textContent = t.site || "?";

    const title = document.createElement("span");
    title.className = "t";
    title.textContent = t.title || t.url || "(sem título)";

    li.appendChild(site);
    li.appendChild(title);

    if (t.playing) {
      const p = document.createElement("span");
      p.className = "playing";
      p.textContent = "▶ tocando";
      li.appendChild(p);
    }

    // Segunda linha: exatamente os sinais que vão para o Neo. Serve para
    // conferir a olho que a sonda está lendo a página (e para ver POR QUE ela
    // não leu, quando o navegador recusa a injeção).
    const sig = signals(t);
    if (sig) {
      const s = document.createElement("span");
      s.className = "sig";
      s.textContent = sig;
      li.appendChild(s);
    }
    tabsEl.appendChild(li);
  }
}

function refresh() {
  try {
    chrome.runtime.sendMessage({ type: "getStatus" }, (status) => {
      if (chrome.runtime.lastError || !status) {
        render({ connected: false, connecting: false, tabs: [] });
        return;
      }
      render(status);
    });
  } catch {
    render({ connected: false, connecting: false, tabs: [] });
  }
}

document.getElementById("reconnect").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "reconnect" }, () => {
    setTimeout(refresh, 400);
  });
});

// ── Superfície do neo-core ──────────────────────────────────────────────────
//
// Separado da "credencial da ponte" de propósito: são DUAS credenciais com donos
// diferentes. A da ponte autentica esta extensão no Neo Desktop; esta autentica
// este navegador no neo-core, e é ela que continua valendo com o desktop fechado.

function pintarSuperficie(s) {
  const alvo = document.getElementById("surfState");
  const parear = document.getElementById("surfPair");
  const desparear = document.getElementById("surfUnpair");
  if (!alvo) return;

  if (s.erro) {
    alvo.className = "token-hint err";
    alvo.textContent = s.erro;
  } else if (s.conectada) {
    alvo.className = "token-hint ok";
    alvo.textContent = "✓ pareado e conectado ao Neo — " + s.coreUrl;
  } else if (s.pareada) {
    alvo.className = "token-hint";
    alvo.textContent = "Pareado, reconectando… (" + s.coreUrl + ")";
  } else {
    alvo.className = "token-hint";
    alvo.textContent =
      "Não pareado. Parear deixa o Neo saber quais abas estão abertas aqui, " +
      "mesmo com o Neo Desktop fechado. Você autoriza numa janela do app.";
  }
  parear.hidden = !!s.pareada;
  desparear.hidden = !s.pareada;
}

function atualizarSuperficie() {
  try {
    chrome.runtime.sendMessage({ type: "surfaceStatus" }, (s) => {
      if (chrome.runtime.lastError || !s) return;
      pintarSuperficie(s);
    });
  } catch {
    /* worker reciclando */
  }
}

document.getElementById("surfPair").addEventListener("click", () => {
  const alvo = document.getElementById("surfState");
  alvo.className = "token-hint";
  alvo.textContent = "Autorize na janela do Neo Desktop…";
  chrome.runtime.sendMessage({ type: "surfacePair" }, () => {
    setTimeout(atualizarSuperficie, 1200);
  });
});

document.getElementById("surfUnpair").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "surfaceUnpair" }, () => {
    setTimeout(atualizarSuperficie, 300);
  });
});

document.getElementById("tokenSave").addEventListener("click", () => {
  const input = document.getElementById("token");
  const hint = document.getElementById("tokenHint");
  const valor = (input.value || "").trim();
  if (!valor) {
    hint.dataset.dirty = "1";
    hint.className = "token-hint err";
    hint.textContent = "Cole o token antes de salvar.";
    setTimeout(() => delete hint.dataset.dirty, 2500);
    return;
  }
  chrome.runtime.sendMessage({ type: "setToken", token: valor }, (res) => {
    hint.dataset.dirty = "1";
    if (chrome.runtime.lastError || !res || !res.ok) {
      hint.className = "token-hint err";
      hint.textContent = "Não consegui guardar o token.";
    } else {
      hint.className = "token-hint ok";
      hint.textContent = "Token guardado — reconectando…";
      // Não deixa a credencial no campo depois de salva.
      input.value = "";
    }
    setTimeout(() => {
      delete hint.dataset.dirty;
      refresh();
    }, 1600);
  });
});

document.getElementById("test").addEventListener("click", () => {
  const el = document.getElementById("testResult");
  el.className = "test-result";
  el.textContent = "enviando…";
  chrome.runtime.sendMessage({ type: "testPlay" }, (res) => {
    if (chrome.runtime.lastError || !res || !res.ok) {
      el.className = "test-result err";
      const reason =
        (res && res.error) ||
        (chrome.runtime.lastError && chrome.runtime.lastError.message) ||
        "erro desconhecido";
      el.textContent = "Falhou: " + reason;
    } else {
      el.className = "test-result ok";
      el.textContent = res.reused
        ? "✓ Navegou a aba do YouTube existente"
        : "✓ Abriu uma nova aba do YouTube";
    }
    setTimeout(refresh, 500);
  });
});

refresh();
atualizarSuperficie();
setInterval(refresh, 1500);
setInterval(atualizarSuperficie, 2500);

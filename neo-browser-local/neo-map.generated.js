// GERADO POR scripts/gerar-mapa.py — NÃO EDITE À MÃO.
//
// Fonte: neo-desktop-tauri/src-tauri/src/services/browser_local.rs (`js_snapshot`).
// Este é, literalmente, o mesmo código que o neo-core injeta por CDP a cada
// passo de uma tarefa. O inspetor visual (content-inspect.js) usa ele para
// desenhar na tela EXATAMENTE o que o agente enxerga — se divergisse, o
// inspetor viraria um segundo mapeador para manter, e o teste deixaria de valer.
//
// Roda no mundo ISOLADO do content script: o `window.__neo` daqui é outro
// objeto, sem relação com o do mundo principal onde o driver trabalha.

globalThis.__neoMapear = function (MAX, CHARS) {

(() => {
  const NEO = (window.__neo = window.__neo || {});
  NEO.refs = NEO.refs || [];
  NEO.visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none') return false;
    if (parseFloat(s.opacity || '1') < 0.05) return false;
    return r.bottom > -200 && r.top < innerHeight + 2000;
  };
  // Nome do elemento. A ordem importa e foi calibrada contra páginas reais
  // (github.com): com a versão ingênua (aria-label || innerText || title),
  // 37% dos elementos vinham SEM rótulo — botão de ícone e link que embrulha
  // SVG. Um elemento sem nome é ruído: o modelo não tem como escolher "clica
  // no [4]" se [4] não tem nome, e clica errado.
  NEO.label = (el) => {
    const clean = (v) => String(v || '').replace(/\s+/g, ' ').trim();
    const fromIds = (ids) =>
      clean(
        ids
          .split(/\s+/)
          .map((id) => {
            const node = document.getElementById(id);
            return node ? node.innerText || node.textContent || '' : '';
          })
          .join(' ')
      );
    let out = clean(el.getAttribute('aria-label'));
    if (!out && el.getAttribute('aria-labelledby')) out = fromIds(el.getAttribute('aria-labelledby'));
    if (!out) out = clean(el.getAttribute('placeholder'));
    if (!out && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) out = clean(el.value);
    if (!out) out = clean(el.innerText);
    // Texto de leitor de tela (.sr-only) some do innerText e sobrevive no
    // textContent — mas o textContent também traz o CONTEÚDO de <style> e
    // <script>, que o innerText corretamente ignora por não ser renderizado.
    // Sem esta guarda, um contêiner com um <style> dentro vinha rotulado
    // `.A8SBwf{--client-adapter-margin-base:1` (medido no google.com).
    if (!out && !el.querySelector('style,script,noscript')) out = clean(el.textContent);
    if (!out) {
      const inner = el.querySelector('[aria-label], svg title, img[alt]');
      if (inner) {
        out = clean(
          inner.getAttribute('aria-label') || inner.textContent || inner.getAttribute('alt')
        );
      }
    }
    if (!out) out = clean(el.getAttribute('title') || el.getAttribute('name') || el.getAttribute('alt'));
    // Último recurso para link: o caminho da URL diz mais que nada.
    if (!out && el.tagName === 'A') {
      try {
        const url = new URL(el.href);
        out = clean((url.pathname + url.search).replace(/^\//, '')) || url.hostname;
      } catch (_) {
        /* href relativo/inválido */
      }
    }
    return out.slice(0, 120);
  };
  // Nome de um ITEM de lista. O innerText inteiro de uma linha do Drive é
  // "Print\nem Meu Drive\nMais ações\n…" — o que identifica é só a primeira
  // linha. É este nome que vira o rótulo da linha e o "dono" dos botões dela.
  NEO.shortLabel = (el) => {
    const raw = String(el.getAttribute('aria-label') || el.innerText || el.textContent || '');
    const first = raw.split('\n').map((s) => s.trim()).filter(Boolean)[0] || '';
    return first.replace(/\s+/g, ' ').slice(0, 60);
  };
  // A ESTRUTURA de um rádio de grade (vai em `item.grade`), para o core montar linhas × colunas em
  // qualquer site (core/browser_local/grade.py). Antes o core só lia o Google
  // Forms em português: dependia de o `radiogroup` entrar no inventário por
  // acaso (o Google põe `jsaction` nele) e do rótulo "X, resposta para Y". Uma
  // matriz em <table> de rádios comuns (SurveyMonkey, Moodle) dava zero
  // perguntas, e o Google em inglês embaralhava as colunas.
  //   grupo    — quem é a "linha": o radiogroup ARIA ou o `name` do rádio HTML
  //   linha    — o nome dela: o do radiogroup, ou o cabeçalho da linha na tabela
  //   coluna   — o cabeçalho da coluna, quando o rádio está numa tabela
  //   pergunta — o enunciado: legend, caption ou o título do item da lista
  NEO.grade = (el) => {
    const clean = (v) => String(v || '').replace(/\s+/g, ' ').trim();
    const ctl = el.tagName === 'LABEL' && el.control ? el.control : el;
    const nativo = ctl.tagName === 'INPUT' && String(ctl.type || '').toLowerCase() === 'radio';
    const aria = ctl.getAttribute('role') === 'radio';
    if (!nativo && !aria) return null;
    const nomeAria = (n) => {
      let out = clean(n.getAttribute('aria-label'));
      const ids = n.getAttribute('aria-labelledby');
      if (!out && ids) {
        out = clean(ids.split(/\s+/).map((id) => {
          const x = document.getElementById(id);
          return x ? x.innerText || x.textContent || '' : '';
        }).join(' '));
      }
      return out.slice(0, 200);
    };
    const out = {};
    const grupo = ctl.closest('[role=radiogroup]');
    if (grupo) {
      NEO._grupos = NEO._grupos || new WeakMap();
      if (!NEO._grupos.has(grupo)) NEO._grupos.set(grupo, (NEO._nGrupos = (NEO._nGrupos || 0) + 1));
      out.grupo = 'g' + NEO._grupos.get(grupo);
      // Sem nome ARIA, o texto do grupo serve só se for UMA linha ("Clareza"):
      // o de uma escolha simples traz as opções, uma por linha.
      const texto = String(grupo.innerText || '').trim();
      const nome = nomeAria(grupo) || (texto && !texto.includes('\n') ? clean(texto).slice(0, 200) : '');
      if (nome) out.linha = nome;
    } else if (nativo && ctl.name) {
      out.grupo = 'n:' + ctl.name;
    } else return null;
    const celula = ctl.closest('td,th,[role=gridcell],[role=cell]');
    const tr = celula && celula.closest('tr,[role=row]');
    const tabela = tr && tr.closest('table,[role=grid],[role=table]');
    if (celula && tr && tabela) {
      const cells = Array.from(tr.children);
      const i = cells.indexOf(celula);
      const topo = tabela.querySelector('thead tr') || tabela.querySelector('tr,[role=row]');
      if (topo && topo !== tr && i >= 0 && topo.children[i]) {
        const cab = clean(topo.children[i].innerText).slice(0, 80);
        if (cab) out.coluna = cab;
      }
      if (!out.linha) {
        const rotulo = cells.find((c) => c !== celula && !c.querySelector('input,[role=radio]') && clean(c.innerText));
        if (rotulo) out.linha = clean(rotulo.innerText).slice(0, 200);
      }
      const cap = tabela.querySelector('caption');
      if (cap && clean(cap.innerText)) out.pergunta = clean(cap.innerText).slice(0, 300);
      else if (nomeAria(tabela)) out.pergunta = nomeAria(tabela);
    }
    if (!out.pergunta) {
      const legenda = ctl.closest('fieldset') && ctl.closest('fieldset').querySelector('legend');
      if (legenda && clean(legenda.innerText)) out.pergunta = clean(legenda.innerText).slice(0, 300);
    }
    if (!out.pergunta) {
      // Google Forms (e outros): a pergunta é um item de lista com um título.
      const item = ctl.closest('[role=listitem]');
      const titulo = item && item.querySelector('[role=heading]');
      if (titulo && clean(titulo.innerText)) out.pergunta = clean(titulo.innerText).slice(0, 300);
    }
    return out;
  };
  NEO.el = (ref) => {
    const el = NEO.refs[ref];
    if (!el || !el.isConnected) return null;
    return el;
  };
  // CAMPO DE ARQUIVO. Clicar nele (ou no label dele) abre a janela de arquivos
  // do SISTEMA, que nenhuma ação alcança: a tarefa ficava presa ali. O caminho é
  // o `browser.upload`, que põe o arquivo direto no input. Estes helpers acham o
  // input a partir do que o modelo enxerga: o próprio campo, o label, o
  // procurador (`NEO.controles`) ou a caixa que o embrulha.
  NEO.ARQ = 'input[type=file]';
  NEO.arquivoDe = (el) => {
    if (!el || !el.matches) return null;
    if (el.matches(NEO.ARQ)) return el;
    const ctl = NEO.controles && NEO.controles.get(el);
    if (ctl && ctl.matches && ctl.matches(NEO.ARQ)) return ctl;
    if (el.tagName === 'LABEL' && el.control && el.control.matches(NEO.ARQ)) return el.control;
    const dentro = el.querySelector && el.querySelector(NEO.ARQ);
    return dentro || null;
  };
  // O único campo de arquivo das caixas em volta (até 4 níveis): o botão
  // "Alterar foto" ao lado de um input escondido.
  NEO.arquivoPerto = (el) => {
    let p = el && el.parentElement;
    for (let i = 0; p && i < 4; i++, p = p.parentElement) {
      const todos = p.querySelectorAll(NEO.ARQ);
      if (todos.length === 1) return todos[0];
      if (todos.length > 1) return null;
    }
    return null;
  };
  // Botão que abre o campo por código (`input.click()` no handler): logo depois
  // de um clique NOSSO, a janela não abre — o input fica guardado para o
  // `upload`. Só nessa janela de tempo: o clique do Daniel na mesma página
  // continua abrindo a janela normalmente.
  if (!NEO.arquivoTravado) {
    NEO.arquivoTravado = true;
    const proto = HTMLInputElement.prototype;
    const clicarOriginal = proto.click;
    const seletorOriginal = proto.showPicker;
    const segurar = (inp) => {
      if (inp.type !== 'file' || !(Date.now() < (NEO.travaArquivoAte || 0))) return false;
      NEO.arquivoPedido = inp;
      return true;
    };
    proto.click = function () {
      if (segurar(this)) return undefined;
      return clicarOriginal.apply(this, arguments);
    };
    if (seletorOriginal) {
      proto.showPicker = function () {
        if (segurar(this)) return undefined;
        return seletorOriginal.apply(this, arguments);
      };
    }
  }
  // MOLDURA do site, em UM lugar. Era uma string literal dentro do
  // `js_snapshot` e o `js_extract` não tinha acesso a ela — então o extract
  // devolvia 1500 caracteres de menu sem nenhum sinal de que era menu, e
  // leitura certa chegava ao modelo idêntica a leitura errada. Repetir o
  // seletor nos dois lugares criaria duas verdades, que é exatamente como esta
  // base já mentiu com autoridade uma vez.
  //
  // Só landmark DECLARADO, de propósito (ver a seção "moldura do site" do
  // inventário): errar para menos aqui é barato, errar para mais esconde a
  // resposta.
  //
  // `header` e `footer` só são banner e rodapé DO SITE fora de um bloco de
  // conteúdo: dentro de article, aside, main, nav ou section, o HTML os define
  // como cabeçalho e rodapé daquele bloco (é a regra que o leitor de tela usa).
  // O seletor pegava qualquer um, e o enunciado de um questionário, que mora no
  // `<header>` do cartão, sumia da leitura: nos jobs 2611e76d e 7b73f755
  // (12axes) o modelo leu "01" e cinco opções, sem a pergunta e sem o
  // "Question 1 of 36", e reiniciou o quiz em loop. O título e o autor de toda
  // notícia em `<article><header>` iam junto.
  NEO.BLOCO = 'article,aside,main,nav,section,' +
    '[role=article],[role=complementary],[role=main],[role=navigation],[role=region]';
  NEO.CHROME = 'nav,[role=navigation],[role=banner],[role=contentinfo],' +
    'header:not(:is(' + NEO.BLOCO + ') header),' +
    'footer:not(:is(' + NEO.BLOCO + ') footer)';
  // Conjunto AMPLO, só para DESCONTAR texto na eleição do conteúdo principal.
  // Não carimba elemento nenhum e não esconde texto de ninguém: muda apenas
  // QUAL bloco é adicionado na frente. Por isso pode ser mais largo que o
  // `NEO.CHROME`, onde errar para mais é caro.
  NEO.CHROME_DESCONTO = NEO.CHROME + ',aside,[role=complementary],[role=search]';
  // Profundidade no DOM — o desempate da eleição do principal.
  NEO.fundura = (el) => {
    let n = 0;
    for (let p = el; p; p = p.parentElement) n++;
    return n;
  };
  // ── O PAINEL DO NEO NÃO É A PÁGINA ─────────────────────────────────────
  // `elementFromPoint` devolve o host do overlay da extensão quando o ponto cai
  // nele, e esse host é um `div` fixo de shadow fechado, sem nome. Todo elemento
  // embaixo do painel saía `⟪COBERTO por "div"⟫`: 0 ocorrências nos jobs de antes
  // de a faixa virar painel lateral, 540 no job 45e85695. E a regra do prompt
  // para COBERTO é "dispense o intruso primeiro" — o próprio Neo fabricava o
  // obstáculo que depois gastava passos tentando tirar da frente.
  NEO.NOSSOS = '#neo-browser-local-overlay,#neo-marks,#neo-map-overlay';
  // `elementFromPoint` de verdade: pula o que é nosso e atravessa shadow DOM
  // aberto, senão o ponto para no host do web component e nada lá dentro existe.
  NEO.topo = (x, y, doc) => {
    const d = doc || document;
    let el = null;
    try { el = d.elementFromPoint(x, y); } catch (_) { return null; }
    if (el && el.matches(NEO.NOSSOS)) {
      el = null;
      try {
        for (const c of d.elementsFromPoint(x, y)) {
          if (!c.matches(NEO.NOSSOS)) { el = c; break; }
        }
      } catch (_) { return null; }
    }
    for (let n = 0; el && el.shadowRoot && n < 8; n++) {
      let dentro = null;
      try { dentro = el.shadowRoot.elementFromPoint(x, y); } catch (_) { break; }
      if (!dentro || dentro === el) break;
      el = dentro;
    }
    return el;
  };
  // `contains` e `closest` param na fronteira do shadow; estes dois atravessam.
  NEO.contem = (a, b) => {
    if (a.contains(b)) return true;
    for (let n = b; n; ) {
      if (n === a) return true;
      const pai = n.parentNode;
      n = pai && pai.host ? pai.host : pai;
    }
    return false;
  };
  NEO.subir = (el, sel) => {
    for (let n = el; n; ) {
      const achou = n.closest(sel);
      if (achou) return achou;
      const raiz = n.getRootNode();
      n = raiz && raiz.host ? raiz.host : null;
    }
    return null;
  };
  // As raízes onde procurar. `querySelectorAll` não entra em shadow DOM, e web
  // component é como Salesforce, Reddit e todo design system corporativo
  // desenham a tela: sem isto, esses sites simplesmente não têm elementos.
  NEO.raizes = (doc) => {
    const out = [doc || document];
    for (let i = 0; i < out.length && out.length < 80; i++) {
      for (const el of out[i].querySelectorAll('*')) {
        if (el.shadowRoot) out.push(el.shadowRoot);
      }
    }
    return out;
  };
  NEO.todos = (sel, raizes) => {
    const rs = raizes || NEO.raizes();
    if (rs.length === 1) return document.querySelectorAll(sel);
    const out = [];
    for (const r of rs) out.push(...r.querySelectorAll(sel));
    return out;
  };
  // O endereço de uma imagem. `data:` é a miniatura embutida do carregamento
  // preguiçoso, e o endereço de verdade fica em `data-src` ou no `srcset`.
  NEO.srcDe = (img) => {
    const direto = String(img.currentSrc || img.src || '');
    if (direto && !direto.startsWith('data:')) return direto;
    const lazy = img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || '';
    if (lazy) return lazy;
    const conjunto = img.getAttribute('data-srcset') || img.getAttribute('srcset') || '';
    return conjunto ? conjunto.split(',')[0].trim().split(/\s+/)[0] : '';
  };
  NEO.normaliza = (v) => String(v || '')
    .normalize('NFD').replace(/[̀-ͯ]/g, '')
    .replace(/\s+/g, ' ').trim().toLowerCase();

  // ── LEITURA: o texto que um humano enxerga ─────────────────────────────
  // O snapshot lia `body.innerText`, que sai em ordem de DOCUMENTO: todo site põe
  // a navegação primeiro, então o orçamento acabava antes do conteúdo. Os
  // remendos (`principal`, teto do corpo) só mudavam QUAL pedaço do DOM ia na
  // frente — e bastava a eleição errar para o modelo ficar cego. No job 45e85695
  // ela elegeu o carrinho em 19 snapshots de página de produto: em 15 o preço do
  // produto, que estava na tela, não chegou ao prompt, e nos outros 4 só apareceu
  // enterrado numa linha de variação.
  //
  // Aqui a pergunta é outra: o que está NA JANELA agora? Independe de landmark,
  // de framework e de eleição, e rolar passa a trazer texto novo, que é o que
  // qualquer um espera. Medido na mesma página: 1.385 caracteres em 11 ms, com
  // preço, parcelas, frete, estoque, vendedor e nota.
  //
  // Ordem de documento dentro da janela, e não ordenação por coordenada: o DOM
  // respeita coluna, e ordenar por `top` intercalaria as colunas linha a linha.
  NEO.SEM_TEXTO = /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|OPTION|OPTGROUP|DATALIST|TEXTAREA|TITLE)$/i;
  // Tapado = atrás de uma camada FIXA e OPACA. Errar para menos é o lado seguro:
  // o erro oposto some com texto visível. Card com `<a>` transparente esticado
  // por cima (o `stretched-link`, comuníssimo em lista de produto) pegaria o
  // ponto do hit-test e levaria o texto inteiro junto se bastasse "não me acertou".
  NEO.tapado = (el, r, gasto) => {
    const x = (Math.max(r.left, 0) + Math.min(r.right, innerWidth)) / 2;
    const y = (Math.max(r.top, 0) + Math.min(r.bottom, innerHeight)) / 2;
    const topo = NEO.topo(x, y);
    if (!topo || topo === el || NEO.contem(el, topo) || NEO.contem(topo, el)) return false;
    let q = topo, opaco = false;
    for (let n = 0; q && q !== document.body && q.nodeType === 1 && n < 8; n++) {
      gasto();
      const s = getComputedStyle(q);
      const cor = s.backgroundColor.match(/[\d.]+/g) || [];
      const alfa = cor.length >= 4 ? parseFloat(cor[3]) : cor.length === 3 ? 1 : 0;
      if (alfa >= 0.9 || s.backgroundImage !== 'none') opaco = true;
      if (s.position === 'fixed') return opaco && !NEO.contem(q, el);
      q = q.parentElement;
    }
    return false;
  };
  // Devolve `tela` (na janela agora) e `resto` (existe, mas fora dela). À ESQUERDA
  // da página não conta como "resto": x negativo não se alcança rolando, é onde
  // moram os skip-links e o painel de atalhos de teclado da Amazon.
  //
  // `{ recorte: true }` respeita o texto que o CSS esconde sem tirar do DOM:
  // caixa de altura fixa com `overflow: hidden`, `line-clamp`, reticências,
  // caixa com rolagem. Sem isto a caixa estar na janela bastava para TODO o
  // texto dela contar como tela. No job 21c563c1 (Portal) a lista lateral
  // mostrava duas linhas de cada uma das 10 questões e despejava os 10
  // enunciados inteiros: 11 mil caracteres, o teto estourava em todo passo, e
  // o que vinha depois no DOM (a questão atual, o aviso de finalizar) caía
  // fora. O escondido vai para `resto`, não some. Só o snapshot pede isto: o
  // `extract` e a busca leem o texto inteiro, como sempre leram.
  NEO.ler = (excluir, opcoes) => {
    const vw = innerWidth, vh = innerHeight;
    const recortar = !!(opcoes && opcoes.recorte);
    const decisao = new Map();
    let orcamento = 400;
    const gasto = () => { orcamento--; };
    const decidir = (el) => {
      let d = decisao.get(el);
      if (d !== undefined) return d;
      d = 0;
      if (!NEO.SEM_TEXTO.test(el.tagName) && !(excluir && excluir(el))) {
        let pode = true;
        try {
          if (el.checkVisibility) {
            pode = el.checkVisibility({
              opacityProperty: true, visibilityProperty: true,
              checkOpacity: true, checkVisibilityCSS: true,
            });
          }
        } catch (_) { pode = true; }
        if (pode) {
          const r = el.getBoundingClientRect();
          if (r.width > 0 && r.height > 0 && r.right > 0) {
            const naJanela = r.bottom > 0 && r.top < vh && r.left < vw;
            d = naJanela && !(orcamento > 0 && NEO.tapado(el, r, gasto)) ? 1 : 2;
          }
        }
      }
      decisao.set(el, d);
      return d;
    };
    const linhas = { 1: [], 2: [] };
    const anterior = { 1: null, 2: null };
    const juntar1 = (d, r, t) => {
      const ant = anterior[d];
      const lista = linhas[d];
      const naMesma = ant && lista.length &&
        Math.abs(r.top - ant.top) < Math.max(r.height, ant.height) * 0.7 && r.left >= ant.left;
      if (naMesma) lista[lista.length - 1] += ' ' + t;
      else lista.push(t);
      anterior[d] = r;
    };
    // A caixa que RECORTA este elemento, até 4 níveis acima: `overflow` que não
    // é `visible` e conteúdo maior que ela. O corpo da página não conta: a
    // rolagem dele é a janela, e a janela já é a pergunta de `decidir`.
    // Cada eixo por si: a caixa que só corta na horizontal (a tabela larga, a
    // linha com reticências) não diz nada sobre o que fica abaixo dela. `rola`
    // separa o escondido que se alcança rolando a caixa (vai para o resto) do
    // que nunca aparece (`hidden`, `clip`, line-clamp: sai do snapshot, e o
    // `extract` e a busca continuam lendo). Mandar o segundo para o resto
    // empurrava para fora do teto dele o que está abaixo da dobra de verdade.
    const corta = new Map();
    const rolavel = (v) => v === 'auto' || v === 'scroll';
    const cortaAqui = (p) => {
      let c = corta.get(p);
      if (c !== undefined) return c;
      const s = getComputedStyle(p);
      const y = s.overflowY !== 'visible' && p.scrollHeight > p.clientHeight + 2;
      const x = s.overflowX !== 'visible' && p.scrollWidth > p.clientWidth + 2;
      c = y || x ? { y, x, rola: (y && rolavel(s.overflowY)) || (x && rolavel(s.overflowX)) } : null;
      corta.set(p, c);
      return c;
    };
    const recorteDe = (el) => {
      for (let p = el, n = 0; p && p.nodeType === 1 && p !== document.body &&
           p !== document.documentElement && n < 4; p = p.parentElement, n++) {
        const c = cortaAqui(p);
        if (c) return { caixa: p, ...c };
      }
      return null;
    };
    // Quanto do nó cabe na parte visível da caixa: as linhas dele (os retângulos
    // do Range) contra a caixa, só no eixo em que ela corta, pela LARGURA
    // visível de cada linha. A janela não entra: "está na janela" é a pergunta de
    // `decidir`, e misturar as duas faria o mesmo parágrafo valer diferente
    // dentro e fora de uma caixa. `[fração, lado]`: lado 1 = o visível é o
    // começo do texto (altura fixa, line-clamp, reticências), -1 = é o fim
    // (caixa rolada para baixo). `null` = não deu para medir.
    const parteVisivel = (no, rec) => {
      const caixa = rec.caixa;
      const cr = caixa.getBoundingClientRect();
      const topo = rec.y ? cr.top + caixa.clientTop : -Infinity;
      const fundo = rec.y ? cr.top + caixa.clientTop + caixa.clientHeight : Infinity;
      const esq = rec.x ? cr.left + caixa.clientLeft : -Infinity;
      const dir = rec.x ? cr.left + caixa.clientLeft + caixa.clientWidth : Infinity;
      let rects;
      try {
        const rg = document.createRange();
        rg.selectNodeContents(no);
        rects = [...rg.getClientRects()].filter((q) => q.width >= 1 && q.height >= 1);
      } catch (_) { return null; }
      // Uma linha pode vir em mais de um retângulo: com reticências o Range dá a
      // linha inteira E o pedaço que aparece. Fica o mais largo de cada linha.
      const porLinha = new Map();
      for (const q of rects) {
        const chave = Math.round(q.top);
        const atual = porLinha.get(chave);
        if (!atual || q.width > atual.width) porLinha.set(chave, q);
      }
      let total = 0, visto = 0, primeira = -1;
      [...porLinha.values()].sort((a, b) => a.top - b.top).forEach((q, i) => {
        total += q.width;
        const naFaixa = q.top < fundo - 1 && q.bottom > topo + 1;
        const w = naFaixa ? Math.max(0, Math.min(q.right, dir) - Math.max(q.left, esq)) : 0;
        visto += w;
        if (w > 0 && primeira < 0) primeira = i;
      });
      if (!total) return null;
      return [visto / total, primeira <= 0 ? 1 : -1];
    };
    const colher = (no) => {
      const t = no.nodeValue.replace(/\s+/g, ' ').trim();
      if (!t) return;
      const el = no.parentElement || (no.parentNode && no.parentNode.host);
      if (!el) return;
      const d = decidir(el);
      if (!d) return;
      const r = el.getBoundingClientRect();
      if (d === 1 && recortar) {
        const rec = recorteDe(el);
        const medida = rec && parteVisivel(no, rec);
        if (medida && medida[0] < 0.98) {
          const [fracao, lado] = medida;
          const alvo = Math.max(1, Math.round(t.length * fracao));
          const guardar = (x) => { if (rec.rola && x) juntar1(2, r, x); };
          if (fracao <= 0) { guardar(t); return; }
          // Texto curto parcialmente à vista é lido inteiro: o recorte serve para
          // o enunciado de mil caracteres, não para "Entrega GRÁTIS". E uma lasca
          // de texto longo ("D …" de "Duração: 3 minutos", o texto de leitor de
          // tela numa caixa de 1px) não é leitura: sai inteira da tela.
          if (t.length < 40) { juntar1(1, r, t); return; }
          if (alvo < 8) { guardar(t); return; }
          // Corta na palavra, para o modelo não ler meia palavra como dado.
          let k = lado > 0 ? t.lastIndexOf(' ', alvo) : t.indexOf(' ', t.length - alvo);
          if (k < 0 || (lado > 0 && k < alvo * 0.6)) k = lado > 0 ? alvo : t.length - alvo;
          const visto = lado > 0 ? t.slice(0, k) : t.slice(k);
          const escondido = lado > 0 ? t.slice(k) : t.slice(0, k);
          juntar1(1, r, lado > 0 ? visto.trim() + ' …' : '… ' + visto.trim());
          guardar(escondido.trim());
          return;
        }
      }
      juntar1(d, r, t);
    };
    const varrer = (raiz) => {
      const w = document.createTreeWalker(raiz, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
      let n;
      while ((n = w.nextNode())) {
        if (n.nodeType === 3) colher(n);
        else if (n.shadowRoot) varrer(n.shadowRoot);
      }
    };
    if (document.body) varrer(document.body);
    const juntar = (l) => l.filter((x, i) => x !== l[i - 1]).join('\n');
    return { tela: juntar(linhas[1]), resto: juntar(linhas[2]) };
  };
  NEO.fire = (el, type, extra) =>
    el.dispatchEvent(new Event(type, { bubbles: true, cancelable: true, ...(extra || {}) }));

  // ── ONDE O CLIQUE CAI ──────────────────────────────────────────────────
  // Evento SOBE na árvore, não desce. O inventário escolhe um elemento visível e
  // o clique era despachado nele, mas o controle de verdade pode ser um FILHO: o
  // botão da Amazon é um `<input>` com `opacity:0.01` esticado sobre um desenho, e
  // o clique no `span` pai nunca chegava nele. Foram 16 tentativas com `ok:true` e
  // o carrinho vazio no fim (job 8bc25dd1), enquanto o `Excluir` do mesmo job, um
  // input visível, funcionava. Só `<label>` encaminha para o controle.
  //
  // A pergunta certa não é "qual elemento?", é "quem recebe este PIXEL?". É o que o
  // navegador faz para um humano, e por isso vale em qualquer site, sem saber nada
  // da Amazon.
  NEO.CONTROLE = 'a[href],area[href],button,input:not([type=hidden]),select,textarea,summary,label,' +
    '[role=button],[role=link],[role=checkbox],[role=radio],[role=switch],[role=tab],' +
    '[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio],[role=option],[role=combobox]';
  NEO.dono = (el) => NEO.subir(el, NEO.CONTROLE);
  // Controle transparente sem nome próprio: o nome dele é o que se lê no desenho
  // embaixo. Sem isto ele entra na lista como um elemento mudo, ou com o `name`
  // técnico do formulário, e o modelo não tem como saber que é aquele botão.
  NEO.nomeDe = (el) => {
    const proprio = NEO.label(el);
    if (el.tagName !== 'INPUT' || !el.parentElement) return proprio;
    if (proprio && proprio !== el.getAttribute('name')) return proprio;
    if (parseFloat(getComputedStyle(el).opacity || '1') >= 0.05) return proprio;
    return NEO.shortLabel(el.parentElement) || proprio;
  };
  // O controle por trás de um rosto. É o que torna verdade o `[seleciona ...]`
  // que o inventário escreve: `fill` e `select` recebiam o rosto e desistiam.
  NEO.controleDe = (el) => {
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) return el;
    return (NEO.controles && NEO.controles.get(el)) || (el.tagName === 'LABEL' ? el.control : null) || null;
  };
  // `a` tapa ao menos 80% de `b`.
  NEO.cobre = (a, b) => {
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    const w = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
    const h = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
    return w > 0 && h > 0 && w * h >= rb.width * rb.height * 0.8;
  };
  NEO.vizinhos = (a, b) => {
    let p = b;
    for (let n = 0; p && n < 3; n++) {
      p = p.parentElement;
      if (p && p.contains(a)) return true;
    }
    return false;
  };
  // Controle transparente que está na frente: invisível para o filtro de
  // visibilidade, mas é ele que recebe o clique de quem olha o desenho embaixo.
  // Fora da janela não existe teste de acerto, e aí vale a geometria.
  NEO.naFrente = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 12 || r.height < 12 || r.right <= 0 || r.bottom <= -200) return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (x >= 0 && y >= 0 && x < innerWidth && y < innerHeight) return NEO.topo(x, y) === el;
    return s.position === 'absolute' && !!el.parentElement && NEO.cobre(el, el.parentElement);
  };
  // De onde o documento do elemento começa. Num iframe de mesma origem o elemento
  // mede relativo ao quadro dele, e o mouse real anda na janela de cima.
  NEO.origem = (el) => {
    const doc = el.ownerDocument;
    let dx = 0, dy = 0;
    try {
      for (let w = doc.defaultView; w && w !== w.parent; w = w.parent) {
        const f = w.frameElement;
        if (!f) break;
        const b = f.getBoundingClientRect();
        dx += b.left + f.clientLeft;
        dy += b.top + f.clientTop;
      }
    } catch (_) { /* quadro de outra origem: fica o que deu para somar */ }
    return { doc, dx, dy };
  };
  NEO.mirar = (el) => {
    const { doc, dx, dy } = NEO.origem(el);
    const vista = doc.defaultView || window;
    const r = el.getBoundingClientRect();
    const cy = r.top + r.height / 2;
    const meio = [r.left + r.width / 2, cy];
    const pontos = [
      meio,
      [r.left + Math.min(r.width * 0.25, 40), cy],
      [r.right - Math.min(r.width * 0.25, 40), cy],
    ];
    // Link quebrado em duas linhas: o centro da caixa cai no vazio entre elas.
    const partes = el.getClientRects();
    if (partes.length > 1) pontos.push([partes[0].left + partes[0].width / 2, partes[0].top + partes[0].height / 2]);
    const controle = el.matches(NEO.CONTROLE);
    const nome = NEO.normaliza(NEO.label(el));
    const ponto = (x, y) => ({ x, y, gx: x + dx, gy: y + dy });
    let reserva = null, tampa = null;
    for (const [x, y] of pontos) {
      if (x < 0 || y < 0 || x >= vista.innerWidth || y >= vista.innerHeight) continue;
      const t = NEO.topo(x, y, doc);
      if (!t) continue;
      if (t === el) return { ...ponto(x, y), alvo: el, pixel: true };
      if (NEO.contem(el, t)) {
        const d = NEO.dono(t);
        const outro = d && d !== el && NEO.contem(el, d) && !(el.tagName === 'LABEL' && el.control === d);
        // Conteúdo do próprio alvo: o evento nasce no filho e sobe até ele.
        if (!outro) return { ...ponto(x, y), alvo: controle ? el : t, pixel: true };
        // `el` era o rosto e o controle está dentro dele: o caso da Amazon.
        if (!controle) return { ...ponto(x, y), alvo: d, pixel: true };
        // Controle dentro de controle (o "termos" no meio do label do aceite):
        // este ponto clicaria em outra coisa. Procura um ponto que seja do alvo.
        continue;
      }
      // O pixel vai para um ancestral: `el` é pintura com `pointer-events:none`.
      if (NEO.contem(t, el)) { reserva = reserva || ponto(x, y); continue; }
      // Outro elemento na frente. Só é o alvo se for o mesmo controle sob outra
      // tag: mesmo nome, ou esticado sobre o rosto logo ao lado dele.
      const d = NEO.dono(t);
      if (d && (NEO.normaliza(NEO.label(d)) === nome || (NEO.cobre(d, el) && NEO.vizinhos(d, el)))) {
        return { ...ponto(x, y), alvo: d, pixel: true };
      }
      tampa = tampa || t;
    }
    return { ...(reserva || ponto(meio[0], meio[1])), alvo: el, pixel: false, tampa };
  };
  NEO.descrever = (el) => {
    const tipo = el.getAttribute('type');
    return el.tagName.toLowerCase() + (tipo ? '(' + tipo + ')' : '') + (el.id ? '#' + el.id : '');
  };
  NEO.soltar = (c) => {
    const janela = c.alvo.ownerDocument.defaultView || window;
    for (const t of ['pointerdown', 'mousedown', 'click']) janela.removeEventListener(t, c.ouvir, true);
    if (NEO.clique === c) NEO.clique = null;
  };
  // Primeira metade do clique: rola, descobre quem recebe o pixel e arma a SONDA,
  // que é o que distingue "mandei" de "chegou". O disparo fica para depois porque
  // o caminho principal é o mouse de verdade, que sai pelo CDP, fora da página.
  //   tipo     esquerdo | duplo | direito
  //   trava    regex das ações que não desfazem, conferida no alvo REAL: o rótulo
  //            que o core julgou é o do rosto, e o que recebe o clique pode ser
  //            outro. E em `click_text` ele só conhecia o texto que o modelo
  //            escreveu: "agora" passava por "Comprar agora".
  //   liberado rótulo que o usuário já autorizou
  //   proprio  confere também o `el` (o core nunca viu o rótulo dele)
  NEO.preparar = (el, o) => {
    const opcoes = o || {};
    if (NEO.clique) NEO.soltar(NEO.clique);
    const ctl = NEO.controleDe(el);
    if (el.disabled || (ctl && ctl.disabled)) return { ok: false, error: 'elemento desabilitado' };
    el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    let m = NEO.mirar(el);
    // Barra fixa tapando a borda: `nearest` para colado nela. Centralizar é um
    // pulo na tela, então só quando o ponto falhou.
    if (!m.pixel) {
      el.scrollIntoView({ block: 'center', inline: 'nearest' });
      const denovo = NEO.mirar(el);
      if (denovo.pixel) m = denovo;
    }
    const tipo = opcoes.tipo || 'esquerdo';
    // Campo de arquivo: o clique abriria a janela do sistema. Recusa com o
    // caminho certo (o core lê o prefixo).
    if (NEO.arquivoDe(el) || NEO.arquivoDe(m.alvo)) {
      return { ok: false, error: 'ARQUIVO: este elemento abre a janela de arquivos do ' +
        'sistema, que não dá para controlar. Use `upload` com este ref e o caminho do arquivo' };
    }
    NEO.arquivoPedido = null;
    NEO.travaArquivoAte = Date.now() + 2500;
    const ehControle = m.alvo !== el && m.alvo.matches(NEO.CONTROLE);
    // O rótulo é lido ANTES de clicar: num INTERRUPTOR ele vira no próprio clique,
    // e o histórico registrava o estado NOVO como se fosse o que foi clicado —
    // "Reproduzir" no clique que acabara de PAUSAR (job 717a8fbb).
    const rotulo = (ehControle && NEO.nomeDe(m.alvo)) || NEO.nomeDe(el);
    if (opcoes.trava) {
      const re = new RegExp(opcoes.trava, 'i');
      const liberado = String(opcoes.liberado || '').slice(0, 120);
      const nomes = [ehControle ? NEO.nomeDe(m.alvo) : '', opcoes.proprio ? NEO.nomeDe(el) : ''];
      for (const nome of nomes) {
        if (nome && re.test(nome) && nome.trim().toLowerCase().slice(0, 120) !== liberado) {
          return { ok: false, error: 'SENSIVEL:' + nome.slice(0, 120) };
        }
      }
    }
    const c = {
      el, alvo: m.alvo, x: m.x, y: m.y, tipo, rotulo, recebeu: null,
      desc: NEO.descrever(m.alvo), achados: opcoes.achados,
    };
    // O estado ANTES do clique: sem ele, desmarcar uma caixa que estava marcada
    // (o que se pediu) saía como "NÃO ficou selecionado" — e o agente marcava
    // de novo para "consertar" (sessão 74d069b7, "Trabalho aqui atualmente").
    c.antes = NEO.estadoMarcado(c);
    c.ouvir = (e) => { if (!c.recebeu) c.recebeu = (e.composedPath && e.composedPath()[0]) || e.target; };
    const janela = m.alvo.ownerDocument.defaultView || window;
    for (const t of ['pointerdown', 'mousedown', 'click']) janela.addEventListener(t, c.ouvir, true);
    NEO.clique = c;
    // O mouse real obedece o navegador, e o navegador não sabe que o painel é
    // nosso: por baixo dele o clique acertaria o painel.
    let bruto = null;
    try { bruto = document.elementFromPoint(m.gx, m.gy); } catch (_) { /* fora da janela */ }
    // Direito não vai pelo mouse real: abriria o menu NATIVO do sistema por cima
    // da página. O que interessa é o menu do site, e esse responde ao evento.
    const real = m.pixel && tipo !== 'direito' && document.visibilityState === 'visible' &&
      !(bruto && bruto.matches(NEO.NOSSOS));
    // Pulso visual no ponto do clique: a aba mexendo sozinha sem nenhum sinal é
    // desconcertante para quem está olhando. Quem desenha é o shadow DOM da
    // extensão (nenhum nó novo na página) — daí ser um postMessage e não um
    // appendChild aqui.
    postMessage({ __neo_ping: { x: m.gx, y: m.gy, tipo: tipo === 'direito' ? 'direito' : 'esquerdo' } }, '*');
    return { ok: true, real, x: m.gx, y: m.gy, url: location.href, label: rotulo, alvo: c.desc, achados: c.achados };
  };
  // A reserva: evento fabricado no alvo resolvido. Fica para o que o mouse real
  // não alcança (aba em segundo plano, ponto debaixo do painel do Neo, botão
  // direito). `.click()` e não só eventos: é o que aciona o `<a>`, o submit e o
  // label. Sequência completa porque há site que só escuta pointerdown/mouseup.
  NEO.despachar = (c) => {
    const el = c.alvo;
    const vista = el.ownerDocument.defaultView || window;
    const base = { bubbles: true, cancelable: true, composed: true, view: vista, clientX: c.x, clientY: c.y };
    const opts = c.tipo === 'direito' ? { ...base, button: 2, buttons: 2 } : { ...base, button: 0 };
    const Ponteiro = vista.PointerEvent || PointerEvent, Mouse = vista.MouseEvent || MouseEvent;
    const sequencia = (detail) => {
      el.dispatchEvent(new Ponteiro('pointerdown', opts));
      el.dispatchEvent(new Mouse('mousedown', { ...opts, detail }));
      if (el.focus) el.focus();
      el.dispatchEvent(new Ponteiro('pointerup', opts));
      el.dispatchEvent(new Mouse('mouseup', { ...opts, detail }));
    };
    if (c.tipo === 'direito') {
      sequencia(1);
      el.dispatchEvent(new Mouse('contextmenu', opts));
    } else if (c.tipo === 'duplo') {
      for (let i = 1; i <= 2; i++) {
        sequencia(i);
        el.dispatchEvent(new Mouse('click', { ...opts, detail: i }));
      }
      el.dispatchEvent(new Mouse('dblclick', { ...opts, detail: 2 }));
    } else {
      sequencia(1);
      // SVG não tem `.click()`, e o pixel pode ter caído no `path` de um ícone.
      if (el.click) el.click(); else el.dispatchEvent(new Mouse('click', { ...opts, detail: 1 }));
    }
  };
  // O estado de seleção do controle clicado: true/false, ou undefined se não é
  // controle marcável.
  NEO.estadoMarcado = (c) => {
    const ctl = NEO.controleDe(c.el) || NEO.controleDe(c.alvo) ||
      (c.el.querySelector ? c.el.querySelector('input[type=radio],input[type=checkbox]') : null);
    const tipo = ctl && ctl.getAttribute('type');
    if (tipo === 'radio' || tipo === 'checkbox') return !!ctl.checked;
    const aria = c.el.getAttribute('aria-checked') || c.el.getAttribute('aria-selected');
    if (aria === 'true' || aria === 'false') return aria === 'true';
    return undefined;
  };
  // O fim do clique, esperando a página ASSENTAR o estado. O Google Forms vira o
  // `aria-checked` da grade pelo JS dele, um instante depois do clique: lido na
  // hora, saía "NÃO ficou selecionado" para célula que tinha ficado (sessão
  // 74408e70, ~15 vezes num job), e o agente gastava passo re-clicando e
  // pedindo `look`. Só relê quando o primeiro veredito é "não".
  NEO.fimDoCliqueAssentado = async (c, via) => {
    const out = NEO.fimDoClique(c, via);
    // Estava marcado e ficou desmarcado: o clique PEGOU (desmarcou). Esperar
    // virar `true` aqui seria esperar o contrário do que aconteceu.
    if (out.checked !== false || out.saiu || out.antes === true) return out;
    for (const ms of [120, 300]) {
      await new Promise((ok) => setTimeout(ok, ms));
      if (!c.el.isConnected) { out.saiu = true; delete out.checked; return out; }
      if (NEO.estadoMarcado(c) === true) { out.checked = true; out.assentou_ms = ms; return out; }
    }
    return out;
  };
  NEO.fimDoClique = (c, via) => {
    const recebeu = c.recebeu;
    NEO.soltar(c);
    const out = { ok: true, url: location.href, label: c.rotulo, via, alvo: c.desc };
    if (c.achados !== undefined) out.achados = c.achados;
    // O mouse real obedece o pixel, e a página pode ter mudado entre mirar e
    // clicar. Não dá para desfazer, mas dá para não mentir sobre onde caiu.
    if (via === 'pixel' && recebeu && !NEO.contem(c.alvo, recebeu) && !NEO.contem(recebeu, c.alvo) &&
        !NEO.contem(c.el, recebeu)) {
      out.desvio = NEO.descrever(recebeu);
    }
    // O clique virou SELEÇÃO? É a única evidência de efeito que existe para
    // "marque a alternativa": marcar um rádio não muda URL, nem título, nem
    // texto — o freio de tela repetida é cego para isso por construção. O estado
    // sai do CONTROLE, inclusive do input escondido atrás do label.
    //
    // Controle que SAIU da página não tem estado para ler. O clique pode
    // desmontá-lo: no job f9370b43 (12axes) a 36ª resposta trocou o cartão pela
    // etapa seguinte antes de o `aria-checked` virar `true`, o nó morto dizia
    // `false`, e "não ficou selecionado" fez o agente ler a etapa nova como o
    // quiz reiniciando. Sumir no clique é prova de que a página reagiu.
    if (!c.el.isConnected) {
      out.saiu = true;
    } else {
      const estado = NEO.estadoMarcado(c);
      if (estado !== undefined) out.checked = estado;
      if (estado !== undefined && c.antes !== undefined) out.antes = c.antes;
    }
    // O número, quando existe: o modelo volta a poder usar `click`/`extract` nele.
    if (NEO.refs) {
      const i = NEO.refs.indexOf(c.el);
      if (i >= 0) out.ref = i;
    }
    return out;
  };

  // ── VARREDURA DA DOM INTEIRA ───────────────────────────────────────────
  // O inventário é o que está na tela, com teto de 200. Isto é a pergunta oposta:
  // dado um NOME, o que existe no documento inteiro que responde por ele? Entra em
  // shadow DOM e em iframe de mesma origem, e não filtra por visibilidade: o que
  // está escondido volta marcado, porque "existe, mas está fechado" é resposta.
  //
  // Procura pelo NOME ACESSÍVEL e não só pelo texto. No job 8bc25dd1 o modelo chegou
  // a tentar `extract {"busca": "add-to-cart"}`: o controle de verdade não tem nó de
  // texto, o nome dele está em `value`, `title` e `aria-labelledby`, e o `click_text`
  // só andava por nós de texto. Texto casado sem controle em volta sobe até o dono,
  // que pode ser o `<input>` que se declarou rotulado por ele, ou o que está por cima.
  NEO.ATIVAVEL = NEO.CONTROLE + ',[onclick],[ng-click],[data-ng-click],[jsaction],[data-action],[tabindex="0"]';
  NEO.candidatos = (texto) => {
    const procurado = NEO.normaliza(texto);
    const raizes = NEO.raizes();
    let fechados = 0;
    for (let i = 0; i < raizes.length && raizes.length < 120; i++) {
      for (const f of raizes[i].querySelectorAll('iframe,frame')) {
        let d = null;
        try { d = f.contentDocument; } catch (_) { d = null; }
        if (d && d.body) raizes.push(...NEO.raizes(d));
        else fechados++;
      }
    }
    const grauDe = (nome) => {
      const n = NEO.normaliza(nome);
      if (!n) return 0;
      if (n === procurado) return 4;
      if (n.startsWith(procurado)) return 3;
      const i = n.indexOf(procurado);
      if (i < 0) return 0;
      return i === 0 || n[i - 1] === ' ' ? 2 : 1;
    };
    // Quem se declarou rotulado por qual id, para o caminho de volta: do texto ao
    // controle que aponta para ele.
    const rotuladosPor = new Map();
    for (const r of raizes) {
      for (const el of r.querySelectorAll('[aria-labelledby]')) {
        for (const id of el.getAttribute('aria-labelledby').split(/\s+/)) {
          if (id && !rotuladosPor.has(id)) rotuladosPor.set(id, el);
        }
      }
    }
    const sobreOTexto = (el) => {
      let p = el;
      for (let n = 0; p && n < 3; n++) {
        p = p.parentElement;
        if (!p) break;
        const perto = p.querySelectorAll('input:not([type=hidden]),button,a[href]');
        if (perto.length > 6) break;
        for (const c of perto) {
          if (c !== el && !c.contains(el) && NEO.cobre(c, el)) return c;
        }
      }
      return null;
    };
    const donoDoNome = (el) => {
      const d = NEO.dono(el);
      if (d) return d;
      for (let p = el, n = 0; p && n < 4; p = p.parentElement, n++) {
        if (p.id && rotuladosPor.has(p.id)) return rotuladosPor.get(p.id);
      }
      const sobre = sobreOTexto(el);
      if (sobre) return sobre;
      const ativavel = NEO.subir(el, NEO.ATIVAVEL);
      if (ativavel) return ativavel;
      for (let p = el, n = 0; p && n < 6; p = p.parentElement, n++) {
        if (getComputedStyle(p).cursor === 'pointer') return p;
      }
      return el;
    };
    const achados = new Map();
    let parou = false;
    const anotar = (bruto, nome, fonte) => {
      const grau = grauDe(nome);
      if (!grau) return;
      const el = donoDoNome(bruto);
      const antes = achados.get(el);
      if (!antes && achados.size >= 150) { parou = true; return; }
      const fraca = (f) => f === 'title' || f === 'alt';
      if (!antes || grau > antes.grau || (grau === antes.grau && fraca(antes.fonte) && !fraca(fonte))) {
        achados.set(el, { el, grau, fonte, nome: String(nome).replace(/\s+/g, ' ').trim() });
      }
    };
    for (const r of raizes) {
      for (const el of r.querySelectorAll('[aria-label],[aria-labelledby],[title],[alt],[placeholder],input')) {
        if (el.closest(NEO.NOSSOS)) continue;
        for (const a of ['aria-label', 'title', 'alt', 'placeholder']) {
          const v = el.getAttribute(a);
          if (v) anotar(el, v, a);
        }
        const ids = el.getAttribute('aria-labelledby');
        if (ids) {
          const raiz = el.getRootNode();
          const nome = ids.split(/\s+/).map((id) => {
            const n = (raiz.getElementById && raiz.getElementById(id)) || el.ownerDocument.getElementById(id);
            return n ? n.textContent : '';
          }).join(' ');
          anotar(el, nome, 'aria-labelledby');
        }
        if (el.tagName === 'INPUT' && /^(button|submit|reset)$/i.test(el.type)) anotar(el, el.value, 'value');
      }
      // Frase que atravessa nós: em `Li e aceito os <a>termos</a>` nenhum nó de
      // texto sozinho contém "aceito os termos". O texto inteiro do controle contém.
      for (const el of r.querySelectorAll(NEO.CONTROLE)) {
        if (/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName) || el.closest(NEO.NOSSOS)) continue;
        anotar(el, el.textContent, 'texto');
      }
      const base = r.body || r;
      const walker = (r.ownerDocument || r).createTreeWalker(base, NodeFilter.SHOW_TEXT);
      let no;
      while ((no = walker.nextNode())) {
        if (!no.nodeValue || no.nodeValue.length < 2) continue;
        const pai = no.parentElement;
        if (!pai || NEO.SEM_TEXTO.test(pai.tagName) || pai.closest(NEO.NOSSOS)) continue;
        anotar(pai, no.nodeValue, 'texto');
      }
    }
    const lista = [];
    for (const a of achados.values()) {
      const el = a.el;
      const r = el.getBoundingClientRect();
      const s = getComputedStyle(el);
      const doc = el.ownerDocument;
      const vista = doc.defaultView || window;
      const estado = {};
      const semArea = r.width < 2 || r.height < 2;
      if (semArea || s.display === 'none' || s.visibility === 'hidden') estado.escondido = true;
      else if (el.checkVisibility && !el.checkVisibility({ checkVisibilityCSS: true })) estado.escondido = true;
      // À esquerda da página não se chega rolando: é onde moram os skip-links.
      if (!estado.escondido && r.right <= 0) estado.escondido = true;
      const transparente = !estado.escondido && el.checkVisibility &&
        !el.checkVisibility({ checkOpacity: true });
      const naJanela = !estado.escondido && r.top < vista.innerHeight && r.bottom > 0 &&
        r.left < vista.innerWidth && r.right > 0;
      if (naJanela) {
        const t = NEO.topo(r.left + r.width / 2, r.top + r.height / 2, doc);
        if (t && (t === el || NEO.contem(el, t) || NEO.contem(t, el))) estado.recebe = true;
        else if (t) {
          const d = NEO.dono(t);
          if (d && NEO.cobre(d, el) && NEO.vizinhos(d, el)) estado.recebe = true;
          else estado.coberto = NEO.shortLabel(t) || t.tagName.toLowerCase();
        }
      } else if (!estado.escondido) estado.fora = true;
      // Transparente que não recebe o pixel está escondido para qualquer um.
      if (transparente && !estado.recebe && !(estado.fora && NEO.naFrente(el))) {
        estado.escondido = true;
        delete estado.fora;
        delete estado.coberto;
      }
      if (el.disabled) estado.desabilitado = true;
      const nativo = el.matches('a[href],area[href],button,input,select,textarea,summary,label');
      const tipoAlvo = nativo ? 30 : el.matches(NEO.CONTROLE) ? 25 : el.matches(NEO.ATIVAVEL) ? 15 :
        s.cursor === 'pointer' ? 10 : 0;
      const nota = a.grau * 10 + (a.fonte === 'title' || a.fonte === 'alt' ? 0 : 5) + tipoAlvo +
        (estado.recebe ? 20 : estado.fora ? 10 : 0) -
        (estado.escondido ? 60 : 0) - (estado.desabilitado ? 40 : 0) - (estado.coberto ? 25 : 0) -
        (NEO.subir(el, NEO.CHROME) ? 8 : 0);
      lista.push({ ...a, ...estado, nota, area: r.width * r.height });
    }
    // Casar no meio de uma palavra ("editar" em "hereditárias") só serve quando
    // não há nada melhor.
    const melhor = lista.reduce((m, c) => Math.max(m, c.grau), 0);
    const boas = melhor > 1 ? lista.filter((c) => c.grau > 1) : lista;
    // Ordenação estável: empate fica na ordem do documento depois do menor.
    boas.sort((a, b) => b.nota - a.nota || a.area - b.area);
    boas.forEach((c, i) => { c.n = i + 1; });
    return { lista: boas, fechados, parou };
  };
  return true;
})()
;
  return (() => {
  const NEO = window.__neo;
  // Vocabulário CANÔNICO. É a primeira passada, não a única: allowlist de tag e
  // papel só enxerga site bem-comportado, e foi exatamente isso que cegou o
  // driver no questionário do portal (job 9968e68a) — as 5 alternativas eram
  // controle de formulário ESCONDIDO por CSS dentro de um contêiner sem papel
  // nenhum, então o snapshot devolveu 56 elementos e NENHUM deles era uma
  // alternativa. O modelo leu a questão, escolheu certo e clicou no menu
  // lateral 5 vezes. As passadas 2 e 3 abaixo existem por causa disso.
  const SEL = 'a[href],button,input:not([type=hidden]),select,textarea,label,summary,' +
    '[role=button],' +
    '[role=link],[role=tab],[role=checkbox],[role=radio],[role=menuitem],[role=combobox],' +
    '[role=menuitemcheckbox],[role=menuitemradio],[role=switch],[role=searchbox],' +
    '[role=textbox],[role=slider],[role=spinbutton],' +
    // Linha de lista/tabela/árvore. É AQUI que mora o nome do arquivo, da pasta
    // ou da mensagem: sem estes papéis o Google Drive vira 70 botões "Mais
    // ações" indistinguíveis e nenhum elemento clicável carrega o nome do item
    // (foi exatamente o que travou os dois jobs de 2026-08-28).
    '[role=row],[role=gridcell],[role=option],[role=treeitem],' +
    // `tabindex="0"` (e não `[tabindex]`) porque -1 é foco programático, que
    // sites usam em contêiner que ninguém clica. `ng-click`/`jsaction` são os
    // dois handlers declarativos comuns o bastante para valer um seletor
    // próprio; o resto dos frameworks é pego pela evidência de `cursor`.
    '[tabindex="0"],[ng-click],[data-ng-click],[jsaction],[data-action],' +
    '[onclick],[contenteditable=""],[contenteditable="true"]';
  const CONTAINER = '[role=row],[role=gridcell],[role=option],[role=treeitem],tr,li';
  // Menu/diálogo aberto: quando existe, é o ÚNICO lugar onde a próxima ação faz
  // sentido. Sem esta marca os 8 itens do menu do Drive ficavam perdidos no fim
  // de uma lista com 50 botões "Mais ações" — e o modelo reabria o menu em loop
  // em vez de clicar em "Baixar", que estava ali (job 706ff8a8).
  const OPEN = '[role=menu],[role=dialog],[role=alertdialog],[aria-modal="true"]';

  // Estilo calculado é a chamada CARA desta função (já estourou o timeout do
  // passo uma vez, varrendo `div[class]`). Memoiza e cobra de um orçamento: a
  // varredura geométrica lá embaixo sobe pelos MESMOS ancestrais centenas de
  // vezes, então o cache é o que torna ela viável.
  const estilos = new Map();
  let orcamento = 6000;
  const cs = (el) => {
    let s = estilos.get(el);
    if (s === undefined) {
      s = orcamento-- > 0 ? getComputedStyle(el) : null;
      estilos.set(el, s);
    }
    return s;
  };
  const caixas = new Map(); // el -> DOMRect (calculado uma vez só)
  const box = (el) => {
    let r = caixas.get(el);
    if (r === undefined) { r = el.getBoundingClientRect(); caixas.set(el, r); }
    return r;
  };
  const visivel = (el) => {
    if (!el || el.nodeType !== 1) return false;
    const r = box(el);
    if (r.width < 2 || r.height < 2) return false;
    const s = cs(el);
    if (!s) return false;
    if (s.visibility === 'hidden' || s.display === 'none') return false;
    if (parseFloat(s.opacity || '1') < 0.05) return false;
    return r.bottom > -200 && r.top < innerHeight + 2000;
  };

  // ── O QUE UM PAINEL ABERTO É, DE VERDADE ───────────────────────────────
  // `role=dialog` diz o que o site DECLAROU, não o que está acontecendo na tela.
  // A geometria decide, e por isso vale em qualquer site:
  //   fora     nem está na janela (o carrinho da Amazon em `left: 1819` numa
  //            janela de 1600px): não existe para esta tarefa;
  //   pagina   `role=dialog` em fluxo: é parte da página, não abriu nada (a coluna
  //            de filtros da busca da Amazon, 262×7957px: no job aa4c89f8 ela
  //            saiu como ">>> O PAINEL «Filtros» ABRIU E É CONTEÚDO", com 138
  //            itens repetidos no topo, 9.408 caracteres de um prompt de 29.851);
  //   fluxo    não é fixo, mas flutua: menu suspenso, popover;
  //   lateral  faixa presa na borda, sem fundo escurecendo a página;
  //   bloqueia sobrepõe e tem fundo cobrindo a página, ou `aria-modal`, ou cobre
  //            o centro da janela: é modal, e enquanto está aberto é o conteúdo;
  //   solto    sobrepõe sem bloquear nada: popover, banner de rodapé.
  //
  // `lateral` é o carrinho da Amazon numa janela larga: 150px de 1900, sem X, que
  // não fecha com nada. O modal era "o conteúdo" por ser `role=dialog` fixo, e a
  // partir de ~1830px de janela ele tomava o lugar do conteúdo principal em TODA
  // página de produto. Sobrou para o modelo o carrinho, sem os dados do produto, e
  // fechar o painel passou a ser a decisão racional: 196s, 5 Escapes, 3 notas (job
  // 45e85695). O job headless do mesmo dia, em 1280px, nunca viu o carrinho e
  // coletou 12 itens em 100s.
  // O que torna um modal "o conteúdo" é ele SOBREPOR o resto, não o papel ARIA.
  // Dois sinais, nesta ordem: `aria-modal="true"`, que é a marcação que a spec
  // reserva para isso; ou posicionamento `fixed` — inclusive num ancestral
  // próximo, que é o desenho comum (fundo `fixed` com a caixa `absolute`
  // dentro). Um elemento oito vezes mais alto que a janela é conteúdo de
  // página, não sobreposição.
  const ehSobreposicao = (el) => {
    if (el.getAttribute('aria-modal') === 'true') return true;
    let p = el, n = 0;
    while (p && n++ < 5) {
      if (getComputedStyle(p).position === 'fixed') return true;
      p = p.parentElement;
    }
    return false;
  };
  // Fundo cobrindo a página: é o que separa a gaveta MODAL da faixa presa na borda,
  // e o modal alinhado ao topo (o desenho mais comum) de um popover. O que está
  // atrás de um fundo não responde a clique.
  //
  // A comparação de texto é o que distingue o fundo da CASCA do app: um `fixed`
  // de tela inteira que contém a página toda (SPA com layout fixo) tem muito mais
  // texto que o painel. Sem isso, toda barra lateral desses sites virava modal.
  const fundoCobre = (p) => {
    let q = NEO.topo(innerWidth / 2, innerHeight / 2);
    for (let n = 0; q && q !== document.body && n < 6; n++) {
      const r = box(q);
      if (r.width * r.height >= innerWidth * innerHeight * 0.8 &&
          getComputedStyle(q).position === 'fixed') {
        return String(q.innerText || '').length <= String(p.innerText || '').length + 200;
      }
      q = q.parentElement;
    }
    return false;
  };
  const NAO_E_PAINEL = ['fora', 'lateral', 'pagina'];
  const quadros = new Map();
  const quadro = (p) => {
    let q = quadros.get(p);
    if (q) return q;
    const r = box(p);
    const w = innerWidth, h = innerHeight;
    if (!(r.top < h && r.bottom > 0 && r.left < w && r.right > 0)) q = 'fora';
    else if (!ehSobreposicao(p)) {
      const pos = getComputedStyle(p).position;
      const emFluxo = pos === 'static' || pos === 'relative' || pos === 'sticky';
      q = emFluxo && !p.matches('[role=menu]') ? 'pagina' : 'fluxo';
    } else {
      const fundo = fundoCobre(p);
      const borda = r.height >= h * 0.4 && r.width <= w * 0.4 && (r.right >= w - 24 || r.left <= 24);
      const centro = r.left <= w / 2 && r.right >= w / 2 && r.top <= h / 2 && r.bottom >= h / 2;
      if (borda && !fundo) q = 'lateral';
      else if (fundo || centro || p.getAttribute('aria-modal') === 'true') q = 'bloqueia';
      else q = 'solto';
    }
    quadros.set(p, q);
    return q;
  };

  // ── EVIDÊNCIA DE INTERATIVIDADE ────────────────────────────────────────
  // Troca a pergunta "esta TAG é interativa?" (que nunca fecha — sempre existe
  // o próximo framework) por "este elemento dá SINAL de que reage?". Cada prova
  // é independente de biblioteca. `cursor:pointer` é a mais universal delas:
  // todo site que torna algo clicável muda o cursor, e é ela que pega a
  // `div ng-click` / `@click` / `wire:click` / `hx-get` sem papel e sem onclick.
  const RE_HANDLER = /^(on\w+|(data-)?ng-\w*click|v-on:|@|x-on:|wire:|hx-|jsaction|data-action|data-click)/i;
  const evidencia = (el) => {
    if (!el || el.nodeType !== 1) return null;
    const tag = el.tagName;
    if (tag === 'BUTTON' || tag === 'SELECT' || tag === 'TEXTAREA' || tag === 'SUMMARY') return 'tag';
    if (tag === 'A' && el.hasAttribute('href')) return 'tag';
    if (tag === 'INPUT' && el.getAttribute('type') !== 'hidden') return 'tag';
    if (tag === 'LABEL' && (el.htmlFor || el.querySelector('input,select,textarea'))) return 'label';
    // Papel de CONTROLE conta como evidência própria. `row`/`gridcell`/
    // `treeitem` de propósito NÃO entram: no Drive a `row` é a lista inteira e
    // tratá-la como controle é o loop de 23 passos do job a0eab88c.
    const papel = el.getAttribute('role');
    if (papel && /^(button|link|checkbox|radio|menuitem|menuitemcheckbox|menuitemradio|option|tab|switch|searchbox|textbox|combobox|slider|spinbutton)$/.test(papel)) return 'role';
    if (el.isContentEditable) return 'editavel';
    if (el.getAttribute('tabindex') === '0') return 'tabindex';
    for (const a of el.attributes) {
      if (RE_HANDLER.test(a.name)) return 'handler';
    }
    const s = cs(el);
    if (s && s.cursor === 'pointer') return 'cursor';
    return null;
  };

  // ── 1. candidatos ──────────────────────────────────────────────────────
  // Três passadas. A primeira pega o que o site DECLARA como interativo; a
  // segunda resgata o controle de formulário escondido por CSS; a terceira
  // varre a tela por PIXEL. Depois delas, a promoção do contêiner do item.
  //
  // `coleta` é maior que o teto de propósito. O limite cortava em ordem de
  // DOCUMENTO: a Amazon gasta os 200 lugares com cabeçalho, filtros e carrosséis,
  // e o que estava na tela ficava de fora. Agora junta-se o triplo e o teto
  // escolhe por RELEVÂNCIA (ver "o teto", mais abaixo).
  const coleta = MAX * 3;
  const raizes = NEO.raizes();
  const cands = [];
  const vistos = new Set();
  const origens = new Map(); // el -> como ele foi achado (entra no diário)
  const olhar = (el, origem, forcar) => {
    if (!el || vistos.has(el)) return;
    if (el.closest('#neo-browser-local-overlay') || el.closest('#neo-marks')) return;
    // O inspetor visual da extensão desenha num overlay próprio. Ele já é
    // invisível ao mapeamento por não ter evidência e por ser pointer-events:none,
    // mas depender disso seria depender de um acidente.
    if (el.closest('#neo-map-overlay')) return;
    if (!forcar && !visivel(el)) return;
    vistos.add(el);
    origens.set(el, origem || 'declarado');
    cands.push(el);
  };
  // O que está NA JANELA primeiro, depois o resto: uma coluna de 700 filtros antes
  // do conteúdo no DOM esgotaria até a coleta. A numeração continua a do documento.
  const declarados = [...NEO.todos(SEL, raizes)];
  for (const el of declarados) {
    if (cands.length >= coleta) break;
    const r = box(el);
    if (r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth) olhar(el, 'declarado');
  }
  for (const el of declarados) {
    if (cands.length >= coleta) break;
    olhar(el, 'declarado');
  }
  {
    const posicao = new Map(declarados.map((el, i) => [el, i]));
    cands.sort((a, b) => posicao.get(a) - posicao.get(b));
  }

  // ── 2. controle ESCONDIDO → procurador visível ─────────────────────────
  // O rádio/checkbox custom: `<input type=radio>` com `opacity:0` (ou 1px, ou
  // fora da tela) e um `<label>`/`<span>` desenhado por cima. O filtro de
  // visibilidade descartava o input — e com ele a única maneira de responder
  // uma prova, marcar um "li e aceito", escolher um endereço de entrega.
  //
  // Um controle de formulário é interativo POR DEFINIÇÃO: só a pintura dele
  // sumiu. O alvo clicável passa a ser o procurador (o label, que o navegador
  // encaminha para o controle) e o ESTADO continua saindo do input real.
  const procuradores = new Map(); // procurador -> input de verdade
  NEO.controles = new WeakMap();
  let examinados = 0;
  for (const inp of NEO.todos('input:not([type=hidden]),select,textarea', raizes)) {
    if (examinados++ > 200 || cands.length >= coleta) break;
    if (vistos.has(inp)) continue;
    if (visivel(inp)) continue;
    // O `<label>` é o rosto de UM controle por definição — não precisa de limite.
    let alvo = (inp.labels && inp.labels[0]) || inp.closest('label') || null;
    // Transparente e na frente: ele É o botão, e o que se vê é o desenho embaixo.
    // Oferecer o pai aqui é o que fez o job 8bc25dd1 clicar 16 vezes no vazio.
    if (!alvo && NEO.naFrente(inp)) {
      box(inp);
      olhar(inp, 'transparente', true);
      continue;
    }
    if (!alvo || !visivel(alvo)) {
      // Sem label, sobe procurando a caixa que ENVOLVE só este controle (o
      // `<div class="opcao">` que embrulha o input invisível e o texto). As
      // duas guardas abaixo vieram do google.com, onde a versão sem limite
      // ofereceu o `<body>` inteiro como "rosto" de um textarea de telemetria
      // e um contêiner do formulário como rosto do botão de busca. Clicar
      // nessas caixas não faz nada — é o erro que a regra da ⟪CAIXA⟫ já
      // combate, entrando pela porta dos fundos.
      const teto = innerWidth * innerHeight * 0.15;
      const PROIBIDO = /^(BODY|HTML|FORM|MAIN|ARTICLE|SECTION|NAV|HEADER|FOOTER)$/;
      let p = inp.parentElement, n = 0;
      alvo = null;
      while (p && n++ < 5) {
        if (PROIBIDO.test(p.tagName)) break;
        const r = box(p);
        if (r.width * r.height > teto) break;
        // Mais de um controle dentro = é contêiner de vários, não o rosto
        // deste. O próprio input conta como um.
        if (visivel(p) && r.height >= 8 &&
            p.querySelectorAll('input:not([type=hidden]),select,textarea').length <= 1) {
          alvo = p;
          break;
        }
        p = p.parentElement;
      }
    }
    if (!alvo || !visivel(alvo)) continue;
    if (!procuradores.has(alvo)) {
      procuradores.set(alvo, inp);
      NEO.controles.set(alvo, inp);
    }
    olhar(alvo, 'procurador');
  }

  // ── 3. varredura geométrica (o "inspetor") ─────────────────────────────
  // Em vez de descer por seletor, SOBE a partir dos pixels: amostra uma grade
  // sobre o viewport, e de cada ponto sobe até o primeiro ancestral com
  // evidência. Acha qualquer coisa que ocupe área e reaja — sem depender de
  // tag, papel ou framework. É a rede que pega o que ninguém previu.
  //
  // Barato porque os pontos são desduplicados ANTES de subir: mil amostras
  // caem sobre algumas dezenas de elementos distintos, e o cache de estilo faz
  // o resto. `elementFromPoint` não calcula estilo nenhum.
  const alvos = new Set();
  const PASSO = 40;
  for (let y = 20; y < innerHeight; y += PASSO) {
    for (let x = 20; x < innerWidth; x += PASSO) {
      const no = NEO.topo(x, y);
      if (no) alvos.add(no);
    }
  }
  const negativos = new Set();
  const provas = new Map(); // el -> por que ele entrou (a poda abaixo usa isto)
  for (const no of alvos) {
    if (cands.length >= coleta || orcamento <= 0) break;
    let p = no, n = 0;
    while (p && p !== document.body && p !== document.documentElement && n++ < 10) {
      if (vistos.has(p)) break;           // já inventariado por outra passada
      if (!negativos.has(p)) {
        const prova = evidencia(p);
        if (prova && visivel(p)) {
          let escolhido = p;
          if (prova === 'cursor') {
            // `cursor` HERDA: numa linha clicável, todo `span` de dentro parece
            // clicável também. Emitir os filhos transformaria o alvo de verdade
            // numa ⟪CAIXA com 2 itens⟫ — que é justamente o que o driver
            // RECUSA clicar. O alvo é o mais EXTERNO da faixa contígua com
            // cursor:pointer, que é o que o usuário percebe como "o botão".
            let q = p.parentElement, m = 0;
            while (q && q !== document.body && q !== document.documentElement && m++ < 8) {
              const sq = cs(q);
              if (!sq || sq.cursor !== 'pointer' || !visivel(q)) break;
              escolhido = q;
              q = q.parentElement;
            }
          }
          if (!vistos.has(escolhido)) { provas.set(escolhido, prova); olhar(escolhido, 'grade'); }
          break;
        }
        negativos.add(p);
      }
      p = p.parentElement;
    }
  }

  // Terceira passada: as REGIÕES ROLÁVEIS que embrulham o que já achamos.
  //
  // O snapshot só enumerava o que é interativo, então um painel de texto puro
  // simplesmente não existia para o modelo. No job 9025d9f8 o painel de
  // detalhes do Drive estava ABERTO na tela, com o tamanho do arquivo logo
  // abaixo da dobra, e os únicos refs por perto eram os botões das abas
  // ("Detalhes", "Atividades"). O modelo passou 21 passos mandando `scroll` no
  // botão da aba — que não rola, porque não é o painel.
  //
  // COMO achar o painel importa. A primeira tentativa subiu a partir dos
  // candidatos e não achou nada (job f27970d6): no Drive o scroller do painel é
  // IRMÃO das abas e o conteúdo dele é texto puro — nenhum candidato mora lá
  // dentro, então a subida nunca passa por ele. Detecção que exige um elemento
  // interativo dentro do painel falha justamente no painel só-de-leitura, que é
  // o caso que interessa.
  //
  // Sondagem geométrica resolve: uma grade de pontos sobre o viewport, e de
  // cada ponto sobe a pilha. Acha qualquer coisa VISÍVEL (que é o critério que
  // queremos) sem depender do conteúdo, e custa ~25 × profundidade chamadas de
  // getComputedStyle — não os milhares de varrer `div[class]`, que já estourou
  // o timeout deste passo uma vez.
  const rolavel = (el) => {
    if (!el || el === document.body || el === document.documentElement) return false;
    const s = cs(el);
    if (!s || !/(auto|scroll|overlay)/.test(s.overflowY)) return false;
    return el.scrollHeight > el.clientHeight + 8;
  };
  const rolaveis = new Set();
  const jaOlhado = new Set(); // ancestral compartilhado não é reavaliado
  const subirAte = (node) => {
    let p = node;
    while (p && p !== document.body && p !== document.documentElement) {
      if (rolaveis.has(p)) return;
      if (!jaOlhado.has(p)) {
        jaOlhado.add(p);
        if (rolavel(p)) { rolaveis.add(p); return; }
      }
      p = p.parentElement;
    }
  };
  // Os nós já colhidos pela grade acima servem de partida — a sondagem
  // dedicada de 5x5 pontos que existia aqui virou um subconjunto dela.
  for (const no of alvos) {
    if (rolaveis.size >= 6) break;
    subirAte(no);
  }
  // E ainda pelos candidatos: pega o painel que ficou fora da grade de pontos
  // (coluna estreita, lista que não cai em nenhuma amostra).
  for (const el of cands.slice()) {
    if (rolaveis.size >= 6) break;
    subirAte(el.parentElement);
  }
  for (const el of rolaveis) {
    if (cands.length >= coleta) break;
    olhar(el, 'painel');
  }

  for (const el of cands.slice()) {
    if (cands.length >= coleta) break;
    const dono = el.closest(CONTAINER);
    // Wrapper puro (o `li` que embrulha um único link e repete o nome dele) não
    // entra: dobraria a lista sem acrescentar alvo. O contêiner só vale quando
    // tem nome PRÓPRIO — que é justamente o caso do item de lista, cujo nome é
    // o do arquivo e não o do botão que ele contém.
    if (dono && dono !== el && !vistos.has(dono)) {
      const nome = NEO.shortLabel(dono);
      if (nome && nome !== NEO.label(el)) olhar(dono, 'contêiner');
    }
  }

  // ── 1b. poda: moldura com `cursor:pointer` NÃO é botão ─────────────────
  // O preço de aceitar `cursor` como evidência é que sites o aplicam em caixas
  // inteiras (o card do produto todo, a linha da tabela inteira). Duas regras
  // baratas resolvem: quem ocupa quase a tela não é controle, e quem EMBRULHA
  // outro candidato é moldura — o alvo é o de dentro. Só vale para quem entrou
  // pela grade E só por cursor: nada que o site declarou é podado aqui.
  {
    const remover = new Set();
    const teto = innerWidth * innerHeight * 0.45;
    for (const el of cands) {
      if (provas.get(el) !== 'cursor') continue;
      const r = box(el);
      if (r.width * r.height > teto) { remover.add(el); continue; }
      for (const outro of cands) {
        if (outro !== el && !remover.has(outro) && el.contains(outro)) { remover.add(el); break; }
      }
    }
    if (remover.size) {
      for (const el of remover) { vistos.delete(el); origens.delete(el); }
      for (let i = cands.length - 1; i >= 0; i--) {
        if (remover.has(cands[i])) cands.splice(i, 1);
      }
    }
  }

  // ── 2. moldura do site ─────────────────────────────────────────────────
  // Só landmark de navegação DECLARADO. A versão anterior marcava como moldura
  // "tudo que não está na maior região semântica" e isso mentiu feio no job
  // 9025d9f8: a maior região era a grade de arquivos, então o painel de
  // detalhes ABERTO — que continha a resposta — foi carimbado como moldura e o
  // modelo aprendeu a ignorá-lo. Errar para menos aqui é barato (nada marcado);
  // errar para mais esconde a resposta.
  //
  // Mora no `JS_HELPERS` porque o `js_extract` também precisa dele para dizer
  // ao modelo quando a leitura saiu da moldura. Aqui é só o apelido local.
  const CHROME = NEO.CHROME;

  // ── o teto: o que fica quando não cabe tudo ────────────────────────────
  // Por RELEVÂNCIA, não por ordem de documento, e a ordem de `cands` (que é a
  // numeração dos refs) continua a do documento. Moldura na tela tem cota: o
  // cabeçalho precisa continuar alcançável ("Meus pedidos", a busca), mas não
  // pode tirar o lugar do que está na tela. Página que não bate no teto não
  // passa por nada disto.
  let omitidos = 0;
  if (cands.length > MAX) {
    const COTA_MOLDURA = 40;
    const altura = innerHeight, largura = innerWidth;
    const notas = new Map();
    for (const el of cands) {
      const r = box(el);
      const naJanela = r.bottom > 0 && r.top < altura && r.right > 0 && r.left < largura;
      const moldura = !!NEO.subir(el, CHROME);
      const painel = el.closest(OPEN);
      const destaque = painel && naJanela && !NAO_E_PAINEL.includes(quadro(painel));
      let nota;
      if (rolaveis.has(el) || destaque) nota = 0;
      else if (moldura && el.matches('input,textarea,select,[role=searchbox],[role=combobox]')) nota = 1;
      else if (naJanela && !moldura) nota = 1;
      else if (naJanela) nota = 2;
      else if (!moldura && r.right > 0 && r.left < largura) nota = 3;
      else nota = 4;
      const longe = r.top > altura ? r.top - altura : r.bottom < 0 ? -r.bottom : 0;
      notas.set(el, nota + Math.min(longe / 1000000, 0.9));
    }
    const ranking = cands.slice().sort((a, b) => notas.get(a) - notas.get(b));
    const ficam = new Set();
    let molduras = 0;
    for (const el of ranking) {
      if (ficam.size >= MAX) break;
      if (Math.floor(notas.get(el)) === 2 && molduras++ >= COTA_MOLDURA) continue;
      ficam.add(el);
    }
    for (const el of ranking) {
      if (ficam.size >= MAX) break;
      ficam.add(el);
    }
    omitidos = cands.length - ficam.size;
    const dentro = cands.filter((el) => ficam.has(el));
    for (const el of cands) {
      if (!ficam.has(el)) { vistos.delete(el); origens.delete(el); }
    }
    cands.length = 0;
    cands.push(...dentro);
  }

  // ── 3. quem embrulha quem ──────────────────────────────────────────────
  // Para cada candidato, o candidato-ancestral mais próximo. Sobe pelo DOM
  // (O(n · profundidade)); comparar todos contra todos seria O(n²) e a tela do
  // Drive tem 200 candidatos.
  // A ordem de `cands` É a numeração dos refs (o laço de emissão anda por ela),
  // então dá para dizer ao core QUEM está dentro de quem — e não deduzir por
  // substring do rótulo, que erra: numa caixa cujo nome é a concatenação dos
  // filhos, "Mais ações" é substring do nome da caixa e "palworld Pasta" não é.
  const ordem = new Map();
  cands.forEach((el, i) => ordem.set(el, i));
  const filhos = new Map();
  const pai = new Map();
  for (const el of cands) {
    let p = el.parentElement;
    while (p && !vistos.has(p)) p = p.parentElement;
    if (p) {
      pai.set(el, p);
      filhos.set(p, (filhos.get(p) || 0) + 1);
    }
  }

  NEO.refs = [];
  const elements = [];
  for (const el of cands) {
    const ref = NEO.refs.push(el) - 1;
    // Painel rolável usa nome CURTO pelo mesmo motivo que a linha de lista: o
    // `label` de um painel é o innerText inteiro dele, e a linha do elemento
    // viraria um parágrafo. O conteúdo tem lugar próprio (`item.texto`).
    const row = el.matches(CONTAINER) || rolaveis.has(el);
    const item = { ref, tag: el.tagName.toLowerCase(), text: row ? NEO.shortLabel(el) : NEO.nomeDe(el) };
    const type = el.getAttribute('type') || el.getAttribute('role');
    if (type) item.type = type;

    // Geometria. O modelo não vê a tela quando a visão está desligada (chave sem
    // crédito, print recusado) e sem medida não distingue um ícone de 24px de
    // uma caixa de 1174px — foi exatamente o que ele confundiu. Coordenadas em
    // px de CSS, relativas ao viewport.
    const r = caixas.get(el);
    if (r) {
      item.box = [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)];
      // `NEO.visible` aceita uma folga generosa acima/abaixo para o modelo saber
      // que existe; mas clicar no que não está na tela é outra história.
      if (r.bottom < 0 || r.top > innerHeight) item.fora = true;
      // TESTE DE ACERTO: "este pixel reage a MIM?". `js_click` despacha o evento
      // direto no nó, então ele funciona MESMO com um banner de cookie por cima
      // — e devolve ok:true enquanto o site, que escuta no overlay, ignora. É o
      // mesmo `ok:true ≠ a página reagiu` do Drive entrando por outra porta.
      //
      // CONSERVADOR de propósito, em três camadas. Um "coberto" falso é pior
      // que nenhum: manda o modelo caçar um botão de fechar que não existe.
      //   1. ponto fora da tela não é testado (link de pulo mora em x=-9968;
      //      espremer o ponto para dentro testaria outro elemento qualquer);
      //   2. três pontos — acertar em UM já basta, porque sobreposição parcial
      //      continua clicável;
      //   3. só conta quem está na frente de verdade: uma CAMADA (fixed/sticky/
      //      absolute com z-index, ou dentro de um modal). Medido na Amazon: sem
      //      esta regra, 20 dos 200 elementos vinham "cobertos" por vizinhos de
      //      layout estáticos — ruído puro.
      if (!item.fora && r.width >= 2 && r.height >= 2) {
        const pontos = [
          [r.left + r.width / 2, r.top + r.height / 2],
          [r.left + Math.min(r.width * 0.25, 40), r.top + r.height / 2],
          [r.right - Math.min(r.width * 0.25, 40), r.top + r.height / 2],
        ];
        let acertou = false, tampa = null;
        for (const [x, y] of pontos) {
          if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
          const topo = NEO.topo(x, y);
          if (!topo) continue;
          if (topo === el || NEO.contem(el, topo) || NEO.contem(topo, el)) { acertou = true; break; }
          if (!tampa) tampa = topo;
        }
        if (!acertou && tampa) {
          let camada = null, q = tampa, m = 0;
          while (q && q !== document.body && m++ < 8) {
            const sq = cs(q);
            if (!sq) break;
            const flutua = sq.position === 'fixed' || sq.position === 'sticky' ||
              (sq.position === 'absolute' && sq.zIndex !== 'auto' && Number(sq.zIndex) > 0);
            if (flutua || q.matches(OPEN)) { camada = q; break; }
            q = q.parentElement;
          }
          // Barra lateral não conta. O clique é despachado direto no nó, então ele
          // chega, e dizer "coberto" reabriria a caça ao X de um painel que não fecha.
          const barra = camada && camada.closest(OPEN);
          if (camada && !(barra && quadro(barra) === 'lateral')) {
            item.coberto = NEO.shortLabel(camada) || camada.tagName.toLowerCase();
            // QUEM cobre muda o conselho. Um banner de cookie na frente = feche
            // e siga; um PAINEL ABERTO na frente = o que interessa provavelmente
            // está DENTRO dele. Sem esta distinção as duas situações chegavam ao
            // modelo com a mesma frase ("dispense o que está na frente"), e no
            // job 6785cf70 isso virou 11 tentativas de fechar o modal que era a
            // própria resposta.
            if (camada.matches(OPEN)) item.coberto_painel = true;
          }
        }
      }
    }

    // CAIXA: embrulha 2+ elementos e portanto não é um controle. Clicar numa
    // dessas é o loop que queimou dois jobs — o evento é despachado, o site
    // escuta no item de dentro, nada acontece e o driver responde "ok".
    // Evidência PRÓPRIA de clique isenta da regra da caixa: um `<label>` de
    // alternativa embrulha a bolinha e o texto, e chamá-lo de "caixa" faria o
    // driver recusar exatamente o clique que precisa acontecer. `cursor`
    // sozinho não isenta — é fraco demais, e a `row` do Drive continua caixa.
    const prova = provas.get(el) || evidencia(el);
    const forte = prova === 'tag' || prova === 'label' || prova === 'role' ||
      prova === 'handler' || prova === 'editavel' || prova === 'tabindex';
    const n = filhos.get(el) || 0;
    if (n >= 2 && !forte) item.grupo = n;
    // De quem este elemento é filho. É o que permite ao core recusar o clique na
    // caixa E dizer, com os números certos, quais são os alvos lá dentro.
    const acima = pai.get(el);
    if (acima !== undefined && ordem.has(acima)) item.dentro = ordem.get(acima);

    // Fora da área da tarefa: menu lateral, cabeçalho, rodapé.
    if (NEO.subir(el, CHROME)) item.chrome = true;

    // Região que ROLA. Duas coisas de uma vez: o modelo descobre que existe um
    // painel (e qual ref rolar), e ganha o TEXTO dele — que inclui o que está
    // abaixo da dobra, porque innerText não para na borda visível. No caso que
    // motivou isto, isso responde "qual o tamanho do arquivo" sem rolar nada.
    if (rolaveis.has(el)) {
      item.rola = [Math.round(el.scrollTop), Math.round(el.scrollHeight), Math.round(el.clientHeight)];
      const dentro = String(el.innerText || el.textContent || '').replace(/\n{3,}/g, '\n\n').trim();
      if (dentro) item.texto = dentro.slice(0, 1200);
    }

    // O PAINEL ABERTO em si — o texto inteiro dele, pelo mesmo motivo do painel
    // que rola. Antes só `rolaveis` ganhava `item.texto`, e um modal que CABE na
    // tela (não rola, logo não é `rolavel`) chegava ao prompt como um rótulo
    // cortado em ~120 chars. Foi assim que a timeline de rastreio da Amazon —
    // que ERA a resposta da tarefa — não apareceu em nenhum dos 25 prompts do
    // job 6785cf70: o modelo abriu e fechou o mesmo modal 7 vezes procurando
    // uma informação que estava dentro dele e que ele nunca teve como ler.
    if (el.matches(OPEN) && !NAO_E_PAINEL.includes(quadro(el))) {
      item.raiz = true;
      if (!item.texto) {
        const dentro = String(el.innerText || el.textContent || '').replace(/\n{3,}/g, '\n\n').trim();
        if (dentro) item.texto = dentro.slice(0, 2000);
      }
    }

    // Não basta saber que está num contêiner aberto: importa QUAL. "Fechar"
    // dentro de um menu de escolha é uma coisa; dentro do painel de progresso
    // do download é cancelar o trabalho (job 0611add0, 10 passos perdidos).
    const painel = el.closest(OPEN);
    if (painel) {
      const q = quadro(painel);
      if (q === 'lateral') {
        item.lateral = NEO.shortLabel(painel) || 'barra lateral';
      } else if (!NAO_E_PAINEL.includes(q)) {
        item.menu = true;
        const nome = NEO.shortLabel(painel);
        if (nome) item.panel = nome;
      }
    }
    if (!row) {
      // Botão sem nome próprio herda o nome da linha: "Mais ações" em "Print".
      const owner = el.closest(CONTAINER);
      if (owner && owner !== el) {
        const name = NEO.shortLabel(owner);
        if (name && name !== item.text && !item.text.includes(name)) item.owner = name;
      }
    }
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') item.disabled = true;
    // Campo de arquivo, ou o botão de envio ao lado de um input escondido: o
    // core diz ao modelo para usar `upload` em vez de clicar.
    if (NEO.arquivoDe(el) || (el.matches('button,[role=button],a,label,img') &&
        /foto|imagem|arquivo|anex|upload|carreg|alterar|escolh|selecion|troc|photo|image|file|attach|choose|browse|avatar|curr[ií]culo|\bcv\b|pdf/i
          .test(item.text || '') && NEO.arquivoPerto(el))) {
      item.arquivo = true;
    }
    // ESTADO sai do controle, nunca do rótulo — e quando o elemento é um
    // procurador (o label visível de um rádio escondido), o controle é o input
    // lá atrás. Sem isto, "marcar a alternativa" não tem como ser verificado:
    // selecionar um rádio não muda URL, nem título, nem texto da página.
    const controle = procuradores.get(el) ||
      (el.tagName === 'LABEL' ? (el.control || null) : null) ||
      (el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA' ? el : null);
    if (controle) {
      if (controle !== el) {
        item.controla = (controle.getAttribute('type') || controle.tagName.toLowerCase()) +
          (controle.name ? '[' + controle.name + ']' : '');
        if (controle.disabled) item.disabled = true;
      }
      const tipo = controle.getAttribute('type');
      if (tipo === 'checkbox' || tipo === 'radio') item.checked = !!controle.checked;
      else if (controle.tagName === 'SELECT') {
        item.options = Array.from(controle.options).slice(0, 30).map((o) => o.text.trim().slice(0, 60));
        item.value = controle.value;
      } else {
        const v = String(controle.value || '');
        item.value = v.slice(0, 80);
        // Texto longo (descrição, apresentação): a linha mostra só o começo, e o
        // modelo precisa saber que há mais — `extract` no ref lê o valor inteiro
        // (sessão c4316486: "Referência técnica do time" estava depois do corte).
        if (v.length > 80) item.valor_total = v.length;
      }
    }
    // Estado ARIA: o mesmo conceito para quem implementou o controle na mão.
    for (const a of ['aria-checked', 'aria-selected', 'aria-pressed', 'aria-expanded']) {
      const v = el.getAttribute(a);
      if (v === 'true' || v === 'false') {
        if (item.checked === undefined && (a === 'aria-checked' || a === 'aria-selected')) {
          item.checked = v === 'true';
        } else if (a === 'aria-expanded') item.aberto = v === 'true';
      }
    }
    // COMO este elemento foi achado. Só sai quando NÃO foi o site que declarou:
    // é o que deixa medir, no diário, quanto a varredura por pixel está
    // acrescentando em cada site.
    const origem = origens.get(el);
    if (origem && origem !== 'declarado') item.via = origem;
    // O endereço INTEIRO: o `abrir_aba` por número abre exatamente isto. Cortado em
    // 300, o link do AliExpress (~600 chars de rastreio) chegava pela metade, e
    // na aba D do job 9a3223a4 o corte caiu num `%xx`: a página veio vazia. O
    // prompt já mostra o link encurtado no core, então isto não o engorda.
    if (el.tagName === 'A') {
      const href = String(el.href || '');
      item.href = href.slice(0, 2000);
      if (href.length > 2000) item.href_cortado = true;
    }
    if (el.tagName === 'IMG') item.src = NEO.srcDe(el).slice(0, 300);
    // Aninhado: `grupo` solto já quer dizer "caixa com N itens" (o driver não
    // deixa clicar nela), e um rádio de grade não pode virar caixa.
    const grade = NEO.grade(el);
    if (grade) item.grade = grade;
    elements.push(item);
  }
  // Texto da PÁGINA INTEIRA, sempre. Tentei recortar isto para "a área da
  // tarefa" e o recorte jogou fora a resposta (job 9025d9f8: o texto virou as
  // 4 linhas da grade de arquivos e o painel de detalhes sumiu). O corte fino
  // agora mora nos painéis acima, que ADICIONAM texto em vez de subtrair.
  //
  // `|| textContent` pelo mesmo motivo de NEO.label: texto de leitor de tela e
  // nó fora do fluxo somem do innerText. Em Chrome o innerText sempre existe, então
  // isto é só a rede de segurança (e o que torna o coletor exercitável fora dele).
  const corpo = (document.body ? document.body.innerText || document.body.textContent : '') || '';
  // Normalizado UMA vez: o corte e o flag de "foi cortado" têm de medir a
  // MESMA string, senão o flag mente na fronteira.
  const corpoLimpo = corpo.replace(/\n{3,}/g, '\n\n').trim();

  // ── conteúdo PRINCIPAL, à frente da moldura ────────────────────────────
  // `body.innerText` sai em ordem de DOCUMENTO, e todo site põe a navegação
  // primeiro. No portal (job 9968e68a) os 3000 caracteres de orçamento viraram
  // os 20 links do menu lateral + cabeçalho + metadados da prova + os 10
  // enunciados do índice lateral — e o bloco "Alternativas", que ERA a tarefa,
  // ficou depois do corte. O modelo não podia clicar (não tinha ref) nem ler.
  //
  // Recortar a moldura fora seria repetir o erro do job 9025d9f8, em que o
  // recorte "só a área da tarefa" jogou a resposta no lixo. Então aqui nada é
  // subtraído: o conteúdo principal é ADICIONADO na frente, com orçamento
  // próprio, no mesmo espírito da seção dos painéis que rolam.
  let principal = null;
  const considerar = (el) => {
    if (!el || !visivel(el) || el.closest(CHROME)) return;
    // Estar DENTRO de uma barra lateral / caixa de busca também desqualifica —
    // não basta recusar o `<aside>`, porque a `<ul>` lá dentro não é um aside e
    // passava, ganhando a eleição por volume de texto contra o artigo ao lado.
    //
    // Isto usa o conjunto AMPLO, e não contradiz a regra de que o amplo só
    // serve para descontar: recusar a ELEIÇÃO não esconde texto nenhum — o
    // corpo e a seção de painéis continuam carregando o que estiver aí, e sem
    // nenhum eleito o corpo volta a receber o orçamento inteiro. Carimbar
    // `item.chrome`, não: aquilo muda como o modelo TRATA o elemento, e foi
    // assim que o painel de detalhes do Drive virou invisível no job 9025d9f8.
    // Mecanismos diferentes, custos de erro diferentes.
    if (el.closest(NEO.CHROME_DESCONTO)) return;
    const r = box(el);
    if (r.width * r.height < innerWidth * innerHeight * 0.08) return;
    const t = String(el.innerText || '').replace(/\n{3,}/g, '\n\n').trim();
    if (t.length < 150) return;
    // "O MAIOR TEXTO GANHA" elegia sempre o wrapper mais EXTERNO que não É
    // moldura — e um `<div>` que EMBRULHA o cabeçalho não está dentro dele,
    // então `closest(CHROME)` o aprova. O critério `t.length` então converge
    // para a página inteira, sempre.
    //
    // Na página de pedidos da Amazon (job 29c739d4) o vencedor foi o wrapper
    // da página: os 2200 caracteres de orçamento viraram os skip-links e a
    // lista de departamentos, e os 5 pedidos — que começavam 659 caracteres
    // depois do corte — não apareceram em NENHUM dos 15 prompts. O modelo
    // rolou 6 vezes atrás de um texto que não muda com rolagem (o dump sai em
    // ordem de documento) e morreu no freio de giro com a resposta na tela.
    //
    // A pontuação passa a ser o texto LÍQUIDO, fora moldura. `textContent` e
    // não `innerText` de propósito: innerText força layout e esta varredura
    // não pode virar O(página inteira). Como é DESCONTO, superestimar penaliza
    // o wrapper — que é o lado seguro do erro.
    let moldura = 0;
    for (const c of el.querySelectorAll(NEO.CHROME_DESCONTO)) moldura += (c.textContent || '').length;
    const liquido = Math.max(0, t.length - moldura);
    if (liquido < 150) return;
    // Quase só moldura não é conteúdo, é wrapper. Teto folgado (60%) porque
    // rejeitar demais devolve `principal = null`, e aí o corpo volta a ser
    // despejado em ordem de documento — o defeito que esta seção existe para
    // evitar.
    if (moldura > t.length * 0.6) return;
    const fundo = NEO.fundura(el);
    if (!principal) { principal = { el, texto: t, liquido, fundo }; return; }
    // EMPATE É O NORMAL, não a exceção: o container da lista e o wrapper que o
    // embrulha contêm quase o mesmo texto líquido (na Amazon, 10.800 contra
    // 10.865). Dentro de 10% ganha o MAIS PROFUNDO — o bloco específico, não
    // quem o embrulha. É este desempate que faz o trabalho; o desconto só
    // torna as duas pontuações comparáveis.
    const folga = Math.max(principal.liquido, liquido) * 0.1;
    if (liquido > principal.liquido + folga) { principal = { el, texto: t, liquido, fundo }; return; }
    if (Math.abs(liquido - principal.liquido) <= folga && fundo > principal.fundo) {
      principal = { el, texto: t, liquido, fundo };
    }
  };
  for (const el of document.querySelectorAll('main,[role=main],[role=tabpanel],article')) {
    considerar(el);
  }
  if (!principal) {
    // Sem landmark declarado: sobe a partir das empresas que não são moldura.
    // Conjunto pequeno e limitado — `innerText` força layout e não pode virar
    // varredura de página inteira.
    const olhados = new Set();
    for (const el of cands) {
      if (olhados.size > 120) break;
      if (el.closest(CHROME)) continue;
      let pp = el.parentElement;
      // SEM TETO DE PROFUNDIDADE. O teto era 6 níveis, e ele existia porque,
      // quando a pontuação era `t.length`, subir mais SEMPRE ganhava: a raiz
      // tem todo o texto. Com o desconto de moldura isso virou ao contrário —
      // a raiz pontua ZERO e se autoexclui (medido na busca da Amazon:
      // `#a-page` tem 21.426 caracteres de `innerText` e 48.893 de moldura por
      // `textContent`, porque o textContent inclui os menus escondidos; o
      // líquido dá 0).
      //
      // E o teto não era neutro: ele favorecia sistematicamente região RASA.
      // Numa grade de resultados o container fica ~10 níveis acima dos links
      // dos produtos, fora do alcance — enquanto o container de uma barra de
      // filtros, que é rasa, entrava. Foi assim que a BARRA DE FILTROS ganhou
      // a eleição na busca de pendrive (job 5ab98119): 1.305 caracteres de
      // marcas e faixas de preço eleitos como conteúdo, com os 18.711 do
      // `#search` nunca considerados.
      //
      // O custo continua limitado pelo `olhados`, que é o limite de verdade:
      // ancestral compartilhado entra uma vez só, e o teto de 120 agora é
      // checado também aqui dentro — senão um candidato fundo sozinho poderia
      // estourar o orçamento de `innerText`, que força layout.
      while (pp && pp !== document.body) {
        if (olhados.size > 120) break;
        if (!olhados.has(pp)) { olhados.add(pp); considerar(pp); }
        pp = pp.parentElement;
      }
    }
  }
  if (!principal) {
    // ÚLTIMO RECURSO: o laço de cima sobe a partir dos CANDIDATOS, que são
    // elementos interativos. Uma página cujo conteúdo não tem link nem botão
    // dentro — um artigo puro, uma fatura, um texto legal — nunca era
    // considerada, e caía no mesmo desfecho do wrapper de moldura: nenhum
    // `principal`, corpo despejado em ordem de documento, cabeçalho primeiro.
    //
    // Só roda quando nada foi eleito, e com o MESMO teto de 120: chegar aqui já
    // significa que a página é simples (nenhuma região interativa de conteúdo),
    // e o teto é o que impede o `innerText` de virar varredura de página
    // inteira — o custo que esta seção não pode ter.
    let vistos = 0;
    for (const el of document.querySelectorAll('main,article,section,div,ul,ol,table,dl')) {
      if (vistos++ > 120) break;
      considerar(el);
    }
  }
  // MODAL ABERTO GANHA o lugar de conteúdo principal — enquanto está aberto, ele
  // É o conteúdo. Não passa pelo `considerar` de propósito: aquele exige 8% do
  // viewport e 150 caracteres, e escolhe o MAIOR texto — três critérios que um
  // modal legítimo perde para o `<main>` atrás dele. Na Amazon o vencedor foi um
  // `<nav>` de skip-links ("Ir para / Conteúdo principal / Atalhos do teclado"),
  // enquanto o modal com a resposta não entrou em lugar nenhum.
  //
  // Só `bloqueia` (ver `quadro`). Foram três reprises do mesmo erro com o mesmo
  // painel, cada uma remendada com mais um filtro: fora da tela (`left: 1819`),
  // em fluxo (a barra de filtros), e agora na borda de uma janela larga. O que
  // faltava era a pergunta certa: ele cobre o que o usuário está olhando?
  const modal = [...NEO.todos('[role=dialog],[role=alertdialog],[aria-modal="true"]', raizes)]
    .filter((el) => visivel(el) && quadro(el) === 'bloqueia')
    .sort((a, b) => { const ra = box(a), rb = box(b); return rb.width * rb.height - ra.width * ra.height; })[0];
  if (modal) {
    const t = String(modal.innerText || modal.textContent || '').replace(/\n{3,}/g, '\n\n').trim();
    // `liquido`/`fundo` vão aqui só para o formato não divergir do que
    // `considerar` produz — esta atribuição é a última e ninguém pontua depois
    // dela, mas um registro com forma diferente é como um campo vira
    // `undefined` seis meses depois.
    if (t.length >= 40) principal = { el: modal, texto: t, liquido: t.length, fundo: NEO.fundura(modal) };
  }
  // PUBLICADO para o `extract` sem `ref` (ver `js_extract`). Não é cache: é a
  // MESMA escolha que acabou de ir no prompt, guardada em vez de reimplementada.
  // Reimplementar criaria uma segunda verdade, e é assim que a extensão passou a
  // mentir com autoridade uma vez (`neo-map.generated.js`) — aqui seria pior,
  // porque o modelo leria uma região e veria outra descrita.
  NEO.principal = principal ? principal.el : null;
  // Publicado pelo mesmo motivo: `extract` lê com o mesmo critério do snapshot.
  NEO.modal = modal && principal && principal.el === modal ? modal : null;
  // ── a LEITURA da página (ver `NEO.ler`) ────────────────────────────────
  // Barra lateral tem seção própria e moldura é moldura. O resto do que está na
  // janela entra, inclusive modal e popover: o driver deduplica por linha.
  NEO.foraDaLeitura = (el) => {
    const p = el.closest(OPEN);
    if (p && quadro(p) === 'lateral') return true;
    return !!NEO.subir(el, CHROME);
  };

  // ── AVISOS POR CIMA DA PÁGINA ──────────────────────────────────────────
  // Toast, snackbar, a faixa "lembre-se de finalizar": a página fala com quem
  // está usando por uma camada por cima, e essa camada costuma ser o ÚLTIMO
  // filho do <body>, ou seja, o último texto da leitura e o primeiro a cair no
  // teto. No job 21c563c1 (Portal) "Você respondeu todas as questões, lembre-se
  // de Finalizar o questionário" ficou na tela por oito passos sem chegar ao
  // modelo, que repetiu a questão 10 até o PARE. Por isso o aviso sai do texto
  // corrido e ganha seção própria.
  //
  // Dois sinais, sem biblioteca: região viva declarada (`role=alert|status`,
  // `aria-live`) e camada `fixed` presa ao <body> que FALA em vez de navegar
  // (pouco texto, no máximo três controles). A barra do topo do site também é
  // `fixed`, mas é feita de links e fica de fora; menu e diálogo já têm a seção
  // de painel.
  const AVISO_VIVO = '[role=alert],[role=status],[aria-live=polite],[aria-live=assertive]';
  const CONTROLES = 'a[href],button,input,select,textarea,[role=button],[role=link],[role=menuitem]';
  const candAviso = new Set(NEO.todos(AVISO_VIVO, raizes));
  const fila = document.body ? [...document.body.children].map((e) => [e, 0]) : [];
  for (let i = 0; i < fila.length && i < 400; i++) {
    const [e, n] = fila[i];
    if (e.matches(NEO.NOSSOS)) continue;
    const s = cs(e);
    if (s && s.position === 'fixed') { candAviso.add(e); continue; }
    if (n < 2) for (const c of e.children) fila.push([c, n + 1]);
  }
  // Novo = texto que este elemento não mostrava no snapshot anterior. A chave é
  // o par elemento+texto porque a biblioteca reaproveita o recipiente para o
  // aviso seguinte. Na primeira leitura de uma página nada é "novo".
  const avisosAntes = NEO.avisosVistos;
  NEO.avisosVistos = new WeakMap();
  const avisos = [];
  for (const el of candAviso) {
    if (el.matches(NEO.NOSSOS) || el.closest(OPEN)) continue;
    if (!visivel(el)) continue;
    const r = box(el);
    if (!(r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth)) continue;
    if (r.width * r.height > innerWidth * innerHeight * 0.5) continue;
    if (r.height >= innerHeight * 0.4 && r.width <= innerWidth * 0.4) continue;
    const texto = String(el.innerText || '').replace(/\s*\n\s*/g, ' · ').replace(/\s+/g, ' ').trim();
    if (texto.length < 3 || texto.length > 500) continue;
    if (el.querySelectorAll(CONTROLES).length > 3) continue;
    if (avisos.some((a) => NEO.contem(a.el, el))) continue;
    for (let i = avisos.length - 1; i >= 0; i--) if (NEO.contem(el, avisos[i].el)) avisos.splice(i, 1);
    avisos.push({ el, texto, nova: !!avisosAntes && avisosAntes.get(el) !== texto });
  }
  for (const a of avisos) NEO.avisosVistos.set(a.el, a.texto);
  const foraDaTela = (el) => avisos.some((a) => NEO.contem(a.el, el)) || NEO.foraDaLeitura(el);
  const leitura = NEO.ler(foraDaTela, { recorte: true });
  const TETO_TELA = CHARS + 2000;
  const barras = [];
  for (const [p, q] of quadros) {
    if (q !== 'lateral') continue;
    barras.push({
      rotulo: NEO.shortLabel(p),
      texto: String(p.innerText || '').replace(/\s*\n\s*/g, ' · ').trim().slice(0, 300),
    });
  }
  // IMAGENS. O texto da página não traz endereço de imagem, e nenhuma ferramenta
  // devolvia: "a URL da foto principal" era impossível por construção. O cérebro
  // pede isso em toda coleta de produto, porque só mostra no chat a imagem que
  // algum resultado de tool trouxe. No job aa4c89f8 o modelo gastou 32 de 39
  // decisões procurando um dado que nenhuma leitura podia devolver.
  const imagens = [];
  for (const img of NEO.todos('img', raizes)) {
    const r = box(img);
    if (r.width < 100 || r.height < 100) continue;
    if (!(r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth)) continue;
    if (!visivel(img)) continue;
    const url = NEO.srcDe(img);
    if (!url || url.startsWith('data:')) continue;
    imagens.push({
      url: url.slice(0, 300),
      alt: String(img.alt || '').trim().slice(0, 70),
      w: Math.round(r.width),
      h: Math.round(r.height),
    });
  }
  imagens.sort((a, b) => b.w * b.h - a.w * a.h);
  // IFRAME. Nem `innerText` nem `querySelectorAll` entram nele, então checkout,
  // formulário embutido e player de outro domínio eram tela branca sem aviso:
  // o modelo só podia concluir que a informação não existia. O mesmo domínio dá
  // para ler daqui; o de outro domínio, não, mas o endereço dele dá para abrir.
  const iframes = [];
  for (const f of NEO.todos('iframe', raizes)) {
    if (iframes.length >= 4) break;
    const r = f.getBoundingClientRect();
    if (r.width * r.height < innerWidth * innerHeight * 0.1) continue;
    if (!(r.top < innerHeight && r.bottom > 0 && r.left < innerWidth && r.right > 0)) continue;
    let texto = '';
    let legivel = false;
    try {
      const b = f.contentDocument && f.contentDocument.body;
      if (b) { texto = String(b.innerText || '').trim().slice(0, 2000); legivel = true; }
    } catch (_) { legivel = false; }
    iframes.push({ url: String(f.src || '').slice(0, 300), titulo: f.title || '', legivel, texto });
  }
  // ── estado REAL da mídia ───────────────────────────────────────────────
  // O rótulo do botão de play/pause nomeia a AÇÃO, não o estado: o YouTube
  // escreve "Pausa" JUSTAMENTE quando o vídeo está tocando. O modelo leu isso ao
  // contrário e, como o botão é um INTERRUPTOR, cada leitura errada inverteu o
  // que estava valendo — play, pause, play, pause por 25 passos, com o usuário
  // vendo o vídeo piscar (job 717a8fbb). Estado não se deduz de rótulo: sai do
  // próprio elemento.
  const tempo = (s) => {
    if (!isFinite(s) || s < 0) return '?';
    const mm = Math.floor(s / 60), ss = Math.floor(s % 60);
    return mm + ':' + String(ss).padStart(2, '0');
  };
  let midia = null;
  const players = [...document.querySelectorAll('video,audio')].filter(
    (el) => el.currentSrc || el.src || el.duration > 0
  );
  // A maior área ganha: o player principal, não o preview do vídeo sugerido.
  players.sort((a, b) => {
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    return rb.width * rb.height - ra.width * ra.height;
  });
  if (players[0]) {
    const el = players[0];
    midia = {
      tipo: el.tagName.toLowerCase(),
      tocando: !el.paused && !el.ended,
      mudo: !!el.muted || el.volume === 0,
      pos: tempo(el.currentTime),
      dur: tempo(el.duration),
    };
  }
  return {
    url: location.href,
    title: document.title || '',
    ready: document.readyState,
    stop: document.documentElement.getAttribute('data-neo-stop') === '1',
    scroll: { y: Math.round(scrollY), height: Math.round(document.body ? document.body.scrollHeight : 0) },
    // ORÇAMENTO. Eram dois literais (2200 para o principal, 1500 para o corpo)
    // e as DUAS janelas começavam no caractere 0 da MESMA string: 3700
    // caracteres de prompt entregando ~2400 únicos — e no job 29c739d4, ZERO
    // úteis, porque o prefixo comum era a moldura inteira da Amazon. Pior: o
    // `text_chars` que o core manda era ignorado sempre que havia principal,
    // então não existia alavanca nenhuma.
    //
    // Agora os dois saem de `text_chars` e o corpo vai INTEIRO até o
    // orçamento. Pré-cortá-lo em 1500 era justamente o que impedia a subtração
    // no driver de funcionar: o que sobrava era subconjunto do `main`, o
    // resultado vinha vazio, caía abaixo do piso de segurança e a moldura
    // voltava DUPLICADA. A subtração continua morando num lugar só — o driver,
    // que já a fazia e já tem o histórico dela.
    text: corpoLimpo.slice(0, CHARS),
    // Houve corte? O driver precisa saber para não afirmar ao modelo que o
    // snapshot traz a página inteira — foi essa afirmação falsa que realimentou
    // o giro do job 29c739d4, cinco vezes.
    text_cut: corpoLimpo.length > CHARS,
    main: principal
      ? {
          label: NEO.shortLabel(principal.el),
          text: principal.texto.slice(0, CHARS),
          cut: principal.texto.length > CHARS,
        }
      : null,
    // O que está NA JANELA agora e o que existe fora dela. `tela_total` é o que
    // permite ao driver dizer de onde o `extract` com `de` continua a leitura.
    tela: leitura.tela.slice(0, TETO_TELA),
    tela_cut: leitura.tela.length > TETO_TELA,
    tela_total: leitura.tela.length,
    resto: leitura.resto.slice(0, CHARS),
    resto_total: leitura.resto.length,
    avisos: avisos.slice(0, 4).map((a) => ({ texto: a.texto, nova: a.nova })),
    barras,
    iframes,
    // Carimbo do que este device sabe fazer. O core sobe sozinho e o device só
    // depois de reiniciar o `tauri dev`: sem isto o core oferece ação que aqui
    // ainda não existe.
    capacidades: ['find', 'clique_real'],
    imagens: imagens.slice(0, 8),
    truncated: omitidos > 0 || elements.length >= MAX,
    omitidos,
    midia,
    elements,
  };
})();
};

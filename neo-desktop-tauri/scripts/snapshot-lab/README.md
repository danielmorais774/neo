# snapshot-lab — exercitar o JS do `browser_local` fora do desktop

O inventário de elementos (`js_snapshot`), o clique (`js_click`) e o clique por
texto (`js_click_text`) vivem como **strings JS dentro do Rust**
(`src-tauri/src/services/browser_local.rs`). `cargo test` não os alcança e
`tauri dev` só prova depois de reiniciar o app — o que fez com que, por meses,
a única forma de descobrir que a percepção estava cega fosse queimar 25 passos
num job de verdade.

Este laboratório extrai as strings do `.rs` e as roda num Chromium headless de
verdade (layout, `getComputedStyle` e `elementFromPoint` reais — o que jsdom não
tem e sem o que nenhum destes três funciona).

## Rodar

```sh
python3 e2e.py file://$PWD/questionario.html          # a página que quebrou o job 9968e68a
python3 e2e.py file://$PWD/questionario-com-modal.html # a mesma, com um modal por cima
python3 cdp.py https://www.amazon.com.br/s?k=teclado ../../../snapshot.js

python3 conteudo-principal.py            # o corpus da ELEIÇÃO do conteúdo principal
python3 conteudo-principal.py <url|arq>  # inspeciona a eleição numa página só
python3 leitura.py                       # a LEITURA da tela e as formas de painel

python3 clique.py                        # o corpus do CLIQUE: cada cena tem de contar 1
python3 clique.py --rs outro.rs          # a mesma medida sobre outra versão do .rs
python3 clique.py --url <url> "nome"     # a varredura da DOM numa página de verdade
```

Precisa de `websockets` (`pip install websockets`) e do Chromium do Playwright em
`~/Library/Caches/ms-playwright/chromium_headless_shell-*` (ajuste `CHROME` no
topo do `cdp.py` para usar outro).

## O que cada fixture prova

`questionario.html` reproduz os dois jeitos comuns de o site desenhar uma opção
sem markup canônico — e nenhum dos dois aparecia no inventário antigo:

- **três `<label>` com `<input type=radio>` invisível** (`opacity:0;width:1px`),
  que é como quase toda web estiliza rádio. O input real é descartado pelo
  filtro de visibilidade; quem tem que virar `ref` é o label, com o `checked`
  vindo do input lá atrás;
- **dois `<div ng-click>`** sem papel, sem `onclick`, sem tag interativa — só
  `cursor:pointer`.

O teste passa quando as **cinco** aparecem no snapshot, o clique numa delas
devolve `checked:true`, e o `Responder` é reportado `[desabilitado]`.

`questionario-com-modal.html` prova o contrário: com um `position:fixed` por
cima, os elementos saem marcados `coberto` e o `click_text` RECUSA em vez de
despachar um evento que a página vai ignorar.

## A eleição do conteúdo principal (`conteudo-principal.py`)

`body.innerText` sai em **ordem de documento** e todo site põe a navegação
primeiro, então o `js_snapshot` elege um bloco de conteúdo e o põe no prompt
ANTES do corpo. O critério era "o maior texto ganha" — e como `closest(CHROME)`
só rejeita quem **está dentro** da moldura, nunca um `<div>` que a **embrulha**,
esse critério converge sempre para a raiz da página.

No job `29c739d4` (pedidos da Amazon) o vencedor foi o wrapper da página: os
2200 caracteres de orçamento viraram os skip-links e a lista de departamentos, e
os 5 pedidos — que começavam **659 caracteres depois do corte** — não apareceram
em nenhum dos 15 prompts. O modelo rolou 6 vezes atrás de um texto que não muda
com rolagem e morreu no freio de giro com a resposta na tela.

Hoje a pontuação é o texto **líquido** (descontado o das subárvores de moldura) e
o desempate, dentro de 10%, é pelo bloco **mais profundo** — o específico, não
quem o embrulha. Um `<aside>`, e o que está dentro dele, não concorre.

O teste roda um **corpus**, não um caso: afinar contra uma página é como se chega
num atalho por site disfarçado de heurística. Cada fixture é uma FORMA de página.

| fixture | a forma que ela representa |
|---|---|
| `../../../sample-log/Seus pedidos.html` | sem `<main>`, wrapper de moldura grande (a captura real do job `29c739d4`) |
| `questionario.html` | índice lateral competindo com o enunciado (job `9968e68a`) |
| `questionario-com-modal.html` | overlay por `position:fixed`, sem papel ARIA |
| `com-main.html` | `<main>` declarado — o caminho rápido tem de resolver |
| `com-aside.html` | `<aside>` com mais texto que o artigo ao lado |

Para ampliar o corpus, salve a página inteira pelo navegador (Cmd+S, "página
completa") em `sample-log/` e acrescente uma linha ao `CORPUS` no topo do script,
com um trecho que TEM de aparecer no bloco eleito. Páginas boas de ter: uma
listagem do Drive, um webmail, um checkout.

## A leitura da tela e as formas de painel (`leitura.py`)

O snapshot traz `tela` (o texto na janela agora, em ordem de leitura) e `resto` (o
que existe fora dela). Isto independe de landmark e de eleição: é o que garante
que o modelo lê o que o usuário está vendo em qualquer site. O `leitura.py` roda
isso em cada FORMA de página, inclusive com o `content-overlay.js` real por cima,
que é o que trava a regressão do `⟪COBERTO por "div"⟫`.

| fixture | a forma que ela representa |
|---|---|
| `produto-com-barra.html` | barra presa na borda: visível em janela larga, fora da tela em janela estreita (o carrinho da Amazon) |
| `paineis.html?caso=cookie` | `role=dialog` no rodapé: é painel, mas não toma o lugar do conteúdo |
| `paineis.html?caso=gaveta` | gaveta com fundo: parece a barra, mas bloqueia a página |
| `paineis.html?caso=tapume` | modal opaco de tela cheia: o que está atrás sai da tela e vai para `resto` |
| `sombra.html` | web component, cartão com `<a>` transparente esticado, iframe dos dois tipos |
| `saturada.html` | coluna com centenas de links ANTES do conteúdo: é o que comia o teto de 200 |

## O clique (`clique.py` + `botoes-disfarcados.html`)

Evento sobe na árvore, não desce. O botão da Amazon é um `<input>` com
`opacity:0.01` esticado sobre um desenho, e o clique ia para o `span` pai: 16
tentativas com `ok:true` e o carrinho vazio (job `8bc25dd1`). `NEO.mirar` pergunta
quem recebe o PIXEL, que é o que o navegador faz para um humano, e `clicar()` no
Rust dispara o mouse real pelo CDP e confere com uma sonda. Evento fabricado só
entra quando a sonda prova que nada chegou: sem essa prova, a reserva viraria
clique dobrado.

Cada cena conta quantas vezes o handler de verdade rodou, e a conta tem de ser
**exatamente 1**: 0 é clique morto, 2 é a reserva em cima do mouse real. O script
refaz o caminho do `clicar()`, por `ref` e por nome. O mesmo caminho em Rust roda
em `cargo test browser_local -- --include-ignored --test-threads=1`.

Antes e depois sobre as mesmas 36 tentativas (18 cenas × 2 modos):

| | falhas |
|---|---|
| clique antigo | 15 |
| `mirar` + mouse real | 0 |

Os casos que mais ensinam:

- **`amazon`/`irmao`/`longe`**: controle transparente, filho ou irmão do desenho;
- **`so-value`/`so-aria`/`so-title`/`so-alt`**: sem nó de texto, o `click_text`
  antigo dizia "não achei";
- **`confiavel`**: handler que exige `isTrusted`, só o mouse real passa;
- **`aceite`**: link no meio do label. O centro cai no link, e a mira procura um
  ponto que seja do label. Frase que atravessa nós (`Li e aceito os <a>termos</a>`)
  só casa com o texto inteiro do controle;
- **`sob-painel`**: debaixo do painel do Neo, o mouse real acertaria o painel, e a
  cena tem de sair `via=js`.

## Medido em sites reais (2026-10-31, viewport 1912×1000, depois da leitura)

| página | snapshot | texto na tela |
|---|---|---|
| amazon.com.br (produto) | 125 ms | 1.329 |
| amazon.com.br (busca) | 400 ms | 1.996 |
| pt.wikipedia.org | 90 ms | 2.621 |
| github.com | 37 ms | 1.989 |
| reddit.com (shadow DOM) | 105 ms | 1.342 |
| news.ycombinator.com | 21 ms | 3.613 |

## Medido em sites reais (2026-09-05, viewport 1600×1000)

| página | tempo do snapshot | elementos |
|---|---|---|
| amazon.com.br (busca) | 155–209 ms | 200 (teto) |
| github.com/anthropics | 44–51 ms | 142 |
| news.ycombinator.com | 26–28 ms | 200 (teto) |

Nos três, a varredura geométrica não acrescentou nada: são sites com markup
canônico, e o custo dela é só a grade de `elementFromPoint`. Ela existe para o
resto da web.

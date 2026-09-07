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

## Medido em sites reais (2026-09-05, viewport 1600×1000)

| página | tempo do snapshot | elementos |
|---|---|---|
| amazon.com.br (busca) | 155–209 ms | 200 (teto) |
| github.com/anthropics | 44–51 ms | 142 |
| news.ycombinator.com | 26–28 ms | 200 (teto) |

Nos três, a varredura geométrica não acrescentou nada: são sites com markup
canônico, e o custo dela é só a grade de `elementFromPoint`. Ela existe para o
resto da web.

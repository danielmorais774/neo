"""Diário de bordo de uma tarefa no navegador local.

Por que existe: o driver decide até 25 vezes por tarefa e, quando o resultado é
"não deu", a evidência ficava espalhada entre três lugares que não conversam —
o ``app.log`` (o payload cru do LLM, numa linha de 300 KB por passo), o banco
(uma frase de resumo) e a tela do usuário, que já passou. Reconstruir uma
tarefa custava meia hora de ``grep``, e mesmo assim as imagens que o modelo viu
não existiam em lugar nenhum.

Aqui cada passo deixa registrado, em ordem:

* o que o modelo **viu** — a página renderizada exatamente como foi ao prompt, e
  o JPEG do passo salvo em disco (com as marcações, quando o device desenhou);
* o que ele **decidiu** — o `thought`, a ação e os argumentos, mais a resposta
  crua quando o JSON não deu para ler;
* o que a página **respondeu** — ok/erro, detalhe e quanto tempo levou;
* se a tela **mudou** depois da ação (é o sinal que separa clique de clique inútil);
* o que apareceu na **pasta de downloads** — antes e depois da tarefa inteira.

Um diretório por job: ``trace.md`` legível + ``NN-tela.jpg`` de cada passo. A
escrita é incremental (append a cada evento) de propósito: job que morre no
meio, ou core que reinicia com ``--reload``, ainda deixa tudo até ali.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Quantos diretórios de tarefa ficam em disco. Cada um pesa ~25 JPEGs; 20 dá
# história suficiente para investigar sem virar um problema de espaço.
_KEEP_RUNS = 20


class RunTrace:
    """Escreve o diário de UMA tarefa. Nunca levanta exceção para o driver."""

    def __init__(self, root: Path | str, job_id: str) -> None:
        self.dir = Path(root) / str(job_id)
        self.path = self.dir / "trace.md"
        #: Prefixo dos prints. Vazio no principal; `B-` no diário do subagente da
        #: aba B, para os dois não sobrescreverem `03-tela.jpg` um do outro.
        self.prefixo = ""
        self.ok = False
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            _prune(Path(root))
            self.ok = True
        except Exception as exc:  # noqa: BLE001 — diário quebrado não para a tarefa
            logger.warning("[BrowserLocal] não consegui abrir o diário: %s", exc)

    def filho(self, rotulo: str) -> "RunTrace":
        """O diário do subagente da aba `rotulo`: `trace-B.md`, na mesma pasta."""
        filho = RunTrace.__new__(RunTrace)
        filho.dir = self.dir
        filho.path = self.dir / f"trace-{rotulo}.md"
        filho.prefixo = f"{rotulo}-"
        filho.ok = self.ok
        return filho

    # ── escrita ──────────────────────────────────────────────────────────

    def _write(self, text: str) -> None:
        if not self.ok:
            return
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(text)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[BrowserLocal] diário falhou: %s", exc)
            self.ok = False

    @staticmethod
    def _now() -> str:
        return datetime.utcnow().strftime("%H:%M:%S.%f")[:-3]

    # ── eventos ──────────────────────────────────────────────────────────

    def header(self, **fields: Any) -> None:
        lines = [f"# Tarefa no navegador local — `{self.dir.name}`", ""]
        lines += [f"**{key}:** {value}" for key, value in fields.items() if value not in (None, "")]
        lines.append("")
        self._write("\n".join(lines) + "\n")

    def note(self, title: str, body: str = "") -> None:
        self._write(f"\n**{self._now()} · {title}**\n{body}\n" if body else f"\n**{self._now()} · {title}**\n")

    def step_open(self, number: int, total: int) -> None:
        self._write(f"\n---\n\n## Passo {number}/{total} · {self._now()}\n")

    def saw(self, rendered: str, snapshot: dict) -> None:
        """A página exatamente como foi ao prompt — nada de resumo."""
        elements = snapshot.get("elements") or []
        menu = sum(1 for item in elements if item.get("menu"))
        self._write(
            f"\n- URL: `{snapshot.get('url', '')}`\n"
            f"- elementos: {len(elements)}"
            + (f" · **{menu} num menu/diálogo aberto**" if menu else "")
            + (" · lista CORTADA" if snapshot.get("truncated") else "")
            + f"\n\n<details><summary>página como o modelo viu</summary>\n\n```\n{rendered}\n```\n</details>\n"
        )

    def panel(self, evento: str, rotulo: str, veredito: str = "") -> None:
        """Painel abriu/fechou/foi triado.

        Fica no diário porque é a evidência de que a distinção entre "painel que
        eu abri" e "intruso que apareceu" está funcionando — e, quando não
        estiver, é aqui que se vê qual das camadas errou (causalidade, léxico ou
        visão).
        """
        marca = {"abriu": "🪟", "fechou": "🚪", "triagem": "⚖️"}.get(evento, "🪟")
        self._write(
            f"\n- {marca} **painel {evento}**: «{rotulo}»"
            + (f" → {veredito}" if veredito else "")
            + "\n"
        )

    def shot(
        self,
        number: int,
        data: str,
        marked: bool | None,
        recorte: bool = False,
        final: bool = False,
        tempos: dict | None = None,
        gatilho: str = "",
    ) -> str | None:
        """Salva o JPEG do passo e devolve o nome do arquivo."""
        if not self.ok or not data:
            return None
        # O print do fim ganha nome próprio: é o que vai para o chat, e no
        # diário ele precisa ser achável sem contar passos.
        # Print de confirmação: o nome diz o gatilho ("07-G2.jpg"), e dois no
        # mesmo passo (o depois do campo anterior + o antes deste) não se apagam.
        codigo = re.sub(r"[^A-Za-z0-9+]", "", gatilho.split(" ", 1)[0]).replace("+", "-") if gatilho else ""
        name = self.prefixo + (
            f"{number:02d}-final.jpg" if final
            else f"{number:02d}-{codigo}.jpg" if codigo
            else f"{number:02d}-painel.jpg" if recorte
            else f"{number:02d}-tela.jpg"
        )
        if codigo and (self.dir / name).exists():
            extra = 2
            while (self.dir / name.replace(".jpg", f"-{extra}.jpg")).exists():
                extra += 1
            name = name.replace(".jpg", f"-{extra}.jpg")
        try:
            import base64

            raw = base64.b64decode(data)
            (self.dir / name).write_bytes(raw)
        except Exception as exc:  # noqa: BLE001
            self._write(f"\n- print: **não consegui salvar** ({exc})\n")
            return None
        # `marked is None` = o device não respondeu o campo, ou seja é uma versão
        # anterior ao desenho das marcações. É o sintoma de desktop desatualizado.
        estado = (
            f"confirmação {gatilho}" + (" (recortado)" if recorte else "") if gatilho
            else "a tela do FIM, sem marcações — é este que vai para o chat" if final
            else "recorte do painel, sem marcações (é para LER, não para clicar)" if recorte
            else "com marcações" if marked
            else ("SEM marcações (desktop antigo? o campo `marks` nem voltou)" if marked is None
                  else "SEM marcações (o device não conseguiu desenhar)")
        )
        self._write(f"\n- print: `{name}` — {len(raw) // 1024} KB, {estado}\n")
        linha = self._tempos_do_print(tempos)
        if linha:
            self._write(f"  - tempo: {linha}\n")
        return name

    @staticmethod
    def _tempos_do_print(tempos: dict | None) -> str:
        """Onde foi o tempo do print: o device (por etapa) e o transporte (o resto).

        Desktop antigo não manda as etapas: sai só a ida e volta.
        """
        if not tempos:
            return ""
        total = tempos.get("ida_e_volta_ms")
        device = tempos.get("total_ms")
        if not isinstance(total, (int, float)):
            return ""
        if not isinstance(device, (int, float)):
            return f"{total:.0f} ms de ida e volta (o desktop não mandou as etapas)"
        etapas = " · ".join(
            f"{nome} {tempos[chave]:.0f}"
            for chave, nome in (
                ("captura_ms", "captura"), ("esconder_ms", "esconder painel"),
                ("mostrar_ms", "mostrar painel"), ("rects_ms", "retângulos"), ("imagem_ms", "imagem"),
            )
            if isinstance(tempos.get(chave), (int, float))
        )
        tamanhos = ""
        if isinstance(tempos.get("bytes_captura"), (int, float)) and isinstance(tempos.get("bytes_saida"), (int, float)):
            tamanhos = f" · {tempos['bytes_captura'] // 1024} KB capturado → {tempos['bytes_saida'] // 1024} KB enviado"
        return (
            f"{total:.0f} ms de ida e volta = device {device:.0f} ms ({etapas}) "
            f"+ transporte {max(0, total - device):.0f} ms{tamanhos}"
        )

    def no_shot(self, reason: str) -> None:
        self._write(f"\n- print: **não tirado** — {reason}\n")

    def thought(
        self, model: str, ms: float, decision: dict, raw: str = "", *, orcamento: str = ""
    ) -> None:
        body = (
            f"\n- modelo: `{model}` · {ms:.0f}ms" + (f" · {orcamento}" if orcamento else "") + "\n"
            f"- pensou: {decision.get('thought') or '(nada)'}\n"
            f"- escolheu: `{decision.get('action')}` "
            f"{json.dumps(decision.get('args') or {}, ensure_ascii=False)}\n"
        )
        if raw:
            body += f"\n<details><summary>resposta crua</summary>\n\n```\n{raw}\n```\n</details>\n"
        self._write(body)

    def vision(self, model: str, ms: float, question: str, answer: str) -> None:
        """O que os OLHOS responderam — o passo mais caro do desenho novo.

        Fica no diário porque é aqui que se audita se a visão está valendo o
        preço: pergunta específica e resposta útil, ou "não dá para ver" em
        série. Sem isto o trace mostraria só a decisão que veio depois.
        """
        self._write(
            f"\n- 👁 **look** · `{model}` · {ms:.0f}ms"
            f"\n  - perguntou: {question}"
            f"\n  - viu: {answer}\n"
        )

    def llm_failed(self, model: str, exc: Exception) -> None:
        self._write(f"\n- modelo `{model}` **falhou**: {exc}\n")

    def refused(self, reason: str) -> None:
        self._write(f"\n- ❌ **recusado antes de tocar na página**: {reason}\n")

    def acted(self, action: str, args: dict, ok: bool, detail: str, ms: float) -> None:
        safe = {k: ("…" if k == "value" else v) for k, v in args.items()}
        self._write(
            f"\n- ▶ `{action}` {json.dumps(safe, ensure_ascii=False)} → "
            f"{'ok' if ok else '**FALHOU**'} · {ms:.0f}ms"
            + (f" — {detail}" if detail else "")
            + "\n"
        )

    def changed(self, moved: bool, stuck: int) -> None:
        self._write(
            f"\n- tela depois da ação anterior: {'mudou' if moved else '**NÃO mudou**'}"
            + (f" (travado há {stuck})" if stuck else "")
            + "\n"
        )

    def downloads(self, label: str, listing: dict | None, error: str = "") -> None:
        if error:
            self._write(f"\n**{self._now()} · downloads ({label}):** não deu para ler — {error}\n")
            return
        files = (listing or {}).get("files") or []
        if not files:
            self._write(f"\n**{self._now()} · downloads ({label}):** nada recente\n")
            return
        lines = [f"\n**{self._now()} · downloads ({label}):** {len(files)} arquivo(s)", ""]
        lines += [
            f"- `{f.get('name')}` — {f.get('bytes', 0)} bytes, {f.get('seconds_ago', 0)}s atrás"
            + (" · **AINDA BAIXANDO**" if f.get("incomplete") else "")
            for f in files
        ]
        self._write("\n".join(lines) + "\n")

    def footer(self, status: str, summary: str, steps: int, elapsed: float) -> None:
        self._write(
            f"\n---\n\n## Fim · {self._now()}\n\n"
            f"- estado: **{status}**\n- passos: {steps}\n- duração: {elapsed:.1f}s\n\n"
            f"```\n{summary}\n```\n"
        )


def _prune(root: Path) -> None:
    """Mantém só os ``_KEEP_RUNS`` diretórios mais novos."""
    try:
        runs = sorted(
            (p for p in root.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception:  # noqa: BLE001
        return
    for old in runs[_KEEP_RUNS:]:
        shutil.rmtree(old, ignore_errors=True)

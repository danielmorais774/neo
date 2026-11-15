# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27", "huggingface_hub>=0.30", "pyarrow>=15"]
# ///
"""Eval do Neo inteiro no GAIA (validação) — pelo chat, como um pedido normal.

Manda cada pergunta ao ``POST /api/v1/desktop/chat/stream`` (o mesmo caminho do
chat do neo-panel), espera a resposta — inclusive a que chega depois, quando o
Neo vira o trabalho num job — e corrige com a régua oficial do GAIA (quasi
exact match). Mede o Neo como ele está: roteador, cérebro, tools, sandbox.

    uv run scripts/eval_gaia.py --dry-run             # confere tudo, sem gastar
    uv run scripts/eval_gaia.py                       # 20 perguntas do nível 1
    uv run scripts/eval_gaia.py --nivel 2 --limite 10
    uv run scripts/eval_gaia.py --relatorio           # só o boletim da rodada

ROTA PROD: ``127.0.0.1:8000`` é o túnel para a VPS. Cada pergunta é um turno
real, que gasta a MESMA chave do OpenRouter do Neo. O script confere a folga
da chave antes de cada pergunta e para abaixo de ``--folga-min``.

ISOLAMENTO: as perguntas vão como um usuário próprio de avaliação
(``EVAL_USER_ID``), não como o Daniel. Algumas perguntas do GAIA falam em
primeira pessoa ("I'm making a grocery list for my mom…"), e o PKS aprenderia
isso como fato sobre ele; a recuperação semântica traria as sessões do teste
para conversas dele. Com outro dono, nada disso cruza. Efeito colateral: esse
usuário não tem aparelho pareado, então o navegador local (Edge do Mac) não
entra — o teste mede web_search/web_fetch/neo_bash/deep_read do servidor.
O ``/workspace`` do sandbox é compartilhado entre sessões.

Funciona enquanto ``NEO_API_AUTH_MODE=shadow``. No ``enforce``, o
``owner_of`` recusa o ``user_id`` declarado (403): aí é preciso um token.

DADOS: o GAIA é um dataset FECHADO no Hugging Face. Aceite os termos em
https://huggingface.co/datasets/gaia-benchmark/GAIA e rode ``hf auth login``
(ou exporte ``HF_TOKEN``). O dataset fica no cache do HF (~/.cache), fora do
Mutagen; os resultados vão para ``~/Documents/neo-gaia/``, também fora. Os
termos pedem para não republicar perguntas/respostas.
"""

from __future__ import annotations

import argparse
import json
import re
import string
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

NEO_ROOT = Path(__file__).resolve().parents[2]  # projects/neo (o .env da raiz)
API = "http://127.0.0.1:8000/api/v1"
EVAL_USER_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "neo-eval/gaia"))
SAIDA_PADRAO = Path.home() / "Documents" / "neo-gaia"

# O prefixo oficial do GAIA, adaptado: o Neo responde em português, então o
# formato vai explícito — a régua tira vírgula de número ("3,5" vira 35).
PROMPT = """[Teste GAIA — avaliação automática, ninguém acompanha este chat]
Resolva a pergunta abaixo. Pode pesquisar, abrir páginas, rodar código e ler o anexo, como num pedido normal. Não me faça perguntas: ninguém vai responder. Se faltar algo, decida e siga.

Ao terminar, a ÚLTIMA linha da resposta tem que ser:
FINAL ANSWER: <resposta>

Formato da resposta: número com ponto decimal, sem separador de milhar e sem unidade (salvo se a pergunta pedir); texto o mais curto possível, sem artigo nem abreviação, no idioma da pergunta; lista separada por vírgula, com as mesmas regras em cada item. Se não conseguir descobrir, escreva FINAL ANSWER: não sei

Pergunta:
{pergunta}"""

ANEXO = "\n\nO arquivo da pergunta está anexado: {nome}"

RE_FINAL = re.compile(r"(?:FINAL ANSWER|RESPOSTA FINAL)\s*[:：]\s*(.+)", re.IGNORECASE)
NAO_SEI = {"não sei", "nao sei", "i don't know", "unknown", "none"}

VERDE, VERMELHO, AMARELO, CINZA, NEGRITO, FIM = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


# ── Régua oficial do GAIA (scorer.py do leaderboard) ─────────────────────────


def _numero(s: str) -> float:
    for c in ("$", "%", ","):
        s = s.replace(c, "")
    try:
        return float(s)
    except ValueError:
        return float("inf")


def _texto(s: str, tirar_pontuacao: bool = True) -> str:
    s = re.sub(r"\s", "", s).lower()
    return s.translate(str.maketrans("", "", string.punctuation)) if tirar_pontuacao else s


def _eh_numero(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def corrigir(resposta: str | None, gabarito: str) -> bool:
    resposta = "None" if resposta is None else resposta
    if _eh_numero(gabarito):
        return _numero(resposta) == float(gabarito)
    if any(c in gabarito for c in ",;"):
        g = re.split(r"[,;]", gabarito)
        r = re.split(r"[,;]", resposta)
        if len(g) != len(r):
            return False
        return all(
            _numero(ri) == float(gi) if _eh_numero(gi) else _texto(ri, False) == _texto(gi, False)
            for ri, gi in zip(r, g)
        )
    return _texto(resposta) == _texto(gabarito)


def extrair_final(textos: list[str]) -> str | None:
    """Última linha ``FINAL ANSWER:`` de todas as bolhas do assistente."""
    achada = None
    for t in textos:
        for m in RE_FINAL.finditer(t or ""):
            achada = m.group(1)
    if achada is None:
        return None
    # "**Paris**." → "Paris": o ponto final no meio do negrito travava a limpeza.
    anterior = None
    while achada != anterior:
        anterior = achada
        achada = achada.strip().strip("*`_\"'")
        if achada.endswith(".") and not _eh_numero(achada):
            achada = achada[:-1]
    return achada


# ── Dataset ──────────────────────────────────────────────────────────────────


@dataclass
class Tarefa:
    task_id: str
    nivel: int
    pergunta: str
    gabarito: str
    arquivo: Path | None


def carregar_gaia() -> list[Tarefa]:
    import pyarrow.parquet as pq
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import GatedRepoError, HfHubHTTPError

    try:
        raiz = Path(snapshot_download(
            repo_id="gaia-benchmark/GAIA", repo_type="dataset",
            allow_patterns=["2023/validation/*"],
        ))
    except (GatedRepoError, HfHubHTTPError) as exc:
        sys.exit(
            "Não consegui baixar o GAIA (dataset fechado). Aceite os termos em "
            "https://huggingface.co/datasets/gaia-benchmark/GAIA e rode `hf auth login` "
            f"ou exporte HF_TOKEN.\n{CINZA}{exc}{FIM}"
        )
    pasta = raiz / "2023" / "validation"
    tarefas = []
    for linha in pq.read_table(pasta / "metadata.parquet").to_pylist():
        nome = (linha.get("file_name") or "").strip()
        tarefas.append(Tarefa(
            task_id=linha["task_id"],
            nivel=int(linha["Level"]),
            pergunta=linha["Question"],
            gabarito=str(linha["Final answer"]),
            arquivo=(pasta / Path(nome).name) if nome else None,
        ))
    return sorted(tarefas, key=lambda t: t.task_id)


# ── Folga da chave (OpenRouter) ──────────────────────────────────────────────


def _chave_openrouter() -> str | None:
    env = NEO_ROOT / ".env"  # o container lê o da RAIZ
    if not env.exists():
        return None
    for linha in env.read_text().splitlines():
        if linha.startswith("OPENROUTER_API_KEY="):
            return linha.split("=", 1)[1].strip().strip("\"'") or None
    return None


def folga_da_chave() -> tuple[float | None, float | None]:
    """(limit_remaining, usage) em US$; None quando não dá para saber."""
    chave = _chave_openrouter()
    if not chave:
        return None, None
    try:
        r = httpx.get("https://openrouter.ai/api/v1/key",
                      headers={"Authorization": f"Bearer {chave}"}, timeout=15)
        d = r.json().get("data", {})
        return d.get("limit_remaining"), d.get("usage")
    except Exception:
        return None, None


# ── Uma pergunta pelo chat ───────────────────────────────────────────────────


class Neo:
    def __init__(self, provider: str | None, modelo: str | None) -> None:
        self.http = httpx.Client(base_url=API, timeout=httpx.Timeout(30, read=900))
        self.provider, self.modelo = provider, modelo

    def anexar(self, arquivo: Path, session_id: str) -> str:
        with arquivo.open("rb") as f:
            r = self.http.post("/files", files={"file": (arquivo.name, f)}, data={
                "retention": "temp", "origin": "eval-gaia",
                "session_id": session_id, "user_id": EVAL_USER_ID,
            })
        r.raise_for_status()
        return r.json()["id"]

    def _ler_stream(self, resp: httpx.Response, registro: dict[str, Any]) -> list[dict]:
        pendentes = []
        for linha in resp.iter_lines():
            if not linha.strip():
                continue
            ev = json.loads(linha)
            tipo, dado = ev.get("type"), ev.get("data") or {}
            registro["eventos"][tipo] = registro["eventos"].get(tipo, 0) + 1
            if tipo in ("assistant_message", "complete"):
                registro["bolhas"].append(dado.get("content") or dado.get("message") or "")
            elif tipo == "command_request":
                pendentes.append(dado)
            elif tipo == "error":
                registro["erro"] = dado.get("message")
            elif tipo == "thinking" and dado.get("content"):
                print(f"   {CINZA}· {dado['content'][:110]}{FIM}", flush=True)
        return pendentes

    def perguntar(self, texto: str, session_id: str, anexos: list[str]) -> dict[str, Any]:
        registro: dict[str, Any] = {"bolhas": [], "eventos": {}, "erro": None}
        corpo: dict[str, Any] = {
            "user_message": texto, "session_id": session_id, "user_id": EVAL_USER_ID,
        }
        if anexos:
            corpo["attachments"] = anexos
        if self.provider:
            corpo["provider"] = self.provider
        if self.modelo:
            corpo["model"] = self.modelo
        with self.http.stream("POST", "/desktop/chat/stream", json=corpo) as resp:
            resp.raise_for_status()
            pendentes = self._ler_stream(resp, registro)
        # Comando no computador dele: pelo painel não roda (não há corpo remoto).
        # Recusa e deixa o turno seguir, em vez de travar a pergunta.
        for _ in range(3):
            if not pendentes:
                break
            cr = pendentes.pop(0)
            registro.setdefault("comandos_recusados", []).append(cr.get("command", "")[:200])
            with self.http.stream("POST", "/desktop/chat/command-response", json={
                "session_id": session_id, "command_id": cr.get("id", ""),
                "command": cr.get("command", ""), "approved": False,
                "user_message": "Neste teste não há computador: siga sem esse comando.",
            }) as resp:
                pendentes += self._ler_stream(resp, registro)
        return registro

    def historico(self, session_id: str) -> list[str]:
        r = self.http.get(f"/desktop/session/{session_id}/history", params={"limit": 300})
        if r.status_code != 200:
            return []
        msgs = r.json().get("messages") or []
        return [m.get("content") or "" for m in msgs if m.get("role") == "assistant"]


def rodar_uma(neo: Neo, t: Tarefa, teto_s: int) -> dict[str, Any]:
    session_id = str(uuid.uuid4())
    inicio = time.monotonic()
    texto = PROMPT.format(pergunta=t.pergunta)
    anexos = []
    if t.arquivo:
        anexos.append(neo.anexar(t.arquivo, session_id))
        texto += ANEXO.format(nome=t.arquivo.name)

    registro = neo.perguntar(texto, session_id, anexos)
    final = extrair_final(registro["bolhas"])
    # Sem FINAL ANSWER no stream: o Neo pode ter virado o trabalho num job
    # (deep_read, neo_bash longo) que entrega depois, numa bolha nova.
    esperou = False
    while final is None and time.monotonic() - inicio < teto_s:
        esperou = True
        time.sleep(20)
        final = extrair_final(neo.historico(session_id))
    if final is None:
        final = extrair_final(neo.historico(session_id))

    return {
        "task_id": t.task_id, "nivel": t.nivel, "session_id": session_id,
        "resposta": final, "gabarito": t.gabarito,
        "acertou": final is not None and corrigir(final, t.gabarito),
        "nao_sei": final is not None and final.strip().lower() in NAO_SEI,
        "sem_final": final is None, "esperou_job": esperou,
        "tem_anexo": t.arquivo is not None,
        "segundos": round(time.monotonic() - inicio, 1),
        "eventos": registro["eventos"], "erro": registro["erro"],
        "comandos_recusados": registro.get("comandos_recusados", []),
        "quando": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ── Boletim ──────────────────────────────────────────────────────────────────


def ler_resultados(arq: Path) -> list[dict[str, Any]]:
    if not arq.exists():
        return []
    return [json.loads(l) for l in arq.read_text().splitlines() if l.strip()]


def boletim(resultados: list[dict[str, Any]], arq: Path) -> None:
    if not resultados:
        print("Nenhum resultado ainda.")
        return
    n = len(resultados)
    ac = sum(r["acertou"] for r in resultados)
    ns = sum(r["nao_sei"] for r in resultados)
    sf = sum(r["sem_final"] for r in resultados)
    errado = n - ac - ns - sf
    print(f"\n{NEGRITO}Boletim GAIA · {arq.name}{FIM}")
    print(f"  acertos        {ac}/{n}  ({100 * ac / n:.0f}%)")
    print(f"  errou confiante {errado}   ← o número que mais importa baixar")
    print(f"  disse não sei   {ns}")
    print(f"  sem resposta    {sf}   (estourou o teto, travou ou não fechou com FINAL ANSWER)")
    for nivel in sorted({r["nivel"] for r in resultados}):
        rs = [r for r in resultados if r["nivel"] == nivel]
        print(f"  nível {nivel}: {sum(r['acertou'] for r in rs)}/{len(rs)}")
    com = [r for r in resultados if r["tem_anexo"]]
    if com:
        print(f"  com anexo: {sum(r['acertou'] for r in com)}/{len(com)}")
    tempos = sorted(r["segundos"] for r in resultados)
    print(f"  tempo mediano  {tempos[len(tempos) // 2]:.0f}s · máx {tempos[-1]:.0f}s")
    custo = [r.get("custo_usd") for r in resultados if r.get("custo_usd") is not None]
    if custo:
        print(f"  custo ≈ US$ {sum(custo):.3f} (contador da chave atrasa; ordem de grandeza)")
    print(f"\n  {CINZA}Para conferir um caso: grep a session_id no diário da sessão.{FIM}")


# ── main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nivel", type=int, choices=(1, 2, 3), default=1)
    ap.add_argument("--limite", type=int, default=20, help="perguntas desta rodada (padrão 20)")
    ap.add_argument("--sem-anexo", action="store_true", help="pula as perguntas que trazem arquivo")
    ap.add_argument("--ids", nargs="*", help="só estes task_id")
    ap.add_argument("--rodada", default=f"{date.today().isoformat()}-n{{nivel}}",
                    help="nome do arquivo de resultados; repetir o nome RETOMA a rodada")
    ap.add_argument("--saida", type=Path, default=SAIDA_PADRAO)
    ap.add_argument("--teto-min", type=float, default=15, help="teto por pergunta, com a espera de job")
    ap.add_argument("--folga-min", type=float, default=1.0, help="para se a chave tiver menos que isto (US$)")
    ap.add_argument("--provider")
    ap.add_argument("--modelo")
    ap.add_argument("--dry-run", action="store_true", help="confere dataset, API e chave sem perguntar nada")
    ap.add_argument("--relatorio", action="store_true", help="só imprime o boletim da rodada")
    a = ap.parse_args()

    a.saida.mkdir(parents=True, exist_ok=True)
    arq = a.saida / f"{a.rodada.format(nivel=a.nivel)}.jsonl"
    feitos = ler_resultados(arq)
    if a.relatorio:
        boletim(feitos, arq)
        return 0

    tarefas = carregar_gaia()
    if a.ids:
        escolha = [t for t in tarefas if t.task_id in set(a.ids)]
    else:
        # Ordem fixa por task_id: a MESMA fatia toda semana, para comparar.
        escolha = [t for t in tarefas if t.nivel == a.nivel and not (a.sem_anexo and t.arquivo)]
        escolha = escolha[: a.limite]
    ja = {r["task_id"] for r in feitos}
    fila = [t for t in escolha if t.task_id not in ja]

    folga, uso = folga_da_chave()
    saude = httpx.get(API.replace("/api/v1", "/health"), timeout=8).status_code
    print(f"{NEGRITO}GAIA validação{FIM}: {len(tarefas)} perguntas no total, "
          f"{len(escolha)} escolhidas, {len(fila)} a rodar ({len(ja)} já feitas em {arq})")
    print(f"  neo-core /health: {saude} · usuário de avaliação {EVAL_USER_ID}")
    print(f"  folga da chave: {'?' if folga is None else f'US$ {folga:.2f}'} (para abaixo de US$ {a.folga_min:.2f})")

    if a.dry_run:
        if fila:
            t = fila[0]
            print(f"\n{CINZA}Exemplo do que vai ao chat ({t.task_id}, nível {t.nivel}):{FIM}\n")
            print(PROMPT.format(pergunta=t.pergunta) + (ANEXO.format(nome=t.arquivo.name) if t.arquivo else ""))
        return 0
    if saude != 200:
        print(f"{VERMELHO}neo-core fora do ar (ou túnel caído).{FIM}")
        return 1

    neo = Neo(a.provider, a.modelo)
    for i, t in enumerate(fila, 1):
        folga, uso_antes = folga_da_chave()
        if folga is not None and folga < a.folga_min:
            print(f"{AMARELO}Parei: folga da chave em US$ {folga:.2f}. Retome com o mesmo --rodada.{FIM}")
            break
        anexo = f" · anexo {t.arquivo.suffix}" if t.arquivo else ""
        print(f"\n{NEGRITO}[{i}/{len(fila)}]{FIM} {t.task_id} · nível {t.nivel}{anexo}")
        try:
            r = rodar_uma(neo, t, int(a.teto_min * 60))
        except Exception as exc:  # um turno quebrado não derruba a rodada
            print(f"   {VERMELHO}falhou: {exc}{FIM}")
            continue
        _, uso_depois = folga_da_chave()
        if uso_antes is not None and uso_depois is not None:
            r["custo_usd"] = round(uso_depois - uso_antes, 4)
        with arq.open("a") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        feitos.append(r)
        cor = VERDE if r["acertou"] else (AMARELO if r["nao_sei"] or r["sem_final"] else VERMELHO)
        marca = "✓" if r["acertou"] else ("?" if r["nao_sei"] or r["sem_final"] else "✗")
        print(f"   {cor}{marca} resposta: {r['resposta']!r}  gabarito: {t.gabarito!r}  ({r['segundos']:.0f}s){FIM}")

    boletim(feitos, arq)
    return 0


if __name__ == "__main__":
    sys.exit(main())

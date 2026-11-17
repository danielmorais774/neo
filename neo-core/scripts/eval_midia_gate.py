#!/usr/bin/env python3
"""Mede o rótulo ``midia`` do classificador nas mensagens REAIS que falam de vídeo/áudio.

Por que nas reais: em 12/09 "faz a transcrição do vídeo 'Gravação de Tela…', ele é
um bate-papo em chat" queria OCR. Uma frase sintética não traz esse tipo de
ambiguidade, e o golden set (``tests/router/golden_intents.yaml``) só tem
sintéticas, porque conversa do Daniel não fica no repo. Este script lê a fatia do
Postgres na hora, roda o ``IntentRouter`` de verdade (mesmo prompt, mesmos
vieses) e imprime a tabela para revisão humana. Nada é gravado.

    docker exec neo-core python scripts/eval_midia_gate.py \\
        --provider openrouter --model minimax/minimax-m3.1-flash@nebius

Cada mensagem é uma chamada ao classificador (~4 s, modelo flash): a fatia de
16/11/2026 tinha ~28 mensagens.

O que olhar na saída:
  • ``→ CÉREBRO``: o desvio vai mandar essa mensagem ao cérebro. Falso positivo
    custa pouco (o cérebro é o caminho geral), mas confira o rótulo fala × tela;
  • ``REDE``: a palavra-chave casaria, mas o classificador disse outra coisa.
    É ali que mora o falso negativo.

Exit code 0 = rodou (a decisão é de quem lê); 1 = o provider falhou em algum caso.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: A fatia: menção a vídeo/áudio, gravação ou extensão de mídia (texto sem acento).
_FATIA = (
    r"(v[ií]deo|[aá]udio|grava[cç][aã]o|transcri|transcre|legend|\.srt\M"
    r"|\.(mp4|mov|mkv|webm|m4a|mp3|wav|ogg|opus)\M)"
)
_CONTINUACAO = "[Continuação automática"


def _eval_router():
    """Reaproveita o registry e a detecção de fallback do eval do roteador."""
    spec = importlib.util.spec_from_file_location("eval_router", ROOT / "scripts" / "eval_router.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # O @dataclass do eval_router procura o próprio módulo em sys.modules.
    sys.modules["eval_router"] = mod
    spec.loader.exec_module(mod)
    return mod


async def _mensagens(limite: int) -> list[tuple[str, str]]:
    from sqlalchemy import text

    from db.async_client import AsyncPostgresClient

    db = AsyncPostgresClient()
    async with db.get_session() as session:
        linhas = (
            await session.execute(
                text(
                    "select to_char(created_at, 'YYYY-MM-DD'), content from conversation_message "
                    "where role = 'user' and content ~* :fatia order by created_at limit :limite"
                ),
                {"fatia": _FATIA, "limite": limite},
            )
        ).all()
    return [(d, c) for d, c in linhas if not str(c).startswith(_CONTINUACAO)]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", help="provider do classificador (o de prod: openrouter)")
    parser.add_argument("--model", help="modelo do classificador (o de prod)")
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()

    ev = _eval_router()
    from core.llm.router import LLMRouter
    from core.router import intent_router as router_mod
    from core.router.intent_router import _MIDIA_ANCORA, _TRANSCRICAO_AFORDANCIA, _strip_accents
    from core.transcricao import config as transcricao

    router = router_mod.IntentRouter(llm_router=LLMRouter(), tool_registry=ev.build_registry())
    if args.provider and args.model:
        async def _fixo() -> tuple[str, str]:
            return args.provider, args.model
        router._resolve_classifier = _fixo  # type: ignore[method-assign]

    mensagens = await _mensagens(args.limit)
    print(f"{len(mensagens)} mensagens na fatia | transcrição disponível: {transcricao.disponivel()}\n")
    contagem: dict[str, int] = {}
    falhas = 0
    for data, conteudo in mensagens:
        d = await router.classify(conteudo)
        if ev.came_from_fallback(d):
            falhas += 1
        midia = d.midia or "-"
        contagem[midia] = contagem.get(midia, 0) + 1
        texto = _strip_accents(conteudo.lower())
        rede = bool(_TRANSCRICAO_AFORDANCIA.search(texto) and _MIDIA_ANCORA.search(texto))
        marca = ""
        if "midia=" in (d.explanation or ""):
            marca = "→ CÉREBRO"
        elif rede and midia not in ("fala", "tela"):
            marca = "REDE"
        intent = d.intent.value if hasattr(d.intent, "value") else str(d.intent)
        resumo = re.sub(r"\s+", " ", conteudo)[:95]
        print(f"{data}  {midia:<8} {intent:<12} {marca:<10} {resumo}")

    print("\nRótulos:", ", ".join(f"{k}={v}" for k, v in sorted(contagem.items())))
    if falhas:
        print(f"ATENÇÃO: {falhas} classificação(ões) vieram do fallback (provider falhou) — rode de novo.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

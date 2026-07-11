#!/usr/bin/env python3
"""Evals de tom — Registro × Afeto (F8 do docs/context-register-plan.md).

Roda os cenários de ``tests/tone/golden_scenarios.yaml`` em dois níveis:

  offline (default)  Deriva o registro e compõe as diretivas (registro + afeto
                     clampado) com as funções puras reais. Checa o registro
                     esperado e os invariantes de substring. Sem LLM, sem rede
                     — determinístico, roda em ~1s.

  --live             Além do offline: monta um system prompt real (persona +
                     diretivas), chama o modelo de produção com a mensagem do
                     cenário e avalia a resposta com um LLM-judge de rubrica
                     fixa (houve_piada / formalidade 0-3 / entregou_conteudo /
                     mencionou_registro_ou_humor). Requer providers
                     configurados (.env) — use na VPS ou com chaves locais.

Uso:
    uv run python scripts/eval_tone.py
    uv run python scripts/eval_tone.py --live
    uv run python scripts/eval_tone.py --scenario alegre-em-incidente -v

Exit code 0 = tudo verde; 1 = alguma falha (invariante offline ou judge).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from core.agent.affect import AffectState, AffectStats, AffectTuning, render_directive  # noqa: E402
from core.agent.persona import neo_persona  # noqa: E402
from core.agent.register import PROFILES, Register, derive_register, render_register_directive  # noqa: E402
from shared.config import get_settings  # noqa: E402
from shared.types import IntentType, RoutingDecision  # noqa: E402

SCENARIOS_PATH = ROOT / "tests" / "tone" / "golden_scenarios.yaml"

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

_JUDGE_SYSTEM = """Você avalia o TOM de uma resposta de assistente. Responda SOMENTE com JSON:
{"houve_piada": true|false, "formalidade": 0-3, "entregou_conteudo": true|false, "mencionou_registro_ou_humor": true|false}

- houve_piada: a resposta contém piada, zoeira, ironia ou provocação?
- formalidade: 0=coloquial/brincalhão, 1=direto informal, 2=sóbrio, 3=formal impecável.
- entregou_conteudo: a resposta atende (ou encaminha concretamente) o pedido?
- mencionou_registro_ou_humor: a resposta fala explicitamente do próprio humor,
  estado emocional, "registro" ou instruções internas? (isto é sempre um erro)"""


def _decision(spec: dict) -> RoutingDecision:
    return RoutingDecision(
        intent=IntentType(spec.get("intent", "cognitive")),
        confidence=0.9,
        speech_act=spec.get("speech_act", "chitchat"),
        tipo_tarefa=spec.get("tipo_tarefa", "simples"),
        gravity=spec.get("gravity", "normal"),
        register_hint=spec.get("register_hint"),
    )


def _affect_state(spec: dict, pressure: str | None) -> AffectState:
    state = AffectState(stats=AffectStats(**spec))
    if pressure:
        state.recent = [{
            "t": datetime.now(timezone.utc).isoformat(),
            "trigger": pressure,
            "deltas": {},
            "why": "cenário de eval",
        }]
    return state


def _compose_directives(scenario: dict) -> tuple[Register, str]:
    """(registro derivado, diretiva composta registro+afeto) — só funções puras."""
    decision = _decision(scenario.get("decision") or {})
    register, _reason = derive_register(
        decision,
        user_message=scenario.get("message", ""),
        live=bool(scenario.get("live")),
        skill_register=scenario.get("skill_register"),
    )
    tuning = AffectTuning.from_settings(get_settings())
    state = _affect_state(scenario.get("affect") or {}, scenario.get("pressure"))
    affect_text = render_directive(state, tuning, caps=PROFILES[register])
    composed = render_register_directive(register)
    if affect_text:
        composed += "\n\n" + affect_text
    return register, composed


def _check_offline(scenario: dict, register: Register, composed: str) -> list[str]:
    failures: list[str] = []
    expect = scenario.get("expect") or {}
    want = expect.get("register")
    if want and register.value != want:
        failures.append(f"registro derivado={register.value}, esperado={want}")
    for needle in expect.get("directive_must") or []:
        if needle not in composed:
            failures.append(f"diretiva sem {needle!r}")
    for needle in expect.get("directive_must_not") or []:
        if needle in composed:
            failures.append(f"diretiva contém {needle!r} (proibido)")
    return failures


async def _run_live(scenario: dict, composed: str) -> list[str]:
    """Chama o modelo real + LLM-judge. Falha do judge = lista de motivos."""
    from core.llm.router import LLMRouter
    from shared import Message, MessageRole

    router = LLMRouter()
    if not router.list_available_providers():
        return ["--live: nenhum provider LLM configurado (rode na VPS ou exporte as chaves)"]

    system_prompt = (
        neo_persona("full")
        + "\n\nSempre responda em português brasileiro.\n\n"
        + composed
    )
    provider = router.get_provider()
    response = await provider.complete(
        messages=[
            Message(role=MessageRole.SYSTEM, content=system_prompt),
            Message(role=MessageRole.USER, content=scenario.get("message", "")),
        ],
        temperature=0.7,
        max_tokens=500,
    )
    reply = response.get_content() or ""

    judge_payload = (
        f"Pedido do usuário:\n{scenario.get('message', '')}\n\n"
        f"Resposta do assistente:\n{reply}"
    )
    judge_resp = await provider.complete(
        messages=[
            Message(role=MessageRole.SYSTEM, content=_JUDGE_SYSTEM),
            Message(role=MessageRole.USER, content=judge_payload),
        ],
        temperature=0.0,
        max_tokens=120,
    )
    match = re.search(r"\{.*\}", judge_resp.get_content() or "", re.DOTALL)
    if not match:
        return ["judge não devolveu JSON"]
    verdict = json.loads(match.group(0))

    failures: list[str] = []
    expect = (scenario.get("expect") or {}).get("judge") or {}
    # Invariantes duros — valem para TODO cenário.
    if verdict.get("mencionou_registro_ou_humor"):
        failures.append("resposta mencionou humor/registro explicitamente (invariante duro)")
    if expect.get("entregou_conteudo") and not verdict.get("entregou_conteudo"):
        failures.append("não entregou o conteúdo (invariante duro)")
    # Expectativas de tom do cenário (None = tanto faz).
    want_joke = expect.get("houve_piada")
    if want_joke is not None and bool(verdict.get("houve_piada")) != bool(want_joke):
        failures.append(f"houve_piada={verdict.get('houve_piada')}, esperado={want_joke}")
    fmin, fmax = expect.get("formalidade_min"), expect.get("formalidade_max")
    formality = int(verdict.get("formalidade", 0))
    if fmin is not None and formality < fmin:
        failures.append(f"formalidade={formality} < min={fmin}")
    if fmax is not None and formality > fmax:
        failures.append(f"formalidade={formality} > max={fmax}")
    if failures:
        failures.append(f"resposta avaliada: {reply[:200]!r}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="chama modelo + LLM-judge")
    parser.add_argument("--scenario", help="roda só o cenário com este name")
    parser.add_argument("-v", "--verbose", action="store_true", help="imprime as diretivas compostas")
    args = parser.parse_args()

    data = yaml.safe_load(SCENARIOS_PATH.read_text(encoding="utf-8"))
    scenarios = data.get("scenarios") or []
    if args.scenario:
        scenarios = [s for s in scenarios if s.get("name") == args.scenario]
        if not scenarios:
            print(f"{RED}cenário {args.scenario!r} não existe{RESET}")
            return 1

    total_failures = 0
    for scenario in scenarios:
        name = scenario.get("name", "?")
        register, composed = _compose_directives(scenario)
        failures = _check_offline(scenario, register, composed)
        if args.live and not failures:
            failures = asyncio.run(_run_live(scenario, composed))
        status = f"{GREEN}PASS{RESET}" if not failures else f"{RED}FAIL{RESET}"
        print(f"{status}  {name:<40} → {register.value}")
        for failure in failures:
            print(f"      {RED}✗{RESET} {failure}")
        if args.verbose:
            print(f"{DIM}{composed}{RESET}\n")
        total_failures += len(failures)

    print(f"\n{len(scenarios)} cenário(s), {'tudo verde' if not total_failures else f'{total_failures} falha(s)'}")
    return 1 if total_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

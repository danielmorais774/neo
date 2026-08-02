#!/usr/bin/env python3
"""Eval do classificador de intenção — A/B entre versões do ROUTER_SYSTEM_PROMPT.

O prompt do router vai em TODA mensagem do usuário, num modelo flash barato: é
custo fixo por turno. Podar repetição só vale se a acurácia não cair — este
script mede isso rodando o golden set de ``tests/router/golden_intents.yaml``
pelo ``IntentRouter`` REAL (mesmos guards, mesmo catálogo de tools, mesmo
provider/modelo), uma vez por variante de prompt.

    uv run python scripts/eval_router.py                 # só o prompt atual
    uv run python scripts/eval_router.py --ab            # baseline (v1) vs atual
    uv run python scripts/eval_router.py --ab --repeat 3 # 3 amostras por caso
    uv run python scripts/eval_router.py --model qwen/qwen3.5-flash-02-23 \
        --provider openrouter                            # o que estiver em prod

Casos marcados ``guarded: true`` são interceptados por um guard determinístico
antes do LLM — não custam token e valem para as duas variantes; use ``--only-llm``
para rodar apenas os que realmente exercitam o prompt.

Exit code 0 = a variante atual não regrediu; 1 = caiu abaixo do baseline (no
modo --ab) ou houve falha (fora dele).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

# As chaves de provider moram no .env da RAIZ do monorepo (é o que o container
# lê); o .env do neo-core só tem overrides. Carregue os dois ANTES de importar
# shared.config, que lê o ambiente no import.
load_dotenv(ROOT / ".env")
load_dotenv(ROOT.parent / ".env")

import yaml  # noqa: E402

from core.llm.router import LLMRouter  # noqa: E402
from core.router import intent_router as router_mod  # noqa: E402
from core.tools.registry import ToolRegistry  # noqa: E402

GOLDEN_PATH = ROOT / "tests" / "router" / "golden_intents.yaml"
BASELINE_PATH = ROOT / "tests" / "router" / "prompt_v1.txt"

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


def build_registry() -> ToolRegistry:
    """Catálogo de tools que o router REALMENTE enxerga.

    Só os módulos cujas tools sobrevivem ao filtro de ``_format_tools``
    (server-side + client-side que não pertencem a skill). Os demais
    (``file_``/``web_``/``neo_``/``ha_``/``whatsapp_``...) são podados pelo
    próprio router, então não mudam o prompt.
    """
    from modules.config import CONFIG_TOOLS
    from modules.desktop import DESKTOP_TOOLS
    from modules.devices import DEVICES_TOOLS
    from modules.location import LOCATION_TOOLS
    from modules.pks_search import PKS_SEARCH_TOOLS
    from modules.voice import VOICE_TOOLS
    from modules.weather import WEATHER_TOOLS

    registry = ToolRegistry()
    for tools in (
        DESKTOP_TOOLS, WEATHER_TOOLS, DEVICES_TOOLS, PKS_SEARCH_TOOLS,
        VOICE_TOOLS, CONFIG_TOOLS, LOCATION_TOOLS,
    ):
        registry.register_many(tools)
    return registry


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    got: str
    detail: str = ""
    guarded: bool = False
    errored: bool = False


@dataclass
class VariantResult:
    name: str
    chars: int
    results: list[CaseResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def accuracy(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def errored(self) -> int:
        """Casos em que o provider falhou (chave morta, rate limit, timeout).

        Aí a decisão vem do ``_fallback_classify`` por palavra-chave, que ignora
        o prompt — um A/B cheio disso mede o fallback, não a poda, e "nenhuma
        diferença" vira falso positivo.
        """
        return sum(1 for r in self.results if r.errored)


def came_from_fallback(decision: Any) -> bool:
    """True quando a decisão NÃO veio do prompt.

    Dois caminhos levam ao ``_fallback_classify`` por palavra-chave, e nenhum
    dos dois levanta exceção visível daqui:
      - o provider falhou (chave sem crédito, rate limit) → ``payload["error"]``;
      - o modelo devolveu conteúdo vazio/sem JSON e o reparo também falhou
        (deepseek-v4 gasta o ``max_tokens`` raciocinando em ~1 de 4 chamadas).
    Um A/B contaminado por esses casos mede o fallback, não o prompt.
    """
    if (decision.classifier_payload or {}).get("error"):
        return True
    raw = getattr(decision, "classifier_raw", None)
    if raw is None:  # guard determinístico: nem chamou o LLM, e isso é esperado
        return False
    from core.router.intent_router import IntentRouter

    return not IntentRouter._carries_json(raw)


def check(case: dict[str, Any], decision: Any) -> tuple[bool, str, str]:
    """Compara a decisão com as expectativas do caso. Retorna (ok, got, detail)."""
    from shared import IntentType

    # DIRECT_RESPONSE é alias de COGNITIVE (mesmo value, "cognitive") — o golden
    # set escreve o nome histórico, então compare pelo membro do enum.
    expected_intent = IntentType[case["expect_intent"]]
    intent = decision.intent.value if hasattr(decision.intent, "value") else str(decision.intent)
    got = case["expect_intent"] if decision.intent == expected_intent else intent
    problems: list[str] = []

    if decision.intent != expected_intent:
        problems.append(f"intent={intent} (esperado {case['expect_intent']})")

    if (expected := case.get("expect_tool")) is not None:
        actual = decision.tool_name
        got += f"/{actual}"
        if expected == "null":
            if actual:
                problems.append(f"tool_name={actual} (esperado vazio)")
        elif actual != expected:
            problems.append(f"tool={actual} (esperado {expected})")

    if (expected := case.get("expect_skill")) is not None:
        if decision.skill_name != expected:
            problems.append(f"skill={decision.skill_name} (esperado {expected})")

    if (expected := case.get("expect_speech")) is not None:
        if decision.speech_act != expected:
            problems.append(f"speech_act={decision.speech_act} (esperado {expected})")

    if (expected := case.get("expect_plan")) is not None:
        if bool(decision.needs_plan) != bool(expected):
            problems.append(f"needs_plan={decision.needs_plan} (esperado {expected})")

    if (expected := case.get("expect_memory")) is not None:
        if bool(decision.needs_memory) != bool(expected):
            problems.append(f"needs_memory={decision.needs_memory} (esperado {expected})")

    if (expected := case.get("expect_gravity")) is not None:
        if decision.gravity != expected:
            problems.append(f"gravity={decision.gravity} (esperado {expected})")

    if (expected := case.get("expect_pipeline")) is not None:
        steps = len(decision.pipeline_plan or [])
        if steps != expected:
            problems.append(f"pipeline_plan={steps} steps (esperado {expected})")

    if (expected := case.get("expect_args")):
        args = decision.tool_arguments or {}
        for key, want in expected.items():
            have = str(args.get(key, "")).strip().lower()
            if want.strip().lower() not in have:
                problems.append(f"args.{key}={args.get(key)!r} (esperado ~{want!r})")

    return (not problems), got, "; ".join(problems)


async def run_variant(
    name: str, prompt: str, cases: list[dict[str, Any]], *,
    provider: str | None, model: str | None, repeat: int, verbose: bool,
) -> VariantResult:
    """Roda o golden set inteiro com ``prompt`` instalado no módulo do router."""
    original = router_mod.ROUTER_SYSTEM_PROMPT
    router_mod.ROUTER_SYSTEM_PROMPT = prompt
    result = VariantResult(name=name, chars=len(prompt))
    try:
        router = router_mod.IntentRouter(llm_router=LLMRouter(), tool_registry=build_registry())
        if provider and model:
            async def _pinned() -> tuple[str, str]:
                return provider, model
            router._resolve_classifier = _pinned  # type: ignore[method-assign]

        for case in cases:
            attempts: list[tuple[bool, str, str]] = []
            errored = False
            for _ in range(repeat):
                decision = await router.classify(
                    case["message"], conversation_history=case.get("history"),
                )
                errored = errored or came_from_fallback(decision)
                attempts.append(check(case, decision))
            # Com --repeat, o caso só passa se TODAS as amostras passarem
            # (instabilidade é regressão: o turno do usuário é uma amostra só).
            ok = all(a[0] for a in attempts)
            got = attempts[0][1]
            detail = next((a[2] for a in attempts if not a[0]), "")
            result.results.append(
                CaseResult(case["id"], ok, got, detail, bool(case.get("guarded")), errored)
            )
            mark = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
            if errored:
                mark = f"{YELLOW}!{RESET}"
                detail = f"provider falhou — decisão veio do fallback. {detail}"
            tag = f" {DIM}[guard]{RESET}" if case.get("guarded") else ""
            line = f"  {mark} {case['id']}{tag}"
            if errored:
                line += f" {YELLOW}— {detail}{RESET}"
            elif not ok:
                line += f" {RED}— {detail}{RESET}"
            elif verbose:
                line += f" {DIM}→ {got}{RESET}"
            if not ok or errored or verbose:
                print(line)
    finally:
        router_mod.ROUTER_SYSTEM_PROMPT = original
    return result


def print_summary(variants: list[VariantResult], cases: list[dict[str, Any]]) -> bool:
    """Imprime o resumo. Retorna False quando o run não é confiável."""
    print(f"\n{BOLD}Resumo{RESET}")
    for v in variants:
        pct = 100 * v.accuracy
        color = GREEN if v.accuracy == 1.0 else (YELLOW if v.accuracy >= 0.9 else RED)
        line = (
            f"  {v.name:<10} {color}{v.passed}/{v.total} ({pct:.1f}%){RESET}"
            f"  {DIM}prompt: {v.chars:,} chars ≈ {v.chars // 4:,} tokens{RESET}"
        )
        if v.errored:
            line += f"  {YELLOW}[{v.errored} caso(s) sem LLM]{RESET}"
        print(line)

    if any(v.errored for v in variants):
        worst = max(v.errored for v in variants)
        print(
            f"\n{RED}{BOLD}RUN INVÁLIDO{RESET} — o provider falhou em até {worst} caso(s) "
            f"(chave sem crédito, rate limit ou timeout). Nesses casos a decisão veio do "
            f"fallback por palavra-chave, que NÃO lê o prompt: qualquer 'nenhuma diferença' "
            f"aqui é falso positivo. Conserte o provider (ou troque de modelo) e rode de novo."
        )
        return False

    if len(variants) < 2:
        return True

    base, cand = variants[0], variants[1]
    by_id = {c["id"]: c for c in cases}
    regressions = [
        r.case_id for r in cand.results
        if not r.passed and next(b.passed for b in base.results if b.case_id == r.case_id)
    ]
    fixes = [
        r.case_id for r in cand.results
        if r.passed and not next(b.passed for b in base.results if b.case_id == r.case_id)
    ]

    print(f"\n{BOLD}Diferenças{RESET}")
    if not regressions and not fixes:
        print(f"  {GREEN}nenhuma — as duas variantes decidem igual em todos os casos{RESET}")
    for case_id in regressions:
        print(f"  {RED}REGREDIU{RESET} {case_id} {DIM}— {by_id[case_id].get('note', '')}{RESET}")
    for case_id in fixes:
        print(f"  {GREEN}MELHOROU{RESET} {case_id} {DIM}— {by_id[case_id].get('note', '')}{RESET}")

    saved = base.chars - cand.chars
    if saved:
        print(
            f"\n  {BOLD}Economia:{RESET} {saved:,} chars/mensagem "
            f"(≈ {saved // 4:,} tokens, {100 * saved / base.chars:.1f}% do template)"
        )
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ab", action="store_true", help="compara o baseline (tests/router/prompt_v1.txt) com o prompt atual")
    parser.add_argument("--repeat", type=int, default=1, help="amostras por caso (default 1); o caso só passa se todas passarem")
    parser.add_argument("--provider", help="provider do classificador (default: o configurado)")
    parser.add_argument("--model", help="modelo do classificador (default: o configurado)")
    parser.add_argument(
        "--full-tool-descriptions", action="store_true",
        help="ignora os router_summary e manda a description inteira de cada tool no "
             "catálogo (mede se a versão curta custou acurácia)",
    )
    parser.add_argument("--only", help="roda só os casos cujo id contém esta substring")
    parser.add_argument("--only-llm", action="store_true", help="pula os casos interceptados por guard")
    parser.add_argument("-v", "--verbose", action="store_true", help="mostra também os casos que passaram")
    args = parser.parse_args()

    cases: list[dict[str, Any]] = yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8"))
    if args.only:
        cases = [c for c in cases if args.only in c["id"]]
    if args.only_llm:
        cases = [c for c in cases if not c.get("guarded")]
    if not cases:
        print("nenhum caso selecionado")
        return 1

    print(f"{BOLD}Eval do router{RESET} — {len(cases)} casos, {args.repeat}x cada")
    if args.provider and args.model:
        print(f"{DIM}classificador fixado: {args.provider}/{args.model}{RESET}")

    if args.full_tool_descriptions:
        # Desliga o encurtamento do catálogo devolvendo a description inteira no
        # lugar do router_summary — é o "antes" do corte dos catálogos.
        registry_builder = build_registry

        def _verbose_registry() -> ToolRegistry:
            registry = registry_builder()
            for tool in registry.get_all():
                tool.router_summary = None
            return registry

        globals()["build_registry"] = _verbose_registry
        print(f"{DIM}catálogo de tools: descriptions completas (sem router_summary){RESET}")

    variants: list[VariantResult] = []
    if args.ab:
        if not BASELINE_PATH.exists():
            print(f"{RED}baseline ausente: {BASELINE_PATH}{RESET}")
            return 1
        print(f"\n{BOLD}v1 (baseline){RESET}")
        variants.append(await run_variant(
            "v1", BASELINE_PATH.read_text(encoding="utf-8").rstrip("\n"), cases,
            provider=args.provider, model=args.model, repeat=args.repeat, verbose=args.verbose,
        ))

    print(f"\n{BOLD}atual{RESET}")
    variants.append(await run_variant(
        "atual", router_mod.ROUTER_SYSTEM_PROMPT, cases,
        provider=args.provider, model=args.model, repeat=args.repeat, verbose=args.verbose,
    ))

    trustworthy = print_summary(variants, cases)

    if not trustworthy:
        return 2
    if len(variants) == 2:
        return 0 if variants[1].accuracy >= variants[0].accuracy else 1
    return 0 if variants[0].accuracy == 1.0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

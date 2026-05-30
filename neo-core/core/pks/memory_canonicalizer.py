"""Phase 3 — MemoryCanonicalizer.

Turns the *clean* training content (envelope already removed) into a list of
canonical write operations. Each operation carries a stable, semantic
``rule_key`` (never a hash of raw text) and a clean ``description`` — never the
raw message. The raw message lives only in ``op["source"]``.

The canonicalizer is fully deterministic (no LLM) and maps recognized intents to
a fixed set of destinations:
- ``neo-personality`` (persona traits/identity)
- ``neo-response-rules`` (behavior rules)
- ``neo-operating-procedures`` (when/then procedures)
- ``neo-training-examples`` (good/bad correction examples)
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from core.pks.training_envelope import NEO_AGENT, NEO_WORKSPACE, normalize_text

DEST_PERSONALITY = "neo-personality"
DEST_RESPONSE_RULES = "neo-response-rules"
DEST_PROCEDURES = "neo-operating-procedures"
DEST_TRAINING_EXAMPLES = "neo-training-examples"

# Persona block applied when acid/deboche humor training is detected.
ACID_PERSONA_PROPERTIES: dict[str, Any] = {
    "tone": "confident_sharp_direct",
    "humor": "dry_sarcastic",
    "humor_style": "acidic_when_appropriate",
    "sarcasm_level": 0.7,
    "communication_style": "direct_with_controlled_deboche",
    "traits": [
        "direct",
        "sharp",
        "confident",
        "pragmatic",
        "loyal",
        "efficient",
        "technically_precise",
        "sarcastic_when_appropriate",
        "anti_corporate_tone",
        "low_patience_for_bad_ideas",
    ],
}

_BAD_MARKERS = ("exemplo ruim", "exemplos ruins", "nao aja assim", "exemplo errado")
_GOOD_MARKERS = ("exemplo bom", "exemplos bons", "correto seria", "certo seria")
_QUOTE_RE = re.compile(r"[\"“”'‘’]([^\"“”'‘’]{12,240})[\"“”'‘’]")


class MemoryCanonicalizer:
    """Maps clean training content to canonical write operations."""

    def canonicalize(
        self,
        *,
        clean_content: str,
        source_message_id: str | None,
        user_id: UUID | None,
        source_audit: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        norm = normalize_text(clean_content)
        created_by = str(user_id) if user_id else "daniel"
        source_audit = source_audit or {}
        source = {
            "raw_message": source_audit.get("raw_message", ""),
            "cleaned_training_content": source_audit.get("clean_content", clean_content),
            "removed_envelope_phrases": source_audit.get("removed_envelope_phrases", []),
            "created_by_user": created_by,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_message_id": source_message_id,
        }

        def base_op(**kwargs: Any) -> dict[str, Any]:
            op: dict[str, Any] = {
                "target_agent": NEO_AGENT,
                "target_workspace": NEO_WORKSPACE,
                "source_message_id": source_message_id,
                "created_by_user": created_by,
                "source": source,
                "confidence": 0.96,
            }
            op.update(kwargs)
            return op

        ops: list[dict[str, Any]] = []

        if self._is_random_useless_question_sarcasm(norm):
            examples_good = self._extract_inline_good_examples(clean_content)
            ops.append(
                base_op(
                    destination=DEST_RESPONSE_RULES,
                    kind="rule",
                    semantic_rule_key="answer_random_useless_questions_with_varied_sarcasm",
                    rule_key="answer_random_useless_questions_with_varied_sarcasm",
                    description=(
                        "Quando o usuário fizer uma pergunta aleatória, inútil, boba "
                        "ou claramente despretensiosa, Neo deve abrir com sarcasmo ou "
                        "deboche contextual e em seguida responder de forma útil."
                    ),
                    scope="response_generation",
                    priority="high",
                    trigger=(
                        "Pergunta aleatória/inútil/despretensiosa, especialmente "
                        "perguntas factuais simples sem contexto importante."
                    ),
                    action=(
                        "Responder com uma abertura sarcástica curta, variar a frase "
                        "para não repetir padrão, e então entregar a resposta correta."
                    ),
                    examples_good=examples_good,
                    variation_policy={
                        "avoid_repetition": True,
                        "rotate_opening_style": True,
                        "keep_answer_useful": True,
                    },
                )
            )

        if self._is_identity_name_only(norm):
            ops.append(
                base_op(
                    destination=DEST_PERSONALITY,
                    kind="persona",
                    semantic_rule_key="neo_identity_name_only",
                    rule_key="neo_identity_name_only",
                    description=(
                        "Neo deve se identificar apenas como Neo e evitar descrições como "
                        "agente pessoal ou assistente pessoal."
                    ),
                    scope="identity",
                    priority="high",
                    persona_properties={"self_identifier": "neo_only"},
                )
            )

        if self._is_acid_humor(norm):
            ops.append(
                base_op(
                    destination=DEST_RESPONSE_RULES,
                    kind="rule",
                    semantic_rule_key="use_acid_sarcasm_when_appropriate",
                    rule_key="use_acid_sarcasm_when_appropriate",
                    description=(
                        "Neo pode usar sarcasmo, ironia e deboche contextual quando isso "
                        "combinar com a situação, sem deixar o humor substituir a resposta útil."
                    ),
                    scope="response_generation",
                    priority="high",
                )
            )
            ops.append(
                base_op(
                    destination=DEST_PERSONALITY,
                    kind="persona",
                    semantic_rule_key="neo_persona_acid_humor",
                    rule_key="neo_persona_acid_humor",
                    description=(
                        "Personalidade do Neo: humor ácido e deboche controlado, tom direto e "
                        "anti corporativo quando apropriado."
                    ),
                    scope="identity",
                    priority="high",
                    persona_properties={key: value for key, value in ACID_PERSONA_PROPERTIES.items()},
                )
            )

        if self._is_avoid_unrelated_context(norm):
            ops.append(
                base_op(
                    destination=DEST_RESPONSE_RULES,
                    kind="rule",
                    semantic_rule_key="avoid_unrelated_historical_context",
                    rule_key="avoid_unrelated_historical_context",
                    description=(
                        "Evitar incluir fatos, objetivos, rotinas, treinos, projetos ou histórico "
                        "não relacionado quando o usuário não mencionou o assunto ou quando isso "
                        "não ajuda diretamente."
                    ),
                    scope="response_generation",
                    priority="high",
                )
            )

        if self._is_push_back_bad_ideas(norm):
            ops.append(
                base_op(
                    destination=DEST_RESPONSE_RULES,
                    kind="rule",
                    semantic_rule_key="push_back_against_bad_ideas",
                    rule_key="push_back_against_bad_ideas",
                    description=(
                        "Neo deve discordar diretamente quando o usuário seguir uma solução ruim, "
                        "frágil, perigosa, cara ou desnecessariamente complexa."
                    ),
                    scope="response_generation",
                    priority="high",
                )
            )

        if self._is_training_processing(norm):
            ops.append(
                base_op(
                    destination=DEST_PROCEDURES,
                    kind="procedure",
                    semantic_rule_key="training_envelope_extract_rules_only",
                    rule_key="training_envelope_extract_rules_only",
                    description=(
                        "Ao receber mensagem de treinamento, separar envelope operacional do "
                        "conteúdo real e salvar apenas regras canônicas como comportamento ativo."
                    ),
                    scope="memory_ingestion",
                    priority="high",
                    trigger="Mensagem contém envelope de treino do Neo",
                    action=(
                        "Separar envelope, extrair regras canônicas e não salvar comandos "
                        "operacionais como regra ativa."
                    ),
                )
            )

        for example in self._extract_examples(clean_content):
            lesson_key = self._lesson_key(example)
            ops.append(
                base_op(
                    destination=DEST_TRAINING_EXAMPLES,
                    kind="example",
                    semantic_rule_key=lesson_key,
                    rule_key=lesson_key,
                    description=self._example_description(example),
                    scope="training_example",
                    priority="high",
                    example={
                        "type": "correction_example",
                        "lesson_key": lesson_key,
                        "bad_behavior": example.get("bad", ""),
                        "expected_behavior": example.get("good", ""),
                        "examples_bad": [example["bad"]] if example.get("bad") else [],
                        "examples_good": [example["good"]] if example.get("good") else [],
                        "source_message_id": source_message_id,
                    },
                )
            )

        return ops

    # ── intent detectors ────────────────────────────────────────────────
    @staticmethod
    def _is_identity_name_only(norm: str) -> bool:
        if "chama apenas neo" in norm or "se chamar apenas neo" in norm:
            return True
        if "apenas neo" in norm and "chama" in norm:
            return True
        if "nao" in norm and ("assistente pessoal" in norm or "agente pessoal" in norm):
            return True
        return False

    @staticmethod
    def _is_acid_humor(norm: str) -> bool:
        tokens = (
            "deboche",
            "debochado",
            "debochada",
            "sarcasmo",
            "sarcastico",
            "sarcastica",
            "ironia",
            "ironico",
            "ironica",
            "humor acido",
            "acido",
            "acida",
            "nutella",
            "ia corporativa",
            "corporativa",
            "corporativo",
        )
        if any(token in norm for token in tokens):
            return True
        return "humor" in norm and ("acido" in norm or "deboche" in norm)

    @staticmethod
    def _is_random_useless_question_sarcasm(norm: str) -> bool:
        random_question_cues = (
            "pergunta aleatoria",
            "perguntas aleatorias",
            "pergunta inutil",
            "perguntas inuteis",
            "pergunta boba",
            "perguntas bobas",
            "pergunta besta",
            "perguntas bestas",
            "qual a capital",
            "qual e a capital",
        )
        sarcasm_cues = (
            "sarcasmo",
            "sarcastico",
            "sarcastica",
            "tirando onda",
            "deboche",
            "debochar",
            "zoar",
            "tirar onda",
        )
        variation_cues = (
            "alternar",
            "nao ficar muito repetitivo",
            "nao ficar repetitivo",
            "variar",
            "varie",
        )
        return (
            any(cue in norm for cue in random_question_cues)
            and any(cue in norm for cue in sarcasm_cues)
        ) or (
            "pergunta" in norm
            and "inutil" in norm
            and any(cue in norm for cue in variation_cues)
        )

    @staticmethod
    def _is_avoid_unrelated_context(norm: str) -> bool:
        avoid_cues = (
            "evite",
            "evitar",
            "nao inclua",
            "nao incluir",
            "nao traga",
            "nao trazer",
            "nao mencione",
            "nao fale",
            "nao puxe",
            "sem contexto",
            "nao relacionado",
            "nao solicitado",
        )
        context_tokens = (
            "contexto",
            "fatos",
            "objetivos",
            "rotinas",
            "projetos",
            "historico",
            "historia",
            "treinos",
            "memoria",
        )
        return any(cue in norm for cue in avoid_cues) and any(
            token in norm for token in context_tokens
        )

    @staticmethod
    def _is_push_back_bad_ideas(norm: str) -> bool:
        if "discord" in norm:
            return True
        if ("ideia" in norm or "ideias" in norm) and "ruim" in norm:
            return True
        if "ideias ruins" in norm:
            return True
        if "solucao" in norm and ("ruim" in norm or "fragil" in norm or "perigosa" in norm):
            return True
        if "me corrija" in norm:
            return True
        return "aponte" in norm and "erro" in norm

    @staticmethod
    def _is_training_processing(norm: str) -> bool:
        if "texto bruto" in norm and (
            "regra" in norm or "nao salve" in norm or "nao salvar" in norm
        ):
            return True
        if "regras canonicas" in norm or "regra canonica" in norm:
            return True
        if "extraia regras" in norm or ("extrair" in norm and "regra" in norm):
            return True
        if "separar" in norm and "envelope" in norm:
            return True
        return "envelope" in norm and "treino" in norm

    # ── example extraction ──────────────────────────────────────────────
    @staticmethod
    def _extract_inline_good_examples(text: str) -> list[str]:
        examples: list[str] = []
        for match in _QUOTE_RE.finditer(text):
            candidate = " ".join(match.group(1).split()).strip()
            normalized = normalize_text(candidate)
            if (
                "resposta" in normalized
                or "assunto" in normalized
                or "tempo livre" in normalized
                or "porra" in normalized
                or "pq" in normalized
                or "porque" in normalized
            ):
                examples.append(candidate)
        deduped: list[str] = []
        seen: set[str] = set()
        for example in examples:
            key = normalize_text(example)
            if key and key not in seen:
                deduped.append(example)
                seen.add(key)
        return deduped[:4]

    def _extract_examples(self, text: str) -> list[dict[str, str]]:
        bad: list[str] = []
        good: list[str] = []
        current: str | None = None
        buffer: list[str] = []

        def flush() -> None:
            nonlocal buffer, current
            if current and buffer:
                content = " ".join(buffer).strip()
                content = content.strip("\"'“”‘’").strip()
                if content:
                    (bad if current == "bad" else good).append(content)
            buffer = []

        for raw_line in text.splitlines():
            line = raw_line.strip()
            normalized_line = normalize_text(line)
            matched: str | None = None
            for marker in _BAD_MARKERS:
                if normalized_line.startswith(marker):
                    matched = "bad"
                    break
            if matched is None:
                for marker in _GOOD_MARKERS:
                    if normalized_line.startswith(marker):
                        matched = "good"
                        break
            if matched:
                flush()
                current = matched
                after = line.split(":", 1)[1].strip() if ":" in line else ""
                after = after.strip("\"'“”‘’").strip()
                if after:
                    buffer.append(after)
                continue
            if current:
                if line:
                    buffer.append(line)
                else:
                    flush()
                    current = None
        flush()

        examples: list[dict[str, str]] = []
        for index in range(max(len(bad), len(good))):
            examples.append(
                {
                    "bad": bad[index] if index < len(bad) else "",
                    "good": good[index] if index < len(good) else "",
                }
            )
        return examples

    @staticmethod
    def _lesson_key(example: dict[str, str]) -> str:
        basis = example.get("good") or example.get("bad") or "correction"
        words = normalize_text(basis).split()[:5]
        slug = "_".join(words) if words else "correction"
        return f"example_{slug}"[:80]

    @staticmethod
    def _example_description(example: dict[str, str]) -> str:
        bad = (example.get("bad") or "").strip()
        good = (example.get("good") or "").strip()
        parts = []
        if bad:
            parts.append(f"evitar: {bad[:120]}")
        if good:
            parts.append(f"preferir: {good[:120]}")
        body = "; ".join(parts) if parts else "exemplo de correção"
        return f"Correção de treino — {body}"[:240]

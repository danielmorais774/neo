"""Prompts dos subagentes leitores (docs/deep-read-plan.md §4.2 e §4.4).

Dois papéis, e a separação é o ponto: quem LÊ o texto só extrai evidência
(citação literal + página), e quem ESCREVE a análise do documento nunca vê o
texto — só as evidências que passaram pelo aterramento. Assim a redação não tem
de onde tirar o que o documento não diz.
"""

from __future__ import annotations

import json
import re
from typing import Any

MAP_SYSTEM = (
    "Você é um leitor meticuloso trabalhando para o Neo. Recebe UM trecho de um "
    "documento maior e uma lista numerada de perguntas. Sua tarefa é só esta: achar, "
    "NESTE trecho, o que responde a cada pergunta.\n\n"
    "Regras, sem exceção:\n"
    "1. Só vale o que está ESCRITO no trecho. Nada de conhecimento externo, nada de "
    "suposição sobre outras partes do documento.\n"
    "2. Todo achado traz uma CITAÇÃO LITERAL copiada do trecho, caractere por caractere "
    "(sem corrigir, sem resumir, sem juntar frases de lugares diferentes), com até 300 "
    "caracteres. Ela será conferida contra o texto e descartada se não bater.\n"
    "3. `pagina` é o número do marcador [[página N]] (ou [[slide N]], [[seção N]]) mais "
    "próximo ACIMA da citação.\n"
    "4. `nota`: uma frase SUA explicando por que a citação responde à pergunta.\n"
    "5. Nada relevante para uma pergunta? Não liste nada para ela. Trecho sem nada? "
    "`achados` vazio. Isso é uma resposta válida e esperada. Se a sua `nota` precisaria "
    "dizer que a citação NÃO responde ou é vaga demais, ela não é um achado — não liste.\n"
    "6. Prefira poucos achados fortes a muitos fracos (no máximo 8 por pergunta).\n"
    "7. `temas`: só os assuntos do trecho, em até 15 palavras, sem repetir as perguntas "
    "nem dizer o que falta nele.\n"
    "Responda SOMENTE com um objeto JSON."
)

REDUCE_SYSTEM = (
    "Você é o analista que consolida a leitura de UM documento para o Neo. Você NÃO "
    "vê o documento: recebe as evidências já conferidas (citações literais com página) "
    "que os leitores extraíram dele, parte por parte, e os temas de cada parte.\n\n"
    "Regras:\n"
    "1. Toda afirmação sobre o documento precisa se apoiar nas evidências recebidas. "
    "Cite páginas no formato (p. N). Não use conhecimento externo sobre o autor, o "
    "partido, a empresa ou o assunto.\n"
    "2. Pergunta sem evidência: diga isso. Se a cobertura informada é total, escreva "
    "que não foi encontrado no documento lido por inteiro; se é parcial, que não foi "
    "encontrado nas partes lidas.\n"
    "3. Siga as instruções de julgamento do usuário quando houver (o que conta, o que "
    "não conta). Distinga o que o documento DIZ do que é a sua avaliação.\n"
    "4. `veredito`: até 90 caracteres, direto (ex.: 'Sim — cria imposto sobre grandes "
    "fortunas (p. 14)', 'Não encontrado', 'Parcial — …').\n"
    "5. `analise`: 2 a 6 frases por pergunta. `resumo`: até 6 frases sobre o documento "
    "como um todo, à luz do objetivo.\n"
    "Responda SOMENTE com um objeto JSON."
)


def _questions_block(numbered: list[tuple[int, str]]) -> str:
    # Numeração GLOBAL: numa extensão só as perguntas novas vão para o leitor,
    # e o número tem de continuar sendo o da seção na nota.
    return "\n".join(f"{i}. {q}" for i, q in numbered)


def map_user(
    *,
    goal: str,
    questions: list[tuple[int, str]],
    instructions: str,
    doc_name: str,
    part_n: int,
    part_total: int,
    unit: str,
    first: int | None,
    last: int | None,
    text: str,
) -> str:
    faixa = f"{unit}s {first}–{last}" if first != last else f"{unit} {first}"
    extra = f"Instruções de leitura: {instructions}\n" if instructions else ""
    return (
        f"Objetivo da análise: {goal}\n{extra}"
        f"Perguntas:\n{_questions_block(questions)}\n\n"
        f"Documento: {doc_name} — parte {part_n} de {part_total} ({faixa})\n\n"
        f"<<<TRECHO\n{text}\nTRECHO>>>\n\n"
        'Formato: {"achados": [{"pergunta": 1, "pagina": 12, "citacao": "texto literal", '
        '"nota": "por que responde"}], "temas": "assuntos do trecho, até 15 palavras"}'
    )


def reduce_user(
    *,
    goal: str,
    questions: list[tuple[int, str]],
    instructions: str,
    doc_name: str,
    coverage: str,
    evidence: str,
    topics: str,
) -> str:
    extra = f"Instruções de julgamento: {instructions}\n" if instructions else ""
    return (
        f"Objetivo da análise: {goal}\n{extra}"
        f"Perguntas:\n{_questions_block(questions)}\n\n"
        f"Documento: {doc_name}\nCobertura da leitura: {coverage}\n\n"
        f"Temas por parte:\n{topics or '(sem registro)'}\n\n"
        f"Evidências conferidas, por pergunta:\n{evidence or '(nenhuma evidência para nenhuma pergunta)'}\n\n"
        'Formato: {"resumo": "...", "perguntas": [{"pergunta": 1, "veredito": "...", '
        '"analise": "..."}]}'
    )


def parse_json_object(raw: str) -> dict[str, Any]:
    """JSON do modelo, tolerante a cerca de markdown e a texto em volta."""
    texto = (raw or "").strip()
    if texto.startswith("```"):
        texto = re.sub(r"^```[a-zA-Z]*\n?", "", texto)
        texto = re.sub(r"\n?```$", "", texto.strip())
    try:
        obj = json.loads(texto)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    inicio, fim = texto.find("{"), texto.rfind("}")
    if inicio >= 0 and fim > inicio:
        obj = json.loads(texto[inicio:fim + 1])
        if isinstance(obj, dict):
            return obj
    raise json.JSONDecodeError("sem objeto JSON", texto[:200], 0)


_FINDING_RE = re.compile(r"\{[^{}]*\"citacao\"[^{}]*\}", re.DOTALL)


def salvage_findings(raw: str) -> list[dict[str, Any]]:
    """Recupera os achados COMPLETOS de uma resposta cortada pelo teto de tokens.

    A lista costuma morrer no último item; os anteriores são evidência boa e
    passam pelo mesmo aterramento de qualquer outro.
    """
    achados: list[dict[str, Any]] = []
    for m in _FINDING_RE.finditer(raw or ""):
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            achados.append(obj)
    return achados

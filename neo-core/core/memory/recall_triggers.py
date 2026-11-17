"""Gatilhos determinísticos de recall — "ele está falando de algo dito antes".

Mora aqui, e não no `ai_service`, porque DOIS lugares precisam da mesma
decisão e divergir entre eles reabre o buraco:

* o `ai_service` usa para tornar `conversation_recall` obrigatória no turno
  cognitivo;
* o `intent_router` usa para IMPEDIR que o classificador atalhe um pedido de
  recall em entrega de arquivo — na sessão 7330e40c, "pega a última reanálise
  completa que fizemos" virou `save_generated_file` de um `resumo_fichas.md` de
  OUTRA conversa, com confiança 0,95, e o cérebro (o único que sabe buscar no
  arquivo de conversas) nunca chegou a rodar.

Medido contra o corpus real do usuário (2.053 mensagens): 9/9 das frases da
sessão 7330e40c passam a casar, com 2,9% de disparo no corpus — ver
docs/context-recall-fixes.md §4.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "is_conversation_recall_request",
    "is_recall_continuation",
    "normalize_for_match",
]


def normalize_for_match(text: str) -> str:
    """Minúsculas, sem acento, espaços colapsados — para casar as regexes."""
    normalized = "".join(
        c for c in unicodedata.normalize("NFD", (text or "").lower())
        if unicodedata.category(c) != "Mn"
    )
    return " ".join(normalized.split())


# Perguntas sobre o ARQUIVO de conversas ("a gente já falou disso?", "você
# lembra quando eu disse...") não podem ser respondidas de cabeça: o cognitivo
# recebe uma JANELA dos turnos recentes, não a conversa inteira, então negar é
# chutar. Foi isso na session-860d0fb1 ("não tem nada sobre a França
# registrado", com 61 mensagens sobre França no banco) e de novo, com a outra
# metade do problema, na 7330e40c. Ver docs/conversation-recall-plan.md e
# docs/context-recall-fixes.md.
_CONVERSATION_RECALL_PATTERNS = [
    re.compile(p)
    for p in (
        r"\b(voce|vc|ce|tu)\s+(se\s+)?lembra\b",
        r"\bse\s+lembra\s+(de|que|quando|do|da)\b",
        r"\b(a\s+gente|nos|noix)\s+(ja\s+)?(conversou|conversamos|falou|falamos|discutiu|discutimos)\b",
        r"\beu\s+(ja\s+)?(te\s+)?(falei|disse|contei|mostrei|comentei)\b",
        r"\b(conversei|falei|comentei)\s+(algo|alguma\s+coisa|sobre)?\s*(contigo|com\s+voce|com\s+vc)\b",
        r"\b(o\s+que|oque)\s+eu\s+(te\s+)?(falei|disse|contei|comentei)\b",
        r"\bja\s+(te\s+)?(falei|disse|contei|comentei|mostrei)\b",
        r"\b(quando|onde)\s+(foi\s+que\s+)?(eu|a\s+gente|nos)\s+(falou|falamos|conversou|conversamos|disse)\b",
        r"\b(lembra|recorda)\s+(daquela|daquele|da|do|de)\s+(conversa|papo|assunto|vez)\b",
        r"\bnossas?\s+conversas?\s+(anteriores|passadas|antigas)\b",
        r"\bja\s+(conversamos|falamos|discutimos)\s+(sobre|disso|nisso)\b",
        # ── Recall do lado do ASSISTENTE ──────────────────────────────
        # Os 11 padrões acima só cobrem o que o USUÁRIO disse ("eu te falei",
        # "a gente conversou"). Falta a outra metade, que na prática é a mais
        # comum: ele pedindo de volta algo que o NEO escreveu. Na sessão
        # 7330e40c ele tentou 9 vezes — "vc me falou aqui pelas mensagens",
        # "tem uma que vc fez uma reanálise final e me mostrou", "olha no seu
        # histórico de conversa", "olha a mensagem que vc mesmo escreveu" — e
        # ZERO casaram, então a tool nunca virou obrigatória e o Neo passou a
        # ler arquivos do disco e apresentá-los como a mensagem dele.
        r"\b(voce|vc|ce|tu)\s+(ja\s+)?(me\s+)?(falou|disse|mandou|mostrou|escreveu|postou|montou|fez|gerou|criou|respondeu)\b",
        r"\bolh[ae]\s+(no|na|o|a)\s+(seu|sua)\s+(historico|conversa|mensagem|resposta)\b",
        r"\b(no|na)\s+(seu|sua)\s+(historico|historico\s+de\s+conversa)\b",
        r"\ba\s+(mensagem|resposta|analise|reanalise|tabela|lista|versao)\s+que\s+(voce|vc|ce|tu)\b",
        # "a ÚLTIMA reanálise", "aquela tabela" — o demonstrativo/ordinal já é a
        # referência para trás; exigir um pronome depois ("que você fez") era o
        # que deixava passar "pega a ultima reanalise completa que fizemos".
        r"\b(ultim[ao]|penultim[ao]|aquel[ae]|primeir[ao])\s+(\w+\s+){0,2}(analise|reanalise|tabela|resumo|lista|texto|resposta|versao|comparativo|ranking|relatorio)\b",
        r"\b(analise|reanalise|tabela|resumo|lista|resposta|versao|comparativo|ranking|relatorio)\s+(completa|final|anterior|passada|inicial|de\s+antes)\b",
        # Referência no passado sem verbo de fala: "tinha uma tabela do pior
        # para menor pior". Ancorada num substantivo de artefato para não pegar
        # "tinha uma dúvida".
        r"\b(tinha|havia)\s+(uma?|aquel[ae])\s+(\w+\s+){0,2}(analise|reanalise|tabela|resumo|lista|texto|resposta|versao|comparativo|ranking|relatorio|mensagem|parte|trecho)\b",
        # 1ª pessoa do plural não traz pronome ("que fizemos"); a 3ª traz.
        r"\bque\s+(a\s+gente|nos|noix|voce|vc)\s+(fez|montou|gerou|criou|escreveu|mandou)\b",
        r"\bque\s+(fizemos|montamos|geramos|criamos|escrevemos|vimos|chegamos)\b",
        r"\b(me\s+)?(mostra|manda|repete|traz|coloca|poe)\s+(ela|isso|aquilo|de\s+novo|novamente|outra\s+vez)\b",
        # Verbo + ARTEFATO + "de novo": "monta a tabela aqui de novo para eu
        # ver". O padrão acima exigia o pronome colado ao verbo e deixava isso
        # passar — foi assim que o Neo, sem o texto em mãos, foi buscar a
        # tabela no arquivo do disco e mostrou as notas erradas. A âncora dupla
        # (artefato E "de novo") é o que segura o falso positivo.
        r"\b(monta|montar|remonta|refaz|refazer|reescreve|reescrever|mostra|"
        r"manda|traz|repete|lista|escreve)\s+(a|o|as|os|essa|esse|aquela|"
        r"aquele|minha|meu)?\s*(analise|reanalise|tabela|resumo|lista|texto|"
        r"resposta|versao|comparativo|ranking|relatorio|mensagem)\b"
        r"[^.?!]{0,40}\b(de\s+novo|novamente|outra\s+vez)\b",
        r"\b(olha|ve|veja|procura|busca|acha)\s+(ai\s+)?(de\s+novo|novamente|no\s+historico|na\s+conversa|nas\s+mensagens)\b",
        r"\b(pelas?|nas?)\s+mensagens?\b",
        r"\bpor\s+completo?a?\b",
    )
]

# Continuação de um recall já feito: "não foi isso, foi antes ainda". Frase curta
# e vaga — sem o gatilho, o modelo pede desculpa e reformula em prosa em vez de
# rebuscar mais para trás.
_RECALL_CONTINUATION_PATTERNS = [
    re.compile(p)
    for p in (
        r"\bnao\s+(foi|era)\s+(isso|essa|esse|essas|esses|dessa|nenhum)",
        r"\bfoi\s+(antes|mais\s+antes|antes\s+ainda|antes\s+disso)\b",
        r"\bantes\s+(disso|dessa|desse)\b",
        r"\bmais\s+(antig[oa]|pra\s+tras|para\s+tras|atras|longe)\b",
        r"\b(tem|teve|ha)\s+mais\s+(antig|coisa|conversa)",
        r"\bprocura\s+mais\s+(pra\s+tras|para\s+tras|atras|antes)\b",
        r"\bvai\s+mais\s+(pra\s+tras|para\s+tras|atras|longe)\b",
        r"\boutra\s+(conversa|vez)\b",
        # "não É esse" — o presente do verbo faltava e era exatamente como ele
        # escreveu ("nao é esse que eu queria") ao rejeitar o arquivo errado.
        r"\bnao\s+(e|eh)\s+(isso|essa|esse|essas|esses|dessa|nenhum)\b",
        r"\b(ainda\s+)?nao\s+(acertou|e\s+esse|foi\s+esse|era\s+esse)\b",
        r"\bnao\s+(fez|pegou|achou)\s+(direito|certo)\b",
        r"\b(ta|esta|ficou)\s+igual\s+(a|ao|à)\b",
    )
]


def is_conversation_recall_request(text: str) -> bool:
    """True quando o usuário se refere a algo dito antes na conversa."""
    normalized = normalize_for_match(text)
    return any(p.search(normalized) for p in _CONVERSATION_RECALL_PATTERNS)


def is_recall_continuation(text: str) -> bool:
    """True quando o usuário rejeita a resposta anterior e quer outra busca."""
    normalized = normalize_for_match(text)
    return any(p.search(normalized) for p in _RECALL_CONTINUATION_PATTERNS)

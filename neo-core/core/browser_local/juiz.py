"""O JUIZ de uma grade de avaliação (`julgar`, docs/browser-conversa-plan.md §4.6).

Nasceu num formulário de avaliação de requisitos, mas não é dele (2026-11-14):
os critérios vêm da PÁGINA, a evidência de qualquer aba, e o caso "requisito"
ficou só como exemplo. Linha que pergunta sobre o Daniel volta "PESSOAL" — o
juiz não decide opinião nem dado dele.

Sessões 2bd028fb, 74408e70 e 697e0628: o modelo do passo (barato, sem
raciocínio) marcou "Conforme" em 70 de 70 células, com o raciocínio "o requisito
existe e foi verificado no AppDemo". Ele confunde a funcionalidade EXISTIR com o
requisito estar bem ESCRITO — e anotava evidência que não casava com o RF (o
RF-16 pedia o botão "Iniciar Maratona", que nunca apareceu na tela, e saiu
"CONFIRMADO (inputs por célula)"). O Daniel desconfiou duas vezes pelo painel.

Julgar é outro trabalho, com outro modelo (o do `[navegador.julgamento]` no
`neo.toml`, com raciocínio). Ele recebe:

* o texto da PÁGINA (os critérios como o formulário os define, os enunciados);
* as perguntas em grade, com linhas e colunas exatas;
* a EVIDÊNCIA: o texto atual de cada outra aba e as anotações feitas nelas —
  marcadas como resumo do agente, que pode estar errado;
* o que o Daniel pediu e disse durante a tarefa.

E devolve, por célula, a coluna e uma frase de porquê. O código confere cada
resposta contra a grade real: coluna que não existe não vira clique.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from core.browser_local.grade import Questao, coluna_de, normal

logger = logging.getLogger(__name__)

SISTEMA = """Você julga perguntas em GRADE (linhas × colunas de rádio) numa página, pelo Daniel, que está acompanhando: avaliação, rubrica, checagem de conformidade — o que a página pedir.
Em cada pergunta, para cada LINHA, escolha exatamente UMA das COLUNAS, escrita como está.

Como julgar:
- Os critérios são os que a PÁGINA define (instruções, legenda, nome das linhas e das colunas). Leia antes de julgar; não traga critério de fora.
- Julgue o que cada critério pergunta. Se ele fala do TEXTO do item (clareza, ambiguidade, completude, verificabilidade de um requisito, por exemplo), leia o texto palavra por palavra: a coisa descrita existir NÃO torna o enunciado claro, inequívoco ou completo — procure termo vago ("adequado", "rápido", "etc."), sujeito ou condição faltando, o que acontece no erro, mais de uma leitura possível. Se fala do OBJETO avaliado (o sistema, o produto, o trabalho), julgue pelo que a evidência mostra dele.
- A EVIDÊNCIA das outras abas mostra o que está sendo avaliado. Use só o que ela DESCREVE da tela. As anotações do agente são resumos dele e podem ter conclusões erradas ("CONFIRMADO" não prova nada): se a tela descrita não mostra o que o item diz, é "sem evidência" — nunca afirme que algo foi visto.
- Julgue cada célula pelo seu critério, sem copiar a resposta da linha de cima.
- Linha que pergunta sobre o DANIEL — opinião, gosto, satisfação, experiência, dado pessoal — não é sua: responda "PESSOAL" (não é coluna) e diga no porquê o que precisaria saber dele. Quem responde é ele, ou a memória dele.
- O que o Daniel disse vale acima destas regras.

Para citar o enunciado dentro das frases, use « » ou aspas simples — NUNCA aspas duplas, que quebram o JSON.

Responda SÓ um JSON, sem texto fora dele:
{"perguntas": [{"chave": "<a chave dada>", "linhas": {"<linha>": {"resposta": "<coluna, ou PESSOAL>", "porque": "<uma frase que cita o enunciado>"}}, "evidencia": "<o que a outra aba mostrou que pesou, ou 'sem evidência'>"}]}"""


class JuizIlegivel(RuntimeError):
    """O juiz respondeu duas vezes algo que não é JSON. ``cru`` vai para o diário."""

    def __init__(self, mensagem: str, cru: str) -> None:
        super().__init__(mensagem)
        self.cru = cru


@dataclass
class Julgamento:
    chave: str
    enunciado: str
    #: linha → (coluna, porquê)
    linhas: dict[str, tuple[str, str]] = field(default_factory=dict)
    evidencia: str = ""
    faltaram: list[str] = field(default_factory=list)
    #: linha → porquê: o juiz disse que é sobre o Daniel (opinião, dado pessoal).
    #: Não é veredito nem falta: quem responde é a memória ou ele.
    pessoais: dict[str, str] = field(default_factory=dict)

    def resumo(self) -> str:
        celulas = " · ".join(f"{l}: {c}" for l, (c, _) in self.linhas.items())
        falta = f" (sem veredito: {', '.join(self.faltaram)})" if self.faltaram else ""
        pessoal = f" (PESSOAL, é sobre o Daniel: {', '.join(self.pessoais)})" if self.pessoais else ""
        return f"{self.chave} → {celulas or '—'}{falta}{pessoal}"


@dataclass
class Resultado:
    julgamentos: dict[str, Julgamento] = field(default_factory=dict)
    problemas: list[str] = field(default_factory=list)
    cru: str = ""
    pedido: str = ""
    modelo: str = ""
    segundos: float = 0.0


def montar_pedido(
    questoes: list[tuple[Questao, str]],
    *,
    pagina: str,
    evidencias: list[str],
    instrucoes: str,
) -> str:
    """O texto do usuário para o juiz. ``questoes``: (questão, enunciado inteiro)."""
    partes = []
    if instrucoes.strip():
        partes.append(f"O QUE O DANIEL PEDIU E DISSE DURANTE A TAREFA:\n{instrucoes.strip()}")
    partes.append(f"PÁGINA (texto, em ordem — os critérios e os enunciados estão aqui):\n{pagina.strip() or '(sem texto)'}")
    linhas = []
    for q, enunciado in questoes:
        linhas.append(
            f"- chave {q.chave!r}: «{enunciado}»\n"
            f"  linhas: {' | '.join(l.rotulo for l in q.linhas)}\n"
            f"  colunas: {' | '.join(q.colunas)}"
        )
    partes.append("PERGUNTAS A JULGAR:\n" + "\n".join(linhas))
    partes.append(
        "EVIDÊNCIA DAS OUTRAS ABAS (o que está sendo avaliado, quando há):\n"
        + ("\n\n".join(evidencias) if evidencias else "nenhuma outra aba aberta — julgue só pelo texto")
    )
    return "\n\n".join(partes)


def _json_da_resposta(texto: str) -> dict:
    bruto = (texto or "").strip()
    bruto = re.sub(r"^```(?:json)?\s*|\s*```$", "", bruto)
    inicio, fim = bruto.find("{"), bruto.rfind("}")
    if inicio < 0 or fim <= inicio:
        raise ValueError("o juiz não devolveu JSON")
    return json.loads(bruto[inicio: fim + 1])


def conferir(dados: dict, questoes: list[tuple[Questao, str]]) -> Resultado:
    """Só passa o que casa com a grade real: chave, linha e coluna."""
    out = Resultado()
    por_chave = {normal(q.chave): (q, e) for q, e in questoes}
    for item in dados.get("perguntas") or []:
        if not isinstance(item, dict):
            continue
        achada = por_chave.get(normal(item.get("chave")))
        if achada is None:
            out.problemas.append(f"o juiz respondeu uma pergunta que não pedi: {str(item.get('chave'))[:40]}")
            continue
        q, enunciado = achada
        j = Julgamento(chave=q.chave, enunciado=enunciado, evidencia=str(item.get("evidencia") or "")[:400])
        celulas = item.get("linhas") if isinstance(item.get("linhas"), dict) else {}
        for nome, valor in celulas.items():
            linha = q.linha(str(nome))
            if linha is None:
                continue
            if isinstance(valor, dict):
                resposta, porque = valor.get("resposta"), valor.get("porque")
            else:
                resposta, porque = valor, ""
            coluna = coluna_de(q, str(resposta or ""))
            porque = " ".join(str(porque or "").split())[:300]
            if not coluna and normal(resposta) == "pessoal":
                j.pessoais[linha.rotulo] = porque
                continue
            if not coluna:
                out.problemas.append(f"{q.chave}/{linha.rotulo}: «{str(resposta)[:30]}» não é coluna da grade")
                continue
            j.linhas[linha.rotulo] = (coluna, porque)
        j.faltaram = [l.rotulo for l in q.linhas if l.rotulo not in j.linhas and l.rotulo not in j.pessoais]
        # Na ordem da grade, não na do JSON.
        j.linhas = {l.rotulo: j.linhas[l.rotulo] for l in q.linhas if l.rotulo in j.linhas}
        if j.linhas or j.pessoais:
            out.julgamentos[normal(q.chave)] = j
    for q, _ in questoes:
        if normal(q.chave) not in out.julgamentos:
            out.problemas.append(f"{q.chave}: o juiz não respondeu")
    return out


def _provider(llm: Any, modelo: str, nome: str = "") -> Any:
    """O provider do juiz: o do slot do painel; sem ele, pelo catálogo.

    Id com '/' que o catálogo não conhece vai pelo OpenRouter.
    """
    if nome and hasattr(llm, "get_provider"):
        try:
            return llm.get_provider(nome)
        except Exception:  # noqa: BLE001 — provider fora do ar: o catálogo decide
            logger.warning("[juiz] provider '%s' indisponível; resolvendo pelo modelo", nome)
    try:
        from core.llm.catalog import provider_for_model

        conhecido = provider_for_model(modelo)
    except Exception:  # noqa: BLE001
        conhecido = None
    if conhecido is None and "/" in modelo and hasattr(llm, "get_provider"):
        try:
            return llm.get_provider("openrouter")
        except Exception:  # noqa: BLE001 — sem OpenRouter, o default decide
            pass
    return llm.get_provider_for_model(modelo)


async def julgar(
    llm: Any,
    questoes: list[tuple[Questao, str]],
    *,
    modelo: str,
    provider: str = "",
    pagina: str,
    evidencias: list[str],
    instrucoes: str,
    raciocinio_max: int,
    resposta_max: int,
    timeout_s: float,
) -> Resultado:
    """Uma chamada ao juiz para todas as ``questoes``. Erro sobe como RuntimeError."""
    from shared.types import Message, MessageRole

    pedido = montar_pedido(questoes, pagina=pagina, evidencias=evidencias, instrucoes=instrucoes)
    cliente = _provider(llm, modelo, provider)
    extras: dict[str, Any] = {}
    if raciocinio_max > 0:
        extras["extra_body"] = {"reasoning": {"max_tokens": int(raciocinio_max)}}
    else:
        extras["disable_thinking"] = True
    inicio = asyncio.get_running_loop().time()
    mensagens = [
        Message(role=MessageRole.SYSTEM, content=SISTEMA),
        Message(role=MessageRole.USER, content=pedido),
    ]

    async def chamar() -> str:
        try:
            resposta = await asyncio.wait_for(
                cliente.complete(
                    messages=mensagens,
                    model=modelo,
                    temperature=0.2,
                    max_tokens=int(resposta_max) + int(max(0, raciocinio_max)),
                    **extras,
                ),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"o juiz ({modelo}) não respondeu em {int(timeout_s)} s") from exc
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"o juiz ({modelo}) falhou: {str(exc)[:200]}") from exc
        return resposta.get_content() or ""

    # Job a2242336: 3 de 15 respostas vieram com aspas duplas sem escape dentro
    # do "porque" ("Expecting ',' delimiter") — e cada uma custou um passo do
    # agente e deixou perguntas sem veredito. A segunda chance é aqui dentro.
    cru = await chamar()
    try:
        dados = _json_da_resposta(cru)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.info("[juiz] JSON ilegível (%s); pedindo de novo", exc)
        mensagens += [
            Message(role=MessageRole.ASSISTANT, content=cru[:12000]),
            Message(
                role=MessageRole.USER,
                content=(
                    f"Isso não é um JSON válido ({exc}). Responda de novo SÓ o JSON completo, "
                    "com as mesmas decisões, citando trechos entre « » — nunca aspas duplas "
                    "dentro dos textos."
                ),
            ),
        ]
        cru = await chamar()
        try:
            dados = _json_da_resposta(cru)
        except (ValueError, json.JSONDecodeError) as exc2:
            raise JuizIlegivel(f"a resposta do juiz não deu para ler, duas vezes ({exc2})", cru) from exc2
    out = conferir(dados, questoes)
    out.cru = cru
    out.pedido = pedido
    out.modelo = modelo
    out.segundos = asyncio.get_running_loop().time() - inicio
    return out

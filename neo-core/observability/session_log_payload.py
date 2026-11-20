"""Formato enxuto das mensagens no diário da sessão, e o caminho de volta.

O diário (`session_file_logger`) gravava o prompt INTEIRO duas vezes a cada
rodada do loop: no bloco "⚡ RAW payload" e mensagem por mensagem em
``<details>``. Como o prompt só cresce de uma rodada para a outra, quase tudo
era cópia. Medido em 20/11/2026 em 9 diários do servidor: de 86% a 96% dos
grandes era repetição (o de 26 MB tinha 4,9 MB de conteúdo novo, e os de
tarefa longa, 14 MB para 0,6 MB). Quem pagava era a análise: um grep por um
erro devolvia a mesma linha 20 vezes, uma por rodada seguinte.

O formato agora:

- a 1ª vez que uma mensagem aparece no arquivo, ela vai inteira, com a marca
  ``#sha`` no ``<summary>``;
- repetida, vira uma linha ``- Mensagem [i] — role (N chars) = #sha · …``;
- um system que mudou pouco vai como diff da versão anterior
  (``#sha_nova · diff de #sha_base``), conferido na hora de gravar;
- mensagem curta (menos de ``MIN_DEDUP`` chars) vai sempre inteira e sem marca:
  a referência não sairia menor que ela.

``ler_rodadas()`` lê os dois formatos (o antigo também) e devolve, rodada por
rodada, as mensagens resolvidas. ``payload()`` remonta o JSON que o bloco RAW
mostrava, byte a byte, que é o que o A/B reenvia ao modelo
(``scripts/payload_do_log.py``).

Só biblioteca padrão: o script roda com qualquer python, dentro ou fora do
container.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

#: Mensagem menor que isto vai sempre inteira: a linha de referência teria o
#: mesmo tamanho e ainda obrigaria quem lê a procurar o original.
MIN_DEDUP = 160

#: Diff que não economiza pelo menos metade vira mensagem inteira: ler um diff
#: quase do tamanho do texto é pior que ler o texto.
DIFF_MAX_FRACAO = 0.5

_SHA_LEN = 12


def sha(texto: str) -> str:
    return hashlib.sha1(texto.encode("utf-8", "surrogatepass")).hexdigest()[:_SHA_LEN]


def json_compacto(valor: Any) -> str:
    try:
        return json.dumps(valor, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        return json.dumps(str(valor), ensure_ascii=False)


# ── diff ────────────────────────────────────────────────────────────────


def fazer_diff(base: str, novo: str) -> str | None:
    """Diff unificado de ``base`` para ``novo``, ou None se não compensar.

    Por linha (``split("\\n")``, sem as quebras): assim toda linha do diff
    começa com ``' '``, ``'-'``, ``'+'`` ou ``'@'`` e nenhuma pode fechar o bloco
    de código do markdown. Só devolve o diff se ``aplicar_diff`` reconstruir
    ``novo`` exatamente: a garantia de que o diário remonta o prompt é medida
    aqui, na gravação, e não na hora de precisar dele.
    """
    linhas = list(
        difflib.unified_diff(base.split("\n"), novo.split("\n"), lineterm="", n=1)
    )[2:]  # tira os cabeçalhos ---/+++
    if not linhas:
        return None
    diff = "\n".join(linhas)
    if len(diff) > len(novo) * DIFF_MAX_FRACAO:
        return None
    try:
        if aplicar_diff(base, diff) != novo:
            return None
    except Exception:
        return None
    return diff


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def aplicar_diff(base: str, diff: str) -> str:
    """Inverso de ``fazer_diff``. Levanta ValueError se o diff não casar com a base."""
    velhas = base.split("\n")
    saida: list[str] = []
    pos = 0  # próxima linha de ``velhas`` ainda não copiada
    for linha in diff.split("\n"):
        m = _HUNK.match(linha)
        if m:
            inicio = int(m.group(1))
            qtd = 1 if m.group(2) is None else int(m.group(2))
            # Hunk que só insere (qtd 0) aponta para a linha ANTES da inserção.
            alvo = inicio if qtd == 0 else inicio - 1
            if alvo < pos:
                raise ValueError("hunks fora de ordem")
            saida.extend(velhas[pos:alvo])
            pos = alvo
            continue
        if not linha:
            raise ValueError("linha de diff sem prefixo")
        op, resto = linha[0], linha[1:]
        if op == " ":
            if pos >= len(velhas) or velhas[pos] != resto:
                raise ValueError(f"contexto não casa na linha {pos + 1}")
            saida.append(resto)
            pos += 1
        elif op == "-":
            if pos >= len(velhas) or velhas[pos] != resto:
                raise ValueError(f"remoção não casa na linha {pos + 1}")
            pos += 1
        elif op == "+":
            saida.append(resto)
        else:
            raise ValueError(f"prefixo desconhecido: {op!r}")
    saida.extend(velhas[pos:])
    return "\n".join(saida)


# ── escrita ─────────────────────────────────────────────────────────────


def bloco_inteiro(i: int, role: str, content: str, marca: str = "") -> str:
    sufixo = f" #{marca}" if marca else ""
    return (
        f"<details>\n<summary>Mensagem [{i}] — {role} ({len(content)} chars){sufixo}</summary>\n\n"
        f"```\n{content}\n```\n\n</details>\n\n"
    )


def bloco_diff(i: int, role: str, content: str, marca: str, base: str, diff: str, onde_base: str) -> str:
    return (
        f"<details>\n<summary>Mensagem [{i}] — {role} ({len(content)} chars) #{marca} · "
        f"diff de #{base} ({onde_base})</summary>\n\n"
        f"```diff\n{diff}\n```\n\n</details>\n\n"
    )


def linha_referencia(i: int, role: str, content: str, marca: str, onde: str) -> str:
    return f"- Mensagem [{i}] — {role} ({len(content)} chars) = #{marca} · igual à {onde}\n\n"


# ── leitura ─────────────────────────────────────────────────────────────


@dataclass
class Rodada:
    interacao: int
    numero: int  # 1ª, 2ª… chamada de LLM dentro da interação
    estagio: str
    model: str = ""
    temperature: Any = None
    tools: list[str] = field(default_factory=list)
    tool_choice: Any = None
    total_declarado: int | None = None
    mensagens: list[dict[str, str]] = field(default_factory=list)
    #: Payload que o formato antigo gravava junto (só nos diários antigos).
    raw_antigo: str | None = None


class DiarioInvalido(ValueError):
    """O diário não remonta: marca sem original, diff que não casa, índice fora de ordem."""


_INTERACAO = re.compile(r"^## Interação (\d+)$")
_REQUEST = re.compile(r"^### 6\. LLM Request — (.*?)(?: \(rodada \d+\))?$")
_SUMMARY = re.compile(
    r"^Mensagem \[(\d+)\] — (\S+) \((\d+) chars\)"
    r"(?: #([0-9a-f]+)(?: · diff de #([0-9a-f]+) \(.*\))?)?$"
)
_REF = re.compile(r"^- Mensagem \[(\d+)\] — (\S+) \((\d+) chars\) = #([0-9a-f]+)")
_TOOLS = re.compile(r"^- \*\*Tools na mesa \((\d+)\):\*\* (.*)$")
_FIM_BLOCO = "\n```\n\n</details>\n\n"


def _valor_json(texto: str) -> Any:
    try:
        return json.loads(texto)
    except Exception:
        return texto


def ler_rodadas(texto: str) -> list[Rodada]:
    """Percorre o diário e devolve as chamadas de LLM com as mensagens resolvidas.

    Anda bloco a bloco: um ``<details>`` de mensagem é consumido INTEIRO pelo
    tamanho declarado no summary (``N chars``), então um prompt que contém
    markdown, cercas de código ou até outro diário não confunde a leitura.
    """
    por_sha: dict[str, str] = {}
    tools_por_sha: dict[str, list[str]] = {}
    rodadas: list[Rodada] = []
    atual: Rodada | None = None
    interacao = 0
    n_na_interacao = 0
    pos = 0
    n = len(texto)

    def _fim_da_linha(p: int) -> int:
        q = texto.find("\n", p)
        return n if q < 0 else q

    while pos < n:
        fim = _fim_da_linha(pos)
        linha = texto[pos:fim]

        if linha == "<details>" and texto.startswith("<summary>", fim + 1):
            s_ini = fim + 1 + len("<summary>")
            s_fim = texto.find("</summary>\n\n", s_ini)
            summary = texto[s_ini:s_fim]
            corpo_ini = s_fim + len("</summary>\n\n")
            m = _SUMMARY.match(summary)
            if m and _cabe(atual) and texto.startswith("```", corpo_ini):
                idx, role, tamanho = int(m.group(1)), m.group(2), int(m.group(3))
                marca, base = m.group(4), m.group(5)
                abre = texto.find("\n", corpo_ini) + 1
                if base:
                    fecha = texto.find(_FIM_BLOCO, abre)
                    diff = texto[abre:fecha]
                    if base not in por_sha:
                        raise DiarioInvalido(f"diff de #{base} sem o original antes dele")
                    try:
                        content = aplicar_diff(por_sha[base], diff)
                    except ValueError as e:
                        raise DiarioInvalido(f"diff #{marca} não casa com #{base}: {e}") from e
                else:
                    fecha = abre + tamanho
                    content = texto[abre:fecha]
                    if not texto.startswith(_FIM_BLOCO, fecha):
                        raise DiarioInvalido(
                            f"mensagem [{idx}] da rodada {atual.numero} não termina onde o tamanho diz"
                        )
                if marca:
                    if sha(content) != marca:
                        raise DiarioInvalido(f"conteúdo de #{marca} não bate com a marca")
                    por_sha[marca] = content
                _anexar(atual, idx, role, content)
                pos = fecha + len(_FIM_BLOCO)
                continue
            if summary.startswith("⚡ RAW payload") and atual is not None:
                abre = texto.find("\n", corpo_ini) + 1
                fecha = texto.find(_FIM_BLOCO, abre)
                atual.raw_antigo = texto[abre:fecha]
                pos = fecha + len(_FIM_BLOCO)
                continue

        m = _INTERACAO.match(linha)
        if m:
            interacao = int(m.group(1))
            n_na_interacao = 0
            atual = None
        elif (m := _REQUEST.match(linha)):
            n_na_interacao += 1
            atual = Rodada(interacao=interacao, numero=n_na_interacao, estagio=m.group(1))
            rodadas.append(atual)
        elif atual is not None and not atual.mensagens:
            # Cabeçalho da rodada (vem antes da 1ª mensagem).
            if linha.startswith("- **Model:** "):
                atual.model = linha[len("- **Model:** "):]
            elif linha.startswith("- **Temperature:** "):
                atual.temperature = _valor_json(linha[len("- **Temperature:** "):])
            elif linha.startswith("- **Total mensagens:** "):
                atual.total_declarado = int(linha[len("- **Total mensagens:** "):])
            elif linha.startswith("- **tool_choice:** `") and linha.endswith("`"):
                atual.tool_choice = _valor_json(linha[len("- **tool_choice:** `"):-1])
            elif (mt := _TOOLS.match(linha)):
                atual.tools = _ler_tools(mt.group(2), tools_por_sha)
        if _cabe(atual) and (mr := _REF.match(linha)):
            idx, role, marca = int(mr.group(1)), mr.group(2), mr.group(4)
            if marca not in por_sha:
                raise DiarioInvalido(f"referência a #{marca} sem o original antes dela")
            _anexar(atual, idx, role, por_sha[marca])
        pos = fim + 1

    for r in rodadas:
        if r.total_declarado is not None and r.total_declarado != len(r.mensagens):
            raise DiarioInvalido(
                f"Interação {r.interacao}, rodada {r.numero}: {len(r.mensagens)} mensagens lidas, "
                f"{r.total_declarado} declaradas"
            )
    return rodadas


def _cabe(rodada: Rodada | None) -> bool:
    """A rodada ainda espera mensagem? Depois da última declarada, nada entra:
    um resultado de tool que cite o formato do diário não vira mensagem."""
    if rodada is None:
        return False
    return rodada.total_declarado is None or len(rodada.mensagens) < rodada.total_declarado


def _anexar(rodada: Rodada, idx: int, role: str, content: str) -> None:
    if idx != len(rodada.mensagens):
        raise DiarioInvalido(
            f"Interação {rodada.interacao}, rodada {rodada.numero}: mensagem [{idx}] fora de ordem"
        )
    rodada.mensagens.append({"role": role, "content": content})


def _ler_tools(resto: str, tools_por_sha: dict[str, list[str]]) -> list[str]:
    if resto.startswith("_nenhuma_"):
        return []
    m = re.match(r"^= #([0-9a-f]+)", resto)
    if m:
        if m.group(1) not in tools_por_sha:
            raise DiarioInvalido(f"tools #{m.group(1)} sem a lista antes")
        return list(tools_por_sha[m.group(1)])
    nomes = re.findall(r"`([^`]*)`", resto)
    m = re.search(r" · #([0-9a-f]+)$", resto)
    if m:
        tools_por_sha[m.group(1)] = nomes
    return nomes


def payload(rodada: Rodada) -> dict[str, Any]:
    """O JSON que o bloco "⚡ RAW payload" mostrava, com as mesmas chaves e ordem."""
    corpo: dict[str, Any] = {"model": rodada.model, "messages": rodada.mensagens}
    if rodada.temperature is not None:
        corpo["temperature"] = rodada.temperature
    corpo["stream"] = True
    if rodada.tools:
        corpo["tools"] = rodada.tools
    if rodada.tool_choice:
        corpo["tool_choice"] = rodada.tool_choice
    return corpo


def payload_texto(rodada: Rodada) -> str:
    """``payload()`` formatado como o diário antigo formatava (indent 2)."""
    return json.dumps(payload(rodada), ensure_ascii=False, indent=2, default=str)

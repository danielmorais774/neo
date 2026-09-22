"""Reconhece quando o browser DO SERVIDOR apanhou de bloqueio, não de bug.

O `neo-browser-agent` roda num container na VPS, e a VPS sai para a internet
por um IP de datacenter (`AS51167 Contabo GmbH`, França). Para uma fatia grande
da web brasileira isso é indistinguível de um robô:

- CDN com WAF (Akamai/EdgeSuite no portal de dados, Cloudflare) devolve 403 para o ASN
  inteiro — User-Agent de Chrome não muda nada, é bloqueio de rede;
- Google e DuckDuckGo servem CAPTCHA para IP de datacenter;
- buscador geolocaliza o IP na Europa e responde noutro idioma/região.

Job 36a81dd6 (2026-09-20, baixar o ZIP de propostas do portal de dados): 22 passos, 51
CAPTCHAs, 43 "Access Denied", 36 respostas 403, e no fim o usuário recebeu
"timeout depois de 570s". O agente não estava quebrado — a internet recusou o
endereço dele. Insistir no servidor é queimar mais 570s pelo mesmo fim.

O que resolve é o navegador DELE: IP residencial, no Brasil. Isso o Neo não faz
sozinho (ver `_gate_browser` — tomar a tela do usuário sem pedir é a falha
grave), então o desfecho certo é PERGUNTAR, com o motivo na mão.
"""

from __future__ import annotations

import re

#: Assinaturas de recusa por origem — não de erro de navegação.
#:
#: Estreito de propósito: "erro"/"falhou"/"não encontrei" NÃO entram. Um site
#: fora do ar, um seletor errado ou uma busca vazia são problemas de tarefa, e
#: sugerir "usa teu navegador" para eles é ruído que ensina o usuário a ignorar
#: a sugestão justamente quando ela importa.
_ASSINATURAS = (
    r"\bcaptcha\b",
    r"\baccess denied\b",
    r"\bacesso negado\b",
    r"\bforbidden\b",
    r"\bedgesuite\b",
    r"\b403\b",
    r"\bcloudflare\b",
    r"just a moment",
    r"unusual traffic",
    r"tráfego incomum",
    r"verify (?:you are|you're) (?:a )?human",
    r"bot detection",
    r"are you a robot",
)
_RE = re.compile("|".join(_ASSINATURAS), re.I)

#: Quantas assinaturas distintas antes de chamar de bloqueio. Uma só pode ser
#: coincidência (a palavra "403" num texto qualquer, um site que só menciona
#: captcha); duas ou mais, num relato de falha, é padrão.
_MIN_DISTINTAS = 2


def sinais_de_bloqueio(*textos: str | None) -> list[str]:
    """Assinaturas distintas encontradas (vazio = não parece bloqueio)."""
    alvo = "\n".join(t for t in textos if t)
    if not alvo:
        return []
    achados: list[str] = []
    for m in _RE.finditer(alvo):
        s = m.group(0).lower()
        if s not in achados:
            achados.append(s)
    return achados


def parece_bloqueio(*textos: str | None) -> bool:
    return len(sinais_de_bloqueio(*textos)) >= _MIN_DISTINTAS


#: O texto que o cérebro recebe. Ele é quem fala com o usuário, então a
#: mensagem é escrita para ser LIDA por um modelo que vai decidir o próximo
#: passo — diz o que houve, por que, e qual é a ação concreta disponível.
AVISO = (
    "\n\n⚠️ Isto não parece bug do navegador e sim BLOQUEIO POR ORIGEM "
    "({sinais}). O navegador do servidor sai por um IP de datacenter fora do "
    "Brasil, e WAF de CDN (Akamai/Cloudflare) e buscadores recusam esse IP — "
    "repetir a mesma tarefa no servidor vai falhar igual. O navegador LOCAL do "
    "usuário (a máquina dele, IP residencial) não tem esse problema. "
    "NÃO abra o navegador dele por conta própria: PERGUNTE se ele quer que "
    "você refaça a tarefa pelo navegador local, dizendo o motivo em uma frase."
)


def anotar_se_bloqueio(resumo: str, *extras: str | None) -> str:
    """Devolve ``resumo`` com o aviso pendurado, quando o padrão bate."""
    sinais = sinais_de_bloqueio(resumo, *extras)
    if len(sinais) < _MIN_DISTINTAS:
        return resumo
    return resumo + AVISO.format(sinais=", ".join(sinais[:4]))

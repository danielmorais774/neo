"""Que tarefa do Claude cada resposta abriu — para o desktop ancorá-la no balão.

A faixa acima do campo de mensagem some quando a tarefa acaba ou quando o
Daniel troca de conversa; o botão no balão da resposta que mandou o pedido é o
jeito de achar o processo de novo (docs/claude-code-delegation-plan.md §13).

A fonte é o ``tool_trace`` da resposta, o mesmo que vai para o
``extra_metadata`` da mensagem: ao vivo (``AgentResponse.jobs``) e no histórico
reaberto (``/desktop/session/{id}/history``) o resultado é o mesmo, e respostas
gravadas antes deste módulo também ganham o botão.
"""

from __future__ import annotations

from typing import Any

#: `claude_reply` entra porque, com a tarefa parada esperando o Daniel, ele cria
#: uma continuação com id NOVO — é o processo que segue a partir desta resposta.
_TOOLS = ("claude_task", "claude_reply")


def tarefas_do_balao(tool_trace: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """``[{"id", "kind"}]`` das tarefas do Claude que o trace abriu, sem repetir."""
    vistos: set[str] = set()
    saida: list[dict[str, str]] = []
    for entrada in tool_trace or []:
        if not isinstance(entrada, dict) or entrada.get("tool") not in _TOOLS:
            continue
        job_id = str(entrada.get("job_id") or "").strip()
        if not job_id or job_id in vistos:
            continue
        vistos.add(job_id)
        saida.append({"id": job_id, "kind": "claude_code"})
    return saida


def tarefas_da_mensagem(metadata: dict[str, Any] | None) -> list[dict[str, str]]:
    """O mesmo, a partir do ``extra_metadata`` de uma mensagem gravada.

    Além das tarefas que a resposta abriu, a ENTREGA do resultado (a mensagem que
    o ProactiveDispatcher grava com ``source="agent_job"``) aponta para a tarefa
    que a produziu — "Terminei o jogo da velha" também leva ao processo.
    """
    meta = metadata or {}
    saida = tarefas_do_balao(meta.get("tool_trace"))
    job_id = str(meta.get("job_id") or "").strip()
    if (
        meta.get("source") == "agent_job"
        and meta.get("kind") == "claude_code"
        and job_id
        and all(t["id"] != job_id for t in saida)
    ):
        saida.append({"id": job_id, "kind": "claude_code"})
    return saida

"""Jobs — trabalho assíncrono do Neo em segundo plano.

O :class:`JobManager` consome a tabela ``agent_job``: reivindica jobs prontos,
roda no sandbox (workbench) sem bloquear o chat, marca o resultado e aciona o
:class:`ProactiveDispatcher` para entregar a resposta de volta na conversa.

Tools ``neo_bash_background`` + ``job_*`` são oferecidas inline ao núcleo
cognitivo (ver ``docs/workbench-background-jobs-plan.md``).
"""

from core.jobs.manager import JobManager
from core.jobs.executor import JobToolExecutor
from core.jobs.dispatcher import ProactiveDispatcher
from core.jobs.tools import JOB_TOOLS, JOB_TOOL_NAMES

__all__ = [
    "JobManager",
    "JobToolExecutor",
    "ProactiveDispatcher",
    "JOB_TOOLS",
    "JOB_TOOL_NAMES",
]

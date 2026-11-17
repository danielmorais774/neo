"""A cascata declarada não pode perder o payload de tool do classificador.

Regressão de 2026-09-16 (sessões 06f237e9 e 93eb1d9b, seis repetições seguidas):
"analisa os relatórios anuais nessa pasta" era classificado CERTO — CLIENT_TOOL
com `raw_command="ls -la /Users/daniel/Documents/analises/..."` — e junto
vinha um `pipeline_plan` de dois steps. O seed da cascata montava uma
`RoutingDecision` nova por step copiando só {intent, confidence, skill_name}, e o
comando morria ali. O step chegava no dispatch com `tool_arguments={}`, virava
`run_command` por default e batia na trava de comando vazio, que encerra o turno
com "Me embananei montando o comando". Como `PipelineStepSpec` não tem campo pra
comando, isso valia pra QUALQUER cascata com step de client tool — não era flaky.
"""

from shared.types import IntentType, PipelineStepSpec, RoutingDecision

_LS = "ls -la /Users/daniel/Documents/analises/relatorios-anuais-2026/"


def _svc(cascade_enabled: bool = True):
    """AIService cru — `_seed_steps` só lê `self.settings`."""
    from modules.desktop.ai_service import AIService

    svc = object.__new__(AIService)

    class _S:
        pipeline_cascade_enabled = cascade_enabled

    svc.settings = _S()
    return svc


def _spec(intent, text, skill=None):
    return PipelineStepSpec(intent=intent, input_text=text, skill_name=skill)


def test_cascade_preserva_o_comando_do_classificador():
    """O caso literal da sessão 93eb1d9b."""
    decision = RoutingDecision(
        intent=IntentType.CLIENT_TOOL,
        tool_name="run_command",
        tool_arguments={},
        raw_command=_LS,
        needs_plan=True,
        pipeline_plan=[
            _spec(IntentType.CLIENT_TOOL, "listar arquivos na pasta ..."),
            _spec(IntentType.COGNITIVE, "ler e analisar os relatórios anuais"),
        ],
    )

    steps = _svc()._seed_steps(decision, "analisa os planos dessa pasta")

    assert len(steps) == 2
    # O step que vai despachar carrega o comando que o classificador acertou.
    assert steps[0].decision.raw_command == _LS
    assert steps[0].decision.tool_name == "run_command"
    # Com comando na mão, o step despacha direto: `needs_plan` aqui faria o
    # handler entrar no planner ANTES de olhar o `raw_command` e o comando
    # seria ignorado de novo.
    assert steps[0].decision.needs_plan is False
    # O segundo step é outra ação — não pode herdar (e rodar) o mesmo comando.
    assert steps[1].decision.raw_command is None


def test_step_de_client_tool_sem_comando_vai_pro_planner_em_vez_do_beco():
    """Segunda metade da sessão 06f237e9: cascata com DOIS steps de client tool.

    O segundo ("ler e analisar o conteúdo dos planos") nunca teve comando — o
    classificador só emite um. Sem `needs_plan`, ele cai na trava de comando
    vazio e mata o pedido inteiro; com ele, o handler tenta o planner e, se o
    planner não devolver passo utilizável, delega ao núcleo cognitivo.
    """
    from modules.desktop.ai_service import _dispatch_would_hang

    decision = RoutingDecision(
        intent=IntentType.CLIENT_TOOL,
        tool_name="run_command",
        raw_command=_LS,
        pipeline_plan=[
            _spec(IntentType.CLIENT_TOOL, "listar arquivos na pasta ..."),
            _spec(IntentType.CLIENT_TOOL, "ler e analisar o conteúdo dos planos"),
        ],
    )

    steps = _svc()._seed_steps(decision, "analisa os planos dessa pasta")

    segundo = steps[1].decision
    # Sem o conserto este step era um dispatch vazio garantido.
    assert _dispatch_would_hang(segundo.tool_name or "run_command", segundo.tool_arguments)
    assert segundo.needs_plan is True
    # ...e o primeiro, que tem comando, segue sem pedir plano.
    assert steps[0].decision.needs_plan is False


def test_payload_vai_pro_step_do_intent_classificado_nao_pro_primeiro():
    decision = RoutingDecision(
        intent=IntentType.CLIENT_TOOL,
        tool_name="run_command",
        raw_command="df -h",
        pipeline_plan=[
            _spec(IntentType.COGNITIVE, "explicar o que é espaço em disco"),
            _spec(IntentType.CLIENT_TOOL, "ver quanto espaço livre tem no disco"),
        ],
    )

    steps = _svc()._seed_steps(decision, "o que é espaço em disco e quanto me sobra?")

    assert steps[0].decision.raw_command is None
    assert steps[1].decision.raw_command == "df -h"


def test_tool_arguments_sobrevivem_a_cascata():
    """Não é só shell: um `config_set` perdia os argumentos do mesmo jeito."""
    decision = RoutingDecision(
        intent=IntentType.SERVER_TOOL,
        tool_name="config_set",
        tool_arguments={"setting": "tts.voice", "value": "jarvis"},
        pipeline_plan=[
            _spec(IntentType.SERVER_TOOL, "mudar a voz para jarvis"),
            _spec(IntentType.MUSIC, "tocar uma música"),
        ],
    )

    steps = _svc()._seed_steps(decision, "usa a voz do jarvis e toca uma música")

    assert steps[0].decision.tool_name == "config_set"
    assert steps[0].decision.tool_arguments == {"setting": "tts.voice", "value": "jarvis"}
    assert steps[1].decision.tool_name is None
    # Cópia, não a mesma referência: um handler que mexe nos argumentos do step
    # (o de `write_file` normaliza `mode`/`destination` in-place) não pode
    # reescrever a decisão do turno.
    assert steps[0].decision.tool_arguments is not decision.tool_arguments


def test_payload_herdado_uma_vez_so():
    decision = RoutingDecision(
        intent=IntentType.CLIENT_TOOL,
        tool_name="run_command",
        raw_command=_LS,
        pipeline_plan=[
            _spec(IntentType.CLIENT_TOOL, "listar a pasta"),
            _spec(IntentType.CLIENT_TOOL, "listar de novo"),
            _spec(IntentType.CLIENT_TOOL, "e mais uma vez"),
        ],
    )

    steps = _svc()._seed_steps(decision, "x")

    assert [s.decision.raw_command for s in steps] == [_LS, None, None]


def test_turno_de_acao_unica_continua_com_a_decisao_original():
    decision = RoutingDecision(
        intent=IntentType.CLIENT_TOOL,
        tool_name="run_command",
        raw_command="df -h",
        needs_plan=True,
    )

    steps = _svc()._seed_steps(decision, "quanto espaço livre tem no disco?")

    assert len(steps) == 1
    assert steps[0].decision is decision
    assert steps[0].decision.needs_plan is True


def test_cascata_desligada_nao_expande():
    decision = RoutingDecision(
        intent=IntentType.CLIENT_TOOL,
        raw_command="df -h",
        pipeline_plan=[
            _spec(IntentType.CLIENT_TOOL, "a"),
            _spec(IntentType.COGNITIVE, "b"),
        ],
    )

    steps = _svc(cascade_enabled=False)._seed_steps(decision, "x")

    assert len(steps) == 1 and steps[0].decision is decision

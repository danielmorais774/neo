"""Grounding guards do classificador CLIENT_TOOL — sessão bd7a304d.

O classificador (LLM single-shot, sem tools, sem chance de verificar nada)
decidiu "relatorio de financas em csv na minha area de trabalho" (usuário
pedindo pra GERAR, sem dizer nome de arquivo) → CLIENT_TOOL read_file com
file_path="/Users/daniel/Desktop/relatorio.csv" — caminho 100% inventado. O
desktop tentou ler o arquivo fantasma e travou ~2min30s até a conexão cair.

Duas causas raiz distintas, dois guards:
1. `read_file` nem existe mais no catálogo (ver modules/desktop/tools.py) — um
   tool_name fora do catálogo real nunca deveria ser despachado pro cliente.
2. Mesmo com o catálogo certo (`run_command`), o classificador pode montar um
   comando (`cat .../relatorio.csv`) citando um arquivo que ninguém mencionou —
   isso também precisa ser pego antes do dispatch.
"""

from core.router.intent_router import IntentRouter
from core.tools.registry import ToolRegistry
from modules.desktop.tools import DESKTOP_TOOLS
from shared import IntentType


def _router() -> IntentRouter:
    """Router com o catálogo real de client tools wireado (run_command +
    save_generated_file), mas sem LLM/DB — só o suficiente pra exercitar
    _parse_classification isoladamente, no mesmo estilo dos outros testes
    deste arquivo (object.__new__ + atributos mínimos)."""
    router = object.__new__(IntentRouter)
    registry = ToolRegistry()
    registry.register_many(DESKTOP_TOOLS)
    router.tool_registry = registry
    return router


def _raw(tool_name, tool_arguments=None, raw_command=None):
    import json

    return json.dumps(
        {
            "intent": "CLIENT_TOOL",
            "confidence": 1.0,
            "tool_name": tool_name,
            "tool_arguments": tool_arguments or {},
            "raw_command": raw_command,
            "explanation": "teste",
            "requires_approval": False,
            "needs_memory": False,
            "knowledge_keywords": [],
            "needs_plan": False,
            "tipo_tarefa": "simples",
            "skill_name": None,
        }
    )


# ── Camada 0: tool_name fora do catálogo real nunca é despachado ──────────


def test_dead_tool_name_downgrades_to_direct_response():
    """read_file foi removido do catálogo (2026-07-24) mas um classificador
    treinado em exemplos antigos ainda pode "lembrar" dele — sessão bd7a304d."""
    router = _router()
    decision = router._parse_classification(
        _raw("read_file", {"file_path": "/Users/daniel/Desktop/relatorio.csv"}),
        user_message="relatorio de financas em csv na minha area de trabalho",
    )
    assert decision.intent == IntentType.DIRECT_RESPONSE
    assert decision.tool_name is None
    assert decision.tool_arguments is None


def test_known_tool_name_is_untouched():
    router = _router()
    decision = router._parse_classification(
        _raw("run_command", raw_command="df -h"),
        user_message="quanto espaço livre eu tenho?",
    )
    assert decision.intent == IntentType.CLIENT_TOOL
    assert decision.tool_name == "run_command"
    assert decision.raw_command == "df -h"


# ── Camada 1: caminho com extensão que não apareceu na conversa ───────────


def test_invented_file_path_forces_verification_plan():
    """O caso literal da sessão bd7a304d, só que via run_command/raw_command
    em vez do read_file morto: usuário nunca disse "relatorio.csv" em lugar
    nenhum — o classificador não pode inventar esse nome.

    Em vez de só desistir pra DIRECT_RESPONSE (que não tem NENHUMA tool pra
    tocar o disco do usuário — só pode perguntar), o guard força needs_plan
    pra o PlanManager fazer "lista primeiro, decide depois" (ver
    core/planning/plan_manager.py generate_plan): Neo verifica o disco de
    verdade em vez de inventar OU de só perguntar."""
    router = _router()
    decision = router._parse_classification(
        _raw("run_command", raw_command="cat /Users/daniel/Desktop/relatorio.csv"),
        user_message="relatorio de financas em csv na minha area de trabalho",
    )
    assert decision.intent == IntentType.CLIENT_TOOL
    assert decision.needs_plan is True
    assert decision.tool_name == "run_command"
    # o comando chutado é descartado — o plano recomeça do zero
    assert decision.tool_arguments is None
    assert decision.raw_command is None


def test_explicit_file_name_from_user_passes_through():
    """Usuário deu o nome do arquivo (mesmo sem o caminho completo) — o
    classificador tem permissão de compor <pasta do contexto> + <nome dito>."""
    router = _router()
    decision = router._parse_classification(
        _raw("run_command", raw_command="cat ~/Downloads/relatorio.pdf"),
        user_message="lê o arquivo relatorio.pdf que tá nos meus downloads",
    )
    assert decision.intent == IntentType.CLIENT_TOOL
    assert decision.raw_command == "cat ~/Downloads/relatorio.pdf"


def test_file_name_grounded_in_history_passes_through():
    """O nome não está na mensagem atual, mas apareceu num turno anterior —
    ainda é fundamentado, não inventado."""
    router = _router()
    decision = router._parse_classification(
        _raw("run_command", raw_command="cat ~/Desktop/extrato_julho.xlsx"),
        user_message="agora envia esse arquivo pra área de trabalho",
        conversation_history=[
            {"role": "assistant", "content": "Salvei como extrato_julho.xlsx no seu catálogo."},
        ],
    )
    assert decision.intent == IntentType.CLIENT_TOOL
    assert decision.raw_command == "cat ~/Desktop/extrato_julho.xlsx"


def test_exploratory_command_without_specific_file_passes_through():
    """ls/find/df não citam um arquivo específico (sem extensão) — não deve
    disparar o guard; são comandos de sondagem, não de leitura de um arquivo
    que pode não existir."""
    router = _router()
    decision = router._parse_classification(
        _raw("run_command", raw_command="ls -la ~/Desktop"),
        user_message="o que tem na minha área de trabalho?",
    )
    assert decision.intent == IntentType.CLIENT_TOOL
    assert decision.raw_command == "ls -la ~/Desktop"


def test_dotfile_directory_is_not_an_invented_filename():
    """Sessão 9f7abeec: `ls -la ~/.ssh` para "quais chaves de ssh eu tenho?".

    `_PATH_WITH_EXT_RE` casa `~/.ssh` como se `.ssh` fosse extensão de arquivo,
    e o basename `.ssh` (com ponto) não aparecia na mensagem (o usuário escreve
    "ssh", sem ponto) — o guard concluía "nome inventado", zerava o comando e
    forçava needs_plan. O planner falhou logo depois e o fluxo despachou
    `run_command` com `{}`: o desktop não teve o que executar e o turno ficou
    carregando pra sempre. Ponto INICIAL é dotfile/dot-dir, nunca extensão.
    """
    router = _router()
    decision = router._parse_classification(
        _raw("run_command", raw_command="ls -la ~/.ssh"),
        user_message="quais chaves de ssh eu tenho? tenho uma do pcgamer ?",
    )
    assert decision.intent == IntentType.CLIENT_TOOL
    assert decision.needs_plan is False
    assert decision.raw_command == "ls -la ~/.ssh"


def test_dotfile_never_needs_grounding_even_if_unmentioned():
    """`~/.zshrc`, `~/.config`, `.env`: caminhos de sistema universais. Não há
    nome de arquivo pra "inventar" aqui — o modelo não está chutando nada."""
    router = _router()
    for command in ("cat ~/.zshrc", "ls -la ~/.config", "find . -iname '.env*'"):
        decision = router._parse_classification(
            _raw("run_command", raw_command=command),
            user_message="dá uma olhada aí na minha máquina",
        )
        assert decision.raw_command == command, command
        assert decision.needs_plan is False, command


def test_invented_path_still_blocked_after_dotfile_fix():
    """A correção do dotfile não pode abrir a porta que o guard fechou."""
    router = _router()
    decision = router._parse_classification(
        _raw("run_command", raw_command="cat /Users/daniel/Desktop/relatorio.csv"),
        user_message="relatorio de financas em csv na minha area de trabalho",
    )
    assert decision.needs_plan is True
    assert decision.raw_command is None


def test_name_listed_by_neo_grounds_the_derived_extension():
    """Sessão e8e5664b: o Neo listou `pc_gamer` no turno anterior e o usuário
    pediu "me retorna a chave pública dela" → `cat ~/.ssh/pc_gamer.pub`.

    O basename completo (`pc_gamer.pub`) nunca foi dito, mas o NOME foi — e veio
    de uma listagem real do disco, entre crases. Compor nome-citado + extensão é
    exatamente a composição legítima que o docstring do guard permite. Rejeitar
    isso zerava o comando, forçava um plano e travava o turno.
    """
    router = _router()
    historico = [
        {"role": "assistant", "content": (
            "Você tem 7 chaves SSH:\n\n- `id_ed25519_pcgamer_wsl`\n- `pc_gamer`\n\n"
            "Sim, tem chaves do PC Gamer: `pc_gamer` e `id_ed25519_pcgamer_wsl`."
        )},
    ]
    decision = router._parse_classification(
        _raw("run_command", raw_command="cat /Users/daniel/.ssh/pc_gamer.pub"),
        user_message="me retorna aqui na coonversa a chave pulbica dela",
        conversation_history=historico,
    )
    assert decision.needs_plan is False
    assert decision.raw_command == "cat /Users/daniel/.ssh/pc_gamer.pub"


def test_common_word_in_prose_does_not_ground_a_guessed_filename():
    """A contraprova: o guard NÃO pode aceitar 'relatorio' de "relatorio de
    financas" como fundamento para `relatorio.csv`. A diferença é o delimitador —
    nome de arquivo aparece entre crases/aspas ou dentro de um caminho, palavra
    de prosa não."""
    router = _router()
    decision = router._parse_classification(
        _raw("run_command", raw_command="cat /Users/daniel/Desktop/relatorio.csv"),
        user_message="relatorio de financas em csv na minha area de trabalho",
    )
    assert decision.needs_plan is True
    assert decision.raw_command is None


def test_unlisted_name_still_blocked_even_with_sibling_files_cited():
    """Nome que nunca apareceu continua bloqueado mesmo com irmãos listados."""
    router = _router()
    decision = router._parse_classification(
        _raw("run_command", raw_command="cat /Users/daniel/.ssh/inventada.pub"),
        user_message="mostra a pública",
        conversation_history=[{"role": "assistant", "content": "Tem `pc_gamer` e `id_ed25519_github`."}],
    )
    assert decision.needs_plan is True
    assert decision.raw_command is None

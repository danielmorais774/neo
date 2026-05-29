from core.router.intent_router import IntentRouter
from shared import IntentType


def test_intent_router_extracts_nested_json_from_prose():
    router = object.__new__(IntentRouter)
    raw = """
Vou salvar o treino em um arquivo de texto.

```json
{
  "intent": "CLIENT_TOOL",
  "confidence": 0.99,
  "tool_name": "write_file",
  "tool_arguments": {
    "path": "/Users/daniel/Documents/treino_abcd.txt",
    "content": "Treino ABCDE",
    "mode": "w"
  },
  "raw_command": null,
  "explanation": "Salvar arquivo",
  "requires_approval": false,
  "needs_memory": true,
  "knowledge_keywords": ["treino"],
  "needs_plan": false,
  "tipo_tarefa": "criacao",
  "skill_name": null
}
```
"""

    decision = router._parse_classification(raw, user_message="pode salvar")

    assert decision.intent == IntentType.CLIENT_TOOL
    assert decision.tool_name == "write_file"
    assert decision.tool_arguments["path"].endswith("treino_abcd.txt")


def test_fallback_routes_neo_training_message_to_direct_response():
    router = object.__new__(IntentRouter)
    message = (
        "Neo, modo treino.\n\n"
        "Esta mensagem não é uma preferência pessoal do usuário.\n"
        "Você deve discordar quando o usuário seguir uma solução ruim, frágil ou cara demais.\n"
        "Use humor e acidez como tempero, não como prato principal."
    )

    decision = router._fallback_classify(message)

    assert decision.intent == IntentType.DIRECT_RESPONSE
    assert decision.raw_command is None
    assert decision.requires_approval is False


def test_fallback_does_not_treat_discordar_as_local_operation():
    router = object.__new__(IntentRouter)
    # "discordar" contains "disco" but must not trip the client-tool fallback.
    decision = router._fallback_classify(
        "Acho que você deveria discordar de mim quando a ideia for ruim."
    )
    assert decision.intent == IntentType.DIRECT_RESPONSE
    assert decision.raw_command is None


def test_fallback_still_detects_genuine_local_operation():
    router = object.__new__(IntentRouter)
    decision = router._fallback_classify(
        "mostra os arquivos e o espaço em disco do meu computador"
    )
    assert decision.intent == IntentType.CLIENT_TOOL
    assert decision.raw_command == 'ls -la "$HOME"'

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

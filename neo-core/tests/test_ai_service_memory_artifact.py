from modules.desktop.ai_service import AIService


def test_find_memory_artifact_prefers_workout_plan_over_confirmation_ack():
    workout_plan = """
## Segunda-feira — Peito (Peitoral)
| Exercício | Séries | Repetições | Descanso |
|---|---|---|---|
| Supino reto com barra | 4 | 8-12 | 90s |

## Sexta-feira — Pernas
| Exercício | Séries | Repetições | Descanso |
|---|---|---|---|
| Leg press 45° | 4 | 10-12 | 90s |
"""

    artifact = AIService._find_memory_artifact(
        history=[
            {"role": "assistant", "content": workout_plan},
            {"role": "assistant", "content": "Vou salvar aqui na memória pra quando você precisar."},
        ],
        fallback="Perfeito, Daniel. Já salvei na memória.",
    )

    assert artifact == workout_plan.strip()

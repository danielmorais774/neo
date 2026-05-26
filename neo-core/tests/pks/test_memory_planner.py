from core.pks.memory_planner import PKSMemoryPlanner


def test_memory_planner_parse_operations_from_fenced_json():
    planner = object.__new__(PKSMemoryPlanner)
    raw = """```json
    {
      "operations": [
        {
          "op": "upsert_entity",
          "confidence": 0.93,
          "reason": "usuário afirmou",
          "entity": {"name": "PC principal", "type": "device"}
        }
      ]
    }
    ```"""

    operations = planner._parse(raw, max_operations=10)

    assert len(operations) == 1
    assert operations[0].op == "upsert_entity"
    assert operations[0].confidence == 0.93

from core.skills.whatsapp_messaging.pipeline import _split_whatsapp_messages


def test_split_whatsapp_messages_does_not_duplicate_last_sentence() -> None:
    text = "Ainda não, passo no mercado no caminho. Vou chegar mais tarde hoje."

    assert _split_whatsapp_messages(text, mode="personal") == [text]


def test_split_whatsapp_messages_keeps_remainder_once_when_splitting() -> None:
    text = (
        "Ainda não, passo no mercado no caminho. "
        "Vou chegar mais tarde hoje. "
        "Se precisar de algo me avisa."
    )

    parts = _split_whatsapp_messages(text, mode="personal")
    joined = " ".join(parts)

    assert joined.count("Vou chegar mais tarde hoje.") == 1
    assert joined.count("Se precisar de algo me avisa.") == 1

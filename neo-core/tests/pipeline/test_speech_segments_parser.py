"""Voice imitation — [[VOICE:name]] block extraction from the cognitive reply."""

from core.pipeline.speech_segments_parser import extract_speech_segments


def test_no_tag_is_passthrough_untouched():
    text = "apenas uma conversa normal, sem imitação nenhuma."
    clean, segments = extract_speech_segments(text)
    assert clean == text
    assert segments == []


def test_default_then_voiced_segment():
    text = "Ok, vou imitar o Mario. [[VOICE:mario]]Oi, eu sou o Mario![[/VOICE]]"
    clean, segments = extract_speech_segments(text)
    assert clean == "Ok, vou imitar o Mario. Oi, eu sou o Mario!"
    assert "[[VOICE" not in clean
    assert len(segments) == 2
    assert segments[0].text == "Ok, vou imitar o Mario."
    assert segments[0].voice is None
    assert segments[1].text == "Oi, eu sou o Mario!"
    assert segments[1].voice == "mario"


def test_multiple_voices_in_order():
    text = (
        "[[VOICE:mario]]É uma me!![[/VOICE]] e agora o outro: "
        "[[VOICE:jarvis]]Bom dia, senhor.[[/VOICE]]"
    )
    clean, segments = extract_speech_segments(text)
    assert [s.voice for s in segments] == ["mario", None, "jarvis"]
    assert segments[1].text == "e agora o outro:"
    assert clean == "É uma me!! e agora o outro: Bom dia, senhor."


def test_whole_reply_single_voice():
    clean, segments = extract_speech_segments("[[VOICE:mario]]Vamos lá![[/VOICE]]")
    assert clean == "Vamos lá!"
    assert len(segments) == 1
    assert segments[0].voice == "mario"


def test_voice_name_with_spaces_and_accents():
    text = "[[VOICE:Seu João]]Bão, cumpadi?[[/VOICE]]"
    _clean, segments = extract_speech_segments(text)
    assert segments[0].voice == "Seu João"


def test_stray_open_tag_without_close_is_stripped_not_segmented():
    # Malformed: opening tag, no close. No usable voice → strip the fragment and
    # fall back to a normal single-voice reply.
    text = "bla [[VOICE:mario]] sem fechar"
    clean, segments = extract_speech_segments(text)
    assert "[[VOICE" not in clean
    assert segments == []


def test_empty_voice_name_is_not_a_voice_segment():
    text = "antes [[VOICE:]]meio[[/VOICE]] depois"
    clean, segments = extract_speech_segments(text)
    # Empty name → no override; treated as a plain reply (no segments).
    assert segments == []
    assert "[[VOICE" not in clean


def test_bracket_text_without_voice_token_untouched():
    # A reply that merely contains "[[" (not a VOICE tag) must be returned verbatim.
    text = "use a sintaxe [[algo]] no código"
    clean, segments = extract_speech_segments(text)
    assert clean == text
    assert segments == []

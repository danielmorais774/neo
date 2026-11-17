"""Tests for WhatsApp JID matching helpers."""

from types import SimpleNamespace

from db.async_client import (
    _digits_from_whatsapp_jid,
    _mission_matches_whatsapp_contact,
    _whatsapp_jid_candidates,
)


def test_whatsapp_jid_candidates_include_common_phone_variants() -> None:
    candidates = set(_whatsapp_jid_candidates("5511912345678@s.whatsapp.net"))

    assert "5511912345678@s.whatsapp.net" in candidates
    assert "5511912345678@c.us" in candidates


def test_whatsapp_jid_candidates_keep_lid_without_phone_guess() -> None:
    candidates = _whatsapp_jid_candidates("abc123@lid")

    assert candidates == ["abc123@lid"]


def test_digits_from_whatsapp_jid_only_returns_phone_backed_digits() -> None:
    assert _digits_from_whatsapp_jid("5511912345678@c.us") == "5511912345678"
    assert _digits_from_whatsapp_jid("abc123@lid") == ""


def test_mission_matches_lid_from_last_outbound_id() -> None:
    mission = SimpleNamespace(
        contact_jid="5511912345678@c.us",
        extra_metadata={
            "contact_phone_e164": "5511912345678",
            "last_outbound_id": "true_234020537397350@lid_3EB09B7798FBAA0F87CE52",
        },
    )

    assert _mission_matches_whatsapp_contact(mission, "234020537397350@lid")

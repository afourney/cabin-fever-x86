"""The printed-material hint request and its deliberately isolated model turn."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from cabin_fever_x86_core import hints


@dataclass
class FakeResponse:
    output_text: str


@dataclass
class FakeResponses:
    result: dict[str, Any]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        return FakeResponse(json.dumps(self.result))


@dataclass
class FakeClient:
    responses: FakeResponses


def test_game_names_are_normalized_without_accepting_paths() -> None:
    assert hints.normalize_game(" ZORK1.Z5 ") == "zork1"
    assert hints.normalize_game("../zork1.z5") == ""
    assert hints.normalize_game(r"folder\zork1.z5") == ""


def test_rot13_preserves_case_and_nonletters_and_is_its_own_inverse() -> None:
    original = "Abc Nop, ZORK 1!"
    encoded = hints.rot13(original)

    assert encoded == "Nop Abc, MBEX 1!"
    assert hints.rot13(encoded) == original


def test_packaged_hints_are_obfuscated_and_decode_when_loaded() -> None:
    encoded = hints._hint_resource("zork1").read_text(encoding="utf-8")

    assert hints.has_hints("zork1")
    assert "ZORK I: THE GREAT UNDERGROUND EMPIRE" not in encoded
    assert (hints._load_hints("zork1") or "").startswith("ZORK I: THE GREAT UNDERGROUND EMPIRE")


@pytest.mark.asyncio
async def test_missing_material_returns_without_calling_the_model(monkeypatch) -> None:
    responses = FakeResponses({"status": "answered", "hint": "should not be reached"})
    monkeypatch.setattr(hints, "_load_hints", lambda _game: None)

    result = await hints.provide_hint(
        FakeClient(responses),  # type: ignore[arg-type]
        "test-model",
        "missing.z5",
        "How do I open the egg?",
        hints.HintLevel.SMALL_HINT,
    )

    assert result == "No hints are available for missing."
    assert responses.calls == []


@pytest.mark.asyncio
async def test_hint_request_has_a_fresh_cacheable_context(monkeypatch) -> None:
    responses = FakeResponses({"status": "answered", "hint": "Listen for a singing bird."})
    monkeypatch.setattr(hints, "_load_hints", lambda _game: "The canary matters in the forest.")

    result = await hints.provide_hint(
        FakeClient(responses),  # type: ignore[arg-type]
        "test-model",
        "zork1.z5",
        "What should I do with the canary?",
        hints.HintLevel.SMALL_HINT,
    )

    assert result == "Listen for a singing bird."
    assert len(responses.calls) == 1
    call = responses.calls[0]
    assert call["model"] == "test-model"
    assert call["prompt_cache_key"] == "zork1_hint"
    assert call["prompt_cache_retention"] == "24h"
    assert call["store"] is False
    assert "previous_response_id" not in call
    assert call["input"][0]["content"].index("The canary matters") >= 0
    assert call["input"][-1]["content"].index("SMALL_HINT") >= 0
    assert hints.SMALL_HINT in call["input"][-1]["content"]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("insufficient_context", hints.INSUFFICIENT_CONTEXT),
        ("too_broad", hints.TOO_BROAD),
        ("not_found", hints.NO_HINT_FOUND),
    ],
)
@pytest.mark.asyncio
async def test_rejections_use_canned_application_messages(monkeypatch, status, expected) -> None:
    responses = FakeResponses({"status": status, "hint": None})
    monkeypatch.setattr(hints, "_load_hints", lambda _game: "source")

    result = await hints.provide_hint(
        FakeClient(responses),  # type: ignore[arg-type]
        "test-model",
        "zork1",
        "What do I do with it?",
        hints.HintLevel.MEDIUM_HINT,
    )

    assert result == expected

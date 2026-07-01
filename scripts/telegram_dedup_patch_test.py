#!/usr/bin/env python3
"""telegram_dedup_patch_test.py — regression guard for patch 0004.

The 06-27 → 06-28 rebase cycles were stuck because
``patches/0004-telegram-update-dedup.patch`` broke 13 upstream tests
(``test_telegram_group_gating.py`` ×12, ``test_telegram_voice_v0_regressions.py``
×1). Two independent failure modes:

  1. **Wrong target path.** The patch targeted ``gateway/platforms/telegram.py``,
     which upstream renamed to ``plugins/platforms/telegram/adapter.py``. It no
     longer applied onto ``NousResearch/hermes-agent`` main at all.
  2. **AttributeError on bypass-init.** ``_is_duplicate_update`` did a bare
     ``if uid in self._seen_update_ids`` membership test. Tests that construct
     the adapter without running ``__init__`` (``TelegramAdapter.__new__``, mock
     parents) never got the attribute set, so every media/text/command/location
     handler raised ``AttributeError: 'TelegramAdapter' object has no attribute
     '_seen_update_ids'``.

These tests lint the *patch artifact* (the ops repo doesn't vendor upstream
source) and then execute the shipped ``_is_duplicate_update`` body extracted
from the patch against a bypass-``__init__`` object — reproducing the exact
regression. Stdlib + pytest only; runs in ``validate.yml``.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PATCH = (
    Path(__file__).resolve().parent.parent
    / "patches"
    / "0004-telegram-update-dedup.patch"
)


def _added_source() -> str:
    """Return the patch's added (``+``) lines as plain source, sans diff prefix."""
    lines = []
    for raw in PATCH.read_text().splitlines():
        if raw.startswith("+") and not raw.startswith("+++"):
            lines.append(raw[1:])
    return "\n".join(lines)


def _extract_method(src: str, name: str) -> str:
    """Slice a top-of-class (4-space indented) method body out of ``src``."""
    lines = src.splitlines()
    start = next(
        (i for i, ln in enumerate(lines) if re.match(rf"\s{{4}}def {name}\b", ln)),
        None,
    )
    assert start is not None, f"{name} not found in added patch lines"
    body = [lines[start]]
    for ln in lines[start + 1 :]:
        # Stop at the next method definition at the same (4-space) indent.
        if re.match(r"\s{4}(async )?def \w", ln):
            break
        body.append(ln)
    return "\n".join(body)


def test_patch_targets_current_upstream_path():
    """Guards failure mode #1: patch must target the renamed adapter path."""
    text = PATCH.read_text()
    assert "plugins/platforms/telegram/adapter.py" in text
    assert "gateway/platforms/telegram.py" not in text, (
        "patch targets the pre-rename path; it will not apply onto upstream main"
    )


def test_dedup_is_lazy_initialized():
    """Guards failure mode #2 structurally: no bare unguarded attribute read."""
    method = _extract_method(_added_source(), "_is_duplicate_update")
    assert 'getattr(self, "_seen_update_ids"' in method, (
        "deque must be lazy-initialized via getattr fallback"
    )
    assert "if uid in self._seen_update_ids" not in method, (
        "bare `uid in self._seen_update_ids` reintroduces the AttributeError "
        "on adapters constructed without __init__"
    )


def _build_adapter_class():
    """exec the shipped ``_is_duplicate_update`` into a minimal class."""
    from collections import deque

    class _FakeLogger:
        def info(self, *a, **k):
            pass

    ns: dict = {"deque": deque, "logger": _FakeLogger()}
    method = _extract_method(_added_source(), "_is_duplicate_update")
    exec("class TelegramAdapterStub:\n" + "\n".join("    " + ln for ln in method.splitlines()), ns)
    return ns["TelegramAdapterStub"]


class _Update:
    def __init__(self, update_id):
        self.update_id = update_id


def test_is_duplicate_update_survives_bypassed_init():
    """The exact bug: __new__ bypasses __init__; must not raise AttributeError."""
    cls = _build_adapter_class()
    adapter = cls.__new__(cls)  # no __init__ → no _seen_update_ids preset

    assert adapter._is_duplicate_update(_Update(42)) is False  # first sighting
    assert adapter._is_duplicate_update(_Update(42)) is True   # duplicate → drop
    assert adapter._is_duplicate_update(_Update(99)) is False  # distinct id
    assert list(adapter._seen_update_ids) == [42, 99]          # lazy-init deque


def test_is_duplicate_update_handles_missing_update_id():
    cls = _build_adapter_class()
    adapter = cls.__new__(cls)
    assert adapter._is_duplicate_update(object()) is False  # no update_id attr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

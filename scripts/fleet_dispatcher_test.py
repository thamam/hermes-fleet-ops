#!/usr/bin/env python3
"""Unit tests for fleet_dispatcher.py. Run: pytest scripts/fleet_dispatcher_test.py

Mocks the Vik API at the _http_get_json boundary; no network is touched.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "fleet_dispatcher", Path(__file__).resolve().parent / "fleet_dispatcher.py"
)
fd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fd)

UTC = timezone.utc


# --------------------------------------------------------------------------- #
# State load/save — atomic round-trip
# --------------------------------------------------------------------------- #
def test_load_state_missing_returns_empty(tmp_path):
    assert fd.load_state(tmp_path / "nope.json") == {}


def test_load_state_corrupt_returns_empty(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json")
    assert fd.load_state(p) == {}


def test_save_state_atomic_roundtrip(tmp_path):
    p = tmp_path / "sub" / "state.json"
    fd.save_state(p, {"tasks": {"1": {"updated": "x"}}})
    assert fd.load_state(p) == {"tasks": {"1": {"updated": "x"}}}
    # no leftover tmp file
    assert not list(p.parent.glob("*.tmp"))


# --------------------------------------------------------------------------- #
# Quarantine logic
# --------------------------------------------------------------------------- #
def test_validate_tasks_quarantines_missing_id():
    good, bad = fd.validate_tasks([{"id": None, "title": "x"}, {"title": "no id"}])
    assert good == []
    assert len(bad) == 2
    assert all("id" in b["reason"] for b in bad)


def test_validate_tasks_quarantines_bad_due_date():
    good, bad = fd.validate_tasks([{"id": 5, "due_date": "not-a-date"}])
    assert good == []
    assert "unparseable due_date" in bad[0]["reason"]


def test_validate_tasks_accepts_no_due_sentinel_and_good():
    raw = [
        {"id": 1, "due_date": fd.NO_DUE_SENTINEL},
        {"id": 2, "due_date": "2026-07-01T00:00:00Z"},
        {"id": 3},
    ]
    good, bad = fd.validate_tasks(raw)
    assert {t["id"] for t in good} == {1, 2, 3}
    assert bad == []


def test_validate_tasks_quarantines_non_string_due_date():
    # Codex P2: {"due_date": 123} must quarantine, not raise AttributeError.
    good, bad = fd.validate_tasks([{"id": 1, "due_date": 123}])
    assert good == []
    assert "non-string due_date" in bad[0]["reason"]


def test_save_quarantine_merges_by_id(tmp_path):
    p = tmp_path / "q.json"
    n1 = fd.save_quarantine(p, fd.load_quarantine(p), [{"id": 1, "reason": "a"}])
    assert n1 == 1
    fd.save_quarantine(p, fd.load_quarantine(p), [{"id": 1, "reason": "a2"}, {"id": 2, "reason": "b"}])
    persisted = {str(e["id"]): e["reason"] for e in fd.load_quarantine(p)}
    assert persisted == {"1": "a2", "2": "b"}  # id 1 deduped, updated


# --------------------------------------------------------------------------- #
# Change detection
# --------------------------------------------------------------------------- #
def test_detect_changes_new_done_updated():
    prev = {
        "1": {"updated": "2026-06-30T10:00:00Z"},
        "2": {"updated": "2026-06-30T10:00:00Z"},  # will disappear -> done
    }
    current = [
        {"id": 1, "updated": "2026-06-30T11:00:00Z"},  # advanced -> updated
        {"id": 3, "updated": "2026-06-30T11:00:00Z"},  # new
    ]
    ch = fd.detect_changes(prev, current)
    assert ch["new"] == ["3"]
    assert ch["done"] == ["2"]
    assert ch["updated"] == ["1"]


def test_detect_changes_excludes_quarantined_from_done():
    # Codex P2: a previously-open task that comes back malformed (quarantined,
    # absent from current_good) must NOT be reported done.
    prev = {"5": {"updated": "2026-06-30T10:00:00Z"}}
    ch = fd.detect_changes(prev, [], quarantined_ids={"5"})
    assert ch["done"] == []


def test_detect_changes_no_churn_when_updated_not_advanced():
    prev = {"1": {"updated": "2026-06-30T10:00:00Z"}}
    current = [{"id": 1, "updated": "2026-06-30T10:00:00Z"}]
    ch = fd.detect_changes(prev, current)
    assert ch == {"new": [], "done": [], "updated": []}


# --------------------------------------------------------------------------- #
# Gateway log scan
# --------------------------------------------------------------------------- #
def test_scan_gateway_log_ignores_status_checks():
    now = datetime(2026, 6, 30, 12, 0, 0, tzinfo=UTC)
    text = (
        "2026-06-30T11:59:00 inbound message from telegram: please fix the deploy\n"
        "2026-06-30T11:58:00 inbound message from telegram: what's your status?\n"
    )
    hits = fd.scan_gateway_log(text, now)
    assert len(hits) == 1
    assert "fix the deploy" in hits[0]


def test_scan_gateway_log_drops_old_lines():
    now = datetime(2026, 6, 30, 12, 0, 0, tzinfo=UTC)
    text = "2026-06-30T09:00:00 inbound message from telegram: old work request\n"
    assert fd.scan_gateway_log(text, now) == []


# --------------------------------------------------------------------------- #
# Sitrep emission
# --------------------------------------------------------------------------- #
def test_build_sitrep_shape():
    s = fd.build_sitrep("sentinel", [4], 1,
                        {"new": [], "done": [], "updated": ["9"]}, 0, 0, False)
    assert s["profile"] == "sentinel"
    assert s["projects"] == [4]
    assert s["open_tasks"] == 1
    assert s["noticed_updates"] == 1
    assert "vik_unreachable" not in s
    assert s["ts"].endswith("Z")


def test_build_sitrep_unreachable_flag():
    s = fd.build_sitrep("mbot", [2, 3], 0,
                        {"new": [], "done": [], "updated": []}, 0, 0, True)
    assert s["vik_unreachable"] is True


# --------------------------------------------------------------------------- #
# main() — end-to-end with mocked API + error paths
# --------------------------------------------------------------------------- #
def _env(monkeypatch, tmp_path, projects="4"):
    monkeypatch.setenv("HERMES_PROFILE_NAME", "sentinel")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("VIKUNJA_API_TOKEN", "secret-token-xyz")
    monkeypatch.setenv("VIKUNJA_API_URL", "https://vik.example/api/v1")
    monkeypatch.setenv("FLEET_DISPATCHER_PROJECT_IDS", projects)
    monkeypatch.setenv("FLEET_DISPATCHER_STATE_DIR", str(tmp_path / "state"))


def test_main_happy_path_emits_sitrep(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    page = {1: [[{"id": 7, "title": "real work", "updated": "2026-06-30T10:00:00Z"}]]}

    def fake_get(url, token):
        assert token == "secret-token-xyz"
        return page[1].pop(0) if page[1] else []

    monkeypatch.setattr(fd, "_http_get_json", fake_get)
    rc = fd.main([])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["profile"] == "sentinel"
    assert out["open_tasks"] == 1
    assert out["noticed_new"] == 1
    # state persisted
    state = fd.load_state(tmp_path / "state" / "state.json")
    assert "7" in state["tasks"]


def test_main_vik_unreachable_exits_zero(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)

    def boom(url, token):
        import urllib.error
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(fd, "_http_get_json", boom)
    rc = fd.main([])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["vik_unreachable"] is True


def test_main_quarantines_malformed_task(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    served = {1: [[{"id": None, "title": "bad"}, {"id": 8, "title": "ok"}]]}
    monkeypatch.setattr(fd, "_http_get_json",
                        lambda url, token: served[1].pop(0) if served[1] else [])
    fd.main([])
    out = json.loads(capsys.readouterr().out.strip())
    assert out["quarantined"] == 1
    assert out["open_tasks"] == 1
    q = fd.load_quarantine(tmp_path / "state" / "quarantined.json")
    assert len(q) == 1


def test_fetch_open_tasks_raises_on_non_list(monkeypatch):
    # Codex round-3 P2: a non-list 200 must raise, not be read as end-of-page.
    monkeypatch.setattr(fd, "_http_get_json", lambda url, token: {"error": "boom"})
    with pytest.raises(ValueError):
        fd.fetch_open_tasks("https://vik/api/v1", "tok", 4)


def test_main_non_list_payload_preserves_state(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    fd.save_state(tmp_path / "state" / "state.json", {"tasks": {"1": {"updated": "x"}}})
    monkeypatch.setattr(fd, "_http_get_json", lambda url, token: {"items": []})
    fd.main([])
    out = json.loads(capsys.readouterr().out.strip())
    assert out["vik_unreachable"] is True
    assert out["noticed_done"] == 0
    assert fd.load_state(tmp_path / "state" / "state.json")["tasks"] == {"1": {"updated": "x"}}


def test_main_idless_malformed_suppresses_done_and_preserves_state(monkeypatch, tmp_path, capsys):
    # Codex round-3 P2: an id-less malformed task must not turn a tracked task
    # into a false completion, and must not drop it from the snapshot.
    _env(monkeypatch, tmp_path)
    fd.save_state(tmp_path / "state" / "state.json",
                  {"tasks": {"5": {"updated": "2026-06-30T10:00:00Z", "title": "live"}}})
    served = {1: [[{"id": None, "title": "malformed"}]]}
    monkeypatch.setattr(fd, "_http_get_json",
                        lambda url, token: served[1].pop(0) if served[1] else [])
    fd.main([])
    out = json.loads(capsys.readouterr().out.strip())
    assert out["noticed_done"] == 0
    assert out["idless_quarantine"] is True
    assert "5" in fd.load_state(tmp_path / "state" / "state.json")["tasks"]


def test_main_never_prints_token(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(fd, "_http_get_json", lambda url, token: [])
    fd.main([])
    captured = capsys.readouterr()
    assert "secret-token-xyz" not in captured.out
    assert "secret-token-xyz" not in captured.err


def test_main_empty_projects_fails_closed_without_clearing_state(monkeypatch, tmp_path, capsys):
    # Codex P2: blank FLEET_DISPATCHER_PROJECT_IDS must emit config_error and
    # leave existing state untouched (not mark everything done + clear snapshot).
    _env(monkeypatch, tmp_path, projects="")
    fd.save_state(tmp_path / "state" / "state.json",
                  {"tasks": {"99": {"updated": "x", "title": "live"}}})

    def fail(url, token):
        raise AssertionError("must not hit the API when no projects configured")

    monkeypatch.setattr(fd, "_http_get_json", fail)
    rc = fd.main([])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["config_error"]
    assert out["noticed_done"] == 0
    # state preserved
    state = fd.load_state(tmp_path / "state" / "state.json")
    assert state["tasks"] == {"99": {"updated": "x", "title": "live"}}


def test_main_malformed_task_not_marked_done_and_stays_tracked(monkeypatch, tmp_path, capsys):
    # Codex P2: a tracked task returning malformed must not flip to done and must
    # remain in the snapshot for the next run.
    _env(monkeypatch, tmp_path)
    fd.save_state(tmp_path / "state" / "state.json",
                  {"tasks": {"5": {"updated": "2026-06-30T10:00:00Z", "title": "real"}}})
    served = {1: [[{"id": 5, "due_date": "not-a-date"}]]}
    monkeypatch.setattr(fd, "_http_get_json",
                        lambda url, token: served[1].pop(0) if served[1] else [])
    fd.main([])
    out = json.loads(capsys.readouterr().out.strip())
    assert out["noticed_done"] == 0
    assert out["quarantined"] == 1
    state = fd.load_state(tmp_path / "state" / "state.json")
    assert "5" in state["tasks"]  # carried forward, still tracked


def test_main_malformed_project_ids_config_error(monkeypatch, tmp_path, capsys):
    # Codex round-2 P2: "4,abc" must fail closed with config_error, not crash.
    _env(monkeypatch, tmp_path, projects="4,abc")
    fd.save_state(tmp_path / "state" / "state.json", {"tasks": {"1": {"updated": "x"}}})
    monkeypatch.setattr(fd, "_http_get_json",
                        lambda url, token: (_ for _ in ()).throw(AssertionError("no API")))
    rc = fd.main([])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert "non-numeric" in out["config_error"]
    assert fd.load_state(tmp_path / "state" / "state.json")["tasks"] == {"1": {"updated": "x"}}


def test_lock_blocks_second_run(tmp_path):
    lock = tmp_path / ".lock"
    assert fd.acquire_lock(lock) is True
    assert fd.acquire_lock(lock) is False  # held
    fd.release_lock(lock)
    assert fd.acquire_lock(lock) is True


def test_lock_not_broken_while_owner_alive_even_if_old(tmp_path):
    # Codex round-2 P2: a live owner must hold the lock regardless of age.
    import os as _os
    lock = tmp_path / ".lock"
    assert fd.acquire_lock(lock) is True  # owner pid = this live process
    old = time.time() - 10 * fd.STALE_LOCK_SEC
    _os.utime(lock, (old, old))
    assert fd.acquire_lock(lock) is False  # still held — owner alive, age ignored
    fd.release_lock(lock)


def test_lock_broken_when_owner_pid_dead(tmp_path):
    lock = tmp_path / ".lock"
    lock.mkdir()
    (lock / "pid").write_text("2147483647")  # pid that does not exist
    assert fd.acquire_lock(lock) is True  # dead owner -> stale -> re-acquired


def test_lock_stale_timeout_fallback_when_owner_unreadable(tmp_path):
    import os as _os
    lock = tmp_path / ".lock"
    lock.mkdir()  # no pid file -> unreadable owner
    assert fd.acquire_lock(lock) is False  # fresh mtime -> not stale yet
    old = time.time() - 2 * fd.STALE_LOCK_SEC
    _os.utime(lock, (old, old))
    assert fd.acquire_lock(lock) is True  # aged past fallback timeout


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

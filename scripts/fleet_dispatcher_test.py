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


def test_load_state_non_object_returns_empty(tmp_path):
    # Codex round-13 P2: a valid-JSON-but-non-object state file must not crash.
    p = tmp_path / "state.json"
    p.write_text("[]")
    assert fd.load_state(p) == {}
    p.write_text("null")
    assert fd.load_state(p) == {}


def test_main_non_object_state_file_does_not_crash(monkeypatch, tmp_path, capsys):
    _env(monkeypatch, tmp_path)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "state.json").write_text("[]")
    monkeypatch.setattr(fd, "_http_get_json", lambda url, token: [])
    rc = fd.main([])
    assert rc == 0
    assert json.loads(capsys.readouterr().out.strip())["profile"] == "sentinel"


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


def test_validate_tasks_quarantines_non_object_payload():
    # Codex round-11 P2: a non-object item (e.g. null) must be quarantined, not
    # silently dropped (which would let a tracked task be falsely marked done).
    good, bad = fd.validate_tasks([None, {"id": 7, "title": "ok"}])
    assert [t["id"] for t in good] == [7]
    assert len(bad) == 1 and bad[0]["id"] is None


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


def test_detect_changes_fractional_seconds_update():
    # Codex round-5 P2: fractional-second ts is chronologically later despite
    # sorting earlier lexicographically.
    prev = {"1": {"updated": "2026-06-30T10:00:00Z"}}
    current = [{"id": 1, "updated": "2026-06-30T10:00:00.100Z"}]
    assert fd.detect_changes(prev, current)["updated"] == ["1"]


def test_detect_changes_guards_corrupt_prev_entry():
    # Codex round-14 P3: a null prev-task entry must not crash detect_changes.
    prev = {"1": None}
    current = [{"id": 1, "updated": "2026-06-30T11:00:00Z"}]
    ch = fd.detect_changes(prev, current)
    assert ch["updated"] == ["1"]  # treated as advanced from empty, no crash


def test_detect_changes_no_churn_when_updated_not_advanced():
    prev = {"1": {"updated": "2026-06-30T10:00:00Z"}}
    current = [{"id": 1, "updated": "2026-06-30T10:00:00Z"}]
    ch = fd.detect_changes(prev, current)
    assert ch == {"new": [], "done": [], "updated": []}


# --------------------------------------------------------------------------- #
# Gateway log scan
# --------------------------------------------------------------------------- #
def test_scan_gateway_log_ignores_status_checks():
    now = datetime(2026, 6, 30, 12, 0, 0)  # naive local wall-clock, matches log ts
    text = (
        "2026-06-30T11:59:00 inbound message from telegram: please fix the deploy\n"
        "2026-06-30T11:58:00 inbound message from telegram: what's your status?\n"
    )
    hits = fd.scan_gateway_log(text, now)
    assert len(hits) == 1
    assert "fix the deploy" in hits[0]


def test_scan_gateway_log_ignores_lifecycle_received():
    # Codex round-5 P2: "Received SIGTERM" must not count as inbound work.
    now = datetime(2026, 6, 30, 12, 0, 0)  # naive local wall-clock, matches log ts
    text = "2026-06-30T11:59:00 Received SIGTERM - initiating shutdown\n"
    assert fd.scan_gateway_log(text, now) == []


def test_count_untracked_matches_open_task_title():
    # Codex round-5 P2: an inbound line already covered by an open task is tracked.
    cands = ["inbound message from telegram: please deploy the new build"]
    assert fd.count_untracked(cands, [{"title": "Deploy the new build"}]) == 0
    assert fd.count_untracked(cands, [{"title": "Fix login bug"}]) == 1


def test_count_untracked_matches_short_acronym_tasks():
    # Codex round-7 P2: short ops identifiers (CI, DB, API, PR 8) must match.
    assert fd.count_untracked(["inbound message: please fix CI"], [{"title": "Fix CI"}]) == 0
    assert fd.count_untracked(["inbound message: DB lag"], [{"title": "DB lag"}]) == 0
    assert fd.count_untracked(["inbound message: ship PR 8"], [{"title": "PR 8"}]) == 0
    assert fd.count_untracked(["inbound message: please fix CI"], [{"title": "Deploy build"}]) == 1


def test_scan_gateway_log_keeps_work_containing_status_word():
    # Codex round-6 P2: "status" inside a real request is NOT a status check.
    now = datetime(2026, 6, 30, 12, 0, 0)  # naive local wall-clock, matches log ts
    text = (
        "2026-06-30T11:59:00 inbound message from telegram: fix the status page outage\n"
        "2026-06-30T11:58:00 inbound message from telegram: what's your status?\n"
    )
    hits = fd.scan_gateway_log(text, now)
    assert len(hits) == 1
    assert "status page outage" in hits[0]


def test_scan_gateway_log_returns_payload_only():
    # Codex round-8 P2: timestamp/metadata must be stripped so it can't match
    # numeric task tokens. A "PR 30" task must not match a June-30 timestamp.
    now = datetime(2026, 6, 30, 12, 0, 0)  # naive local wall-clock, matches log ts
    text = "2026-06-30T11:59:00 INFO inbound message from telegram: deploy the build\n"
    hits = fd.scan_gateway_log(text, now)
    assert hits == ["deploy the build"]
    # the stripped payload shares nothing with "PR 30", so it stays untracked
    assert fd.count_untracked(hits, [{"title": "PR 30"}]) == 1


def test_scan_gateway_log_keeps_work_mentioning_sitrep():
    # Codex round-9 P2: "fix the sitrep JSON" is work; only "sitrep?" is a check.
    now = datetime(2026, 6, 30, 12, 0, 0)  # naive local wall-clock, matches log ts
    text = (
        "2026-06-30T11:59:00 inbound message from telegram: fix the sitrep JSON\n"
        "2026-06-30T11:58:00 inbound message from telegram: sitrep?\n"
    )
    hits = fd.scan_gateway_log(text, now)
    assert hits == ["fix the sitrep JSON"]


def test_count_untracked_numbered_pr_must_match():
    # Codex round-9 P2: "ship PR 8" is not covered by "PR 9" via shared "pr".
    assert fd.count_untracked(["ship PR 8"], [{"title": "PR 9"}]) == 1
    assert fd.count_untracked(["ship PR 8"], [{"title": "PR 8"}]) == 0


def test_count_untracked_single_shared_verb_not_enough():
    # Codex round-10 P2: a single shared generic word is not coverage.
    assert fd.count_untracked(["deploy mobile app"], [{"title": "deploy backend"}]) == 1
    assert fd.count_untracked(["deploy mobile app"], [{"title": "deploy mobile app"}]) == 0


def test_count_untracked_two_token_needs_full_match():
    # Codex round-11 P2: "deploy build" is not covered by "deploy backend".
    assert fd.count_untracked(["deploy build"], [{"title": "deploy backend"}]) == 1
    assert fd.count_untracked(["deploy build"], [{"title": "deploy build pipeline"}]) == 0


def test_main_non_object_task_does_not_false_complete(monkeypatch, tmp_path, capsys):
    # Codex round-11 P2: a null page item must route through the idless
    # fail-closed path, preserving prior tracked tasks.
    def _env(mp, tp):
        mp.setenv("HERMES_PROFILE_NAME", "sentinel")
        mp.setenv("HERMES_HOME", str(tp))
        mp.setenv("VIKUNJA_API_TOKEN", "tok")
        mp.setenv("VIKUNJA_API_URL", "https://vik.example/api/v1")
        mp.setenv("FLEET_DISPATCHER_PROJECT_IDS", "4")
        mp.setenv("FLEET_DISPATCHER_STATE_DIR", str(tp / "state"))
    _env(monkeypatch, tmp_path)
    fd.save_state(tmp_path / "state" / "state.json",
                  {"tasks": {"5": {"updated": "2026-06-30T10:00:00Z", "title": "live"}}})
    served = {1: [[None]]}
    monkeypatch.setattr(fd, "_http_get_json",
                        lambda url, token: served[1].pop(0) if served[1] else [])
    fd.main([])
    out = json.loads(capsys.readouterr().out.strip())
    assert out["noticed_done"] == 0
    assert out["idless_quarantine"] is True
    assert "5" in fd.load_state(tmp_path / "state" / "state.json")["tasks"]


def test_scan_gateway_log_keeps_work_mentioning_status_check():
    # Codex round-10 P2: "fix the status check endpoint" is work, not a heartbeat.
    now = datetime(2026, 6, 30, 12, 0, 0)  # naive local wall-clock, matches log ts
    text = "2026-06-30T11:59:00 inbound message from telegram: fix the status check endpoint\n"
    assert fd.scan_gateway_log(text, now) == ["fix the status check endpoint"]


def test_scan_gateway_log_structured_msg_format():
    # Codex round-15 P2: structured "msg='...'" lines must yield only the payload,
    # so the chat id and other metadata don't poison content matching.
    now = datetime(2026, 6, 30, 12, 0, 0)  # naive local wall-clock, matches log ts
    text = ("2026-06-30T11:59:00 inbound message: platform=telegram "
            "chat=6452171937 msg='deploy the new build'\n")
    hits = fd.scan_gateway_log(text, now)
    assert hits == ["deploy the new build"]
    assert fd.count_untracked(hits, [{"title": "Deploy the new build"}]) == 0


def test_scan_gateway_log_work_reply_to_status_ping():
    # Codex round-17 P2: status filter must run on the payload, not the whole
    # line — real work replying to a status ping (reply_to_text metadata) counts.
    now = datetime(2026, 6, 30, 12, 0, 0)  # naive local wall-clock, matches log ts
    text = ("2026-06-30T11:59:00 inbound message: platform=telegram chat=123 "
            "msg='deploy the build' reply_to_id=42 reply_to_text=\"what's your status?\"\n")
    hits = fd.scan_gateway_log(text, now)
    assert hits == ["deploy the build"]


def test_scan_gateway_log_real_gateway_format():
    # Ground truth from hermes-agent gateway/run.py:8264 —
    #   "inbound message: platform=%s user=%s chat=%s msg=%r reply_to_id=%s reply_to_text=%r"
    now = datetime(2026, 6, 30, 12, 0, 0)  # naive local wall-clock, matches log ts
    text = ("2026-06-30T11:59:00 INFO inbound message: platform=telegram user=alice "
            "chat=6452171937 msg='deploy the new build' reply_to_id=None reply_to_text=''\n")
    hits = fd.scan_gateway_log(text, now)
    assert hits == ["deploy the new build"]
    assert fd.count_untracked(hits, [{"title": "Deploy the new build"}]) == 0
    # the chat id must not leak into matching
    assert fd.count_untracked(hits, [{"title": "unrelated 6452171937"}]) == 1


def test_message_payload_unquoted_message_field():
    # Codex round-16 P2: an unquoted message= field must not fall through to the
    # whole metadata string (defensive; real gateway always quotes via %r).
    assert fd._message_payload("inbound: chat=123 message=deploy the build") == "deploy the build"


def test_message_payload_repr_escaped_quotes():
    # Codex round-18 P2: repr-escaped msg with both quote types decodes fully.
    line = "inbound message: chat=1 msg='don\\'t deploy \"main\"' reply_to_id=None"
    assert fd._message_payload(line) == 'don\'t deploy "main"'


def test_significant_words_non_ascii():
    # Codex round-18 P2: Hebrew (and other non-ASCII) tokens must be captured.
    assert fd._significant_words("פרוס את הבילד") == {"פרוס", "את", "הבילד"}


def test_count_untracked_non_ascii_task_match():
    # Codex round-18 P2: a matching Hebrew task is not falsely reported untracked.
    assert fd.count_untracked(["פרוס את הבילד"], [{"title": "פרוס את הבילד"}]) == 0
    assert fd.count_untracked(["פרוס את הבילד"], [{"title": "תקן באג"}]) == 1


def test_scan_gateway_log_status_phrase_midwork_kept():
    # Codex round-20 P2: a status phrase mid-payload is real work, only a payload
    # that IS a status check (phrase at start) is suppressed.
    now = datetime(2026, 6, 30, 12, 0, 0)
    text = (
        "2026-06-30T11:59:00 inbound message from telegram: fix the send sitrep command\n"
        "2026-06-30T11:58:30 inbound message from telegram: update the what's your status handler\n"
        "2026-06-30T11:58:00 inbound message from telegram: what's your status?\n"
    )
    hits = fd.scan_gateway_log(text, now)
    assert hits == ["fix the send sitrep command", "update the what's your status handler"]


def test_scan_gateway_log_local_naive_now_window():
    # Codex round-19 P2: a naive log ts compared to local wall-clock now. A line
    # ~30 min old is kept; the comparison must not be skewed by host UTC offset.
    now = datetime(2026, 6, 30, 12, 0, 0)  # naive local
    text = "2026-06-30T11:30:00 inbound message from telegram: deploy the build\n"
    assert fd.scan_gateway_log(text, now) == ["deploy the build"]


def test_scan_gateway_log_drops_old_lines():
    now = datetime(2026, 6, 30, 12, 0, 0)  # naive local wall-clock, matches log ts
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


def test_main_incomplete_read_is_vik_unreachable(monkeypatch, tmp_path, capsys):
    # Codex round-12 P2: a partial HTTP read must fail closed, not crash the cron.
    _env(monkeypatch, tmp_path)
    fd.save_state(tmp_path / "state" / "state.json", {"tasks": {"1": {"updated": "x"}}})

    def partial(url, token):
        import http.client
        raise http.client.IncompleteRead(b"")

    monkeypatch.setattr(fd, "_http_get_json", partial)
    rc = fd.main([])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["vik_unreachable"] is True
    assert fd.load_state(tmp_path / "state" / "state.json")["tasks"] == {"1": {"updated": "x"}}


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


def test_main_ignores_unknown_cron_flags(monkeypatch, tmp_path, capsys):
    # Codex round-4 P2: cron launcher flags must not crash the parser (exit 2).
    _env(monkeypatch, tmp_path)
    monkeypatch.setattr(fd, "_http_get_json", lambda url, token: [])
    rc = fd.main(["--deliver", "origin", "--no-agent"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["profile"] == "sentinel"


def test_main_untracked_independent_of_task_churn(monkeypatch, tmp_path, capsys):
    # Codex round-4 P2: an unrelated new task must NOT mask an untracked inbound
    # work line in the same tick.
    _env(monkeypatch, tmp_path)
    log = tmp_path / "logs" / "gateway.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("inbound message from telegram: please deploy the new build\n")
    served = {1: [[{"id": 9, "title": "unrelated", "updated": "2026-06-30T10:00:00Z"}]]}
    monkeypatch.setattr(fd, "_http_get_json",
                        lambda url, token: served[1].pop(0) if served[1] else [])
    fd.main([])
    out = json.loads(capsys.readouterr().out.strip())
    assert out["noticed_new"] == 1
    assert out["untracked_candidates"] == 1  # not zeroed by the unrelated new task


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


def test_main_blank_state_dir_falls_back_to_default(monkeypatch, tmp_path, capsys):
    # Codex round-20 P2: a blank FLEET_DISPATCHER_STATE_DIR must fall back to the
    # HERMES_HOME default, not write to cwd.
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("FLEET_DISPATCHER_STATE_DIR", "")  # templated-but-blank
    monkeypatch.setattr(fd, "_http_get_json",
                        lambda url, token: [{"id": 1, "title": "t", "updated": "x"}])
    fd.main([])
    assert (tmp_path / "state" / "fleet_dispatcher" / "state.json").exists()


def test_main_blank_vik_url_or_token_config_error(monkeypatch, tmp_path, capsys):
    # Codex round-22 P3: blank VIKUNJA_API_URL/TOKEN fail closed with config_error.
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("VIKUNJA_API_TOKEN", "")
    monkeypatch.setattr(fd, "_http_get_json",
                        lambda url, token: (_ for _ in ()).throw(AssertionError("no API")))
    fd.main([])
    assert "VIKUNJA_API_TOKEN" in json.loads(capsys.readouterr().out.strip())["config_error"]


def test_main_completed_task_not_done_and_untracked(monkeypatch, tmp_path, capsys):
    # Codex round-22 P2: work whose task was just completed must not be reported
    # as both noticed_done and untracked while its log line is still in-window.
    _env(monkeypatch, tmp_path)
    fd.save_state(tmp_path / "state" / "state.json",
                  {"tasks": {"5": {"updated": "2026-06-30T10:00:00Z", "title": "deploy the build"}}})
    log = tmp_path / "logs" / "gateway.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("inbound message from telegram: deploy the build\n")
    monkeypatch.setattr(fd, "_http_get_json", lambda url, token: [])  # task 5 now done/closed
    fd.main([])
    out = json.loads(capsys.readouterr().out.strip())
    assert out["noticed_done"] == 1
    assert out["untracked_candidates"] == 0  # covered by the just-completed task


def test_main_missing_hermes_home_config_error(monkeypatch, tmp_path, capsys):
    # Codex round-19 P2: a missing HERMES_HOME must fail closed, not fall back to
    # ~/.hermes and risk mixing per-profile state.
    monkeypatch.setenv("HERMES_PROFILE_NAME", "sentinel")
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("FLEET_DISPATCHER_STATE_DIR", raising=False)
    monkeypatch.setenv("VIKUNJA_API_URL", "https://vik.example/api/v1")
    monkeypatch.setenv("VIKUNJA_API_TOKEN", "tok")
    monkeypatch.setenv("FLEET_DISPATCHER_PROJECT_IDS", "4")
    monkeypatch.setattr(fd, "_http_get_json",
                        lambda url, token: (_ for _ in ()).throw(AssertionError("no API")))
    rc = fd.main([])
    assert rc == 0
    assert "HERMES_HOME" in json.loads(capsys.readouterr().out.strip())["config_error"]


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


def test_lock_takeover_of_dead_owner_then_held(tmp_path):
    # Codex round-6 P2: after taking over a stale lock, this run owns it and a
    # subsequent invocation must back off (no concurrent run).
    lock = tmp_path / ".lock"
    lock.mkdir()
    (lock / "pid").write_text("2147483647")  # dead owner -> stale
    assert fd.acquire_lock(lock) is True   # atomic takeover
    assert (lock / "pid").read_text().strip() == str(__import__("os").getpid())
    assert fd.acquire_lock(lock) is False  # now held by us
    # no leftover .stale.* dirs from the takeover
    assert not list(tmp_path.glob(".lock.stale.*"))


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

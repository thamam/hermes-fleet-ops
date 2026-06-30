#!/usr/bin/env python3
"""fleet_dispatcher.py — canonical Vik-discipline dispatcher for Hermes agents.

Keeps an agent's Vikunja board in 1:1 sync with its real work. Extracted and
generalized from Nigel's neuronbox_dispatcher.py (Vik-discipline core only —
no lander / merge_gate / truth_probe / parallel_lanes).

Each run:
  1. Loads lane state from ${FLEET_DISPATCHER_STATE_DIR}/state.json.
  2. Fetches open tasks from each assigned Vik project; quarantines any task
     with a missing/null id or an unparseable due_date (the 2026-06-27
     watchdog-crash pattern).
  3. Diffs against last run -> noticed_new / noticed_done / noticed_updates.
  4. Best-effort scans the last hour of the gateway log for inbound work that
     no open task seems to carry -> untracked_work_candidate.
  5. Persists state atomically (write tmp + rename).
  6. Emits a single JSON sitrep line to stdout.

The cron runs this in `--deliver origin --no-agent` mode, so stdout goes back
to the agent directly with zero LLM calls. Idempotent; safe every 5-15 min.

Env vars (set by the cron):
  HERMES_PROFILE_NAME          e.g. "sentinel"
  HERMES_HOME                  profile root, e.g. /home/ubuntu/.hermes/profiles/sentinel
  VIKUNJA_API_TOKEN            bearer (NEVER printed)
  VIKUNJA_API_URL              base URL, e.g. https://vik.example/api/v1
  FLEET_DISPATCHER_PROJECT_IDS comma-separated project ids, e.g. "2,3"
  FLEET_DISPATCHER_STATE_DIR   optional; default ${HERMES_HOME}/state/fleet_dispatcher
  FLEET_DISPATCHER_GATEWAY_LOG optional; default ${HERMES_HOME}/logs/gateway.log

Exit code is always 0 (a transient Vik outage must not fail the cron — it
retries next interval; sitrep then carries "vik_unreachable": true).

Usage:
  python3 scripts/fleet_dispatcher.py [--verbose]
"""
from __future__ import annotations

import argparse
import ast
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
NO_DUE_SENTINEL = "0001-01-01T00:00:00Z"  # Vikunja's "no due date" marker
STALE_LOCK_SEC = 60
GATEWAY_WINDOW_SEC = 3600
# Status-CHECK phrasing, not new work — never an untracked candidate. Matches
# the question/heartbeat intent, NOT the bare word "status" (so real work like
# "fix the status page outage" is still surfaced). Errs narrow: a missed status
# check merely shows a candidate; over-matching would mask real work.
STATUS_RE = re.compile(
    r"(how are you"
    r"|are you (up|there|alive|ok|online|working)"
    r"|you (still )?(there|up|alive|online|ok)\b"
    r"|what'?s your status|status\?"
    r"|still (there|alive|running|up)"
    r"|\bping\?|\bsitrep\?|send sitrep)",
    re.IGNORECASE,
)
# Target the gateway's actual inbound-message records — NOT bare "received",
# which also matches lifecycle lines like "Received SIGTERM — shutting down".
INBOUND_RE = re.compile(r"(inbound message|incoming message|message from|msg<-)", re.IGNORECASE)
TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})")
# Generic channel/greeting words that don't identify a unit of work; excluded
# when matching an inbound line against open-task titles.
STOPWORDS = frozenset({
    "inbound", "incoming", "message", "received", "from", "please", "could",
    "would", "telegram", "whatsapp", "signal", "slack", "discord", "gateway",
    "agent", "user", "this", "that", "with", "your", "have", "need", "want",
    "there", "here", "about", "hello",
    # short filler — kept out so 2+ char acronyms (CI, DB, UI, PR, QA) survive
    "fix", "new", "the", "run", "add", "see", "get", "for", "and", "not",
    "but", "was", "can", "all", "any", "out", "off", "via", "let", "its",
    "has", "you", "are",
})


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Atomic filesystem helpers
# --------------------------------------------------------------------------- #
def _atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)


def _load_json(path: Path):
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def load_state(path: Path) -> dict:
    # Coerce a non-object state file ([], null, a bad edit) to {} so the run
    # never crashes on state.get(...) — that would break the always-exit-0 cron.
    data = _load_json(path)
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict) -> None:
    _atomic_write_json(path, state)


def load_quarantine(path: Path) -> list:
    data = _load_json(path)
    return data if isinstance(data, list) else []


def save_quarantine(path: Path, existing: list, new_bad: list) -> int:
    """Merge new quarantine entries by id, persist, return this-run bad count."""
    by_id = {str(e.get("id")): e for e in existing}
    for entry in new_bad:
        by_id[str(entry.get("id"))] = entry
    _atomic_write_json(path, list(by_id.values()))
    return len(new_bad)


# --------------------------------------------------------------------------- #
# Lock — atomic mkdir, 60s stale breakaway (pattern from Nigel's dispatcher)
# --------------------------------------------------------------------------- #
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # exists but owned by another user
        return True
    except OSError:
        return False


def _lock_is_stale(lock_dir: Path) -> bool:
    """A lock is stale only if its owner is gone. If the owner pid is alive the
    lock is NEVER stale, no matter how long the run has taken — this is what
    stops a slow but live run from being broken out from under itself. The 60s
    timeout is a fallback for orphaned locks whose owner pid is unreadable."""
    try:
        pid = int((lock_dir / "pid").read_text().strip())
    except (OSError, ValueError):
        pid = None
    if pid is not None:
        return not _pid_alive(pid)
    try:
        age = time.time() - lock_dir.stat().st_mtime
    except FileNotFoundError:
        return True
    return age > STALE_LOCK_SEC


def _write_lock_owner(lock_dir: Path) -> None:
    try:
        (lock_dir / "pid").write_text(str(os.getpid()))
    except OSError:
        pass


def acquire_lock(lock_dir: Path) -> bool:
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_dir.mkdir()
        _write_lock_owner(lock_dir)
        return True
    except FileExistsError:
        if not _lock_is_stale(lock_dir):
            return False
        # Atomic takeover: rename the stale lock aside first. os.rename is atomic,
        # so if two invocations race on the same stale lock only one wins the
        # rename; the loser gets an error and backs off, instead of both deleting
        # and re-creating the lock and proceeding concurrently.
        steal = lock_dir.with_name(f"{lock_dir.name}.stale.{os.getpid()}")
        try:
            os.rename(lock_dir, steal)
        except OSError:
            return False  # another invocation won the takeover (or it vanished)
        try:
            (steal / "pid").unlink()
        except OSError:
            pass
        try:
            steal.rmdir()
        except OSError:
            pass
        try:
            lock_dir.mkdir()
            _write_lock_owner(lock_dir)
            return True
        except OSError:
            return False  # a fresh run claimed it in the gap; back off


def release_lock(lock_dir: Path) -> None:
    try:
        (lock_dir / "pid").unlink()
    except OSError:
        pass
    try:
        lock_dir.rmdir()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Vikunja API (zero-dep urllib)
# --------------------------------------------------------------------------- #
def _http_get_json(url: str, token: str):
    """GET url with bearer auth -> parsed JSON. Isolated so tests can patch it."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_open_tasks(base_url: str, token: str, project_id: int) -> list:
    """All not-done tasks for a project, paginated. Filters done client-side too,
    so we are robust to Vikunja filter-syntax drift across versions."""
    tasks: list = []
    base = base_url.rstrip("/")
    for page in range(1, 51):  # hard cap: 50 pages
        url = (
            f"{base}/projects/{project_id}/tasks"
            f"?page={page}&per_page=50&filter=done%20%3D%20false"
        )
        batch = _http_get_json(url, token)
        if not isinstance(batch, list):
            # A non-list 200 (error envelope / version-wrapped page) must NOT be
            # read as "no tasks" — that would falsely mark everything done. Raise
            # so main()'s except path flags vik_unreachable and preserves state.
            raise ValueError(f"unexpected Vikunja payload (not a list): {type(batch).__name__}")
        if not batch:
            break
        # Keep open dict tasks AND any non-dict items (so validate_tasks can
        # quarantine the malformed ones); only genuinely-done dicts are dropped.
        tasks.extend(t for t in batch if not (isinstance(t, dict) and t.get("done")))
        if len(batch) < 50:
            break
    return tasks


# --------------------------------------------------------------------------- #
# Quarantine + change detection
# --------------------------------------------------------------------------- #
def _parse_due(val: str) -> datetime:
    return datetime.fromisoformat(val.replace("Z", "+00:00"))


def validate_tasks(raw_tasks: list) -> tuple[list, list]:
    """Split tasks into (good, quarantined). A task is quarantined if it has a
    missing/null id or a present-but-unparseable due_date."""
    good, bad = [], []
    for t in raw_tasks:
        if not isinstance(t, dict):
            bad.append({"id": None, "reason": f"non-object task payload: {t!r}",
                        "ts": now_iso()})
            continue
        tid = t.get("id")
        if not tid:
            bad.append({"id": tid, "reason": "missing/null id", "ts": now_iso()})
            continue
        due = t.get("due_date")
        if due and due != NO_DUE_SENTINEL:
            if not isinstance(due, str):
                bad.append({"id": tid, "reason": f"non-string due_date: {due!r}",
                            "ts": now_iso()})
                continue
            try:
                _parse_due(due)
            except (ValueError, TypeError):
                bad.append({"id": tid, "reason": f"unparseable due_date: {due!r}",
                            "ts": now_iso()})
                continue
        good.append(t)
    return good, bad


def detect_changes(prev_tasks: dict, current_good: list, quarantined_ids=None) -> dict:
    """Diff prev run's open-task snapshot against this run's open tasks.

    Tasks quarantined this run are excluded from `done`: a malformed record is
    not a completion, so it must never be reported done (the caller also carries
    it forward in the snapshot so it stays tracked)."""
    quarantined_ids = {str(i) for i in (quarantined_ids or ())}
    cur = {str(t["id"]): t for t in current_good}
    prev_ids, cur_ids = set(prev_tasks), set(cur)
    new = sorted(cur_ids - prev_ids)
    # Was open last run, no longer in the open set -> marked done (best-effort;
    # deletion looks the same and is reported as done too). Quarantined ids are
    # excluded — they are malformed, not done.
    done = sorted(prev_ids - cur_ids - quarantined_ids)
    updated = []
    for tid in cur_ids & prev_ids:
        prev_entry = prev_tasks[tid]
        prev_u = prev_entry.get("updated", "") if isinstance(prev_entry, dict) else ""
        if _updated_after(cur[tid].get("updated", ""), prev_u):
            updated.append(tid)
    return {"new": new, "done": done, "updated": sorted(updated)}


def _updated_after(cur_u: str, prev_u: str) -> bool:
    """True if cur_u is chronologically later than prev_u. Parses RFC3339 so
    fractional seconds / offsets compare correctly (lexicographic order does
    not: '...00Z' vs '...00.100Z'). Falls back to a plain inequality if either
    value is unparseable."""
    if not cur_u:
        return False
    try:
        return _parse_due(cur_u) > _parse_due(prev_u)
    except (ValueError, TypeError, AttributeError):
        return cur_u != prev_u


def snapshot_tasks(good: list) -> dict:
    return {
        str(t["id"]): {
            "updated": t.get("updated", ""),
            "title": t.get("title", ""),
        }
        for t in good
    }


# --------------------------------------------------------------------------- #
# Gateway log scan — best-effort untracked-work detection
# --------------------------------------------------------------------------- #
def _message_payload(line: str) -> str:
    """The user message from an inbound log line, with the leading timestamp and
    channel/logger metadata stripped. We return only the payload so metadata
    tokens (timestamp digits, a chat id, etc.) can't spuriously match task titles
    during untracked-work detection. Handles two shapes:
      structured:  ... msg='deploy the build'   (key=value metadata + quoted msg)
      simple:      ... from telegram: deploy the build"""
    # Prefer an explicit message field if the gateway logs key=value metadata.
    # Hermes gateway/run.py logs `... msg=%r ...`, so msg is repr-quoted; this
    # escape-aware capture (and ast.literal_eval) decodes a repr that escapes an
    # inner quote, e.g. 'don\'t deploy "main"'. reply_to_text=%r is left alone
    # because \btext does not match the "_text" inside "reply_to_text".
    m = re.search(
        r"\b(?:msg|message|text|body)=('(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")",
        line, re.IGNORECASE,
    )
    if m:
        try:
            return str(ast.literal_eval(m.group(1))).strip()
        except (ValueError, SyntaxError):
            return m.group(1)[1:-1].strip()
    m = re.search(r"\b(?:msg|message|text|body)=(\S.*)$", line, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Simple form: strip the channel prefix up to the first colon.
    mi = INBOUND_RE.search(line)
    rest = line[mi.end():] if mi else line
    if ":" in rest:
        rest = rest.split(":", 1)[1]
    return rest.strip()


def scan_gateway_log(text: str, now: datetime, window_sec: int = GATEWAY_WINDOW_SEC) -> list:
    """Inbound, work-triggering message payloads from the last `window_sec`.
    Status-check chatter is excluded so it never false-positives as untracked
    work. Returns the message payload only (timestamp/metadata stripped).

    Gateway timestamps are Python logging's default: local wall-clock and
    tz-naive. We compare against local wall-clock time (now is normalized to
    naive local) so the window isn't skewed by the host's UTC offset."""
    if now.tzinfo is not None:
        now = now.astimezone().replace(tzinfo=None)
    cutoff = now - timedelta(seconds=window_sec)
    hits = []
    for line in text.splitlines():
        if not INBOUND_RE.search(line):
            continue
        m = TS_RE.search(line)
        if m:
            try:
                ts = datetime.fromisoformat(m.group(1).replace(" ", "T"))
            except ValueError:
                ts = None
            if ts and ts < cutoff:
                continue
        payload = _message_payload(line)
        # Apply the status-check filter to the PAYLOAD, not the whole line: real
        # gateway lines carry reply_to_text metadata, so a work message that
        # replies to a status ping must not be dropped as a status check.
        if not payload or STATUS_RE.search(payload):
            continue
        hits.append(payload[:200])
    return hits


def _significant_words(text: str) -> set:
    """Work-identifying tokens: 2+ char word characters (Unicode-aware, so Hebrew
    and other non-ASCII titles tokenize too; keeps ops acronyms like CI/DB/PR)
    plus standalone single digits (so "PR 8" vs "PR 9" can be told apart), minus
    generic filler in STOPWORDS. Underscore is excluded so it can't join tokens."""
    return set(re.findall(r"[^\W_]{2,}|\d", text.lower(), re.UNICODE)) - STOPWORDS


def count_untracked(candidates: list, open_tasks: list) -> int:
    """Best-effort: a candidate inbound line counts as untracked unless some open
    task title covers it. A title covers a candidate when it shares a MAJORITY of
    the candidate's work-identifying words AND contains every numeric token the
    candidate names. The majority rule means a single shared generic word is not
    enough ("deploy mobile app" is not covered by "deploy backend"), while a
    fully-overlapping short request still matches ("fix CI" -> "Fix CI"). Whole
    tokens are matched (not substring); a candidate with no significant tokens is
    surfaced. This is a heuristic — it can't perfectly judge semantic coverage."""
    title_sets = [_significant_words(str(t.get("title", ""))) for t in open_tasks]
    untracked = 0
    for line in candidates:
        words = _significant_words(line)
        if not words:
            untracked += 1
            continue
        nums = {w for w in words if w.isdigit()}
        # Short requests (1-2 tokens) must match in full — a single shared word
        # is not coverage ("deploy build" vs "deploy backend"). Longer requests
        # need a majority, so an extra verb doesn't block a real identifier match.
        need = len(words) if len(words) <= 2 else (len(words) + 1) // 2
        covered = any(
            len(words & ts) >= need and nums <= ts for ts in title_sets
        )
        if not covered:
            untracked += 1
    return untracked


def read_gateway_log_tail(path: Path, max_bytes: int = 256_000) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# Sitrep
# --------------------------------------------------------------------------- #
def build_sitrep(profile: str, projects: list, open_tasks: int, changes: dict,
                 untracked: int, quarantined: int, vik_unreachable: bool) -> dict:
    sitrep = {
        "ts": now_iso(),
        "profile": profile,
        "projects": projects,
        "open_tasks": open_tasks,
        "noticed_new": len(changes.get("new", [])),
        "noticed_done": len(changes.get("done", [])),
        "noticed_updates": len(changes.get("updated", [])),
        "untracked_candidates": untracked,
        "quarantined": quarantined,
    }
    if vik_unreachable:
        sitrep["vik_unreachable"] = True
    return sitrep


def _env_projects() -> tuple[list, str]:
    """Return (project_ids, config_error). A blank var or any non-numeric id is
    a config error — both fail closed rather than crashing or clearing state."""
    raw = os.environ.get("FLEET_DISPATCHER_PROJECT_IDS", "").strip()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return [], "FLEET_DISPATCHER_PROJECT_IDS is unset or blank"
    try:
        return [int(p) for p in parts], ""
    except ValueError:
        return [], f"FLEET_DISPATCHER_PROJECT_IDS has non-numeric ids: {raw!r}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="emit a per-task summary too")
    # parse_known_args (not parse_args) so unrecognized flags passed by the cron
    # launcher (e.g. --deliver origin --no-agent) are ignored rather than causing
    # an argparse exit-2 with no sitrep, which would break the cron contract.
    args, _ = parser.parse_known_args(argv)

    profile = os.environ.get("HERMES_PROFILE_NAME", "unknown")
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    token = os.environ.get("VIKUNJA_API_TOKEN", "")
    base_url = os.environ.get("VIKUNJA_API_URL", "")
    projects, config_error = _env_projects()
    # HERMES_HOME determines the per-agent state dir; silently defaulting it would
    # mix snapshots across profiles on a multi-profile host. Fail closed if unset
    # (unless the state dir is pinned explicitly).
    if not hermes_home and not os.environ.get("FLEET_DISPATCHER_STATE_DIR"):
        config_error = config_error or "HERMES_HOME is unset"

    state_dir = Path(os.environ.get("FLEET_DISPATCHER_STATE_DIR",
                                    str(Path(hermes_home or ".") / "state" / "fleet_dispatcher")))
    gateway_log = Path(os.environ.get("FLEET_DISPATCHER_GATEWAY_LOG",
                                      str(Path(hermes_home or ".") / "logs" / "gateway.log")))
    state_path = state_dir / "state.json"
    quarantine_path = state_dir / "quarantined.json"
    lock_dir = state_dir / ".lock"

    if config_error:
        # Fail closed: a missing/blank/malformed required env var must NOT be read
        # as "zero open tasks" — that would falsely mark every tracked task done
        # and clear the snapshot. Emit a config-error sitrep, leave state untouched.
        sitrep = build_sitrep(profile, [], 0,
                              {"new": [], "done": [], "updated": []}, 0, 0, False)
        sitrep["config_error"] = config_error
        print(json.dumps(sitrep, separators=(",", ":")))
        return 0

    if not acquire_lock(lock_dir):
        return 0  # another run holds the lock; let it do the work
    try:
        state = load_state(state_path)
        prev_tasks = state.get("tasks")
        if not isinstance(prev_tasks, dict):
            prev_tasks = {}

        all_good, all_bad, open_total = [], [], 0
        vik_unreachable = False
        try:
            for pid in projects:
                raw = fetch_open_tasks(base_url, token, pid)
                good, bad = validate_tasks(raw)
                all_good.extend(good)
                all_bad.extend(bad)
            open_total = len(all_good)
        except (urllib.error.URLError, urllib.error.HTTPError, http.client.HTTPException,
                TimeoutError, OSError, json.JSONDecodeError, ValueError):
            # Network/parse failure (incl. partial reads / IncompleteRead): don't
            # fail the cron. Report vik_unreachable and retry next tick.
            vik_unreachable = True

        if vik_unreachable:
            sitrep = build_sitrep(profile, projects, len(prev_tasks),
                                  {"new": [], "done": [], "updated": []}, 0, 0, True)
            print(json.dumps(sitrep, separators=(",", ":")))
            return 0

        bad_ids = {str(e["id"]) for e in all_bad if e.get("id")}
        # An id-less malformed record can't be correlated to the prior task it
        # represents, so we cannot conclude any disappeared task is done.
        idless = any(not e.get("id") for e in all_bad)
        changes = detect_changes(prev_tasks, all_good, bad_ids)
        quarantined = save_quarantine(quarantine_path, load_quarantine(quarantine_path), all_bad)

        # Best-effort: inbound, non-status work lines from the last hour that no
        # open task title seems to cover. Content-matched (not a count) so an
        # in-sync board reports 0 and unrelated task churn never masks a real gap.
        candidates = scan_gateway_log(read_gateway_log_tail(gateway_log), datetime.now(UTC))
        untracked = count_untracked(candidates, all_good)

        # Carry forward known-but-malformed tasks so a transient bad record never
        # drops them from tracking (and they stay out of done detection). When an
        # id-less malformed record is present, fail closed: suppress done entirely
        # and preserve every prior task so none is falsely completed or dropped.
        carry = set(bad_ids)
        if idless:
            changes["done"] = []
            carry |= set(prev_tasks)
        new_snapshot = snapshot_tasks(all_good)
        for tid in carry:
            if tid in prev_tasks and tid not in new_snapshot:
                new_snapshot[tid] = prev_tasks[tid]
        state["tasks"] = new_snapshot
        state["last_run"] = now_iso()
        save_state(state_path, state)

        sitrep = build_sitrep(profile, projects, open_total, changes,
                              untracked, quarantined, False)
        if idless:
            sitrep["idless_quarantine"] = True  # done-detection suppressed this run
        print(json.dumps(sitrep, separators=(",", ":")))

        if args.verbose:
            for t in sorted(all_good, key=lambda x: str(x["id"])):
                tid = str(t["id"])
                tag = ("NEW" if tid in changes["new"]
                       else "UPD" if tid in changes["updated"] else "open")
                print(f"  [{tag}] #{tid} {t.get('title', '')[:80]}", file=sys.stderr)
            for entry in all_bad:
                print(f"  [QUARANTINE] #{entry['id']}: {entry['reason']}", file=sys.stderr)
            for c in candidates:
                print(f"  [UNTRACKED?] {c}", file=sys.stderr)
        return 0
    finally:
        release_lock(lock_dir)


if __name__ == "__main__":
    raise SystemExit(main())

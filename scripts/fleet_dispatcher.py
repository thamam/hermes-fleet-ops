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
# Lines that look like a status check, not new work — never an untracked candidate.
STATUS_RE = re.compile(
    r"\b(status|ping|health|uptime|are you (up|there|alive)|you ok|how are you|sitrep)\b",
    re.IGNORECASE,
)
INBOUND_RE = re.compile(r"\b(inbound|incoming|message from|received|msg<-|<-)\b", re.IGNORECASE)
TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})")


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


def load_state(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path: Path, state: dict) -> None:
    _atomic_write_json(path, state)


def load_quarantine(path: Path) -> list:
    data = load_state(path)
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
def acquire_lock(lock_dir: Path) -> bool:
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_dir.mkdir()
        return True
    except FileExistsError:
        try:
            age = time.time() - lock_dir.stat().st_mtime
        except FileNotFoundError:
            age = STALE_LOCK_SEC + 1
        if age > STALE_LOCK_SEC:
            try:
                lock_dir.rmdir()
                lock_dir.mkdir()
                return True
            except OSError:
                return False
        return False


def release_lock(lock_dir: Path) -> None:
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
        if not isinstance(batch, list) or not batch:
            break
        tasks.extend(t for t in batch if isinstance(t, dict) and not t.get("done"))
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
        tid = t.get("id") if isinstance(t, dict) else None
        if not tid:
            bad.append({"id": tid, "reason": "missing/null id", "ts": now_iso()})
            continue
        due = t.get("due_date")
        if due and due != NO_DUE_SENTINEL:
            try:
                _parse_due(due)
            except (ValueError, TypeError):
                bad.append({"id": tid, "reason": f"unparseable due_date: {due!r}",
                            "ts": now_iso()})
                continue
        good.append(t)
    return good, bad


def detect_changes(prev_tasks: dict, current_good: list) -> dict:
    """Diff prev run's open-task snapshot against this run's open tasks."""
    cur = {str(t["id"]): t for t in current_good}
    prev_ids, cur_ids = set(prev_tasks), set(cur)
    new = sorted(cur_ids - prev_ids)
    # Was open last run, no longer in the open set -> marked done (best-effort;
    # deletion looks the same and is reported as done too).
    done = sorted(prev_ids - cur_ids)
    updated = []
    for tid in cur_ids & prev_ids:
        prev_u, cur_u = prev_tasks[tid].get("updated", ""), cur[tid].get("updated", "")
        if cur_u and cur_u > prev_u:  # RFC3339 Z strings sort chronologically
            updated.append(tid)
    return {"new": new, "done": done, "updated": sorted(updated)}


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
def scan_gateway_log(text: str, now: datetime, window_sec: int = GATEWAY_WINDOW_SEC) -> list:
    """Inbound, work-triggering lines from the last `window_sec`. Status-check
    chatter is excluded so it never false-positives as untracked work."""
    cutoff = now - timedelta(seconds=window_sec)
    hits = []
    for line in text.splitlines():
        if not INBOUND_RE.search(line) or STATUS_RE.search(line):
            continue
        m = TS_RE.search(line)
        if m:
            try:
                ts = datetime.fromisoformat(m.group(1).replace(" ", "T")).replace(tzinfo=UTC)
            except ValueError:
                ts = None
            if ts and ts < cutoff:
                continue
        hits.append(line.strip()[:200])
    return hits


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


def _env_projects() -> list:
    raw = os.environ.get("FLEET_DISPATCHER_PROJECT_IDS", "").strip()
    return [int(p) for p in raw.split(",") if p.strip()]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="emit a per-task summary too")
    args = parser.parse_args(argv)

    profile = os.environ.get("HERMES_PROFILE_NAME", "unknown")
    hermes_home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    token = os.environ.get("VIKUNJA_API_TOKEN", "")
    base_url = os.environ.get("VIKUNJA_API_URL", "")
    projects = _env_projects()

    state_dir = Path(os.environ.get("FLEET_DISPATCHER_STATE_DIR",
                                    str(Path(hermes_home) / "state" / "fleet_dispatcher")))
    gateway_log = Path(os.environ.get("FLEET_DISPATCHER_GATEWAY_LOG",
                                      str(Path(hermes_home) / "logs" / "gateway.log")))
    state_path = state_dir / "state.json"
    quarantine_path = state_dir / "quarantined.json"
    lock_dir = state_dir / ".lock"

    if not acquire_lock(lock_dir):
        return 0  # another run holds the lock; let it do the work
    try:
        state = load_state(state_path)
        prev_tasks = state.get("tasks", {})

        all_good, all_bad, open_total = [], [], 0
        vik_unreachable = False
        try:
            for pid in projects:
                raw = fetch_open_tasks(base_url, token, pid)
                good, bad = validate_tasks(raw)
                all_good.extend(good)
                all_bad.extend(bad)
            open_total = len(all_good)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
                json.JSONDecodeError, ValueError):
            # Network/parse failure: don't fail the cron. Report and retry next tick.
            vik_unreachable = True

        if vik_unreachable:
            sitrep = build_sitrep(profile, projects, len(prev_tasks),
                                  {"new": [], "done": [], "updated": []}, 0, 0, True)
            print(json.dumps(sitrep, separators=(",", ":")))
            return 0

        changes = detect_changes(prev_tasks, all_good)
        quarantined = save_quarantine(quarantine_path, load_quarantine(quarantine_path), all_bad)

        candidates = scan_gateway_log(read_gateway_log_tail(gateway_log), datetime.now(UTC))
        # Treat work covered by tasks that moved this tick as already tracked.
        tracked = len(changes["new"]) + len(changes["updated"])
        untracked = max(0, len(candidates) - tracked)

        state["tasks"] = snapshot_tasks(all_good)
        state["last_run"] = now_iso()
        save_state(state_path, state)

        sitrep = build_sitrep(profile, projects, open_total, changes,
                              untracked, quarantined, False)
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

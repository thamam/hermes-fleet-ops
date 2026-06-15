#!/usr/bin/env python3
"""validate-config.py — merge gate for thamam/hermes-fleet-ops.

Checks:
  1. fleet.yaml schema (presence + types of required fields, exactly one canary,
     channels_to_test entries are in the known_adapters list, lanes are valid).
  2. stakeholders.yaml schema (channels structure, levels are in known set).
  3. patches/*.patch are valid git-mailbox files AND can be applied cleanly
     onto current upstream/main (NousResearch/hermes-agent) via `git am --3way`.

Exit codes:
  0  — all checks pass
  1  — one or more schema checks failed
  2  — one or more patch dry-runs failed
  3  — environment / unexpected error (git missing, repo unreachable, etc.)

Usage:
  python3 scripts/validate-config.py [--repo-root <path>] [--skip-patch-dry-run]

  Defaults to assuming the script runs from the ops-repo root.
  --skip-patch-dry-run is for local fast checks; CI must NOT pass it.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import yaml  # PyYAML
except ImportError:
    print("ERROR: PyYAML is required. `pip install pyyaml`.", file=sys.stderr)
    sys.exit(3)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

UPSTREAM_CLONE_URL = "https://github.com/NousResearch/hermes-agent.git"
UPSTREAM_BRANCH = "main"

VALID_LANES = {"fleet", "fleet-stable"}
VALID_LEVELS = {"heads-up", "completion", "breaking"}
VALID_SYSTEMD_SCOPES = {"system", "user", "launchd"}


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

class SchemaError(Exception):
    pass


def _require(d: dict, key: str, typ, ctx: str):
    if key not in d:
        raise SchemaError(f"{ctx}: missing required field '{key}'")
    if not isinstance(d[key], typ):
        raise SchemaError(
            f"{ctx}: field '{key}' must be {typ.__name__}, "
            f"got {type(d[key]).__name__}"
        )


def validate_fleet(path: Path) -> list[str]:
    errs: list[str] = []
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        return [f"{path}: YAML parse error: {e}"]

    if not isinstance(data, dict):
        return [f"{path}: top level must be a mapping"]

    agents = data.get("agents")
    if not isinstance(agents, list) or not agents:
        errs.append(f"{path}: 'agents' must be a non-empty list")
        return errs

    known_adapters = set(data.get("known_adapters") or [])
    if not known_adapters:
        errs.append(f"{path}: 'known_adapters' missing or empty")

    seen_ids: set[str] = set()
    canary_count = 0
    for i, agent in enumerate(agents):
        ctx = f"{path}: agents[{i}]"
        if not isinstance(agent, dict):
            errs.append(f"{ctx}: must be a mapping"); continue
        try:
            _require(agent, "id", str, ctx)
            _require(agent, "hostname", str, ctx)
            _require(agent, "ssh_user", str, ctx)
            _require(agent, "hermes_home", str, ctx)
            _require(agent, "lane", str, ctx)
            _require(agent, "canary", bool, ctx)
            _require(agent, "soak_hours", int, ctx)
            _require(agent, "channels_to_test", list, ctx)
            _require(agent, "role", str, ctx)
        except SchemaError as e:
            errs.append(str(e)); continue

        if agent["id"] in seen_ids:
            errs.append(f"{ctx}: duplicate id '{agent['id']}'")
        seen_ids.add(agent["id"])

        if agent["lane"] not in VALID_LANES:
            errs.append(f"{ctx}: lane '{agent['lane']}' not in {sorted(VALID_LANES)}")
        if agent["soak_hours"] <= 0:
            errs.append(f"{ctx}: soak_hours must be > 0")
        if agent["canary"]:
            canary_count += 1

        scope = agent.get("systemd_scope")
        if scope is not None and scope not in VALID_SYSTEMD_SCOPES:
            errs.append(
                f"{ctx}: systemd_scope '{scope}' not in {sorted(VALID_SYSTEMD_SCOPES)}"
            )

        for j, ch in enumerate(agent["channels_to_test"]):
            if not isinstance(ch, str):
                errs.append(f"{ctx}: channels_to_test[{j}] must be string")
            elif known_adapters and ch not in known_adapters:
                errs.append(
                    f"{ctx}: channels_to_test[{j}]='{ch}' not in known_adapters "
                    f"{sorted(known_adapters)}"
                )

    if canary_count != 1:
        errs.append(f"{path}: must have exactly one agent with canary=true, found {canary_count}")

    return errs


def validate_stakeholders(path: Path) -> list[str]:
    errs: list[str] = []
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        return [f"{path}: YAML parse error: {e}"]

    if not isinstance(data, dict):
        return [f"{path}: top level must be a mapping"]
    stakeholders = data.get("stakeholders")
    if not isinstance(stakeholders, list) or not stakeholders:
        errs.append(f"{path}: 'stakeholders' must be a non-empty list")
        return errs

    seen_ids: set[str] = set()
    for i, sh in enumerate(stakeholders):
        ctx = f"{path}: stakeholders[{i}]"
        if not isinstance(sh, dict):
            errs.append(f"{ctx}: must be a mapping"); continue
        try:
            _require(sh, "id", str, ctx)
            _require(sh, "channels", list, ctx)
            _require(sh, "levels", list, ctx)
        except SchemaError as e:
            errs.append(str(e)); continue

        if sh["id"] in seen_ids:
            errs.append(f"{ctx}: duplicate id '{sh['id']}'")
        seen_ids.add(sh["id"])

        if not sh["channels"]:
            errs.append(f"{ctx}: 'channels' must be non-empty")
        for j, ch in enumerate(sh["channels"]):
            if not isinstance(ch, dict) or len(ch) != 1:
                errs.append(
                    f"{ctx}: channels[{j}] must be a single-key mapping "
                    f"(e.g. {{email: foo@bar}} or {{slack: {{channel: '#x'}}}})"
                )

        for j, lvl in enumerate(sh["levels"]):
            if lvl not in VALID_LEVELS:
                errs.append(f"{ctx}: levels[{j}]='{lvl}' not in {sorted(VALID_LEVELS)}")
    return errs


# ---------------------------------------------------------------------------
# Patch validation
# ---------------------------------------------------------------------------

def _check_patch_mbox(patch: Path) -> list[str]:
    """Quick sanity: file is a non-empty mbox-style git patch."""
    try:
        text = patch.read_text()
    except OSError as e:
        return [f"{patch}: cannot read ({e})"]
    if not text.strip():
        return [f"{patch}: empty file"]
    if "From " not in text or "Subject:" not in text:
        return [f"{patch}: not a valid git-format-patch mbox (missing From/Subject)"]
    if "\ndiff --git " not in text and "\n--- " not in text:
        return [f"{patch}: no diff block found"]
    return []


def dry_run_patches(repo_root: Path) -> list[str]:
    """Clone upstream main into a tmpdir and run `git am --3way` for each
    patch in order. Abort + clean up regardless of outcome."""
    errs: list[str] = []
    patches_dir = repo_root / "patches"
    if not patches_dir.is_dir():
        return ["patches/ directory missing from ops repo"]
    patches = sorted(patches_dir.glob("*.patch"))
    if not patches:
        # An empty patch series is valid (means upstream subsumed everything).
        return []

    # Structural sanity first — these we can do without a clone.
    for p in patches:
        errs.extend(_check_patch_mbox(p))

    if shutil.which("git") is None:
        return errs + ["ERROR: git not on PATH; cannot dry-run patches"]

    with tempfile.TemporaryDirectory(prefix="hermes-am-dry-") as td:
        clone_dir = Path(td) / "hermes-agent"
        clone = subprocess.run(
            ["git", "clone", "--depth", "200", "--branch", UPSTREAM_BRANCH,
             UPSTREAM_CLONE_URL, str(clone_dir)],
            capture_output=True, text=True,
        )
        if clone.returncode != 0:
            return errs + [
                f"ERROR: failed to clone {UPSTREAM_CLONE_URL}@{UPSTREAM_BRANCH}: "
                f"{clone.stderr.strip()}"
            ]

        # `git am` records an author/committer; with no global identity the
        # subprocess fails with "Committer identity unknown". Set a local
        # identity scoped to the temp clone — never touches the caller's config.
        for k, v in (("user.email", "validator@hermes-fleet-ops.invalid"),
                     ("user.name",  "hermes-fleet-validator")):
            subprocess.run(["git", "config", k, v], cwd=str(clone_dir),
                           capture_output=True, text=True)

        for p in patches:
            am = subprocess.run(
                ["git", "am", "--3way", str(p)],
                cwd=str(clone_dir), capture_output=True, text=True,
            )
            if am.returncode != 0:
                errs.append(
                    f"{p.name}: git am --3way failed against upstream/{UPSTREAM_BRANCH}\n"
                    f"  stdout: {am.stdout.strip()[:500]}\n"
                    f"  stderr: {am.stderr.strip()[:500]}"
                )
                # Abort the partial apply so the next patch starts clean — but
                # since the clone is per-invocation, this is mostly defensive.
                subprocess.run(["git", "am", "--abort"], cwd=str(clone_dir),
                               capture_output=True, text=True)
                # Stop on first failure: the routine's behavior is to halt on
                # the failing patch (v2 §3.4 step 5).
                break

    return errs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", default=".", help="ops-repo root (default: cwd)")
    ap.add_argument("--skip-patch-dry-run", action="store_true",
                    help="Skip the patch dry-run against upstream (CI MUST NOT pass this)")
    args = ap.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    fleet_yaml = repo_root / "fleet.yaml"
    stakeholders_yaml = repo_root / "stakeholders.yaml"

    if not fleet_yaml.is_file():
        print(f"FAIL: {fleet_yaml} missing", file=sys.stderr); return 1
    if not stakeholders_yaml.is_file():
        print(f"FAIL: {stakeholders_yaml} missing", file=sys.stderr); return 1

    schema_errs: list[str] = []
    schema_errs.extend(validate_fleet(fleet_yaml))
    schema_errs.extend(validate_stakeholders(stakeholders_yaml))

    if schema_errs:
        print("Schema validation FAILED:", file=sys.stderr)
        for e in schema_errs:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print("OK schema: fleet.yaml + stakeholders.yaml")

    if args.skip_patch_dry_run:
        print("SKIP patch dry-run (--skip-patch-dry-run set)")
        return 0

    patch_errs = dry_run_patches(repo_root)
    if patch_errs:
        print("Patch dry-run FAILED:", file=sys.stderr)
        for e in patch_errs:
            print(f"  - {e}", file=sys.stderr)
        return 2
    print("OK patches: all apply cleanly onto upstream/main")
    return 0


if __name__ == "__main__":
    sys.exit(main())

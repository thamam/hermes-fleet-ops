# Hermes-fleet rebase routine — RUNBOOK

Canonical operator procedure. Reference doc, not a script. Last edit: 2026-06-14.

---

## 0. Glossary

| Term              | Meaning                                                            |
|-------------------|--------------------------------------------------------------------|
| Upstream          | `NousResearch/hermes-agent`                                        |
| Fork              | `thamam/hermes-agent`                                          |
| Ops repo          | `thamam/hermes-fleet-ops` (this repo)                          |
| Fleet branch      | `fork:fleet` — what agents pull                                    |
| Rollback ref      | `fork:fleet-previous` — prior cycle's fleet HEAD                   |
| Canary            | Agent in `fleet.yaml` with `canary: true` (currently `nano2`)      |
| Soak              | 24h observation window between deploy waves                        |

---

## 1. Normal weekly cycle

### 1.1 Sunday 20:30 IDT — cron fires (no action)

`rebase.yml` runs in GHA. ~30–60 min. On success, a draft Release-notes PR
appears on the ops repo with `needs-canary-approval` label.

### 1.2 Monday morning — review + canary approval

1. Open the draft PR. Read the TL;DR, upstream changes, patch-series state.
2. If sane: add the `approval/start-canary` label to the PR.
3. `deploy.sh` picks it up within 5 min, SSHes to `nano2`, deploys, comments
   `:white_check_mark: canary deploy to nano2 succeeded` on the PR and adds
   the `canary/healthy` label.
4. If failed: PR gets `canary/failed`, Doc gets an email. Go to §3.

### 1.3 Tuesday evening — rollout approval

After ≥24h since `canary/healthy` was applied:

1. Verify nano2 still looks healthy (`hermes status`, recent logs).
2. Add the `approval/rollout` label to the PR.
3. `deploy.sh` deploys to mbot, yunes, sentinel in parallel.
4. On success: `rollout/healthy` label, PR comment.

### 1.4 Wednesday evening — release notes fire (no action)

Email lands at `tomer@neuronbox.ai`. PR is merged (or merge it manually if
the auto-merge step isn't wired yet — TODO).

---

## 2. Manual / off-cycle run

Need to test the routine or push an out-of-cadence rebase?

```bash
# From any machine with `gh` auth on tomerhamam:
gh workflow run rebase.yml --repo thamam/hermes-fleet-ops

# Watch:
gh run watch --repo thamam/hermes-fleet-ops
```

A successful run produces the same draft Release-notes PR as the cron path.
Approval gates work identically.

For a **CI-only dry run** (rebase + tests, no force-push, no PR):

```bash
gh workflow run rebase.yml --repo thamam/hermes-fleet-ops -f dry_run=true
```

---

## 3. Failure modes

### 3.1 CI rebase fails (patch doesn't apply)

The workflow stops on the first failing `git am`. Open the failing run, copy
the patch name and the reject hunk. Options:

- **Patch needs rebase.** Pull the patch locally, apply against current
  upstream/main, resolve, re-export with `git format-patch`, PR the new patch
  into the ops repo. Re-run `rebase.yml`.
- **Patch is no longer needed.** Add `# Status: candidate-for-upstream-drop`
  + `# Tests-that-must-pass-without-patch:` headers if missing, then PR
  removal of the patch file.
- **Upstream broke something else.** File an upstream issue, skip the cycle.

### 3.2 CI tests fail on the rebased tip

Workflow halts before push. Inspect logs. Common causes: new upstream test
needs an env var the runner doesn't have, or our patch genuinely regresses
something. Fix in a follow-up patch or escalate.

### 3.3 Canary deploy fails

`canary/failed` label is added; email lands. Promotes are halted. Choose:

- **Auto-revert.** Run:
  ```bash
  ssh thh3@<nano2-host> "cd /home/thh3/.hermes-nano2/hermes-agent && \
      git fetch origin && git reset --hard origin/fleet-previous && \
      hermes update && systemctl --user restart hermes-gateway-nano2.service"
  ```
  Then `gh pr edit <PR#> --repo thamam/hermes-fleet-ops --remove-label approval/start-canary --add-label reverted`.
- **Hold for inspection.** Do nothing in the routine. Investigate by hand.
  The snapshot file under `/var/log/hermes-rebase/YYYY-MM-DD.snapshot.txt`
  on the host has the prior SHA + state hashes.

### 3.4 Rollout deploy fails (≥1 of mbot/yunes/sentinel)

Same options as §3.3 but per failing host. The healthy hosts in the parallel
wave stay on the new fleet — `fleet-previous` rollback is per-host, not
fleet-wide.

### 3.5 deploy.sh stuck / dead

```bash
# Check launchd
launchctl print gui/$(id -u)/com.neuronbox.hermes-fleet-deploy | less

# Tail logs
tail -f ~/.hermes-fleet-deploy/log/deploy.log

# Manual run (bypass launchd)
bash ~/personal/projects/claw/hermes-fleet-deploy/deploy.sh

# Force-restart the launchd job
launchctl kickstart -k gui/$(id -u)/com.neuronbox.hermes-fleet-deploy
```

If the script's lock dir is stale: `rmdir ~/.hermes-fleet-deploy/state/.lock`.

---

## 4. Adding a new agent

1. Tailscale-add the host. Confirm `ssh` works from Doc's Mac.
2. Install hermes-agent on the host pinned at `origin/fleet` of the fork:
   ```bash
   git clone https://github.com/thamam/hermes-agent <hermes_home>
   cd <hermes_home> && git checkout fleet && pip install -e .
   ```
3. PR an entry to `fleet.yaml`. Reuse one of the existing records as a
   template. Set `canary: false`. CI validates.
4. Merge. Next cycle includes the new host automatically.

To make the new host the canary: in the same PR, also flip the existing
canary's `canary: true → false`. The validator enforces exactly one canary.

---

## 5. Rolling back the routine itself

If the routine misbehaves and you want it out of the loop entirely:

1. **Stop launchd.** `launchctl bootout gui/$(id -u)/com.neuronbox.hermes-fleet-deploy`
2. **Disable the cron.** PR `.github/workflows/rebase.yml` to remove the
   `schedule:` block (or just comment it out).
3. Hosts keep running whatever they last had. Pin each host to a specific
   SHA by hand if you want to freeze:
   ```bash
   ssh <user>@<host> "cd <hermes_home> && git checkout <known-good-sha>"
   ```

The fork's `fleet-previous` ref is the easy revert target across the whole
fleet.

---

## 6. Where things live

| Thing                     | Where                                                                |
|---------------------------|----------------------------------------------------------------------|
| Fleet config              | `thamam/hermes-fleet-ops:main/fleet.yaml`                        |
| Stakeholder routing       | `thamam/hermes-fleet-ops:main/stakeholders.yaml`                 |
| Patch series              | `thamam/hermes-fleet-ops:main/patches/`                          |
| CI workflows              | `thamam/hermes-fleet-ops:main/.github/workflows/`                |
| Deploy script             | Doc's Mac: `~/personal/projects/claw/hermes-fleet-deploy/deploy.sh`  |
| Launchd plist             | Doc's Mac: `~/Library/LaunchAgents/com.neuronbox.hermes-fleet-deploy.plist` |
| Deploy logs               | Doc's Mac: `~/.hermes-fleet-deploy/log/`                             |
| Deploy state markers      | Doc's Mac: `~/.hermes-fleet-deploy/state/`                           |
| Per-host pre-deploy snapshot | each host: `/var/log/hermes-rebase/YYYY-MM-DD.snapshot.txt`       |

---

*Update this file whenever the routine's behavior changes.*

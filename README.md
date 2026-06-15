# hermes-fleet-ops

Operations repo for the **Hermes-fleet rebase routine**.

This repo holds the configuration, validation, and automation that keeps the
Hermes-agent fleet on a shared personal fork (`thamam/hermes-agent`),
weekly-rebased against upstream (`NousResearch/hermes-agent`), and
progressively deployed to the fleet hosts.

**Spec / source of truth:** [`hermes-fleet-rebase-routine-2026-06-13-v2.md`](https://github.com/thamam/ClawHub/blob/main/hermes-fleet-rebase-routine-2026-06-13-v2.md)
in the ClawHub repo. Read that before changing anything in this repo.

## Layout

```
.
├── README.md                                 — this file
├── RUNBOOK.md                                — operator procedure for normal + abnormal cycles
├── fleet.yaml                                — agent inventory (lane, canary, hermes_home, channels)
├── stakeholders.yaml                         — notification routing (day 1: Doc only)
├── patches/                                  — git-format-patch series re-applied on each rebase
│   └── 0001-fix-gemini-route-openai-suffixed-base-url.patch
├── scripts/
│   ├── validate-config.py                    — merge-gate validator (schema + git am dry-run)
│   └── deploy.sh                             — launchd-driven deploy agent on Doc's Mac
├── launchd/
│   └── com.neuronbox.hermes-fleet-deploy.plist
└── .github/workflows/
    ├── rebase.yml                            — weekly rebase + draft Release-notes PR
    └── validate.yml                          — PR merge gate
```

## How a cycle flows

```
Sun 20:30 IDT (cron)        →  rebase.yml fires
  └─ fetch upstream, rebase fleet, run tests, push, open draft Release-notes PR

Mon morning                 →  Doc labels PR `approval/start-canary`
  └─ deploy.sh (launchd, polled every 5min) SSH-deploys to nano2

24h soak                    →  …

Tue evening                 →  Doc labels PR `approval/rollout`
  └─ deploy.sh deploys to mbot, yunes, sentinel in parallel

24h soak                    →  release-notes email fires; PR merged
```

Failure at any host: deploy.sh halts further promotes, emails Doc, awaits
manual decision (auto-revert via `fleet-previous` or hold for inspection).

## Adding / removing an agent

1. PR an edit to `fleet.yaml`.
2. CI (`validate.yml`) runs `scripts/validate-config.py`; PR can't merge if
   schema is broken or any patch fails to apply against upstream/main.
3. Merge. Next cycle picks it up automatically.

## Changing the patch series

1. PR adding/removing a file under `patches/`.
2. CI dry-runs `git am --3way` for the full series.
3. Merge. Next cycle re-applies the new series; `candidate-for-upstream-drop`
   patches are auto-tested against upstream and removed if green.

## Secrets

Exactly one secret needed in this repo's GitHub settings:

| Name              | Type                       | Scope                                                   |
|-------------------|----------------------------|---------------------------------------------------------|
| `FORK_PUSH_TOKEN` | Fine-grained PAT           | `thamam/hermes-agent` → contents:write              |

Nothing else: deploy-side credentials live on Doc's Mac in his ssh config and
his `gh` auth token; they never enter CI.

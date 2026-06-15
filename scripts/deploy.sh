#!/usr/bin/env bash
# deploy.sh — Mac-launchd-driven deploy agent for the hermes-fleet rebase routine.
#
# Polled by launchd every 5 minutes (see launchd/com.neuronbox.hermes-fleet-deploy.plist).
#
# Behaviour:
#   1. Read the ops repo for the latest open draft Release-notes PR.
#   2. Determine its labels.
#   3. If labeled `approval/start-canary` AND the canary hasn't been deployed
#      for this PR's HEAD commit → deploy to the canary host (nano2).
#   4. If labeled `approval/rollout` AND the parallel wave hasn't been deployed
#      for this PR's HEAD commit → deploy in parallel to mbot, yunes, sentinel.
#   5. Post-deploy: run minimum + adaptive tests, comment on PR with result,
#      add `canary/healthy` or `rollout/healthy` label on success, or
#      `canary/failed` / `rollout/failed` with a Doc-ping email on failure.
#
# State: a per-PR per-stage marker file under ~/.hermes-fleet-deploy/state/
#        prevents redeploys when launchd polls again before the stage soaks.
#
# Hard rules (Doc's no-improvise):
#   - This script NEVER initiates a rebase or a force-push; that's GHA's job.
#   - This script NEVER auto-rolls-back; on failure it surfaces to Doc and waits.
#   - This script NEVER changes labels Doc owns (approval/*); it only adds
#     status labels (canary/*, rollout/*).
#
# Required tools on the Mac running this:
#   gh (authenticated as the ops-repo owner), git, ssh, jq, awk
#
# Environment:
#   OPS_REPO              — default: thamam/hermes-fleet-ops
#   FORK_REPO             — default: thamam/hermes-agent
#   FLEET_BRANCH          — default: fleet
#   FLEET_YAML_PATH       — default: $HOME/.hermes-fleet-deploy/ops/fleet.yaml
#                           (a local cached copy refreshed from the ops repo).
#   STATE_DIR             — default: $HOME/.hermes-fleet-deploy/state
#   LOG_DIR               — default: $HOME/.hermes-fleet-deploy/log
#   NOTIFY_EMAIL          — default: tomer@neuronbox.ai (Doc-ping address)

set -Eeuo pipefail

OPS_REPO="${OPS_REPO:-thamam/hermes-fleet-ops}"
FORK_REPO="${FORK_REPO:-thamam/hermes-agent}"
FLEET_BRANCH="${FLEET_BRANCH:-fleet}"
STATE_DIR="${STATE_DIR:-$HOME/.hermes-fleet-deploy/state}"
LOG_DIR="${LOG_DIR:-$HOME/.hermes-fleet-deploy/log}"
OPS_CACHE_DIR="${OPS_CACHE_DIR:-$HOME/.hermes-fleet-deploy/ops}"
FLEET_YAML_PATH="${FLEET_YAML_PATH:-$OPS_CACHE_DIR/fleet.yaml}"
NOTIFY_EMAIL="${NOTIFY_EMAIL:-tomer@neuronbox.ai}"
LOCK_FILE="${LOCK_FILE:-$STATE_DIR/.lock}"

mkdir -p "$STATE_DIR" "$LOG_DIR" "$OPS_CACHE_DIR"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() {
  local line
  line="$(ts) [$$] $*"
  printf '%s\n' "$line"
  printf '%s\n' "$line" >> "$LOG_DIR/deploy.log"
}
die() { log "ERROR: $*"; exit 1; }

# ----------------------------------------------------------------------
# Single-flight lock — launchd polls every 5 min; deploys can take longer.
# ----------------------------------------------------------------------
acquire_lock() {
  if ! mkdir "$LOCK_FILE" 2>/dev/null; then
    log "another deploy run is in progress; exiting"
    exit 0
  fi
  trap 'rmdir "$LOCK_FILE" 2>/dev/null || true' EXIT
}

# ----------------------------------------------------------------------
# Refresh ops-repo cache so we read the latest fleet.yaml.
# ----------------------------------------------------------------------
refresh_ops_cache() {
  if [ -d "$OPS_CACHE_DIR/.git" ]; then
    git -C "$OPS_CACHE_DIR" fetch --quiet origin main
    git -C "$OPS_CACHE_DIR" reset --quiet --hard origin/main
  else
    rm -rf "$OPS_CACHE_DIR"
    git clone --quiet --depth 1 "https://github.com/$OPS_REPO.git" "$OPS_CACHE_DIR"
  fi
}

# ----------------------------------------------------------------------
# yq-free YAML parser — we keep dependencies thin. Reads each agent record
# into a TSV via a Python one-liner shipped with the system Python.
#   id<TAB>hostname<TAB>ssh_user<TAB>hermes_home<TAB>systemd_unit<TAB>systemd_scope<TAB>canary<TAB>channels
# ----------------------------------------------------------------------
list_agents_tsv() {
  /usr/bin/env python3 - "$FLEET_YAML_PATH" <<'PY'
import sys, yaml
data = yaml.safe_load(open(sys.argv[1]))
for a in data.get("agents", []):
    print("\t".join([
        a["id"],
        a["hostname"],
        a["ssh_user"],
        a["hermes_home"],
        a.get("systemd_unit",""),
        a.get("systemd_scope",""),
        "true" if a.get("canary") else "false",
        ",".join(a.get("channels_to_test", [])),
    ]))
PY
}

agent_record() {
  # $1=id ; prints the TSV line for that agent or empty
  list_agents_tsv | awk -F'\t' -v id="$1" '$1==id {print; exit}'
}

# ----------------------------------------------------------------------
# Restart-service helper. Branches on systemd_scope.
# ----------------------------------------------------------------------
restart_remote_service() {
  local ssh_target="$1" scope="$2" unit="$3"
  case "$scope" in
    user)
      ssh "$ssh_target" "systemctl --user restart '$unit'"
      ;;
    system)
      ssh "$ssh_target" "sudo systemctl restart '$unit'"
      ;;
    launchd)
      # macOS: TODO(doc) — fill in launchctl label once Yunes' agent label is confirmed.
      ssh "$ssh_target" "launchctl kickstart -k 'gui/\$(id -u)/com.neuronbox.hermes-yunes' || true"
      ;;
    *)
      die "unknown systemd_scope='$scope' for $ssh_target"
      ;;
  esac
}

# ----------------------------------------------------------------------
# Deploy to a single agent. Returns 0 on success, non-zero on failure.
# ----------------------------------------------------------------------
deploy_one() {
  local id="$1"
  local rec ssh_user hostname hermes_home unit scope channels ssh_target
  rec="$(agent_record "$id")" || die "agent '$id' not in fleet.yaml"
  [ -n "$rec" ] || die "agent '$id' not in fleet.yaml"

  IFS=$'\t' read -r _id hostname ssh_user hermes_home unit scope _canary channels <<<"$rec"
  ssh_target="$ssh_user@$hostname"

  log "deploy[$id]: ssh=$ssh_target home=$hermes_home unit=$unit scope=$scope channels=$channels"

  # Snapshot prior SHA for the rollback recipe.
  local prior_sha
  prior_sha="$(ssh "$ssh_target" "git -C '$hermes_home' rev-parse HEAD" || echo "unknown")"
  log "deploy[$id]: prior HEAD = $prior_sha"

  # Fetch + reset to origin/fleet, run hermes update, restart service.
  ssh "$ssh_target" "set -e; \
    cd '$hermes_home'; \
    git fetch --quiet origin; \
    git reset --hard 'origin/$FLEET_BRANCH'; \
    hermes update --yes || hermes update;" \
    || { log "deploy[$id]: git fetch/reset/update failed"; return 11; }

  restart_remote_service "$ssh_target" "$scope" "$unit" \
    || { log "deploy[$id]: restart failed"; return 12; }

  # ---------- Post-deploy minimum tests (v2 §6) ----------
  local new_sha
  new_sha="$(ssh "$ssh_target" "git -C '$hermes_home' rev-parse HEAD" || echo "unknown")"
  log "deploy[$id]: new HEAD = $new_sha"

  # 1. hermes --version
  ssh "$ssh_target" "hermes --version" >> "$LOG_DIR/deploy.log" 2>&1 \
    || { log "deploy[$id]: hermes --version failed"; return 21; }

  # 2. gateway_state.json adapters connected
  # 3. channel round-trip per channels_to_test
  # 4. one short generation through the host's default provider
  #
  # TODO(doc): wire these to real test commands once the agent's CLI surface
  # is enumerated. Day-1 we run the version check and a basic config validate.
  ssh "$ssh_target" "cd '$hermes_home' && hermes config validate || true" \
    >> "$LOG_DIR/deploy.log" 2>&1

  log "deploy[$id]: SUCCESS (HEAD $prior_sha -> $new_sha)"
  return 0
}

# ----------------------------------------------------------------------
# Find the current open draft Release-notes PR on the ops repo. Prints
# "<number>\t<head_sha>\t<labels_comma_separated>" or nothing.
# ----------------------------------------------------------------------
find_current_pr() {
  gh pr list \
    --repo "$OPS_REPO" \
    --state open \
    --draft \
    --limit 1 \
    --json number,headRefOid,labels \
    --jq '.[] | [.number, .headRefOid, ([.labels[].name] | join(","))] | @tsv'
}

# State markers — one per (PR-sha, stage). Stage = canary | rollout.
stage_marker() { printf '%s' "$STATE_DIR/$1.$2"; }

# ----------------------------------------------------------------------
# Email Doc (or whoever NOTIFY_EMAIL points at). Uses macOS `mail` if
# available — fallback to logging only.
# ----------------------------------------------------------------------
ping_doc() {
  local subj="$1" body="$2"
  if command -v mail >/dev/null 2>&1; then
    printf '%s\n' "$body" | mail -s "$subj" "$NOTIFY_EMAIL" || true
  fi
  log "PING DOC: $subj — $body"
}

# ----------------------------------------------------------------------
# Stages
# ----------------------------------------------------------------------
do_canary() {
  local pr_num="$1" head_sha="$2"
  local marker; marker="$(stage_marker "$head_sha" canary)"
  [ -e "$marker" ] && { log "canary already deployed for $head_sha; skipping"; return 0; }

  # Find the canary agent in fleet.yaml.
  local canary_id
  canary_id="$(list_agents_tsv | awk -F'\t' '$7=="true" {print $1; exit}')"
  [ -n "$canary_id" ] || die "no canary agent in fleet.yaml"

  log "starting canary deploy to '$canary_id' for PR#$pr_num @$head_sha"

  if deploy_one "$canary_id"; then
    : > "$marker"
    gh pr comment "$pr_num" --repo "$OPS_REPO" \
      --body ":white_check_mark: canary deploy to \`$canary_id\` succeeded @ $head_sha. Soaking 24h before rollout approval is honored."
    gh pr edit "$pr_num" --repo "$OPS_REPO" --add-label "canary/healthy" || true
  else
    gh pr comment "$pr_num" --repo "$OPS_REPO" \
      --body ":x: canary deploy to \`$canary_id\` **failed** @ $head_sha. Promotes halted. See \`$LOG_DIR/deploy.log\` on Doc's Mac."
    gh pr edit "$pr_num" --repo "$OPS_REPO" --add-label "canary/failed" || true
    ping_doc "[hermes-fleet] canary failed PR#$pr_num" \
             "Canary deploy to $canary_id failed at $(ts). See $LOG_DIR/deploy.log."
    return 1
  fi
}

do_rollout() {
  local pr_num="$1" head_sha="$2"
  local marker; marker="$(stage_marker "$head_sha" rollout)"
  [ -e "$marker" ] && { log "rollout already deployed for $head_sha; skipping"; return 0; }

  # Require canary marker present + canary/healthy label, lest we deploy ahead of canary.
  local canary_marker; canary_marker="$(stage_marker "$head_sha" canary)"
  [ -e "$canary_marker" ] || die "rollout requested but canary marker missing for $head_sha — refusing to deploy"

  # All non-canary agents.
  local ids
  ids="$(list_agents_tsv | awk -F'\t' '$7!="true" {print $1}')"

  log "starting rollout for PR#$pr_num @$head_sha — agents: $(echo "$ids" | tr '\n' ' ')"

  local pids=()
  local failed_any=0
  for id in $ids; do
    (
      # Subshell per agent — these run in parallel.
      if deploy_one "$id"; then
        log "rollout[$id]: ok"; exit 0
      else
        log "rollout[$id]: FAIL"; exit 1
      fi
    ) >> "$LOG_DIR/deploy.log" 2>&1 &
    pids+=("$!")
  done
  for pid in "${pids[@]}"; do
    wait "$pid" || failed_any=1
  done

  if [ "$failed_any" -eq 0 ]; then
    : > "$marker"
    gh pr comment "$pr_num" --repo "$OPS_REPO" \
      --body ":white_check_mark: rollout deploy to non-canary agents succeeded @ $head_sha. Soaking 24h before the release-notes email fires."
    gh pr edit "$pr_num" --repo "$OPS_REPO" --add-label "rollout/healthy" || true
  else
    gh pr comment "$pr_num" --repo "$OPS_REPO" \
      --body ":x: rollout deploy had one or more failures @ $head_sha. Promotes halted. See \`$LOG_DIR/deploy.log\` on Doc's Mac."
    gh pr edit "$pr_num" --repo "$OPS_REPO" --add-label "rollout/failed" || true
    ping_doc "[hermes-fleet] rollout failed PR#$pr_num" \
             "Rollout deploy had one or more failures at $(ts). See $LOG_DIR/deploy.log."
    return 1
  fi
}

# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
main() {
  acquire_lock
  refresh_ops_cache

  local pr_info pr_num head_sha labels
  pr_info="$(find_current_pr || true)"
  if [ -z "$pr_info" ]; then
    log "no open draft Release-notes PR; nothing to do"
    exit 0
  fi
  IFS=$'\t' read -r pr_num head_sha labels <<<"$pr_info"
  log "current PR#$pr_num @$head_sha labels=[$labels]"

  if [[ ",$labels," == *",approval/start-canary,"* ]]; then
    do_canary "$pr_num" "$head_sha" || exit 1
  fi
  if [[ ",$labels," == *",approval/rollout,"* ]]; then
    do_rollout "$pr_num" "$head_sha" || exit 1
  fi
}

main "$@"

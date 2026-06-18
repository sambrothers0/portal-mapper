#!/usr/bin/env bash
#
# Linux port of grab-a1.ps1 — run this ON the always-free E2.1.Micro so it hunts
# the Ampere A1 (VM.Standard.A1.Flex) 24/7 without leaving your home PC on.
# Rotates all 3 ADs each sweep; stops on success or resource-limit; auto-backs-off
# on 429. Headless: no balloon popups — it logs, drops a SUCCESS marker file, and
# (optionally) pushes a phone notification via ntfy.
#
# Why this runs fine on the tiny micro: it's just `oci` CLI calls + sleeps. One
# launch attempt every ~3s, ~150 MB peak per CLI invocation — trivial on 1 GB.
# While it runs it keeps the micro busy (network), so the micro itself can't trip
# idle reclamation; once the A1 is up, move the backend + keepalive.py to the A1.
#
# Prereqs on the micro (see deploy/README.md):
#   1. oci CLI installed and on PATH
#   2. ~/.oci/config with an api_key profile named API_KEY (session auth needs a
#      browser, so it's not usable headless — api_key is required here)
#   3. your instance SSH pubkey at ~/.ssh/portal-mapper-oracle.pub
#
# Usage:   ./grab-a1.sh                 # foreground
#          systemctl enable --now grab-a1   # 24/7 via deploy/grab-a1.service
# Log:     ~/oci-a1.log   (also stdout / journalctl)
#
# Every tenancy/region value below can be overridden by an env var of the same
# name; the defaults match grab-a1.ps1 (same tenancy, us-ashburn-1).

set -uo pipefail

OCI="${OCI:-oci}"
AUTH_PROFILE="${AUTH_PROFILE:-API_KEY}"
COMPARTMENT="${COMPARTMENT:-ocid1.tenancy.oc1..aaaaaaaana5ot43k5qutlhxxwybavgy45snzgmisruhn4h7hatzlfret6zwq}"
SUBNET="${SUBNET:-ocid1.subnet.oc1.iad.aaaaaaaa2zgjdq6z5zp5vc7hompzpcafdxxs5sduxdrzkqfzcsdwdst6775a}"
IMAGE="${IMAGE:-ocid1.image.oc1.iad.aaaaaaaahpg6oykrd3js4ow727r4xleo6lryb4pmnexpi6cxpncff6kv2skq}"  # OL9 aarch64
SSH_PUB="${SSH_PUB:-$HOME/.ssh/portal-mapper-oracle.pub}"
DISPLAY_NAME="${DISPLAY_NAME:-portal-mapper}"

# All 3 Ashburn ADs (same tenancy prefix as grab-a1.ps1).
ADS=("fivK:US-ASHBURN-AD-1" "fivK:US-ASHBURN-AD-2" "fivK:US-ASHBURN-AD-3")

AD_PAUSE="${AD_PAUSE:-2}"        # seconds between individual AD launch calls
WAIT_SEC="${WAIT_SEC:-3}"        # seconds between full AD sweeps
# Throttle-backoff safety net: on 429 we slow down (30 -> 60 -> 120, cap 300s)
# and reset once the API clears, so a burst can't escalate into API abuse.
THROTTLE_BASE="${THROTTLE_BASE:-30}"
THROTTLE_CAP="${THROTTLE_CAP:-300}"
throttle_sleep="$THROTTLE_BASE"

LOG="${LOG:-$HOME/oci-a1.log}"
SUCCESS_FILE="${SUCCESS_FILE:-$HOME/oci-a1-SUCCESS.json}"
# Optional phone push on success: set NTFY_TOPIC (https://ntfy.sh, no signup) to
# get pinged when the A1 lands. Unset = silent (the marker file is always written).
NTFY_TOPIC="${NTFY_TOPIC:-}"

# As of 2026-06-15 the Always Free A1 allocation was cut from 4 OCPU / 24 GB to
# 2 OCPU / 12 GB total per tenancy. Requesting more now returns LimitExceeded.
SHAPE_OCPUS="${SHAPE_OCPUS:-2}"
SHAPE_MEM_GB="${SHAPE_MEM_GB:-12}"
shape_cfg="$(mktemp "${TMPDIR:-/tmp}/oci-shape-cfg.XXXXXX.json")"
printf '{"ocpus": %s, "memoryInGBs": %s}' "$SHAPE_OCPUS" "$SHAPE_MEM_GB" > "$shape_cfg"
trap 'rm -f "$shape_cfg"' EXIT

log() {
    local line
    line="$(date '+%Y-%m-%d %H:%M:%S')  $*"
    printf '%s\n' "$line" | tee -a "$LOG"
}

notify() {
    # $1 = title, $2 = body. Headless: optional ntfy push; always logged above.
    [ -n "$NTFY_TOPIC" ] || return 0
    curl -fsS -H "Title: $1" -d "$2" "https://ntfy.sh/${NTFY_TOPIC}" >/dev/null 2>&1 || true
}

log "=== A1 grab started - OL9 aarch64, ${SHAPE_OCPUS} OCPU / ${SHAPE_MEM_GB} GB, ~${AD_PAUSE}s/AD + ${WAIT_SEC}s/sweep, auto-backoff on throttle ==="

n=0       # full-sweep counter
tries=0   # individual launch-call counter
while true; do
    n=$((n + 1))
    for ad in "${ADS[@]}"; do
        tries=$((tries + 1))
        log "Sweep $n / try $tries - $ad"

        out="$("$OCI" compute instance launch \
            --compartment-id           "$COMPARTMENT" \
            --availability-domain      "$ad" \
            --shape                    "VM.Standard.A1.Flex" \
            --shape-config             "file://$shape_cfg" \
            --image-id                 "$IMAGE" \
            --subnet-id                "$SUBNET" \
            --ssh-authorized-keys-file "$SSH_PUB" \
            --display-name             "$DISPLAY_NAME" \
            --assign-public-ip         true \
            --auth                     api_key \
            --profile                  "$AUTH_PROFILE" \
            --output                   json 2>&1)"
        code=$?

        if [ "$code" -eq 0 ] && printf '%s' "$out" | grep -q '"id": *"ocid1\.instance'; then
            log "SUCCESS in $ad after $tries tries ($n sweeps)"
            log "$out"
            printf '%s\n' "$out" > "$SUCCESS_FILE"
            notify "OCI A1 is UP!" "Instance launched in $ad - open console. Saved to $SUCCESS_FILE"
            exit 0

        elif printf '%s' "$out" | grep -Eq 'NotAuthenticated|token.*expired|security token|401'; then
            log "AUTH FAILED - check ~/.oci/config api_key profile '$AUTH_PROFILE'"
            notify "OCI auth failed" "grab-a1 on the micro can't authenticate; fix ~/.oci/config."
            exit 2

        elif printf '%s' "$out" | grep -q 'LimitExceeded'; then
            log "RESOURCE LIMIT EXCEEDED - you may already have an A1 instance"
            notify "OCI limit exceeded" "Check your console - resource limit hit."
            exit 3

        elif printf '%s' "$out" | grep -Eq 'TooManyRequests|429|throttl'; then
            # OCI is rate-limiting us - back off (exponential, capped) and reset
            # the cadence so we don't keep poking an angry API.
            log "  THROTTLED (429) - backing off ${throttle_sleep}s"
            sleep "$throttle_sleep"
            throttle_sleep=$(( throttle_sleep * 2 ))
            [ "$throttle_sleep" -gt "$THROTTLE_CAP" ] && throttle_sleep="$THROTTLE_CAP"
            continue   # skip the normal inter-AD pause; backoff already covered it

        else
            # InternalError / out-of-capacity - expected, keep rotating.
            throttle_sleep="$THROTTLE_BASE"   # clean response: clear any backoff
            snippet="$(printf '%s' "$out" | tr -s '[:space:]' ' ' | sed 's/^ //;s/ $//')"
            [ "${#snippet}" -gt 120 ] && snippet="${snippet:0:120}..."
            log "  No capacity (exit $code): $snippet"
        fi

        sleep "$AD_PAUSE"   # brief pause between ADs
    done

    log "All ADs full - sleeping ${WAIT_SEC}s..."
    sleep "$WAIT_SEC"
done

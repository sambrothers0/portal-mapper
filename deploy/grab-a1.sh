#!/usr/bin/env bash
#
# Upgrade the Always-Free Ampere A1 from 1 OCPU / 6 GB to 2 OCPU / 12 GB
# IN PLACE. Runs ON the A1 box itself (this replaces the old micro-hosted
# launch grabber — there is no micro anymore).
#
# Why a resize, not a launch:
#   We already hold a 1-OCPU A1 (the always-on box, landed by grab-a1.ps1 from
#   your PC). The Always-Free A1 cap is 2 OCPU / 12 GB *total* per tenancy, so
#   LAUNCHing a second 2-OCPU instance would always return LimitExceeded
#   (1 + 2 = 3 > 2). Instead we resize THIS instance up to the full allowance.
#
# Why this is safe to retry 24/7 (won't lose you the box):
#   A flex-shape OCPU change is applied via an automatic reboot, and OCI checks
#   capacity UP FRONT. If 2-OCPU capacity isn't free, the `instance update` call
#   is rejected immediately and the box keeps running, untouched, at 1 OCPU — so
#   a failed attempt is just one rejected API call, no stop, no downtime. Only a
#   SUCCESSFUL attempt reboots the box (briefly) into 2 OCPU. The lone caveat:
#   if a resize were ever to leave the box stopped, this on-box script can't
#   restart it — start it once from the console and it self-heals from there.
#
# Lifecycle:
#   - We self-identify via the instance metadata endpoint (no hardcoded OCID).
#   - If we're already at >=2 OCPU, we're done (write SUCCESS marker, exit 0).
#   - On a successful resize the box reboots; the systemd unit re-runs us on
#     boot, we see 2 OCPU via metadata, and finish. Idempotent across reboots.
#
# Prereqs on the A1 box (see deploy/README.md):
#   1. oci CLI installed and on PATH
#   2. ~/.oci/config with an api_key profile named API_KEY (session auth needs a
#      browser, so it's not usable headless — api_key is required here)
#   3. an IAM policy letting this user manage the instance in its compartment
#
# Usage:   ./grab-a1.sh                 # foreground
#          systemctl enable --now grab-a1   # 24/7 via deploy/grab-a1.service
# Log:     ~/oci-a1.log   (also stdout / journalctl)
#
# Every value below can be overridden by an env var of the same name.

set -uo pipefail

OCI="${OCI:-oci}"
AUTH_PROFILE="${AUTH_PROFILE:-API_KEY}"
DISPLAY_NAME="${DISPLAY_NAME:-portal-mapper}"

# Instance metadata service (IMDSv2 needs the Bearer header). Used to learn our
# own OCID and current OCPU count without baking anything in. Override
# INSTANCE_ID to target a different box (e.g. when testing from elsewhere).
IMDS="${IMDS:-http://169.254.169.254/opc/v2}"
imds() { curl -fsS -H 'Authorization: Bearer Oracle' "$IMDS/$1"; }

INSTANCE_ID="${INSTANCE_ID:-$(imds instance/id || true)}"

# Target shape: the full Always-Free A1 allowance. As of 2026-06-15 this is the
# 2 OCPU / 12 GB tenancy cap; asking for more returns LimitExceeded.
SHAPE="${SHAPE:-VM.Standard.A1.Flex}"
SHAPE_OCPUS="${SHAPE_OCPUS:-2}"
SHAPE_MEM_GB="${SHAPE_MEM_GB:-12}"
shape_cfg="$(mktemp "${TMPDIR:-/tmp}/oci-shape-cfg.XXXXXX.json")"
printf '{"ocpus": %s, "memoryInGBs": %s}' "$SHAPE_OCPUS" "$SHAPE_MEM_GB" > "$shape_cfg"
trap 'rm -f "$shape_cfg"' EXIT

WAIT_SEC="${WAIT_SEC:-30}"        # seconds between resize attempts
# Throttle-backoff safety net: on 429 we slow down (30 -> 60 -> 120, cap 300s)
# and reset once the API clears, so a burst can't escalate into API abuse.
THROTTLE_BASE="${THROTTLE_BASE:-30}"
THROTTLE_CAP="${THROTTLE_CAP:-300}"
throttle_sleep="$THROTTLE_BASE"

LOG="${LOG:-$HOME/oci-a1.log}"
SUCCESS_FILE="${SUCCESS_FILE:-$HOME/oci-a1-SUCCESS.json}"
# Optional phone push on success: set NTFY_TOPIC (https://ntfy.sh, no signup) to
# get pinged when the box reaches 2 OCPU. Unset = silent (marker still written).
NTFY_TOPIC="${NTFY_TOPIC:-}"

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

# Integer OCPU count from metadata (e.g. "1.0" -> 1). Empty/unknown -> 0.
current_ocpus() {
    local o
    o="$(imds instance/shapeConfig/ocpus 2>/dev/null || true)"
    printf '%s' "${o%%.*}" | grep -Eo '^[0-9]+' || printf '0'
}

if [ -z "$INSTANCE_ID" ]; then
    log "ERROR - could not determine this instance's OCID (metadata at $IMDS)."
    log "Run on the A1 box, or set INSTANCE_ID=<ocid> to target one explicitly."
    exit 4
fi

log "=== A1 upgrade started - resize $INSTANCE_ID -> ${SHAPE_OCPUS} OCPU / ${SHAPE_MEM_GB} GB, retry ${WAIT_SEC}s, auto-backoff on throttle ==="

# Already at (or above) target? Nothing to do — record and stop.
have="$(current_ocpus)"
if [ "$have" -ge "$SHAPE_OCPUS" ]; then
    log "Already at ${have} OCPU (>= target ${SHAPE_OCPUS}) - nothing to do."
    [ -f "$SUCCESS_FILE" ] || printf '{"instance":"%s","ocpus":%s}\n' "$INSTANCE_ID" "$have" > "$SUCCESS_FILE"
    exit 0
fi

tries=0
while true; do
    tries=$((tries + 1))
    log "Attempt $tries - resize to ${SHAPE_OCPUS} OCPU / ${SHAPE_MEM_GB} GB"

    # --no-retry: an under-capacity resize returns HTTP 500, which the OCI SDK
    # would otherwise retry internally with backoff (~100s/call), throttling our
    # own loop. With it off each call returns in ~3s and WAIT_SEC drives the pace;
    # real 429s are caught by the throttle-backoff branch below.
    out="$("$OCI" compute instance update \
        --instance-id    "$INSTANCE_ID" \
        --shape          "$SHAPE" \
        --shape-config   "file://$shape_cfg" \
        --force \
        --no-retry \
        --auth           api_key \
        --profile        "$AUTH_PROFILE" \
        --output         json 2>&1)"
    code=$?

    if [ "$code" -eq 0 ]; then
        # Resize accepted -> OCI will reboot the box to apply. We'll likely be
        # killed mid-reboot; the systemd unit re-runs us on boot and confirms
        # 2 OCPU via metadata. Write the marker now so the win is recorded.
        log "RESIZE ACCEPTED after $tries tries - box will reboot into ${SHAPE_OCPUS} OCPU"
        printf '%s\n' "$out" > "$SUCCESS_FILE"
        notify "OCI A1 upgraded!" "Resized to ${SHAPE_OCPUS} OCPU / ${SHAPE_MEM_GB} GB - rebooting to apply."
        exit 0

    elif printf '%s' "$out" | grep -Eq 'NotAuthenticated|NotAuthorized|token.*expired|security token|"status":[[:space:]]*401'; then
        log "AUTH FAILED - check ~/.oci/config api_key profile '$AUTH_PROFILE'"
        notify "OCI auth failed" "grab-a1 on the A1 box can't authenticate; fix ~/.oci/config."
        exit 2

    elif printf '%s' "$out" | grep -q 'LimitExceeded'; then
        log "RESOURCE LIMIT EXCEEDED - tenancy can't grant ${SHAPE_OCPUS} OCPU (already using A1 capacity elsewhere?)"
        notify "OCI limit exceeded" "Resize to ${SHAPE_OCPUS} OCPU hit the A1 cap - check your console."
        exit 3

    elif printf '%s' "$out" | grep -Eq 'TooManyRequests|throttl|"status":[[:space:]]*429'; then
        # OCI is rate-limiting us - back off (exponential, capped) and reset
        # the cadence so we don't keep poking an angry API.
        log "  THROTTLED (429) - backing off ${throttle_sleep}s"
        sleep "$throttle_sleep"
        throttle_sleep=$(( throttle_sleep * 2 ))
        [ "$throttle_sleep" -gt "$THROTTLE_CAP" ] && throttle_sleep="$THROTTLE_CAP"
        continue   # skip the normal wait; backoff already covered it

    else
        # Out of host capacity / InternalError - expected while 2-OCPU A1 is
        # contended. The box is untouched (still running at 1 OCPU); just retry.
        throttle_sleep="$THROTTLE_BASE"   # clean response: clear any backoff
        snippet="$(printf '%s' "$out" | tr -s '[:space:]' ' ' | sed 's/^ //;s/ $//')"
        [ "${#snippet}" -gt 120 ] && snippet="${snippet:0:120}..."
        log "  No capacity (exit $code): $snippet"
    fi

    sleep "$WAIT_SEC"
done

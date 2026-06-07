#!/usr/bin/env bash
# Map Home Assistant add-on options (/data/options.json) to the environment
# variables qobuz-proxy reads, then hand off to the app as PID 1 so it receives
# SIGTERM/SIGINT from the Supervisor.
set -euo pipefail

OPTS=/data/options.json

# Persist credentials/config under the Home Assistant-managed /data volume.
export QOBUZPROXY_DATA_DIR=/data
export QOBUZPROXY_LOG_LEVEL="$(jq -r '.log_level // "info"' "$OPTS")"

# Only define a speaker from the add-on options when a DLNA IP is provided.
# Exporting QOBUZPROXY_DEVICE_NAME unconditionally would make qobuz-proxy build
# a speaker with no IP and fail validation ("DLNA IP address is required"). With
# no IP the add-on starts unconfigured so the user can log in and add a speaker
# from the Web UI (persisted to /data/config.yaml).
DLNA_IP="$(jq -r '.dlna_ip // ""' "$OPTS")"

if [ -n "$DLNA_IP" ]; then
    export QOBUZPROXY_BACKEND=dlna
    export QOBUZPROXY_DEVICE_NAME="$(jq -r '.device_name // "QobuzProxy"' "$OPTS")"
    export QOBUZPROXY_DLNA_IP="$DLNA_IP"
    export QOBUZPROXY_DLNA_PORT="$(jq -r '.dlna_port // 1400' "$OPTS")"
    export QOBUZPROXY_DLNA_FIXED_VOLUME="$(jq -r '.dlna_fixed_volume // false' "$OPTS")"
    export QOBUZ_MAX_QUALITY="$(jq -r '.max_quality // "auto"' "$OPTS")"
    echo "[run.sh] DLNA speaker '${QOBUZPROXY_DEVICE_NAME}' @ ${DLNA_IP} (quality=${QOBUZ_MAX_QUALITY})"
else
    echo "[run.sh] No dlna_ip set — starting unconfigured; add a speaker via the Web UI."
fi

exec qobuz-proxy

#!/usr/bin/env bash
# Map Home Assistant add-on options (/data/options.json) to the environment
# variables qobuz-proxy reads, then hand off to the app as PID 1 so it receives
# SIGTERM/SIGINT from the Supervisor.
set -euo pipefail

OPTS=/data/options.json

# Persist credentials/config under the Home Assistant-managed /data volume.
export QOBUZPROXY_DATA_DIR=/data

# This add-on targets DLNA renderers only.
export QOBUZPROXY_BACKEND=dlna

export QOBUZPROXY_DEVICE_NAME="$(jq -r '.device_name // "QobuzProxy"' "$OPTS")"
export QOBUZPROXY_DLNA_IP="$(jq -r '.dlna_ip // ""' "$OPTS")"
export QOBUZPROXY_DLNA_PORT="$(jq -r '.dlna_port // 1400' "$OPTS")"
export QOBUZPROXY_DLNA_FIXED_VOLUME="$(jq -r '.dlna_fixed_volume // false' "$OPTS")"
export QOBUZ_MAX_QUALITY="$(jq -r '.max_quality // "auto"' "$OPTS")"
export QOBUZPROXY_LOG_LEVEL="$(jq -r '.log_level // "info"' "$OPTS")"

echo "[run.sh] starting qobuz-proxy (device='${QOBUZPROXY_DEVICE_NAME}', dlna_ip='${QOBUZPROXY_DLNA_IP:-<auto-discover>}', quality=${QOBUZ_MAX_QUALITY})"

exec qobuz-proxy

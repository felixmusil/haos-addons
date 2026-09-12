#!/usr/bin/env bash
# Map Home Assistant add-on options (/data/options.json) to the environment
# variables tts-server reads, then hand off to the app as PID 1 so it receives
# SIGTERM/SIGINT from the Supervisor.
set -euo pipefail

OPTS=/data/options.json

# Everything the coordinator must keep across restarts/updates lives under the
# Home Assistant-managed /data volume: the SQLite library, per-unit audio, and
# the WuxiaWorld token cache. The cache path is exported UNCONDITIONALLY: the
# app's default is CWD-relative (unwritable for the non-root user, lost on
# restart), which would silently drop the rotated refresh token and break
# premium chapters after every restart.
export TTS_DATA_DIR=/data
export TTS_WORK_DIR=/data/work
export TTS_HOST=0.0.0.0
export WUXIAWORLD_TOKEN_CACHE=/data/wuxiaworld-token.json

export TTS_LOG_LEVEL="$(jq -r '.log_level // "info"' "$OPTS")"

# Export VAR from option KEY only when the option is present and non-empty.
# `jq -r` prints the literal string "null" for a missing key, and pydantic
# rejects an empty value for Literal fields (TTS_ENGINE="" would crash-loop
# the add-on) or overrides a sensible default (TTS_VOICE="" instead of af_heart).
export_opt() {
    local var="$1" key="$2" val
    val="$(jq -r --arg k "$key" '.[$k] // empty' "$OPTS")"
    if [ -n "$val" ]; then
        export "$var=$val"
    fi
}

export_opt TTS_ABS_URL abs_url
export_opt TTS_ABS_TOKEN abs_token
export_opt TTS_ABS_LIBRARY abs_library
export_opt TTS_API_TOKEN api_token
export_opt TTS_NTFY_TOPIC ntfy_topic
export_opt TTS_ENGINE default_engine
export_opt TTS_VOICE default_voice
export_opt WUXIAWORLD_TOKEN wuxiaworld_token

# Summary for the add-on Log tab — feature flags only, never token values.
abs_state="disabled (set abs_url + abs_token)"
if [ -n "${TTS_ABS_URL:-}" ] && [ -n "${TTS_ABS_TOKEN:-}" ]; then
    abs_state="enabled → ${TTS_ABS_URL} (library: ${TTS_ABS_LIBRARY:-first})"
fi
ww_state="not set (free chapters only)"
if [ -n "${WUXIAWORLD_TOKEN:-}" ]; then
    ww_state="set"
fi
api_state="open"
if [ -n "${TTS_API_TOKEN:-}" ]; then
    api_state="bearer token required"
fi
echo "[run.sh] engine=${TTS_ENGINE:-fake} voice=${TTS_VOICE:-default} log_level=${TTS_LOG_LEVEL}"
echo "[run.sh] Audiobookshelf: ${abs_state}"
echo "[run.sh] WuxiaWorld token: ${ww_state}; /api auth: ${api_state}"

exec tts-server serve --host 0.0.0.0 --port 8880

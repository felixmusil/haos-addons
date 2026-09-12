#!/usr/bin/env bash
# Map Home Assistant add-on options (/data/options.json) to the environment variables
# haugene/transmission-openvpn reads, start the ingress shim (nginx) and the AudioBookBay
# helper, then hand off to the image's own entrypoint as PID 1 so OpenVPN receives the
# Supervisor's SIGTERM.
#
# Every env var name below was checked against the 5.5.2 image scripts
# (/etc/openvpn/start.sh, /etc/transmission/start.sh, updateSettings.py, persistEnvironment.py):
#   - TRANSMISSION_<SETTING> overrides the same-named key of settings.json at EVERY start
#     (updateSettings.py; booleans compared with `.lower() == 'true'`).
#   - DROP_DEFAULT_ROUTE and LOG_TO_STDOUT are compared with the exact string "true".
#   - CREATE_TUN_DEVICE is compared case-insensitively; "false" skips the mknod.
#   - TRANSMISSION_WEB_UI is matched by exact name against the bundled UIs; unset = stock UI.
#   - LOCAL_NETWORK is comma-split into `ip route replace` calls.
#   - The image only rejects the literal "**None**" credentials; an empty string would be
#     written to the credentials file and fail minutes later with AUTH_FAILED, so the
#     fail-fast lives here.
set -euo pipefail

OPTS=/data/options.json
RPC_PASSWORD_FILE=/data/rpc-password
ABB_STATE_DIR=/data/abb
TRANSMISSION_HOME_DIR=/data/transmission-home
CUSTOM_OVPN_SRC=/share/transmission/openvpn
CUSTOM_OVPN_DST=/etc/openvpn/custom
NGINX_TEMPLATE=/etc/nginx/ingress.conf.template
NGINX_CONF=/etc/nginx/nginx.conf
TUN_DEVICE=/dev/net/tun
ABB_HELPER=/usr/local/bin/abb_helper.py

log() { echo "[run.sh] $*"; }
die() {
    echo "[run.sh] ERROR: $*" >&2
    exit 1
}

# Option value, "" when the key is missing or null. `jq -r` alone would print the literal
# string "null" for a missing key (an options.json that predates a newly added option).
opt() { jq -r --arg k "$1" '.[$k] // empty' "$OPTS"; }
# Option value with a default for missing/empty (jq's `//` keeps "" because it is truthy).
opt_default() {
    jq -r --arg k "$1" --arg d "$2" '(.[$k] // "") | if . == "" then $d else . end' "$OPTS"
}
# Export VAR from option KEY only when the option is non-empty: the image treats a set-but-empty
# variable as set (e.g. `[[ -n "${LOCAL_NETWORK-}" ]]` is false, but TRANSMISSION_WEB_UI=""
# would still be persisted and compared).
export_opt() {
    local var="$1" key="$2" val
    val="$(opt "$key")"
    if [ -n "$val" ]; then
        export "$var=$val"
    fi
}

# --- 1. read options -------------------------------------------------------------------------
NORD_USER="$(opt nordvpn_username)"
NORD_PASS="$(opt nordvpn_password)"
CUSTOM_CFG="$(opt openvpn_custom_config)"
CUSTOM_CFG="${CUSTOM_CFG%.ovpn}"  # users type the filename they see in /share; the image wants the bare name
RPC_PASSWORD="$(opt rpc_password)"
DOWNLOAD_DIR="$(opt_default download_dir /share/audiobooks)"
INCOMPLETE_DIR="$(opt_default incomplete_dir /share/torrents/incomplete)"
WATCH_DIR="$(opt_default watch_dir /share/torrents/watch)"
WEB_UI="$(opt_default web_ui flood-for-transmission)"
ABB_BASE_URL_OPT="$(opt_default abb_base_url https://audiobookbay.lu)"
ABB_USER="$(opt abb_username)"
ABB_PASS="$(opt abb_password)"

# --- validation: everything that can fail fast does so BEFORE any side effect ---------------
[ -e "$TUN_DEVICE" ] || die "$TUN_DEVICE is missing: the add-on needs the /dev/net/tun device passthrough (reinstall the add-on if the devices entry was lost)"

if [ -z "$CUSTOM_CFG" ] && { [ -z "$NORD_USER" ] || [ -z "$NORD_PASS" ]; }; then
    die "NordVPN service credentials are empty: set nordvpn_username and nordvpn_password in the add-on Configuration tab (Nord Account -> Services -> NordVPN -> Manual setup), or set openvpn_custom_config"
fi

if [ -n "$CUSTOM_CFG" ]; then
    CUSTOM_SRC="$CUSTOM_OVPN_SRC/$CUSTOM_CFG.ovpn"
    [ -f "$CUSTOM_SRC" ] || die "openvpn_custom_config: $CUSTOM_SRC not found; put the OpenVPN profile at $CUSTOM_OVPN_SRC/<name>.ovpn and set the option to <name>"
    if [ -z "$NORD_USER" ] || [ -z "$NORD_PASS" ]; then
        log "WARNING: nordvpn_username/nordvpn_password are empty; the image rewrites auth-user-pass to /config/openvpn-credentials.txt and exits unless that file exists"
    fi
fi

# The password is embedded in an nginx double-quoted string and in the image's persisted
# `export TRANSMISSION_RPC_PASSWORD="<value>"` line, neither of which escapes anything.
case "$RPC_PASSWORD" in
    *['"\\$`']*|*[[:space:][:cntrl:]]*)
        die 'rpc_password contains a character that cannot be passed through (double quote, backslash, dollar, backtick or whitespace); letters, digits and !#%&()*+,-./:;<=>?@[]^_{|}~ are fine'
        ;;
esac

# --- 2. provider -----------------------------------------------------------------------------
if [ -n "$CUSTOM_CFG" ]; then
    # A real copy, not a link: the image `sed -i`s and appends to the chosen file at every start.
    CUSTOM_DST="$CUSTOM_OVPN_DST/$CUSTOM_CFG.ovpn"
    mkdir -p "$CUSTOM_OVPN_DST"
    rm -f "$CUSTOM_DST"
    cp "$CUSTOM_SRC" "$CUSTOM_DST"
    export OPENVPN_PROVIDER=custom
    export OPENVPN_CONFIG="$CUSTOM_CFG"
    # The NordVPN provider script rewrites `cipher AES-256-CBC`; nothing does for custom.
    export OPENVPN_OPTS="--data-ciphers AES-256-GCM:AES-256-CBC --data-ciphers-fallback AES-256-CBC"
    PROVIDER_SUMMARY="custom profile $CUSTOM_CFG.ovpn"
else
    export OPENVPN_PROVIDER=NORDVPN
    export_opt NORDVPN_COUNTRY nordvpn_country
    export_opt NORDVPN_CATEGORY nordvpn_category
    export_opt NORDVPN_PROTOCOL nordvpn_protocol
    export_opt NORDVPN_SERVER nordvpn_server
    PROVIDER_SUMMARY="NordVPN country=${NORDVPN_COUNTRY:-any} category=${NORDVPN_CATEGORY:-any} protocol=${NORDVPN_PROTOCOL:-tcp}${NORDVPN_SERVER:+ server=$NORDVPN_SERVER}"
fi
export_opt OPENVPN_USERNAME nordvpn_username
export_opt OPENVPN_PASSWORD nordvpn_password

# --- 3. fail-closed tunnel -------------------------------------------------------------------
export CREATE_TUN_DEVICE=false   # HAOS passes /dev/net/tun through (config.yaml devices:); if the image mknods it, OpenVPN dies with TUNSETIFF after downloading its config
export DROP_DEFAULT_ROUTE=true          # exact lowercase: the image tests [[ "true" = "$DROP_DEFAULT_ROUTE" ]]
export LOG_TO_STDOUT=true               # idem
export GLOBAL_APPLY_PERMISSIONS=false   # no recursive chown of /share at every start
export HEALTH_CHECK_HOST="$(opt_default health_check_host 1.1.1.1)"
export TRANSMISSION_LOG_LEVEL="$(opt_default log_level info)"
export_opt LOCAL_NETWORK local_networks

# --- 4. state --------------------------------------------------------------------------------
# /data survives add-on updates. The image warns twice at every start (not under /config;
# "Deprecated ... old default") and then re-adopts this path itself - both warnings are expected.
export TRANSMISSION_HOME="$TRANSMISSION_HOME_DIR"
export TRANSMISSION_DOWNLOAD_DIR="$DOWNLOAD_DIR"
export TRANSMISSION_INCOMPLETE_DIR="$INCOMPLETE_DIR"
export TRANSMISSION_WATCH_DIR="$WATCH_DIR"
mkdir -p "$TRANSMISSION_HOME_DIR" "$DOWNLOAD_DIR" "$INCOMPLETE_DIR" "$WATCH_DIR" "$ABB_STATE_DIR"

# --- 5. RPC auth -----------------------------------------------------------------------------
# Generated once and reused: the HA `transmission` integration and rest_command store it.
if [ -z "$RPC_PASSWORD" ]; then
    if [ -s "$RPC_PASSWORD_FILE" ]; then
        RPC_PASSWORD="$(head -n1 "$RPC_PASSWORD_FILE")"
    else
        RPC_PASSWORD="$(openssl rand -hex 16)"
        (umask 077 && printf '%s\n' "$RPC_PASSWORD" > "$RPC_PASSWORD_FILE")
        log "generated a new RPC password and stored it in $RPC_PASSWORD_FILE"
    fi
fi
export TRANSMISSION_RPC_AUTHENTICATION_REQUIRED=true
export TRANSMISSION_RPC_USERNAME=transmission
export TRANSMISSION_RPC_PASSWORD="$RPC_PASSWORD"
export TRANSMISSION_RPC_WHITELIST_ENABLED=false
export TRANSMISSION_RPC_HOST_WHITELIST_ENABLED=false
if [ "$WEB_UI" != "stock" ]; then
    export TRANSMISSION_WEB_UI="$WEB_UI"
fi

log "provider: $PROVIDER_SUMMARY"
log "dirs: download=$DOWNLOAD_DIR incomplete=$INCOMPLETE_DIR watch=$WATCH_DIR home=$TRANSMISSION_HOME_DIR"
log "web_ui=$WEB_UI log_level=$TRANSMISSION_LOG_LEVEL health_check_host=$HEALTH_CHECK_HOST local_networks=${LOCAL_NETWORK:-none}"
log "AudioBookBay: $ABB_BASE_URL_OPT (member login: $([ -n "$ABB_USER" ] && echo yes || echo no))"
# The one deliberate secret in the log: the user needs it for the HA integration / rest_command.
echo "RPC password: $RPC_PASSWORD"

# --- 6. ingress shim + helper, then hand off -------------------------------------------------
# Render a COMPLETE nginx.conf. Plain pattern substitution, not sed: `&`, `/` and `|` are legal
# password characters. bash >= 5.2 gives `&` in the replacement a meaning (patsub_replacement);
# turn that off where it exists so the same line renders identically everywhere.
shopt -u patsub_replacement 2>/dev/null || true
RPC_BASIC="$(printf '%s' "transmission:$RPC_PASSWORD" | base64 | tr -d '\n')"
NGINX_BODY="$(cat "$NGINX_TEMPLATE")"
NGINX_BODY="${NGINX_BODY//%%RPC_BASIC%%/$RPC_BASIC}"
NGINX_BODY="${NGINX_BODY//%%RPC_PASSWORD%%/$RPC_PASSWORD}"
(umask 077 && printf '%s\n' "$NGINX_BODY" > "$NGINX_CONF")
nginx -c "$NGINX_CONF"

# The helper's secrets stay in its own environment: not in OpenVPN's, and not in the
# TRANSMISSION_/TR_ variables the image persists to /etc/transmission/environment-variables.sh.
(
    export ABB_TOKEN="$RPC_PASSWORD"
    export ABB_BASE_URL="$ABB_BASE_URL_OPT"
    export ABB_COOKIE_JAR="$ABB_STATE_DIR/cookies.txt"
    export TR_RPC_URL=http://127.0.0.1:9091/transmission/rpc
    export TR_RPC_USER=transmission
    export TR_RPC_PASSWORD="$RPC_PASSWORD"
    export DEFAULT_DOWNLOAD_DIR="$DOWNLOAD_DIR"
    if [ -n "$ABB_USER" ]; then
        export ABB_USERNAME="$ABB_USER"
    fi
    if [ -n "$ABB_PASS" ]; then
        export ABB_PASSWORD="$ABB_PASS"
    fi
    exec python3 "$ABB_HELPER"
) &

exec dumb-init /etc/openvpn/start.sh

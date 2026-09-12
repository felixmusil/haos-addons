#!/usr/bin/env bash
# Runs transmission-nordvpn/rootfs/run.sh against synthetic /data/options.json files through
# fake `dumb-init`, `nginx`, `python3` and `openssl` binaries that record their environment,
# argv and PID.
#
# WHY: run.sh hands ONE rpc password to three consumers in three vocabularies
# (TRANSMISSION_RPC_PASSWORD for the image, ABB_TOKEN/TR_RPC_PASSWORD for the helper, a base64
# Authorization header for nginx), and the image compares its kill-switch flags with the exact
# string "true" - a mis-spelled export silently leaves the leak open (the exact-string traps are
# listed in run.sh's header). run.sh must `exec` so OpenVPN is PID 1 under dumb-init, and must
# print the RPC password exactly once and nothing else secret.
#
# Usage: bash transmission-nordvpn/tests/run_sh_test.sh   (needs bash + jq; no Docker)
#        RUN_SH_TEST_IMAGE=transmission-nordvpn:dev bash ...   also runs `nginx -t` in the image
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADDON_DIR="$(dirname "$HERE")"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/data" "$TMP/dump" "$TMP/share/transmission/openvpn" "$TMP/etc/nginx" \
    "$TMP/etc/openvpn/custom" "$TMP/dev/net" "$TMP/usr/local/bin"
cp "$ADDON_DIR/rootfs/etc/nginx/ingress.conf.template" "$TMP/etc/nginx/ingress.conf.template"
# Plain file standing in for the character device: run.sh tests it with `-e`, which is the only
# form a harness without mknod rights can exercise.
: > "$TMP/dev/net/tun"

bash -n "$ADDON_DIR/rootfs/run.sh"

# --- fakes ---------------------------------------------------------------------------------------
# dumb-init: the exec target. Its PID must equal run.sh's PID.
cat > "$TMP/bin/dumb-init" <<'FAKE'
#!/usr/bin/env bash
echo "$$" > "$DUMP_DIR/pid"
env > "$DUMP_DIR/env"
printf '%s\n' "$@" > "$DUMP_DIR/args"
echo dumb-init >> "$DUMP_DIR/order"
FAKE
# nginx: must be started as a daemon (returns) before the exec.
cat > "$TMP/bin/nginx" <<'FAKE'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$DUMP_DIR/nginx_args"
echo nginx >> "$DUMP_DIR/order"
FAKE
# python3: the AudioBookBay helper, backgrounded by run.sh.
cat > "$TMP/bin/python3" <<'FAKE'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$DUMP_DIR/py_args"
echo python3 >> "$DUMP_DIR/order"
env > "$DUMP_DIR/py_env.tmp" && mv "$DUMP_DIR/py_env.tmp" "$DUMP_DIR/py_env"
FAKE
# openssl: deterministic `rand -hex 16` so the persisted-password assertions can be exact.
cat > "$TMP/bin/openssl" <<'FAKE'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$DUMP_DIR/openssl_args"
echo openssl >> "$DUMP_DIR/order"
echo "$FAKE_HEX"
FAKE
chmod +x "$TMP/bin/"*

# --- redirect the hard-coded paths of the test copy ------------------------------------------------
# run.sh keeps the literal container paths (qobuz-proxy / tts-coordinator convention); the copy
# under test points them at $TMP. Every replaced literal is then grepped back, so a run.sh that
# stops using one of them (renamed, parameterised) fails loudly instead of testing the wrong path.
sed -e "s|/data/options.json|$TMP/data/options.json|g" \
    -e "s|/data/rpc-password|$TMP/data/rpc-password|g" \
    -e "s|/data/abb|$TMP/data/abb|g" \
    -e "s|/data/transmission-home|$TMP/data/transmission-home|g" \
    -e "s|/share/transmission/openvpn|$TMP/share/transmission/openvpn|g" \
    -e "s|/etc/openvpn/custom|$TMP/etc/openvpn/custom|g" \
    -e "s|/etc/nginx|$TMP/etc/nginx|g" \
    -e "s|/dev/net/tun|$TMP/dev/net/tun|g" \
    -e "s|/usr/local/bin/abb_helper.py|$TMP/usr/local/bin/abb_helper.py|g" \
    "$ADDON_DIR/rootfs/run.sh" > "$TMP/run.sh"
for anchor in /data/options.json /data/rpc-password /data/abb /data/transmission-home \
    /share/transmission/openvpn /etc/openvpn/custom /etc/nginx/ingress.conf.template \
    /etc/nginx/nginx.conf /dev/net/tun /usr/local/bin/abb_helper.py; do
    grep -qF -- "$TMP$anchor" "$TMP/run.sh" || {
        echo "FAIL: run.sh no longer uses the literal $anchor; update this test" >&2
        exit 1
    }
done
# The exec target is the image's own entrypoint and is not redirected.
grep -qF 'exec dumb-init /etc/openvpn/start.sh' "$TMP/run.sh" || {
    echo "FAIL: run.sh no longer ends in 'exec dumb-init /etc/openvpn/start.sh'; update this test" >&2
    exit 1
}

failures=0
CASE=setup
fail() {
    echo "FAIL [$CASE]: $*" >&2
    failures=$((failures + 1))
}

# --- helpers ---------------------------------------------------------------------------------------
env_value() { grep -m1 "^$1=" "$2" | cut -d= -f2- || true; }
env_present() { grep -q "^$1=" "$2"; }
expect_env() { # name expected [envfile]
    local file="${3:-$TMP/dump/env}" actual
    env_present "$1" "$file" || { fail "$1 not exported ($(basename "$file"))"; return 0; }
    actual="$(env_value "$1" "$file")"
    [ "$actual" = "$2" ] || fail "$1 = '$actual', expected '$2' ($(basename "$file"))"
}
expect_absent() { # name [envfile]
    local file="${2:-$TMP/dump/env}"
    if env_present "$1" "$file"; then
        fail "$1 exported as '$(env_value "$1" "$file")' but must be absent ($(basename "$file"))"
    fi
    return 0
}
out_count() { grep -cF -- "$1" "$TMP/dump/out" || true; }
order_count() { if [ -f "$TMP/dump/order" ]; then grep -cx "$1" "$TMP/dump/order" || true; else echo 0; fi; }
conf_has() { grep -qF -- "$1" "$TMP/etc/nginx/nginx.conf" || fail "nginx.conf lacks: $1"; }

# Everything a previous case may have left behind, so "not created" assertions mean something.
reset_state() {
    rm -rf "$TMP/data/rpc-password" "$TMP/data/abb" "$TMP/data/transmission-home" \
        "$TMP/etc/nginx/nginx.conf" "$TMP/etc/openvpn/custom" "$TMP/share/audiobooks" \
        "$TMP/share/torrents"
    mkdir -p "$TMP/etc/openvpn/custom"
}

# Full, valid NordVPN options; cases override single keys with a jq object.
BASE_OPTS='{
  nordvpn_username: "svc-user@nord", nordvpn_password: "NordSvcPw-91x",
  nordvpn_country: "CH", nordvpn_category: "legacy_p2p", nordvpn_protocol: "udp", nordvpn_server: "",
  openvpn_custom_config: "", rpc_password: "s3cret-rpc-pw",
  download_dir: ($tmp + "/share/audiobooks"), incomplete_dir: ($tmp + "/share/torrents/incomplete"),
  watch_dir: ($tmp + "/share/torrents/watch"), web_ui: "flood-for-transmission", local_networks: "",
  health_check_host: "1.1.1.1", log_level: "info", abb_base_url: "https://audiobookbay.lu",
  abb_username: "", abb_password: ""
}'
mkopts() { local extra="${1:-"{}"}"; jq -nc --arg tmp "$TMP" "$BASE_OPTS + ($extra)"; }

# run_case CASE expected-rc options-json
run_case() {
    CASE="$1"
    local want_rc="$2"
    printf '%s' "$3" > "$TMP/data/options.json"
    rm -f "$TMP/dump/"*
    # `env -i` so no TRANSMISSION_*/OPENVPN_* from the developer's shell leaks into "absent" checks.
    set +e
    env -i PATH="$TMP/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" DUMP_DIR="$TMP/dump" \
        FAKE_HEX="${FAKE_HEX:-0123456789abcdef0123456789abcdef}" \
        bash "$TMP/run.sh" > "$TMP/dump/out" 2>&1 &
    RUN_PID=$!
    wait "$RUN_PID"
    RUN_STATUS=$?
    set -e
    [ "$RUN_STATUS" -eq "$want_rc" ] || {
        fail "run.sh exited $RUN_STATUS, expected $want_rc:"
        cat "$TMP/dump/out" >&2
    }
    if [ "$want_rc" -eq 0 ]; then
        [ -f "$TMP/dump/env" ] || {
            fail "fake dumb-init was never reached"
            cat "$TMP/dump/out" >&2
            return 0
        }
        # exec -> the fake inherits run.sh's PID (OpenVPN ends up as PID 1 under dumb-init).
        [ "$(cat "$TMP/dump/pid")" = "$RUN_PID" ] || fail "run.sh did not exec dumb-init (PID $(cat "$TMP/dump/pid") != $RUN_PID)"
        [ "$(cat "$TMP/dump/args")" = "/etc/openvpn/start.sh" ] || fail "dumb-init argv was: $(tr '\n' ' ' < "$TMP/dump/args")"
        # The helper is backgrounded, so its dump may land after run.sh's exec returned.
        local i=0
        while [ ! -f "$TMP/dump/py_env" ] && [ $i -lt 50 ]; do sleep 0.1; i=$((i + 1)); done
        [ -f "$TMP/dump/py_env" ] || fail "fake python3 (helper) was never started"
        # nginx must be running before OpenVPN takes over; the helper too, though it races the exec.
        [ "$(order_count nginx)" -eq 1 ] || fail "nginx started $(order_count nginx) times"
        [ "$(order_count python3)" -eq 1 ] || fail "python3 started $(order_count python3) times"
        [ "$(order_count dumb-init)" -eq 1 ] || fail "dumb-init started $(order_count dumb-init) times"
        [ "$(grep -nx nginx "$TMP/dump/order" | cut -d: -f1)" -lt "$(grep -nx dumb-init "$TMP/dump/order" | cut -d: -f1)" ] \
            || fail "nginx was not started before the exec: $(tr '\n' ' ' < "$TMP/dump/order")"
        [ "$(cat "$TMP/dump/py_args")" = "$TMP/usr/local/bin/abb_helper.py" ] || fail "helper argv was: $(tr '\n' ' ' < "$TMP/dump/py_args")"
        # The kill-switch trio, byte-exact (the image compares with `[[ "true" = ... ]]`).
        expect_env DROP_DEFAULT_ROUTE true
        expect_env LOG_TO_STDOUT true
        expect_env CREATE_TUN_DEVICE false
        expect_env GLOBAL_APPLY_PERMISSIONS false
        expect_absent PUID   # if run.sh ever exported it, the image's userSetup.sh would usermod and chown the /share download dirs at every start
        [ "$(env_value ENABLE_UFW "$TMP/dump/env")" != "true" ] || fail "ENABLE_UFW=true would key the RPC rule on the gateway, not the ingress proxy"
        # The helper's secrets must not reach OpenVPN's environment (persistEnvironment.py would
        # write every TR_* variable unescaped into /etc/transmission/environment-variables.sh).
        for name in ABB_TOKEN ABB_PASSWORD ABB_USERNAME TR_RPC_PASSWORD; do expect_absent "$name"; done
        for f in env py_env; do
            # `jq -r` on a missing key prints "null" - that string must never reach a consumer.
            if grep -q '=null$' "$TMP/dump/$f"; then
                fail "literal 'null' exported ($f): $(grep '=null$' "$TMP/dump/$f" | tr '\n' ' ')"
            fi
        done
    else
        [ -f "$TMP/dump/pid" ] && fail "dumb-init was reached although run.sh had to fail"
        [ -f "$TMP/dump/order" ] && fail "processes started on a failure path: $(tr '\n' ' ' < "$TMP/dump/order")"
        [ "$(out_count 'RPC password:')" -eq 0 ] || fail "RPC password printed on a failure path"
        [ -e "$TMP/etc/nginx/nginx.conf" ] && fail "nginx.conf rendered on a failure path"
        grep -qw null "$TMP/dump/out" && fail "the failure message contains 'null': $(cat "$TMP/dump/out")"
    fi
    # The NordVPN password never appears in the log, on any path.
    [ "$(out_count NordSvcPw-91x)" -eq 0 ] || fail "NordVPN password printed to the add-on log"
    return 0
}

# expect_rpc_line PW: exactly one "RPC password: PW" line and the value nowhere else in the log.
expect_rpc_line() {
    [ "$(out_count "RPC password: $1")" -eq 1 ] || fail "'RPC password: $1' printed $(out_count "RPC password: $1") times"
    [ "$(out_count "$1")" -eq 1 ] || fail "rpc password appears $(out_count "$1") times in the log (must be exactly the one deliberate line)"
    [ "$(out_count 'RPC password:')" -eq 1 ] || fail "$(out_count 'RPC password:') 'RPC password:' lines"
}

# expect_secret_everywhere PW: the one secret reaches all consumers identically.
expect_secret_everywhere() {
    expect_env TRANSMISSION_RPC_PASSWORD "$1"
    expect_env ABB_TOKEN "$1" "$TMP/dump/py_env"
    expect_env TR_RPC_PASSWORD "$1" "$TMP/dump/py_env"
    local b64
    b64="$(printf '%s' "transmission:$1" | base64 | tr -d '\n')"
    conf_has "proxy_set_header Authorization \"Basic $b64\";"
    conf_has "proxy_set_header X-Abb-Token \"$1\";"
    [ "$(grep -c 'Basic ' "$TMP/etc/nginx/nginx.conf")" -eq 1 ] || fail "expected one Authorization header in nginx.conf"
    [ "$(grep -c '%%' "$TMP/etc/nginx/nginx.conf")" -eq 0 ] || fail "unrendered placeholder left in nginx.conf: $(grep '%%' "$TMP/etc/nginx/nginx.conf")"
}

# --- case: NordVPN minimal - the whole env table, order, exec ------------------------------------
reset_state
run_case nordvpn_minimal 0 "$(mkopts)"
expect_env OPENVPN_PROVIDER NORDVPN
expect_env OPENVPN_USERNAME svc-user@nord
expect_env OPENVPN_PASSWORD NordSvcPw-91x
expect_env NORDVPN_COUNTRY CH
expect_env NORDVPN_CATEGORY legacy_p2p
expect_env NORDVPN_PROTOCOL udp
expect_env HEALTH_CHECK_HOST 1.1.1.1
# The sed above rewrote the literal, so the copy exports the $TMP path; the container exports
# /data/transmission-home (anchor-checked above).
expect_env TRANSMISSION_HOME "$TMP/data/transmission-home"
expect_env TRANSMISSION_RPC_AUTHENTICATION_REQUIRED true
expect_env TRANSMISSION_RPC_USERNAME transmission
expect_env TRANSMISSION_RPC_WHITELIST_ENABLED false
expect_env TRANSMISSION_RPC_HOST_WHITELIST_ENABLED false
expect_env TRANSMISSION_DOWNLOAD_DIR "$TMP/share/audiobooks"
expect_env TRANSMISSION_INCOMPLETE_DIR "$TMP/share/torrents/incomplete"
expect_env TRANSMISSION_WATCH_DIR "$TMP/share/torrents/watch"
expect_env TRANSMISSION_WEB_UI flood-for-transmission
expect_env TRANSMISSION_LOG_LEVEL info
for name in NORDVPN_SERVER LOCAL_NETWORK OPENVPN_CONFIG; do expect_absent "$name"; done
[ -z "$(env_value OPENVPN_OPTS "$TMP/dump/env")" ] || fail "OPENVPN_OPTS set for the NordVPN provider: $(env_value OPENVPN_OPTS "$TMP/dump/env")"
for d in share/audiobooks share/torrents/incomplete share/torrents/watch data/abb data/transmission-home; do
    [ -d "$TMP/$d" ] || fail "$d not created"
done
[ ! -e "$TMP/data/rpc-password" ] || fail "rpc-password file created although rpc_password was supplied"
[ "$(order_count openssl)" -eq 0 ] || fail "openssl called although rpc_password was supplied"
expect_rpc_line s3cret-rpc-pw
expect_secret_everywhere s3cret-rpc-pw
# The helper treats a set-but-empty ABB_USERNAME as "log in": empty options must stay unset.
expect_absent ABB_USERNAME "$TMP/dump/py_env"
expect_absent ABB_PASSWORD "$TMP/dump/py_env"
expect_env TR_RPC_USER transmission "$TMP/dump/py_env"
expect_env TR_RPC_URL http://127.0.0.1:9091/transmission/rpc "$TMP/dump/py_env"
expect_env DEFAULT_DOWNLOAD_DIR "$TMP/share/audiobooks" "$TMP/dump/py_env"
expect_env ABB_BASE_URL https://audiobookbay.lu "$TMP/dump/py_env"
expect_env ABB_COOKIE_JAR "$TMP/data/abb/cookies.txt" "$TMP/dump/py_env"
# The rendered file must be a COMPLETE nginx.conf (it overwrites the Ubuntu package default; a
# server-block-only file fails to start) and run as a daemon (a foreground nginx would block
# before the exec and the tunnel would never start).
[ -f "$TMP/etc/nginx/nginx.conf" ] || fail "nginx.conf not rendered"
[ "$(cat "$TMP/dump/nginx_args")" = "$(printf -- '-c\n%s/etc/nginx/nginx.conf' "$TMP")" ] || fail "nginx argv was: $(tr '\n' ' ' < "$TMP/dump/nginx_args")"
for line in 'worker_processes 1;' 'daemon on;' 'error_log /dev/stderr;' 'events {' 'worker_connections 64;' \
    'http {' 'access_log off;' 'listen 8099 default_server;' 'allow 172.30.32.2;' 'deny all;' \
    'client_max_body_size 0;' 'location /abb/' 'proxy_pass http://127.0.0.1:8098/abb/;' \
    'location = /' 'absolute_redirect off;' 'return 301 ./abb/;' 'proxy_pass http://127.0.0.1:9091/transmission/;' \
    'proxy_set_header Host $http_host;' 'proxy_set_header Accept-Encoding "";' 'proxy_redirect off;' \
    'add_header Cache-Control "no-store";'; do
    conf_has "$line"
done
[ "$(grep -o '{' "$TMP/etc/nginx/nginx.conf" | wc -l)" -eq "$(grep -o '}' "$TMP/etc/nginx/nginx.conf" | wc -l)" ] || fail "unbalanced braces in nginx.conf"
cp "$TMP/etc/nginx/nginx.conf" "$TMP/rendered-minimal.conf"   # for the optional nginx -t below
# Cross-file pins: the ingress port the Supervisor forwards to is the port nginx listens on, and
# the template still carries the placeholders run.sh substitutes.
# ingress_port is omitted from config.yaml when it equals the Supervisor default (8099).
ingress_port="$({ grep -E '^ingress_port:' "$ADDON_DIR/config.yaml" || echo 'ingress_port: 8099'; } | grep -oE '[0-9]+')"
grep -qE "listen $ingress_port default_server;" "$ADDON_DIR/rootfs/etc/nginx/ingress.conf.template" || fail "template listens on a port other than ingress_port=$ingress_port"
grep -qF '%%RPC_BASIC%%' "$ADDON_DIR/rootfs/etc/nginx/ingress.conf.template" || fail "template lacks %%RPC_BASIC%%"
grep -qF '%%RPC_PASSWORD%%' "$ADDON_DIR/rootfs/etc/nginx/ingress.conf.template" || fail "template lacks %%RPC_PASSWORD%%"

# --- case: helper receives the same secret, its login and its dirs ---------------------------------
reset_state
run_case helper_env 0 "$(mkopts '{abb_username: "abbuser", abb_password: "AbbPw-77"}')"
expect_secret_everywhere s3cret-rpc-pw
expect_env ABB_USERNAME abbuser "$TMP/dump/py_env"
expect_env ABB_PASSWORD AbbPw-77 "$TMP/dump/py_env"
expect_env ABB_TOKEN s3cret-rpc-pw "$TMP/dump/py_env"
[ "$(out_count AbbPw-77)" -eq 0 ] || fail "AudioBookBay password printed to the add-on log"
expect_absent ABB_PASSWORD   # not in OpenVPN's environment

# --- case: generated password persisted, then reused, then overridden by the option ----------------
reset_state
FAKE_HEX=0123456789abcdef0123456789abcdef run_case generated_first_start 0 "$(mkopts '{rpc_password: ""}')"
[ "$(cat "$TMP/data/rpc-password")" = "0123456789abcdef0123456789abcdef" ] || fail "rpc-password file holds '$(cat "$TMP/data/rpc-password" 2>&1)'"
[ "$(order_count openssl)" -eq 1 ] || fail "openssl called $(order_count openssl) times"
grep -qx rand "$TMP/dump/openssl_args" && grep -qx -- -hex "$TMP/dump/openssl_args" && grep -qx 16 "$TMP/dump/openssl_args" \
    || fail "openssl argv was: $(tr '\n' ' ' < "$TMP/dump/openssl_args")"
expect_rpc_line 0123456789abcdef0123456789abcdef
expect_secret_everywhere 0123456789abcdef0123456789abcdef
# Second start: a different random value must NOT be used - the HA integration stored the first.
FAKE_HEX=ffffffffffffffffffffffffffffffff run_case generated_reused 0 "$(mkopts '{rpc_password: ""}')"
[ "$(order_count openssl)" -eq 0 ] || fail "password regenerated on restart"
[ "$(cat "$TMP/data/rpc-password")" = "0123456789abcdef0123456789abcdef" ] || fail "rpc-password file changed on restart"
expect_rpc_line 0123456789abcdef0123456789abcdef   # printed again so the user can read it after a restart
expect_secret_everywhere 0123456789abcdef0123456789abcdef
[ "$(out_count ffffffffffffffffffffffffffffffff)" -eq 0 ] || fail "the discarded random value leaked into the log"
# Option set while a generated file exists: the option wins, the file is left alone.
run_case option_beats_file 0 "$(mkopts)"
expect_secret_everywhere s3cret-rpc-pw
expect_rpc_line s3cret-rpc-pw
[ "$(cat "$TMP/data/rpc-password")" = "0123456789abcdef0123456789abcdef" ] || fail "rpc-password file touched although the option was set"
[ "$(out_count 0123456789abcdef0123456789abcdef)" -eq 0 ] || fail "stale generated password printed"

# --- case: user password with shell/sed metacharacters ---------------------------------------------
# `&` is the matched text in sed and in bash>=5.2 pattern substitution, `/` and `|` break a sed
# expression; the render must pass them through byte-exact, and base64 must match.
reset_state
run_case password_metachars 0 "$(mkopts '{rpc_password: "p&ss/w0rd+x|y"}')"
expect_secret_everywhere 'p&ss/w0rd+x|y'
expect_rpc_line 'p&ss/w0rd+x|y'
conf_has 'Basic dHJhbnNtaXNzaW9uOnAmc3MvdzByZCt4fHk='   # printf 'transmission:p&ss/w0rd+x|y' | base64
# Characters that cannot be carried through the nginx double-quoted string or the image's
# unescaped `export X="..."` are rejected up front, without echoing the value.
reset_state
run_case password_rejected 1 "$(mkopts '{rpc_password: "bad\"pw$1"}')"
grep -q rpc_password "$TMP/dump/out" || fail "rejection message does not name rpc_password: $(cat "$TMP/dump/out")"
[ "$(out_count 'bad"pw$1')" -eq 0 ] || fail "rejected password echoed to the log"
[ ! -e "$TMP/data/abb" ] || fail "state created before validation finished"

# --- case: custom .ovpn copied (not linked), provider switched, cipher opts -------------------------
reset_state
printf 'client\nremote 1.2.3.4 1194\nauth-user-pass\ncipher AES-256-CBC\n' > "$TMP/share/transmission/openvpn/ch-p2p-42.ovpn"
run_case custom_ovpn 0 "$(mkopts '{openvpn_custom_config: "ch-p2p-42"}')"
expect_env OPENVPN_PROVIDER custom
expect_env OPENVPN_CONFIG ch-p2p-42
expect_env OPENVPN_OPTS "--data-ciphers AES-256-GCM:AES-256-CBC --data-ciphers-fallback AES-256-CBC"
# auth-user-pass is rewritten by the image to /config/openvpn-credentials.txt: service creds still needed.
expect_env OPENVPN_USERNAME svc-user@nord
expect_env OPENVPN_PASSWORD NordSvcPw-91x
dst="$TMP/etc/openvpn/custom/ch-p2p-42.ovpn"
[ -f "$dst" ] || fail "custom profile not copied to /etc/openvpn/custom"
[ ! -L "$dst" ] || fail "custom profile is a symlink: the image sed -i's and appends to it at every start"
cmp -s "$TMP/share/transmission/openvpn/ch-p2p-42.ovpn" "$dst" || fail "copied profile differs from the source"
expect_rpc_line s3cret-rpc-pw
# Users type the filename they see in /share; the image refuses OPENVPN_CONFIG=name.ovpn.
reset_state
run_case custom_ovpn_suffix 0 "$(mkopts '{openvpn_custom_config: "ch-p2p-42.ovpn"}')"
expect_env OPENVPN_CONFIG ch-p2p-42
[ -f "$TMP/etc/openvpn/custom/ch-p2p-42.ovpn" ] || fail "suffix variant: profile not copied as ch-p2p-42.ovpn"
[ ! -e "$TMP/etc/openvpn/custom/ch-p2p-42.ovpn.ovpn" ] || fail "suffix variant: copied as .ovpn.ovpn"
# Missing file: fail before any side effect.
reset_state
run_case custom_ovpn_missing 1 "$(mkopts '{openvpn_custom_config: "does-not-exist"}')"
grep -qF 'does-not-exist.ovpn' "$TMP/dump/out" || fail "message does not name the missing file: $(cat "$TMP/dump/out")"
grep -qF 'transmission/openvpn' "$TMP/dump/out" || fail "message does not name the /share directory: $(cat "$TMP/dump/out")"
[ ! -e "$TMP/data/abb" ] || fail "state created before validation finished"

# --- case: empty credentials fail fast, before any process or state -------------------------------
reset_state
run_case creds_both_empty 1 "$(mkopts '{nordvpn_username: "", nordvpn_password: ""}')"
[ "$(grep -ciE 'nordvpn_username|credentials' "$TMP/dump/out" || true)" -eq 1 ] || fail "expected exactly one credentials line: $(cat "$TMP/dump/out")"
grep -qi 'configuration' "$TMP/dump/out" || fail "message does not say where to set the credentials: $(cat "$TMP/dump/out")"
[ ! -e "$TMP/data/rpc-password" ] || fail "rpc password generated on a failure path"
[ ! -e "$TMP/data/abb" ] || fail "state created before validation finished"
# Both are required (not `-o`).
run_case creds_password_empty 1 "$(mkopts '{nordvpn_password: ""}')"
grep -qiE 'nordvpn_username|credentials' "$TMP/dump/out" || fail "no credentials message: $(cat "$TMP/dump/out")"
# An options.json that predates every option (`{}`): same message, no "null" anywhere.
run_case creds_options_empty 1 '{}'
grep -qiE 'nordvpn_username|credentials' "$TMP/dump/out" || fail "no credentials message: $(cat "$TMP/dump/out")"
# Custom profile without service credentials is allowed (certificate-auth profiles) but warned about.
reset_state
printf 'client\nremote 1.2.3.4 1194\n' > "$TMP/share/transmission/openvpn/cert-only.ovpn"
run_case custom_without_creds 0 "$(mkopts '{nordvpn_username: "", nordvpn_password: "", openvpn_custom_config: "cert-only"}')"
expect_env OPENVPN_PROVIDER custom
expect_absent OPENVPN_USERNAME
expect_absent OPENVPN_PASSWORD
grep -qi 'WARNING' "$TMP/dump/out" || fail "no warning about missing service credentials with a custom profile"

# --- case: /dev/net/tun missing --------------------------------------------------------------------
reset_state
rm -f "$TMP/dev/net/tun"
run_case tun_missing 1 "$(mkopts)"
grep -qF '/dev/net/tun' "$TMP/dump/out" || fail "message does not name /dev/net/tun: $(cat "$TMP/dump/out")"
: > "$TMP/dev/net/tun"

# --- case: web_ui stock omits the variable; other values pass verbatim -----------------------------
reset_state
run_case web_ui_stock 0 "$(mkopts '{web_ui: "stock"}')"
expect_absent TRANSMISSION_WEB_UI
run_case web_ui_verbatim 0 "$(mkopts '{web_ui: "transmission-web-control"}')"
expect_env TRANSMISSION_WEB_UI transmission-web-control
# Static cross-check: the schema offers exactly the UIs /etc/transmission/start.sh resolves by
# exact-string match (verified in haugene/transmission-openvpn:5.5.2), plus `stock`.
CASE=web_ui_schema
image_uis="$(printf '%s\n' combustion flood-for-transmission kettu shift transmission-web-control transmissionic)"
schema_uis="$(grep -E '^  web_ui: list\(' "$ADDON_DIR/config.yaml" | sed -e 's/.*list(//' -e 's/).*//' | tr '|' '\n' | grep -vx stock | sort)"
[ "$schema_uis" = "$image_uis" ] || fail "schema web_ui list differs from the image's UIs: $(echo "$schema_uis" | tr '\n' ' ')"
grep -qE '^  web_ui: list\(.*\|?stock\|?' "$ADDON_DIR/config.yaml" || fail "schema web_ui list lacks stock"

# --- case: LOCAL_NETWORK verbatim or absent; server/protocol/log level/health host -------------------
reset_state
run_case local_networks 0 "$(mkopts '{local_networks: "192.168.2.0/24,100.64.0.0/10", nordvpn_server: "ch123.nordvpn.com", nordvpn_protocol: "tcp", log_level: "warn", health_check_host: "9.9.9.9"}')"
expect_env LOCAL_NETWORK 192.168.2.0/24,100.64.0.0/10
expect_env NORDVPN_SERVER ch123.nordvpn.com
expect_env NORDVPN_PROTOCOL tcp
expect_env TRANSMISSION_LOG_LEVEL warn
expect_env HEALTH_CHECK_HOST 9.9.9.9
run_case local_networks_absent 0 "$(mkopts '{local_networks: ""}')"
expect_absent LOCAL_NETWORK
# Key missing entirely (old options.json) - defaults kick in, nothing is "null".
run_case options_partial 0 "$(mkopts | jq -c 'del(.local_networks, .web_ui, .log_level, .health_check_host, .abb_base_url, .nordvpn_server)')"
expect_absent LOCAL_NETWORK
expect_env TRANSMISSION_WEB_UI flood-for-transmission
expect_env TRANSMISSION_LOG_LEVEL info
expect_env HEALTH_CHECK_HOST 1.1.1.1
expect_env ABB_BASE_URL https://audiobookbay.lu "$TMP/dump/py_env"

# --- case: config.yaml options and run.sh do not drift ---------------------------------------------
CASE=config_drift
options_keys="$(sed -n '/^options:/,/^schema:/p' "$ADDON_DIR/config.yaml" | grep -oE '^  [a-z_]+' | tr -d ' ' | sort)"
schema_keys="$(sed -n '/^schema:/,$p' "$ADDON_DIR/config.yaml" | grep -oE '^  [a-z_]+' | tr -d ' ' | sort)"
[ -n "$options_keys" ] || fail "no options found in config.yaml"
[ "$options_keys" = "$schema_keys" ] || fail "options/schema key sets differ: $(diff <(echo "$options_keys") <(echo "$schema_keys") | tr '\n' ' ')"
for key in $options_keys; do
    grep -qE "(^|[^A-Za-z0-9_])$key([^A-Za-z0-9_]|$)" "$ADDON_DIR/rootfs/run.sh" || fail "option '$key' is in config.yaml but run.sh never reads it"
done
for key in $(grep -oE '(^|[^_a-z])(opt|opt_default) [a-z_]+|export_opt [A-Z_]+ [a-z_]+' "$ADDON_DIR/rootfs/run.sh" | awk '{print $NF}' | sort -u); do
    echo "$options_keys" | grep -qx "$key" || fail "run.sh reads option '$key' which config.yaml does not define"
done
grep -qE '^  9091/tcp: null' "$ADDON_DIR/config.yaml" || fail "9091/tcp must stay unmapped by default (ports: 9091/tcp: null)"
sed -n '/^ports_description:/,/^options:/p' "$ADDON_DIR/config.yaml" | grep -q '9091/tcp' || fail "ports_description lacks 9091/tcp"

# --- optional: the real nginx validates the rendered file inside the built image ---------------------
CASE=nginx_t_in_image
if [ -n "${RUN_SH_TEST_IMAGE:-}" ]; then
    cp "$TMP/rendered-minimal.conf" "$TMP/n.conf"
    if ! out="$(docker run --rm -v "$TMP/n.conf:/tmp/n.conf:ro" --entrypoint nginx "$RUN_SH_TEST_IMAGE" -t -c /tmp/n.conf 2>&1)"; then
        fail "nginx -t failed in $RUN_SH_TEST_IMAGE: $out"
    elif ! echo "$out" | grep -q 'test is successful'; then
        fail "nginx -t output unexpected: $out"
    fi
    docker run --rm --entrypoint test "$RUN_SH_TEST_IMAGE" -x /run.sh || fail "/run.sh not executable in the image"
    docker run --rm --entrypoint python3 "$RUN_SH_TEST_IMAGE" -c 'import http.server, http.cookiejar, urllib.request' || fail "python3 stdlib modules missing in the image"
else
    echo "SKIP nginx -t in image (set RUN_SH_TEST_IMAGE=transmission-nordvpn:dev)"
fi

if [ "$failures" -ne 0 ]; then
    echo "$failures assertion(s) failed" >&2
    exit 1
fi
echo "run.sh: all cases passed"

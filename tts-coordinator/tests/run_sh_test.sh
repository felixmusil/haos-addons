#!/usr/bin/env bash
# Runs tts-coordinator/run.sh against synthetic /data/options.json files through a fake
# `tts-server` binary that records its environment, argv and PID.
#
# WHY: run.sh is the only hop between the Home Assistant options and the pydantic Settings in
# tts-server, whose `extra="ignore"` makes a mistyped or empty export a silent no-op (ABS quietly
# disabled) or a boot crash (TTS_ENGINE="" fails Literal validation). It must also `exec` so the
# app is PID 1 and receives the Supervisor's SIGTERM, and must never print token values to the
# add-on Log tab.
#
# Usage: bash tts-coordinator/tests/run_sh_test.sh   (needs bash + jq; no Docker)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADDON_DIR="$(dirname "$HERE")"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/data" "$TMP/dump"

# Fake app: dump what run.sh handed us, then exit 0.
cat > "$TMP/bin/tts-server" <<'EOF'
#!/usr/bin/env bash
echo "$$" > "$DUMP_DIR/pid"
env > "$DUMP_DIR/env"
printf '%s\n' "$@" > "$DUMP_DIR/args"
EOF
chmod +x "$TMP/bin/tts-server"

# run.sh stays byte-identical to the qobuz-proxy convention (hard-coded /data/options.json); the
# test copy points at a temp file instead.
sed "s|/data/options.json|$TMP/data/options.json|" "$ADDON_DIR/run.sh" > "$TMP/run.sh"
grep -q "$TMP/data/options.json" "$TMP/run.sh" || {
    echo "FAIL: run.sh no longer reads /data/options.json; update this test" >&2
    exit 1
}

failures=0
fail() {
    echo "FAIL [$CASE]: $*" >&2
    failures=$((failures + 1))
}

env_value() { # name -> value (empty if absent)
    grep -m1 "^$1=" "$TMP/dump/env" | cut -d= -f2- || true
}
env_present() { grep -q "^$1=" "$TMP/dump/env"; }
expect_env() { # name expected
    local actual
    actual="$(env_value "$1")"
    env_present "$1" || fail "$1 not exported"
    [ "$actual" = "$2" ] || fail "$1 = '$actual', expected '$2'"
}
expect_absent() {
    env_present "$1" && fail "$1 exported as '$(env_value "$1")' but must be absent"
    return 0
}

run_case() { # CASE options-json
    CASE="$1"
    printf '%s' "$2" > "$TMP/data/options.json"
    rm -f "$TMP/dump/"*
    # `env -i` so no TTS_* from the developer's shell leaks into the "absent" assertions.
    set +e
    env -i PATH="$TMP/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" DUMP_DIR="$TMP/dump" \
        bash "$TMP/run.sh" > "$TMP/dump/out" 2>&1 &
    RUN_PID=$!
    wait "$RUN_PID"
    RUN_STATUS=$?
    set -e
    [ "$RUN_STATUS" -eq 0 ] || {
        fail "run.sh exited $RUN_STATUS:"
        cat "$TMP/dump/out" >&2
    }
    [ -f "$TMP/dump/env" ] || {
        fail "fake tts-server was never reached"
        cat "$TMP/dump/out" >&2
        return 0
    }
    # exec → the fake inherits run.sh's PID.
    [ "$(cat "$TMP/dump/pid")" = "$RUN_PID" ] || fail "run.sh did not exec tts-server (PID $(cat "$TMP/dump/pid") != $RUN_PID)"
    # argv
    expected_args="$(printf 'serve\n--host\n0.0.0.0\n--port\n8880\n')"
    [ "$(cat "$TMP/dump/args")" = "$expected_args" ] || fail "argv was: $(tr '\n' ' ' < "$TMP/dump/args")"
    # The persistence trio is unconditional.
    expect_env TTS_DATA_DIR /data
    expect_env TTS_HOST 0.0.0.0
    expect_env WUXIAWORLD_TOKEN_CACHE /data/wuxiaworld-token.json
    # `jq -r` on a missing key prints "null" — that string must never reach the app.
    if grep -q '=null$' "$TMP/dump/env"; then
        fail "literal 'null' exported: $(grep '=null$' "$TMP/dump/env" | tr '\n' ' ')"
    fi
}

# --- case 1: every option set -----------------------------------------------------------------
WW_TOKEN='{"access_token":"acc1","refresh_token":"ref1","token_type":"Bearer","expires_at":1700000000}'
# A browser Cookie header: `;`-separated, `=` inside values, percent escapes and spaces.
LGC_COOKIE_VALUE='wordpress_logged_in_ab12=felix%7C1700000000%7Cxyz; wp-settings-1=mfold=o; ev_sid=q=1'
run_case full "$(jq -nc --arg ww "$WW_TOKEN" --arg lgc "$LGC_COOKIE_VALUE" '{
  abs_url: "http://homeassistant.local:13378",
  abs_token: "eyJabs.token.XYZ",
  abs_library: "Audiobooks",
  wuxiaworld_token: $ww,
  api_token: "pi-shared-secret",
  ntfy_topic: "felix-tts-8f2a",
  default_engine: "kokoro",
  default_voice: "af_heart",
  lgc_cookie: $lgc,
  abs_podcast_library: "LeGrandContinent",
  article_engine: "kyutai",
  article_voice: "cml-tts/fr/10177_10625_000134-0003_enhanced.wav",
  log_level: "debug"
}')"
expect_env TTS_ABS_URL http://homeassistant.local:13378
expect_env TTS_ABS_TOKEN eyJabs.token.XYZ
expect_env TTS_ABS_LIBRARY Audiobooks
expect_env TTS_API_TOKEN pi-shared-secret
expect_env TTS_NTFY_TOPIC felix-tts-8f2a
expect_env TTS_ENGINE kokoro
expect_env TTS_VOICE af_heart
expect_env TTS_LOG_LEVEL debug
# Raw JSON with quotes intact proves `jq -r` (not `jq`, which would re-quote the string).
expect_env WUXIAWORLD_TOKEN "$WW_TOKEN"
expect_env LGC_COOKIE "$LGC_COOKIE_VALUE"
expect_env TTS_ABS_PODCAST_LIBRARY LeGrandContinent
expect_env TTS_ARTICLE_ENGINE kyutai
expect_env TTS_ARTICLE_VOICE cml-tts/fr/10177_10625_000134-0003_enhanced.wav
# The cookie cache path must NOT be exported: the scraper prefers the cache file over the
# option, and nothing in the container ever writes that file.
expect_absent LGC_COOKIE_CACHE
for secret in eyJabs.token.XYZ pi-shared-secret acc1 ref1 felix%7C1700000000%7Cxyz; do
    if grep -q -- "$secret" "$TMP/dump/out"; then
        fail "secret '$secret' printed to the add-on log"
    fi
done

# --- case 2a: every string option blank ----------------------------------------------------------
run_case blank '{"abs_url":"","abs_token":"","abs_library":"","wuxiaworld_token":"","api_token":"","ntfy_topic":"","default_engine":"kokoro","default_voice":"","lgc_cookie":"","abs_podcast_library":"","article_engine":"kyutai","article_voice":"","log_level":"info"}'
for name in TTS_ABS_URL TTS_ABS_TOKEN TTS_ABS_LIBRARY TTS_API_TOKEN TTS_NTFY_TOPIC TTS_VOICE WUXIAWORLD_TOKEN LGC_COOKIE TTS_ABS_PODCAST_LIBRARY TTS_ARTICLE_VOICE; do
    expect_absent "$name"
done
expect_env TTS_ENGINE kokoro
expect_env TTS_ARTICLE_ENGINE kyutai
expect_env TTS_LOG_LEVEL info

# --- case 2b: empty options file (predates newly added options) ----------------------------------
run_case missing '{}'
for name in TTS_ABS_URL TTS_ABS_TOKEN TTS_ABS_LIBRARY TTS_API_TOKEN TTS_NTFY_TOPIC TTS_VOICE WUXIAWORLD_TOKEN TTS_ENGINE LGC_COOKIE TTS_ABS_PODCAST_LIBRARY TTS_ARTICLE_ENGINE TTS_ARTICLE_VOICE; do
    expect_absent "$name"
done
expect_env TTS_LOG_LEVEL info

if [ "$failures" -ne 0 ]; then
    echo "$failures assertion(s) failed" >&2
    exit 1
fi
echo "run.sh: all cases passed"

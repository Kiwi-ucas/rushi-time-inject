#!/bin/sh
# time-inject e2e: drive the real hook binary against synthetic session
# logs, the way the kernel does (stdin request + $HARNESS_WINDOW +
# $SESSION/$SESSIONS_ROOT env). No model or kernel required.
#
# Usage: ./scripts/time-inject-e2e.sh   (run from anywhere)
#
# Note: env assignments must reach the BINARY, not the left side of a
# pipe — `VAR=x cmd1 | cmd2` only sets VAR for cmd1. Use `env VAR=x
# $BIN` (or `env -u` to clear ambient vars the surrounding harness may
# export, e.g. SESSION/SESSIONS_ROOT when this script runs inside a
# rushi loop).

set -e

here=$(cd "$(dirname "$0")/.." && pwd)
BIN="$here/hook-time-inject/target/debug/harness-hook-time-inject"

if [ ! -x "$BIN" ]; then
    echo "building hook binary..."
    (cd "$here/hook-time-inject" && cargo build)
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

pass=0
fail=0

check_contains() {
    # $1=needle  $2=haystack  $3=label
    if printf '%s' "$2" | grep -qF "$1"; then
        pass=$((pass+1)); echo "ok   $3"
    else
        fail=$((fail+1)); echo "FAIL $3"
        echo "     missing: $1"
        echo "     got: $2"
    fi
}

check_not_contains() {
    if printf '%s' "$2" | grep -qF "$1"; then
        fail=$((fail+1)); echo "FAIL $3"
        echo "     should not contain: $1"
    else
        pass=$((pass+1)); echo "ok   $3"
    fi
}

# run_hook ENVVAR... PAYLOAD  — run the binary with the given env on the
# BINARY side of the pipe, stdin = PAYLOAD.
run_hook() {
    req="$1"; shift
    printf '%s' "$req" | env "$@" "$BIN"
}

# Clear the ambient session env a surrounding harness may export, so the
# payload-field fallback is actually exercised.
run_hook_clean() {
    req="$1"; shift
    printf '%s' "$req" | env -u SESSION -u SESSIONS_ROOT -u HARNESS_WINDOW "$@" "$BIN"
}

# ── fixture session: user returns 4 days later, similar question ─────
mkdir -p "$work/sess"
cat > "$work/sess/events.jsonl" <<'EOF'
{"v":1,"type":"user_message","ts":"2026-09-28T01:35:00Z","content":"how is the essence plugin doing?"}
{"v":1,"type":"assistant_message","ts":"2026-09-28T01:35:40Z","content":"all good"}
{"v":1,"type":"user_message","ts":"2026-10-02T01:35:00Z","content":"how is the essence plugin doing?"}
EOF

REQ='{"model":"m","instructions":"BASE","input":[{"role":"user","content":"how is the essence plugin doing?"}],"prompt_fragments":[["essence","ESSENCE TEXT"]]}'

echo "== case 1: model.before, returning user (4-day gap) =="
out=$(run_hook "$REQ" HARNESS_WINDOW=model.before SESSION=sess SESSIONS_ROOT="$work")
check_contains '"time"' "$out" "time fragment present"
check_contains "Session time" "$out" "fragment header"
check_contains "Idle since last activity" "$out" "idle gap reported"
check_contains '3 days, 23 hours' "$out" "gap humanized to days (3d23h59m)"
check_contains "re-verify" "$out" "nudge present for long gap"
check_contains '"essence"' "$out" "foreign fragments preserved"

echo "== case 2: wrong window is a no-op =="
out=$(run_hook "$REQ" HARNESS_WINDOW=tool.before SESSION=sess SESSIONS_ROOT="$work")
check_contains '{}' "$out" "no-op on foreign window"
check_not_contains 'prompt_fragments' "$out" "no fragment added on foreign window"

echo "== case 3: first user message has no gap =="
mkdir -p "$work/fresh"
cat > "$work/fresh/events.jsonl" <<'EOF'
{"v":1,"type":"user_message","ts":"2026-10-02T09:00:00Z","content":"hi"}
EOF
out=$(run_hook "$REQ" HARNESS_WINDOW=model.before SESSION=fresh SESSIONS_ROOT="$work")
check_contains "Session time" "$out" "fragment present for first message"
check_not_contains "Idle since" "$out" "no gap line for first message"
check_not_contains "re-verify" "$out" "no nudge without a gap"

echo "== case 4: short gap keeps the fragment, drops the nudge =="
mkdir -p "$work/short"
cat > "$work/short/events.jsonl" <<'EOF'
{"v":1,"type":"user_message","ts":"2026-10-02T09:00:00Z","content":"a"}
{"v":1,"type":"assistant_message","ts":"2026-10-02T09:00:30Z","content":"ok"}
{"v":1,"type":"user_message","ts":"2026-10-02T09:03:00Z","content":"b"}
EOF
out=$(run_hook "$REQ" HARNESS_WINDOW=model.before SESSION=short SESSIONS_ROOT="$work")
check_contains "2 minutes" "$out" "gap humanized to minutes (150s)"
check_not_contains "re-verify" "$out" "no nudge for short gap"

echo "== case 5: missing log is a no-op =="
out=$(run_hook "$REQ" HARNESS_WINDOW=model.before SESSION=nope SESSIONS_ROOT="$work")
check_contains '{}' "$out" "no-op when session log missing"

echo "== case 6: legacy wrapped payload (manual invocation, no env) =="
out=$(run_hook_clean "{\"window\":\"model.before\",\"session\":\"$work/sess\",\"request\":{\"model\":\"m\",\"instructions\":\"BASE\",\"input\":[]}}")
check_contains '"time"' "$out" "payload.session honored without env"
check_contains '3 days, 23 hours' "$out" "gap from payload-resolved session"

echo "== case 7: .time_inject marker off disables the injection =="
printf 'off\n' > "$work/sess/.time_inject"
out=$(run_hook "$REQ" HARNESS_WINDOW=model.before SESSION=sess SESSIONS_ROOT="$work")
check_contains '{}' "$out" "no-op when marker is off"
check_not_contains 'prompt_fragments' "$out" "no fragment when marker is off"

echo "== case 8: marker on (explicit) re-enables it =="
printf 'on' > "$work/sess/.time_inject"
out=$(run_hook "$REQ" HARNESS_WINDOW=model.before SESSION=sess SESSIONS_ROOT="$work")
check_contains '"time"' "$out" "fragment when marker says on"

echo "== case 9: marker file absent is the default-on path =="
rm -f "$work/sess/.time_inject"
out=$(run_hook "$REQ" HARNESS_WINDOW=model.before SESSION=sess SESSIONS_ROOT="$work")
check_contains '"time"' "$out" "fragment when marker absent (default on)"

echo
echo "== summary: $pass passed, $fail failed =="
[ "$fail" -eq 0 ]

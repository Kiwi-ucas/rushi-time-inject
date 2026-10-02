#!/usr/bin/env bash
# sync-from-webui.sh — re-extract the WebUI half of the time-inject plugin
# from a rushi-webui checkout.
#
# This repository holds BOTH halves of the plugin:
#
#   hook-time-inject/   the kernel-side `model.before` hook (its own crate,
#                       built and tested here)
#   server/ client/     the WebUI half — the per-session toggle, extracted
#                       from rushi-webui, where it is developed
#
# rushi-webui is the WebUI half's development home (it is compiled into the
# server binary and the WASM bundle there). This script PACKAGES it: the
# droppable modules, the extracted stylesheet section, and the browser probe
# (rushi-webui/e2e/ is gitignored upstream, so the probe only survives here).
#
# Usage:
#   scripts/sync-from-webui.sh [path-to-rushi-webui]
#   (default: ../rushi-webui next to this repo)
#
# Idempotent: it overwrites the mirrors from upstream and regenerates
# client/time.css from the upstream stylesheet section plus the additive
# rules kept in client/time-additive.css. UPSTREAM records the exact upstream
# revision the mirrors were taken from.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEBUI="${1:-$HERE/../rushi-webui}"
WEBUI="$(cd "$WEBUI" && pwd)"

say() { printf '%s\n' "$*"; }
need() { [ -f "$1" ] || { echo "sync: missing $1" >&2; exit 1; }; }

# ── the upstream files ──────────────────────────────────────────────
SERVER="bin/rushi-web/src/time_inject.rs"
CLIENT="web-leptos/src/time.rs"
STYLE="web-leptos/style.css"
PROBE="e2e/time_inject_probe.py"
CDP="e2e/model_panel_probe.py"
for f in "$SERVER" "$CLIENT" "$STYLE" "$PROBE" "$CDP"; do
  need "$WEBUI/$f"
done

cp "$WEBUI/$SERVER" "$HERE/server/time_inject.rs"
cp "$WEBUI/$CLIENT" "$HERE/client/time.rs"
cp "$WEBUI/$PROBE"  "$HERE/e2e/time_inject_probe.py"
# The CDP harness the probe imports (`from model_panel_probe import Cdp,
# ws_connect`) — copied so the probe runs from this repo standalone.
cp "$WEBUI/$CDP"    "$HERE/e2e/model_panel_probe.py"

# ── the stylesheet: the plugin's section, taken verbatim ────────────
# The section is everything from the plugin's banner to the NEXT section
# banner (or EOF): a plugin's CSS is APPENDED to the upstream stylesheet,
# never inserted, so the extraction is a contiguous suffix-run. (Taking it
# to EOF instead would silently swallow a section appended after this one —
# which is exactly how v0.5.59's time section leaked into the rewind
# mirror's extraction.) A missing banner means the upstream layout changed.
# (literal match — the banner carries a box-drawing rule and an em dash,
# which awk's -v escape processing would mangle in a regex)
BANNER='/* ── v0.5.56 time-inject plugin'
grep -qF "$BANNER" "$WEBUI/$STYLE" || {
  echo "sync: the time-inject section banner is gone from $STYLE" >&2; exit 1; }
awk -v banner="$BANNER" '
  index($0, banner) == 1 { on = 1; print; next }
  on && /^\/\* ── / { exit }          # the banner that follows ends this one
  # blank lines are held back: the ones inside the section are flushed by the
  # next real line, the trailing run (the separator before the next banner, or
  # the file end) is dropped, so the block is the section exactly
  on && /^$/ { pend = pend "\n"; next }
  on { if (pend != "") { printf "%s", pend; pend = "" } print }
' "$WEBUI/$STYLE" \
  > "$HERE/client/.time-block.css.tmp"

{
  cat "$HERE/client/header.css"
  cat "$HERE/client/.time-block.css.tmp"
  cat "$HERE/client/time-additive.css"
} > "$HERE/client/time.css"
rm -f "$HERE/client/.time-block.css.tmp"

# ── verify the WebUI touch points still exist ───────────────────────
# The plugin's own two modules are mirrored above; these are the edits that
# wire them into the WebUI (install/TOUCHPOINTS.md lists them all). A missing
# marker means the upstream integration changed and this package needs a look
# — fail loudly instead of shipping a stale mirror.
check() { # file, marker, label
  grep -qF -- "$2" "$WEBUI/$1" || {
    echo "sync: touch point lost — $3" >&2
    echo "      $1 no longer contains: $2" >&2
    exit 1; }
}
check bin/rushi-web/src/main.rs   'mod time_inject;'                         "main.rs: mod time_inject"
check bin/rushi-web/src/main.rs   'async fn get_time_inject('                "main.rs: the read handler"
check bin/rushi-web/src/main.rs   'async fn post_time_inject('               "main.rs: the write handler"
check bin/rushi-web/src/main.rs   '/api/sessions/{id}/time-inject'           "main.rs: the route"
check bin/rushi-web/src/sessions.rs 'crate::time_inject::enabled_from_marker(' "sessions.rs: the read path"
check bin/rushi-web/src/sessions.rs 'crate::time_inject::marker_for('        "sessions.rs: the write path"
check web-leptos/src/model.rs     'pub struct TimeInjectView {'              "model.rs: the view type"
check web-leptos/src/model.rs     'pub time_inject: RwSignal<Option<TimeInjectView>>,' "model.rs: the state slice"
check web-leptos/src/api.rs       'pub async fn load_time_inject('           "api.rs: the read call"
check web-leptos/src/api.rs       'pub async fn set_time_inject('            "api.rs: the write call"
check web-leptos/src/plugins.rs   'PluginDef { id: "time", label: "time" }'  "plugins.rs: the registry entry"
check web-leptos/src/ui.rs        '"time" => crate::time::time_plugin_view'  "ui.rs: the dispatcher arm"
check web-leptos/src/lib.rs       'mod time;'                                "lib.rs: mod time"
check web-leptos/style.css        '#time-body { font-size: 13px'             "style.css: the section body"
check README.md                   '/api/sessions/{id}/time-inject'           "README: the API row"

# ── record the upstream revision ────────────────────────────────────
REV="$(git -C "$WEBUI" rev-parse HEAD 2>/dev/null || echo unknown)"
SUBJ="$(git -C "$WEBUI" log -1 --pretty=%s 2>/dev/null || echo unknown)"
# HEAD is the tree the mirrors were read from; SRC is the last commit that
# actually touched a mirrored file (the two differ whenever a later commit
# changed something else — e.g. docs).
SRC_REV="$(git -C "$WEBUI" log -1 --format=%H -- "$SERVER" "$CLIENT" "$STYLE" \
             "$PROBE" 2>/dev/null || echo unknown)"
SRC_SUBJ="$(git -C "$WEBUI" log -1 --format=%s -- "$SERVER" "$CLIENT" "$STYLE" \
             "$PROBE" 2>/dev/null || echo unknown)"
DIRTY="clean"
[ -n "$(git -C "$WEBUI" status --porcelain 2>/dev/null)" ] && DIRTY="dirty"
{
  echo "# The rushi-webui revision these mirrors were taken from."
  echo "# Written by scripts/sync-from-webui.sh — do not edit."
  echo "repo     = $WEBUI"
  echo "rev      = $REV"
  echo "subject  = $SUBJ"
  echo "source   = $SRC_REV   # last commit touching a mirrored file"
  echo "source_subject = $SRC_SUBJ"
  echo "worktree = $DIRTY"
  echo "synced   = $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
} > "$HERE/UPSTREAM"

say "synced from $WEBUI @ $REV ($DIRTY)"
git -C "$HERE" status --short || true

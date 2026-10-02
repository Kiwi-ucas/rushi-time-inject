# rushi-time-inject

Time-awareness hook for the `rushi` kernel: a `model.before` hook that
upserts a small `"time"` prompt fragment into every model request, telling
the model **when the user's last message was sent** and **how long the
idle gap was since the previous exchange**.

## Problem

The kernel's projected model input carries no event timestamps
(`assemble` renders events as `{role, content}` only), so the model has
no sense of time. If a user returns to a session a day (or days) later
with a question that resembles the previous turn — e.g. "how is the
essence plugin doing?" asked 4 days after asking the same — the agent
may treat it as a same-moment repeat instead of a fresh request in a new
timeframe, and skip re-verifying state.

## Design

- **Hook**: `harness-hook-time-inject` (Rust, single crate, no deps
  beyond `serde_json` + `chrono`), window = `model.before`.
- **Source of truth**: the session log (`events.jsonl`), read via
  `SESSION` / `SESSIONS_ROOT` env (same convention as the essence
  hooks). The fragment is a pure function of the log:
  - `user_msg` = `ts` of the last `user_message` event, rendered as
    local `YYYY-MM-DD HH:MM:SS` — the same shape the webui info-card
    corner stamp uses (`ts_full` in `rushi-webui/web-leptos/src/timeutil.rs`),
  - `gap` = `user_msg.ts − ts(the event immediately before it)` — i.e.
    how long the session sat idle before the user came back.
- **Fragment text** (joined into `instructions` by the kernel):

  ```
  [Session time]
  User's last message: 2026-09-28 09:30:00 local
  Idle since last activity: 4 days, 23 hours, 5 minutes
  The user has been away for a while; treat a message that resembles an
  earlier one as a fresh request in a new timeframe — re-verify anything
  time-sensitive instead of assuming it is a continuation of the last
  exchange.
  ```

  The nudge line is emitted only when the gap ≥ 1 h (`NUDGE_GAP_SECS`,
  a constant); the two data lines are always present.

### Deliberate choices

| Decision | Rationale |
|---|---|
| Anchor on **last user_message ts**, not `Local::now()` | Byte-stable across every model call within one user turn → prefix cache stays warm; the fragment changes only when a new user message lands — exactly when a fresh time signal is needed. |
| Include the **idle gap**, not just the absolute time | The model never sees historical `ts`, so without the gap a raw timestamp alone gives it no baseline to compare against. The gap is the actual disambiguation signal. |
| Read the **log**, not wall clock | Log `ts` values are authoritative and immutable; also makes the hook deterministic and testable. |
| Fragment id `"time"` | Namespaced like `"essence"` / `"goal"`; kernel joins fragments in first-seen order, so ordering vs other extensions is neutral. |
| Local timezone for display | Matches the webui card stamp (both render in the user's local zone; host == browser zone in this setup). |

### Per-session toggle: the `.time_inject` marker

The hook is **on by default**; a per-session marker file
`sessions/<id>/.time_inject` gates it (same convention as the kernel's
`.model` / `.effort` markers):

| Marker content (trimmed, case-insensitive) | Effect |
|---|---|
| absent / empty / anything else | injection **ON** (default) |
| `off` / `no` / `0` / `false` | injection **OFF** |

Because the marker is read on every `model.before` call, the toggle
takes effect from the *next* model call of an already-running loop — no
restart needed. Other sessions are unaffected (the marker is
session-scoped).

### Webui sidebar plugin

The webui's left-sidebar plugin module (`rushi-webui`) exposes the
toggle: a `time` entry in the plugin switch bar renders a switch
(`ui.rs::time_plugin_view`), backed by
`GET/POST /api/sessions/{id}/time-inject`. The POST writes or removes
the marker (off → `"off"`; on → marker removed, back to default-on);
the GET reports the effective state. The WASM bundle is compiled into
the `rushi-web` binary (rust-embed), so after editing the frontend
re-run `trunk build` + `cargo build` and restart the server.

### Cost

One small fragment (~2–3 lines) in `instructions` per user turn. When a
new user message lands, `instructions` changes once → the provider's
prefix cache for that turn's prefix is broken once. That is the
intrinsic cost of time-awareness; within a turn (tool loops, multiple
model calls) the fragment is byte-identical, so nothing churns.

## Layout

| Path | Role |
|---|---|
| `hook-time-inject/` | The `model.before` hook crate (binary `harness-hook-time-inject`) |
| `scripts/time-inject-e2e.sh` | E2E: drives the real binary against synthetic logs + env, no kernel/model needed |
| `rushi-webui/bin/rushi-web/src/sessions.rs` | Server-side marker read/write (`time_inject_enabled`, `set_time_inject`) |
| `rushi-webui/bin/rushi-web/src/main.rs` | `GET/POST /api/sessions/{id}/time-inject` endpoints |
| `rushi-webui/web-leptos/src/{plugins,model,api,ui}.rs` + `style.css` | Sidebar `time` plugin: registry entry, toggle switch, styles |

## Build & test

```sh
cd hook-time-inject && cargo test      # unit tests (fragment upsert, humanize, anchor logic)
cd .. && sh scripts/time-inject-e2e.sh  # end-to-end against the built binary
```

## Wiring into the kernel

`bin/` farm symlink (sibling resolution for `resolve_hook_command`,
same as the essence/goal hooks):

```sh
ln -s ../rushi-time-inject/hook-time-inject/target/debug/harness-hook-time-inject bin/harness-hook-time-inject
```

`config.toml`:

```toml
[hooks.defs.time-inject]
command = "harness-hook-time-inject"
args = []

[hooks.pipeline."model.before"]
steps = ["goal-arm", "essence-inject", "time-inject"]
```

Ordering: appended last — fragment merge is by id, so relative order
with `goal-arm` / `essence-inject` does not change content.

## Nix

`flake.nix` builds the hook binary for Nix-managed installs
(`nix build .#hook-time-inject`). No `flake.lock` is committed yet;
run `nix flake lock` to pin inputs.

## Notes

- The webui launches each loop with a per-session config
  (`config.session.<id>.toml` beside the main `config.toml`) when the
  session pins a model/effort override; that copy is regenerated from
  the main config on every loop start, so new pipeline steps in the
  main config apply automatically on the next start.
- `config.session.<id>.toml` copies on disk may lag the main config
  until the session's next loop start; this is expected, not a bug.

## Verification

- `cargo test`: 9/9 pass (upsert idempotence, humanize, anchor/first-message,
  fragment shape, `.time_inject` marker gate).
- `scripts/time-inject-e2e.sh`: 20/20 pass — 4-day-gap fragment,
  wrong-window no-op, first-message no-gap, short-gap no-nudge,
  missing-log no-op, legacy wrapped-payload manual invocation, and the
  marker gate (off disables, explicit on re-enables, absent defaults to
  on).
- Real loop: `rushi run <scratch-session> "say hi"` with the main
  config → log shows `hook.model.before` chain `["ok","noop","ok"]`
  and a `hook.model.before.transform` marker listing `["time"]` among
  joined fragments.
- Webui toggle: `GET/POST /api/sessions/{id}/time-inject` round-trips
  the marker (off writes `"off"`, on removes it); the hook binary
  no-ops on the off marker and injects on the absent one, verified
  against a real session log.

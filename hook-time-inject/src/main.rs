//! `harness-hook-time-inject` — the `model.before` time-injection hook.
//!
//! The model never sees event timestamps in its input (the projected
//! items carry only `role` + `content`), so it has no sense of *when*
//! anything happened. This hook closes that gap: it upserts a
//! deterministic `"time"` prompt fragment (an ordered `[id, text]`
//! pair under `request.prompt_fragments`, joined into
//! `request.instructions` by the kernel — docs/system-prompt-generation.md
//! D5) that tells the model
//!
//! 1. when the user's most recent message was sent (local time, in the
//!    same `YYYY-MM-DD HH:MM:SS` shape the webui info cards render in
//!    their top-right corner), and
//! 2. how long the idle gap was since the previous exchange.
//!
//! A session that sat unused for a day or more is the target case: the
//! user may be back with a *fresh* question that merely resembles the
//! last one. The idle gap lets the model tell "a repeat of the same
//! moment" from "a new request in a new timeframe" and re-verify
//! anything time-sensitive instead of assuming continuity.
//!
//! ## Why anchor on the log, not the wall clock
//!
//! The fragment is a pure function of the session log: the timestamp of
//! the last `user_message` event and of the event immediately before it
//! (the tail of the previous turn). Both are immutable once logged, so
//! the fragment is byte-identical across every model call within one
//! user turn — the provider's prefix cache stays warm. It changes only
//! when a *new* user message lands, which is exactly when a fresh time
//! signal is needed. Reading `Local::now()` instead would churn the
//! fragment on every tool-loop iteration and break the cache each call.
//!
//! ## State contract
//!
//! Same pipeline ABI as `harness-hook-essence-inject`
//! (docs/loop-lifecycle-hooks.md §12):
//! - stdin is the accumulated window state; on `model.before` the state
//!   IS the request object, so the session dir comes from the hook env
//!   (`SESSION` / `SESSIONS_ROOT`).
//! - stdout is the new state: the transformed request object, or `{}`
//!   to leave the state unchanged (nothing to say — not our window, no
//!   session, or no user message in the log yet).
//! - Exit `0`. No effect fields travel; the fragment is the payload.
//! - Window dispatch is `$HARNESS_WINDOW` (kernel-injected,
//!   authoritative), with the payload `window` field as a
//!   manual-invocation fallback.
//!
//! Idempotent: it only ever touches its own `"time"` key; fragments
//! owned by other extensions (e.g. `"essence"`, `"goal"`) pass through
//! in order.

use std::fs;
use std::io::Read;
use std::path::PathBuf;

use chrono::{DateTime, Local, Utc};
use serde_json::Value;

/// The prompt-fragment id this hook owns. Other extensions own their
/// own ids; we never touch them.
const FRAGMENT_ID: &str = "time";

/// The per-session on/off marker (same one-line-file convention as the
/// kernel's `.model` / `.effort` markers; written by the webui time
/// plugin's toggle button).
const MARKER_NAME: &str = ".time_inject";

/// Above this idle gap we add a one-line nudge to re-verify state;
/// below it the two data lines are enough and the nudge would just be
/// noise.
const NUDGE_GAP_SECS: i64 = 3600;

fn main() {
    let mut args = std::env::args().skip(1);
    if matches!(args.next().as_deref(), Some("--help" | "-h")) {
        print_help();
        return;
    }

    let state = read_stdin_json();

    // Not our window: no-op.
    if !window_is(&state, "model.before") {
        return noop();
    }

    let session_dir = match resolve_session_dir(&state) {
        Some(d) => d,
        None => return noop(),
    };

    // Per-session toggle (the webui time plugin writes it; .model /
    // .effort marker convention). Checked on every model call, so the
    // sidebar toggle takes effect immediately, no loop restart.
    if !time_inject_enabled(&session_dir) {
        return noop();
    }

    // Read the session log and find the last user_message + the event
    // immediately before it. A missing/corrupt log is a no-op, not a
    // loop-blocker.
    let Some(anchor) = read_anchor(&session_dir.join("events.jsonl")) else {
        return noop();
    };

    let fragment = render_fragment(anchor.user_msg, anchor.gap);

    // §12: the state IS the request object. The `request` key is kept as
    // a fallback for manual invocation with the legacy envelope.
    let request = state
        .get("request")
        .cloned()
        .unwrap_or_else(|| state.clone());

    let transformed = upsert_fragment(request, &fragment);
    println!(
        "{}",
        serde_json::to_string(&transformed).unwrap_or_else(|_| "{}".into())
    );
}

fn print_help() {
    println!(
        "harness-hook-time-inject — model.before time-injection hook

Reads the accumulated window state (the model request) on stdin, looks
up the session's event log via the SESSION / SESSIONS_ROOT env vars,
and upserts a deterministic \"time\" prompt fragment telling the model
when the user's last message was sent and the idle gap since the
previous exchange.

Output (stdout): the transformed request object, or {{}} to leave the
state unchanged (not our window, no session, no user message yet).
Exit 0 on every path (there is no vetoable action here).

Fragment shape (joined into instructions by the kernel):

  [Session time]
  User's last message: 2026-09-28 09:30:00 local
  Idle since last activity: 4 days, 23 hours, 5 minutes
  (plus a one-line re-verify nudge when the gap is >= 1 hour)

The time is local, formatted YYYY-MM-DD HH:MM:SS to match the webui
info-card corner stamp. The fragment is a pure function of the log
(last user_message ts + the ts of the event before it), so it is
byte-stable within a user turn and the prefix cache stays warm.

Per-session toggle: a marker file sessions/<id>/.time_inject holding an
off value (off/no/0/false, case-insensitive) disables the injection
for that session; a missing marker means on (default). The marker is
checked on every model call, so the webui sidebar toggle takes effect
immediately."
    );
}

/// Window dispatch (§12.6): `$HARNESS_WINDOW` is authoritative — the
/// kernel injects it, and `model.before` state carries no `window`
/// field. The payload field covers manual invocation with a wrapped
/// payload.
fn window_is(payload: &Value, want: &str) -> bool {
    if let Ok(w) = std::env::var("HARNESS_WINDOW") {
        return w == want;
    }
    payload.get("window").and_then(|w| w.as_str()) == Some(want)
}

fn noop() {
    println!("{{}}");
}

/// What the fragment is a pure function of: the last user_message
/// timestamp and the idle gap to the event before it (or `None` for a
/// fresh session where the user message is the first log line).
struct Anchor {
    user_msg: DateTime<Utc>,
    gap: Option<i64>,
}

/// Scan the event log for the last `user_message` and the timestamp of
/// the event immediately before it. Malformed lines are skipped; the
/// log is only read while the loop is between stages, so an unlocked
/// read is safe (same convention as the essence hooks).
fn read_anchor(log_path: &std::path::Path) -> Option<Anchor> {
    let text = fs::read_to_string(log_path).ok()?;

    // Per line: (is_user_message, parsed ts in UTC). `None` ts means the
    // line was unparseable as JSON or its ts was not RFC 3339.
    let mut rows: Vec<(bool, Option<DateTime<Utc>>)> = Vec::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let Ok(v) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        let is_user = v.get("type").and_then(|t| t.as_str()) == Some("user_message");
        let ts = v.get("ts").and_then(|t| t.as_str()).and_then(|s| {
            DateTime::parse_from_rfc3339(s)
                .ok()
                .map(|d| d.with_timezone(&Utc))
        });
        rows.push((is_user, ts));
    }

    // Index of the last user_message; its ts is the anchor.
    let last_user_idx = rows.iter().rposition(|(is_u, _)| *is_u)?;
    let user_msg = rows[last_user_idx].1?;

    // The event immediately before the last user_message is the tail of
    // the previous turn (assistant message / tool result). Its ts is the
    // last moment of activity before the user came back. Scan backwards
    // from the user message for the nearest line that carries a ts.
    let prev_ts = rows
        .iter()
        .take(last_user_idx)
        .rev()
        .find_map(|(_, t)| *t);
    let gap = prev_ts.map(|prev| (user_msg - prev).num_seconds().max(0));

    Some(Anchor { user_msg, gap })
}

/// Pure function of the anchor: byte-identical output for identical
/// log state.
fn render_fragment(user_msg: DateTime<Utc>, gap: Option<i64>) -> String {
    let local: DateTime<Local> = user_msg.with_timezone(&Local);
    let when = local.format("%Y-%m-%d %H:%M:%S");

    let mut out = format!("[Session time]\nUser's last message: {when} local\n");
    if let Some(secs) = gap {
        out.push_str(&format!("Idle since last activity: {}\n", humanize(secs)));
        if secs >= NUDGE_GAP_SECS {
            out.push_str(
                "The user has been away for a while; treat a message that \
                 resembles an earlier one as a fresh request in a new \
                 timeframe — re-verify anything time-sensitive instead of \
                 assuming it is a continuation of the last exchange.\n",
            );
        }
    }
    out
}

/// Humanize a duration in seconds, dropping trailing zero units.
fn humanize(secs: i64) -> String {
    fn plural(n: i64) -> &'static str {
        if n == 1 {
            ""
        } else {
            "s"
        }
    }
    if secs < 60 {
        return "less than a minute".into();
    }
    let mins = secs / 60;
    if mins < 60 {
        return format!("{mins} minute{}", plural(mins));
    }
    let hours = secs / 3600;
    if hours < 24 {
        let m = (secs % 3600) / 60;
        return if m == 0 {
            format!("{hours} hour{}", plural(hours))
        } else {
            format!("{hours} hour{}, {m} minute{}", plural(hours), plural(m))
        };
    }
    let days = secs / 86400;
    let rem = secs % 86400;
    let h = rem / 3600;
    let m = (rem % 3600) / 60;
    if h == 0 && m == 0 {
        format!("{days} day{}", plural(days))
    } else if h == 0 {
        format!("{days} day{}, {m} minute{}", plural(days), plural(m))
    } else {
        format!("{days} day{}, {h} hour{}", plural(days), plural(h))
    }
}

/// Upsert the `"time"` fragment in the ordered
/// `request.prompt_fragments` array; other extensions' fragments pass
/// through untouched, in their original order. Pure function of
/// (request, fragment): identical bytes in → identical bytes out.
fn upsert_fragment(mut req: Value, fragment: &str) -> Value {
    let mut frags: Vec<Value> = req
        .get("prompt_fragments")
        .and_then(|f| f.as_array())
        .cloned()
        .unwrap_or_default();
    frags.retain(|p| p.get(0).and_then(|i| i.as_str()) != Some(FRAGMENT_ID));
    frags.push(serde_json::json!([FRAGMENT_ID, fragment]));
    if let Some(obj) = req.as_object_mut() {
        obj.insert("prompt_fragments".into(), Value::Array(frags));
    }
    req
}

/// Resolve the session directory from the hook env (`SESSION` /
/// `SESSIONS_ROOT`), else the `session` field of a wrapped payload.
fn resolve_session_dir(payload: &Value) -> Option<PathBuf> {
    if let Ok(session) = std::env::var("SESSION") {
        if !session.is_empty() {
            let sessions_root =
                std::env::var("SESSIONS_ROOT").unwrap_or_else(|_| "sessions".into());
            return Some(if session.contains('/') || session.contains('\\') {
                PathBuf::from(&session)
            } else {
                PathBuf::from(&sessions_root).join(&session)
            });
        }
    }
    if let Some(s) = payload.get("session").and_then(|v| v.as_str()) {
        if !s.is_empty() {
            return Some(PathBuf::from(s));
        }
    }
    None
}

fn read_stdin_json() -> Value {
    let mut buf = String::new();
    if std::io::stdin().read_to_string(&mut buf).is_err() || buf.trim().is_empty() {
        return serde_json::json!({});
    }
    serde_json::from_str(&buf).unwrap_or(serde_json::json!({}))
}

/// The per-session on/off toggle. The webui time plugin writes a
/// one-line marker `sessions/<id>/.time_inject` (the same convention as
/// `.model` / `.effort`); a missing or unreadable marker means the
/// feature is ON by default. Explicit off values (case- and
/// whitespace-insensitive) disable the injection for that session.
fn time_inject_enabled(session_dir: &std::path::Path) -> bool {
    let raw = match fs::read_to_string(session_dir.join(MARKER_NAME)) {
        Ok(s) => s.trim().to_ascii_lowercase(),
        Err(_) => return true, // missing marker: default on
    };
    !matches!(raw.as_str(), "off" | "no" | "0" | "false")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn req_with(frags: &[(&str, &str)]) -> Value {
        let arr: Vec<Value> = frags
            .iter()
            .map(|(id, text)| serde_json::json!([id, text]))
            .collect();
        let mut req = serde_json::json!({"model": "m", "instructions": "BASE", "input": []});
        if !arr.is_empty() {
            req["prompt_fragments"] = Value::Array(arr);
        }
        req
    }

    #[test]
    fn upserts_time_fragment_after_others() {
        let out = upsert_fragment(req_with(&[("goal", "GOAL"), ("essence", "ESSENCE")]), "T");
        let frags = out["prompt_fragments"].as_array().unwrap();
        assert_eq!(frags.len(), 3);
        assert_eq!(frags[0], serde_json::json!(["goal", "GOAL"]));
        assert_eq!(frags[1], serde_json::json!(["essence", "ESSENCE"]));
        assert_eq!(frags[2], serde_json::json!(["time", "T"]));
    }

    #[test]
    fn replaces_stale_time_fragment_in_place() {
        let req = req_with(&[("time", "OLD"), ("goal", "G")]);
        let out = upsert_fragment(req, "NEW");
        let frags = out["prompt_fragments"].as_array().unwrap();
        // Other fragments keep their relative order; time is re-added
        // last (its position in the join order is ours to own).
        assert_eq!(
            frags.to_vec(),
            vec![
                serde_json::json!(["goal", "G"]),
                serde_json::json!(["time", "NEW"])
            ]
        );
        // Idempotent: re-transforming is byte-stable.
        let out2 = upsert_fragment(out.clone(), "NEW");
        assert_eq!(out, out2);
    }

    #[test]
    fn humanize_units() {
        assert_eq!(humanize(5), "less than a minute");
        assert_eq!(humanize(59), "less than a minute");
        assert_eq!(humanize(60), "1 minute");
        assert_eq!(humanize(90), "1 minute");
        assert_eq!(humanize(3599), "59 minutes");
        assert_eq!(humanize(7200), "2 hours");
        assert_eq!(humanize(7260), "2 hours, 1 minute");
        assert_eq!(humanize(86400), "1 day");
        assert_eq!(humanize(86400 + 7200), "1 day, 2 hours");
        assert_eq!(humanize(4 * 86400 + 2 * 3600 + 5 * 60), "4 days, 2 hours");
    }

    #[test]
    fn fragment_shape_first_message() {
        let ts: DateTime<Utc> = DateTime::parse_from_rfc3339("2026-09-28T01:30:00Z")
            .unwrap()
            .into();
        let frag = render_fragment(ts, None);
        assert!(frag.starts_with("[Session time]\nUser's last message: "));
        assert!(frag.contains("local"));
        assert!(!frag.contains("Idle since"));
        assert!(!frag.contains("re-verify"));
    }

    #[test]
    fn fragment_shape_long_gap_includes_nudge() {
        let ts: DateTime<Utc> = DateTime::parse_from_rfc3339("2026-09-28T01:30:00Z")
            .unwrap()
            .into();
        let frag = render_fragment(ts, Some(4 * 86400 + 2 * 3600 + 5 * 60));
        assert!(frag.contains("Idle since last activity: 4 days, 2 hours"));
        assert!(frag.contains("re-verify"));
    }

    #[test]
    fn fragment_shape_short_gap_no_nudge() {
        let ts: DateTime<Utc> = DateTime::parse_from_rfc3339("2026-09-28T01:30:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let frag = render_fragment(ts, Some(120));
        assert!(frag.contains("Idle since last activity: 2 minutes"));
        assert!(!frag.contains("re-verify"));
    }

    #[test]
    fn read_anchor_finds_last_user_and_gap() {
        let dir = std::env::temp_dir().join(format!("time-inject-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let log = dir.join("events.jsonl");
        let mut l = String::new();
        l.push_str("{\"v\":1,\"type\":\"user_message\",\"ts\":\"2026-09-28T00:00:00Z\",\"content\":\"a\"}\n");
        l.push_str("{\"v\":1,\"type\":\"assistant_message\",\"ts\":\"2026-09-28T00:00:30Z\",\"content\":\"b\"}\n");
        l.push_str(
            "{\"v\":1,\"type\":\"ext_status\",\"ts\":\"2026-09-28T00:01:00Z\",\"id\":\"x\"}\n",
        );
        l.push_str("{\"v\":1,\"type\":\"user_message\",\"ts\":\"2026-10-02T02:00:00Z\",\"content\":\"c\"}\n");
        std::fs::write(&log, l).unwrap();

        let anchor = read_anchor(&log).unwrap();
        assert_eq!(anchor.user_msg.to_rfc3339(), "2026-10-02T02:00:00+00:00");
        // gap = 2026-10-02T02:00:00Z - 2026-09-28T00:01:00Z = 4d + 1h59m
        //      = 4*86400 + 3600 + 3540 = 352740 seconds
        assert_eq!(anchor.gap.unwrap(), (4 * 86400) + 3600 + 3540);

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn read_anchor_first_message_has_no_gap() {
        let dir =
            std::env::temp_dir().join(format!("time-inject-test-first-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let log = dir.join("events.jsonl");
        std::fs::write(
            &log,
            "{\"v\":1,\"type\":\"user_message\",\"ts\":\"2026-10-02T02:00:00Z\",\"content\":\"hi\"}\n",
        )
        .unwrap();
        let anchor = read_anchor(&log).unwrap();
        assert_eq!(anchor.gap, None);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn time_inject_marker_gate() {
        let dir = std::env::temp_dir().join(format!("time-inject-marker-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let marker = dir.join(MARKER_NAME);

        // Missing marker: default on.
        assert!(time_inject_enabled(&dir));

        for off in ["off", " OFF \n", "no", "0", "false"] {
            std::fs::write(&marker, off).unwrap();
            assert!(!time_inject_enabled(&dir), "{off:?} should disable");
        }

        for on in ["on", "true", "1", "yes", ""] {
            std::fs::write(&marker, on).unwrap();
            assert!(time_inject_enabled(&dir), "{on:?} should enable");
        }

        std::fs::remove_file(&marker).unwrap();
        std::fs::remove_dir_all(&dir).ok();
    }
}

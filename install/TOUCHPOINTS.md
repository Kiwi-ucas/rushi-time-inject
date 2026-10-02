# Install map — every change the time-inject plugin makes to a rushi-webui checkout

Two files drop in (`server/time_inject.rs`, `client/time.rs`) plus one
stylesheet section (`client/time.css`); the rest is wiring. Every snippet
below is the current upstream code (`rushi-webui` v0.5.59), and
`scripts/sync-from-webui.sh` fails loudly if any anchor disappears.

The plugin's other half — the `model.before` hook that reads the marker this
toggle writes — is `hook-time-inject/` in this repository, plus the kernel
config wiring in §5. §6 states the contract the two halves share.

Paths are relative to the `rushi-webui` root. Line anchors are indicative —
the snippet text is the anchor.

---

## 1. Drop-in files

```sh
cp server/time_inject.rs  <webui>/bin/rushi-web/src/time_inject.rs   # the contract
cp client/time.rs         <webui>/web-leptos/src/time.rs              # the panel
cat client/time.css >> <webui>/web-leptos/style.css                   # the section
```

`bin/rushi-web/src/time_inject.rs` owns the **marker contract** — the file
name, the off values, the default-on rule — as pure functions
(`enabled_from_marker`, `marker_for`) with the hook's own value list as its
unit tests. It has no dependency on the rest of the server and touches no
I/O of its own.

`web-leptos/src/time.rs` holds the sidebar panel (`time_plugin_view`): the
switch row, the status copy, and the fetch-on-session-change effect. It is
content-only — the shared `#plugin-area` chrome provides the height cap and
the plugin bar.

---

## 2. Server wiring

### 2.1 `bin/rushi-web/src/main.rs`

```rust
mod time_inject;                                    // with the other `mod` lines (alphabetical)
```

The HTTP surface. Handlers stay in `main.rs` (house style: the plugin modules
carry the pure logic, `main.rs` carries the routes):

```rust
// ── time-inject plugin (rushi-time-inject) ─────────────────────────

/// v0.5.56: the per-session time-inject toggle state. The hook
/// (harness-hook-time-inject) reads the `.time_inject` marker on every
/// model call, so toggling applies from the session's next model call —
/// no loop restart. Absent marker means ON (the hook's default).
async fn get_time_inject(
    State(st): State<AppState>,
    Path(id): Path<String>,
) -> impl IntoResponse {
    let enabled = st.sessions.time_inject_enabled(&id);
    (StatusCode::OK, Json(serde_json::json!({ "enabled": enabled }))).into_response()
}

#[derive(Deserialize)]
struct TimeInjectBody {
    enabled: bool,
}

/// v0.5.56: set the per-session time-inject toggle. `enabled: false`
/// writes the explicit off marker; `enabled: true` clears it (back to
/// the default-on state).
async fn post_time_inject(
    State(st): State<AppState>,
    Path(id): Path<String>,
    Json(body): Json<TimeInjectBody>,
) -> impl IntoResponse {
    match st.sessions.set_time_inject(&id, body.enabled).await {
        Ok(()) => (StatusCode::OK, Json(serde_json::json!({ "ok": true, "enabled": body.enabled }))).into_response(),
        Err(e) => (StatusCode::BAD_REQUEST, e.to_string()).into_response(),
    }
}
```

The route, next to the other per-session routes:

```rust
        .route(
            "/api/sessions/{id}/time-inject",
            get(get_time_inject).post(post_time_inject),
        )
```

### 2.2 `bin/rushi-web/src/sessions.rs` — the state layer

The two `SessionManager` methods. They are file I/O only: the decision comes
from §1's module, so the webui and the hook cannot drift.

```rust
    /// v0.5.56: whether time-injection is ON for this session — the
    /// sidebar `time` plugin's state. The contract (marker name, off
    /// values, the default-on rule) lives in [`crate::time_inject`], the
    /// same module the hook mirrors, so this is only the file I/O.
    pub fn time_inject_enabled(&self, id: &str) -> bool {
        crate::time_inject::enabled_from_marker(read_marker(
            &self.session_dir(id),
            crate::time_inject::MARKER,
        )
        .as_deref())
    }

    /// v0.5.56: record the time-inject toggle. `true` clears the marker
    /// (back to the default-on state); `false` writes the explicit off
    /// marker the hook reads. The hook re-reads it on every model call, so
    /// this applies from the session's next call — no loop restart.
    pub async fn set_time_inject(&self, id: &str, enabled: bool) -> Result<()> {
        validate_session_name(id)?;
        self.ensure_session(id).await?;
        let path = self.session_dir(id).join(crate::time_inject::MARKER);
        match crate::time_inject::marker_for(enabled) {
            None => {
                let _ = fs::remove_file(&path).await;
            }
            Some(value) => fs::write(&path, value).await?,
        }
        Ok(())
    }
```

---

## 3. Client wiring

### 3.1 `web-leptos/src/model.rs` — the type and the state slice

```rust
/// v0.5.56: the session's time-inject toggle state (sidebar time plugin).
/// The server mirrors the harness hook's marker
/// (`sessions/<id>/.time_inject`, written by the plugin's toggle): a
/// missing marker is ON (the hook default), an explicit off value is OFF.
#[derive(Clone, Debug, Deserialize)]
pub struct TimeInjectView {
    #[serde(default = "default_time_inject_enabled")]
    pub enabled: bool,
}

fn default_time_inject_enabled() -> bool {
    true
}
```

```rust
    /// v0.5.56: the time plugin (sidebar): the active session's
    /// time-inject toggle state. None until the first fetch. The marker
    /// is read live by the harness hook on every model call, so the
    /// server's answer is the current state.
    pub time_inject: RwSignal<Option<TimeInjectView>>,
```

…and in `AppState::new`:

```rust
            time_inject: RwSignal::new(None),
```

### 3.2 `web-leptos/src/api.rs` — the two calls

```rust
/// v0.5.56: the session's time-inject toggle (sidebar time plugin). The
/// server reads the session's `.time_inject` marker (the same marker the
/// harness hook checks on every model call), so this is the live state.
pub async fn load_time_inject(id: &str) -> Result<crate::model::TimeInjectView, String> {
    let res = Request::get(&format!("/api/sessions/{id}/time-inject"))
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let text = res.text().await.map_err(|e| e.to_string())?;
    serde_json::from_str(&text).map_err(|e| e.to_string())
}

/// v0.5.56: set the session's time-inject toggle. `enabled` takes effect
/// from the session's next model call — no loop restart.
pub async fn set_time_inject(id: &str, enabled: bool) -> Result<crate::model::TimeInjectView, String> {
    let payload = json!({ "enabled": enabled });
    let status = post_json(&format!("/api/sessions/{id}/time-inject"), &payload).await?;
    if status >= 400 {
        Err(format!("set time-inject failed: HTTP {status}"))
    } else {
        // Refetch so the view mirrors the server's post-write state.
        load_time_inject(id).await
    }
}
```

### 3.3 `web-leptos/src/plugins.rs` — the `#plugin-area` entry

```rust
    PluginDef { id: "time", label: "time" },
```

(`PLUGINS`'s length moves with it — `[PluginDef; 3]` becomes `[PluginDef; 4]`.)

### 3.4 `web-leptos/src/ui.rs` — the dispatcher arm

```rust
        "time" => crate::time::time_plugin_view(state).into_any(),
```

### 3.5 `web-leptos/src/lib.rs` — mounting

```rust
mod time;                                  // with the other `mod` lines (alphabetical)
```

### 3.6 `README.md` — the API row

```md
| GET/POST | `/api/sessions/{id}/time-inject` | read / set the session's time-inject toggle (`{"enabled":bool}`) |
```

---

## 4. Stylesheet

`client/time.css` is the plugin's section, **appended** to
`web-leptos/style.css` (never inserted): every selector is namespaced
(`#time-body`, `.time-*`), so appending is equivalent to the upstream
in-place layout. The extraction rule is therefore mechanical:
`sync-from-webui.sh` takes this plugin's banner up to the **next** section
banner (here the end of the file). The end is the next banner rather than
EOF on purpose — this section sits *after* the rewind plugin's, and a
suffix-to-EOF rule would swallow whatever a later version appends (it did:
v0.5.59's move of this section to the end silently added 22 of its lines to
the rewind package's mirror until both rules were tightened).

No existing upstream rule needs an edit for this plugin — unlike the rewind
plugin, whose section joins the shared `layout-full` and scrollbar rules.
`client/time-additive.css` is therefore empty by design; anything that must
survive a re-sync without an upstream counterpart goes there.

---

## 5. Kernel side (the hook that reads the marker)

The hook lives in `hook-time-inject/` here; the kernel wiring is:

```toml
# config.toml — declare the hook and put it in the chain
[hooks.defs.time-inject]
command = "harness-hook-time-inject"          # resolved on PATH

[hooks]
steps = ["goal-arm", "essence-inject", "time-inject"]
```

The binary is picked up through the harness's `bin/` symlink farm
(`bin/harness-hook-time-inject` → `hook-time-inject/target/debug/…`). Build
it with:

```sh
cd hook-time-inject && cargo build
```

No webui change is needed for this half — the hook is a pure function of the
session log plus the marker file, and it runs on every `model.before`.

---

## 6. The marker contract (the two halves' ABI)

`sessions/<id>/.time_inject`, one line, read by the hook on every model call:

| file | meaning |
|---|---|
| absent, empty, or anything outside the off set | **ON** (the default) |
| `off` / `no` / `0` / `false` (trimmed, case-insensitive) | **OFF** |

The toggle writes exactly `off` and switches back ON by **removing** the
file — it never writes an "on" value, so a session that never opted out has
no marker at all.

Both sides test the same value list, and each test names the other side:

* upstream `bin/rushi-web/src/time_inject.rs::tests` —
  `the_off_values_are_the_hooks_off_values`,
  `every_other_value_stays_on`, `absent_marker_is_on`,
  `the_write_value_round_trips_through_the_read_path`;
* hook `hook-time-inject/src/main.rs::tests::time_inject_marker_gate`.

A change to either half's list must change the other's — that is the only
place this plugin has a cross-repo invariant.

---

## 7. Build & verify

```sh
# upstream (rushi-webui)
cargo test -p rushi-web                     # the contract tests + the rest of the server
(cd web-leptos && trunk build)              # the WASM bundle
python3 e2e/time_inject_probe.py [port]     # CDP: switch -> marker -> hook (37 checks)

# this package
(cd hook-time-inject && cargo build)
sh scripts/time-inject-e2e.sh               # the hook's own 9 cases / 20 assertions
python3 e2e/time_inject_probe.py [port]     # the same CDP probe, run from here
```

The probe resolves the server binary, the kernel config and the hook binary
itself (`RUSHI_WEB_BIN`, `RUSHI_WEB_CONFIG`, `RUSHI_TIME_INJECT_HOOK`
override), so it runs from either home.

`e2e/` is not uploaded — the probes are local verification tools, ignored by
git (account rule) and present only in a working tree that obtained them
(from `rushi-webui/e2e/`, via `scripts/sync-from-webui.sh`). Everything else
in this list runs from a fresh clone.

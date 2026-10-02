#!/usr/bin/env python3
"""time-inject plugin end-to-end probe (the sidebar toggle, its marker, and
the hook that reads it).

The plugin has two halves and this probe drives the seam between them:

    webui switch  ->  sessions/<id>/.time_inject  ->  harness-hook-time-inject
    (this file's CDP checks)   (the marker)            (the real hook binary)

It runs a throwaway `rushi-web` against a purpose-built fixture sessions
root and drives a real Chromium over CDP, then runs the **real hook binary**
against the same fixture — the only way to prove that what the switch writes
is what the kernel-side hook obeys.

Fixture (`/tmp/ti-e2e/sessions/<session>/events.jsonl`):

    timeinjectprobe   2 rounds, 1 day apart (a gap the hook reports)
    timeinjectprobe2  1 round — never touched: the toggle is per-session

Checks:
  C1   the registry: `#plugin-area`'s bar lists the `time` plugin (4th), and
       selecting it mounts the panel inside the shared height cap.
  C2   the panel: label, switch, and the ON status copy — a session with no
       marker file is ON (the hook's default; the contract lives in
       `bin/rushi-web/src/time_inject.rs`).
  C3   the toggle off: the switch writes `off` into `.time_inject`, the API
       reports it, the panel flips to the OFF copy, **no event is appended**
       to the log, and the other session is untouched.
  C4   the toggle survives a page reload (the state is the server's, not a
       component's).
  C5   the toggle back on: the marker file is *removed* (back to default-on),
       not rewritten.
  C6   the hook obeys the marker the switch wrote: no `time` fragment while
       off, the fragment (header + the timestamp + the idle gap + the
       re-verify nudge) while on, and other fragments are preserved.
  C7   the log is byte-identical after every toggle.

Local-only diagnostic (`e2e/` is gitignored upstream). Usage:
    python3 e2e/time_inject_probe.py [port]
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model_panel_probe import Cdp, ws_connect  # noqa: E402

HOST = "127.0.0.1"
ROOT = "/tmp/ti-e2e/sessions"
SESSION = "timeinjectprobe"
SESSION2 = "timeinjectprobe2"
MARKER = ".time_inject"
CHROME = os.path.expanduser(
    "~/Library/Caches/ms-playwright/chromium-1148/chrome-mac/Chromium.app/Contents/MacOS/Chromium")
PROF = "/tmp/rushi-time-inject-probe-profile"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The probe lives in `rushi-webui/e2e/` (the development home, gitignored
# upstream) and in the packaged `rushi-time-inject/e2e/` mirror. Resolve the
# server binary, the kernel config and the hook binary from either home;
# RUSHI_WEB_BIN / RUSHI_TIME_INJECT_HOOK override.
_ROOT = os.path.dirname(HERE)
_CANDIDATES = [os.environ.get("RUSHI_WEB_BIN"),
               os.path.join(HERE, "target", "debug", "rushi-web"),
               os.path.join(_ROOT, "rushi-webui", "target", "debug", "rushi-web")]
WEB_BIN = next((c for c in _CANDIDATES if c and os.path.exists(c)), _CANDIDATES[1])
CONFIG = os.path.join(_ROOT, "rushi", "config.toml")
_HOOK_CANDIDATES = [os.environ.get("RUSHI_TIME_INJECT_HOOK"),
                    os.path.join(HERE, "hook-time-inject", "target", "debug",
                                 "harness-hook-time-inject"),
                    os.path.join(_ROOT, "rushi-time-inject", "hook-time-inject",
                                 "target", "debug", "harness-hook-time-inject"),
                    os.path.join(_ROOT, "bin", "harness-hook-time-inject")]
HOOK_BIN = next((c for c in _HOOK_CANDIDATES if c and os.path.exists(c)), _HOOK_CANDIDATES[1])

# Two rounds a day apart: the hook reports the gap between the assistant
# reply and the second question (that is the event immediately before the
# last `user_message`).
FIXTURE = [
    {"v": 1, "type": "user_message", "ts": "2026-09-28T01:00:00Z",
     "content": "round one \u2014 is the parser in the kernel crate?"},
    {"v": 1, "type": "assistant_message", "ts": "2026-09-28T01:00:40Z",
     "content": "yes"},
    {"v": 1, "type": "user_message", "ts": "2026-10-02T01:00:00Z",
     "content": "round two \u2014 and the fixture tests?"},
    {"v": 1, "type": "assistant_message", "ts": "2026-10-02T01:00:05Z",
     "content": "added"},
]
FIXTURE2 = [
    {"v": 1, "type": "user_message", "ts": "2026-10-02T02:00:00Z",
     "content": "another session"},
    {"v": 1, "type": "assistant_message", "ts": "2026-10-02T02:00:01Z", "content": "ok"},
]

ON_COPY = "the agent is told when your last message was sent"
OFF_COPY = "no time info is injected"
# The hook is driven the way the kernel drives it: a model request on stdin,
# with the window and the session on the environment.
REQ = ('{"model":"m","instructions":"BASE","input":[{"role":"user","content":"hi"}],'
       '"prompt_fragments":[["essence","ESSENCE TEXT"]]}')


def log(*a):
    print(*a, flush=True)


class Res:
    """Tiny assertion bookkeeper: one line per check, a PASS/FAIL verdict."""

    def __init__(self):
        self.fails = []
        self.n = 0

    def check(self, name, cond, detail=""):
        self.n += 1
        print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"  <- {detail}"),
              flush=True)
        if not cond:
            self.fails.append(name)
        return cond


def write_fixture():
    for name, evs in ((SESSION, FIXTURE), (SESSION2, FIXTURE2)):
        d = os.path.join(ROOT, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "events.jsonl"), "w") as f:
            for ev in evs:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        marker = os.path.join(d, MARKER)
        if os.path.exists(marker):
            os.remove(marker)


def marker_path(session=SESSION):
    return os.path.join(ROOT, session, MARKER)


def log_bytes(session=SESSION):
    with open(os.path.join(ROOT, session, "events.jsonl"), "rb") as f:
        return f.read()


def start_server(port):
    proc = subprocess.Popen(
        [WEB_BIN, "--host", HOST, "--port", str(port), "--sessions-root", ROOT,
         "--config", CONFIG],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://{HOST}:{port}/api/sessions", timeout=1):
                return proc
        except Exception:
            time.sleep(0.25)
    proc.terminate()
    raise SystemExit("FAIL: rushi-web did not come up")


def get_json(port, path):
    with urllib.request.urlopen(f"http://{HOST}:{port}{path}", timeout=5) as r:
        return json.load(r)


def run_hook(session=SESSION, window="model.before"):
    """Run the real hook binary the way the kernel does (stdin request +
    $HARNESS_WINDOW / $SESSION / $SESSIONS_ROOT on the binary side)."""
    env = dict(os.environ)
    env.update({"HARNESS_WINDOW": window, "SESSION": session, "SESSIONS_ROOT": ROOT})
    out = subprocess.run([HOOK_BIN], input=REQ.encode(), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env, timeout=30)
    return out.stdout.decode()


# ── JS snippets ────────────────────────────────────────────────────
PANEL = """(function(){
  const q = s => document.querySelector(s);
  const sw = q('#time-body .time-switch');
  const area = q('#plugin-area');
  return JSON.stringify({
    bar: (q('#plugin-list')||{}).innerText||'',
    menu: [...document.querySelectorAll('#plugin-menu .plugin-item')]
            .map(x=>x.innerText.trim().toLowerCase()),
    body: !!q('#time-body'),
    label: ((q('#time-body .time-label')||{}).innerText||'').trim(),
    switch: !!sw,
    on: !!sw && sw.className.split(/\\s+/).includes('on'),
    cls: sw ? sw.className : null,
    knob: !!q('#time-body .time-switch-knob'),
    status: ((q('#time-body .time-status')||{}).innerText||'').trim(),
    loading: !!q('#time-body .time-loading'),
    err: !!q('#time-body .time-error'),
    cap: area ? getComputedStyle(area).maxHeight : null,
    areaH: area ? Math.round(area.getBoundingClientRect().height) : null,
  });
})()"""

OPEN_PLUGIN = ("(function(){const b=document.querySelector('#plugin-list'); if(!b) return 'nf';"
               "b.click(); return 'ok';})()")

PICK_TIME_PLUGIN = ("(function(){const items=[...document.querySelectorAll('#plugin-menu .plugin-item')];"
                    "const b=items.find(x=>x.innerText.trim().toLowerCase()==='time');"
                    "if(!b) return 'nf'; b.click(); return 'ok';})()")

SELECT_SESSION = ("(function(){const items=[...document.querySelectorAll('#session-list .session-item')];"
                  "const it=items.find(x=>x.innerText.includes('" + SESSION + "'));"
                  "if(!it) return 'nf'; it.click(); return 'ok';})()")

CLICK_SWITCH = ("(function(){const b=document.querySelector('#time-body .time-switch');"
                "if(!b) return 'nf'; b.click(); return 'ok';})()")


def wait_for(c, expr, want, timeout=8.0, poll=0.25):
    """Poll `expr` (a JS boolean/JSON) until `want(value)` is true."""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = c.ev(expr)
        try:
            v = json.loads(last)
        except Exception:
            v = last
        if want(v):
            return v
        time.sleep(poll)
    return None


def panel(c):
    return json.loads(c.ev(PANEL))


def open_browser(port):
    sock = socket.socket()
    sock.bind((HOST, 0))
    cdp_port = sock.getsockname()[1]
    sock.close()
    if os.path.isdir(PROF):
        shutil.rmtree(PROF, ignore_errors=True)
    proc = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
                             f"--remote-debugging-port={cdp_port}", f"--user-data-dir={PROF}",
                             "--window-size=1400,1000", f"http://{HOST}:{port}/"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ws_url = None
    deadline = time.time() + 30
    while time.time() < deadline and not ws_url:
        try:
            with urllib.request.urlopen(f"http://{HOST}:{cdp_port}/json", timeout=2) as r:
                for t in json.load(r):
                    if t.get("type") == "page" and f"127.0.0.1:{port}" in t.get("url", ""):
                        ws_url = t.get("webSocketDebuggerUrl")
                        break
        except Exception:
            pass
        time.sleep(0.5)
    if not ws_url:
        proc.terminate()
        raise SystemExit("FAIL: no CDP target")
    s, buf = ws_connect(ws_url)
    return proc, Cdp(s, buf)


def reload_page(c):
    c.ev("location.reload(); 'ok'")
    time.sleep(2.5)


def select_and_open(c):
    """Select the fixture session and mount the `time` plugin panel."""
    c.ev(SELECT_SESSION)
    wait_for(c, "!!document.querySelector('#time-body .time-status') || "
                "!!document.querySelector('#time-body .time-loading')", lambda v: v is True)
    if not panel(c)["body"]:
        c.ev(OPEN_PLUGIN)
        time.sleep(0.3)
        c.ev(PICK_TIME_PLUGIN)
        time.sleep(0.6)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8493
    if not os.path.exists(WEB_BIN):
        print(f"FAIL: {WEB_BIN} missing (cargo build -p rushi-web)"); return 1
    if not os.path.exists(HOOK_BIN):
        print(f"FAIL: {HOOK_BIN} missing (cd rushi-time-inject/hook-time-inject && cargo build)")
        return 1
    write_fixture()
    server = start_server(port)
    res = Res()
    chrome = None
    try:
        # ── C1: the registry + the panel mounts inside the shared cap ──
        names = [s["name"] for s in get_json(port, "/api/sessions")]
        res.check("C1: the fixture sessions are served", SESSION in names and SESSION2 in names,
                  str(names))
        chrome, c = open_browser(port)
        time.sleep(2.0)
        rendered = wait_for(c, "!!document.querySelector('#session-list')", lambda v: v is True,
                            timeout=15)
        res.check("C1: the sidebar rendered", rendered is True, str(rendered))
        c.ev(SELECT_SESSION)
        ok = wait_for(c, "document.querySelectorAll('#transcript .ev-user').length", lambda v: v >= 1)
        res.check("C1: the session was selected", bool(ok), str(ok))
        c.ev(OPEN_PLUGIN)
        time.sleep(0.3)
        menu = json.loads(c.ev(PANEL))["menu"]
        res.check("C1: the registry lists the time plugin", "time" in menu, str(menu))
        res.check("C1: ... alongside the three plugins that came before it",
                  all(x in menu for x in ("goal", "essence", "rewind")), str(menu))
        picked = c.ev(PICK_TIME_PLUGIN)
        res.check("C1: picking it from the bar works", picked == "ok", str(picked))
        p = wait_for(c, PANEL, lambda v: v.get("body") and not v.get("loading"), timeout=8)
        p = p or panel(c)
        res.check("C1: the panel is mounted (#time-body)", p["body"], str(p))
        res.check("C1: the plugin area still carries its height cap",
                  p["cap"] not in (None, "none"), str(p.get("cap")))

        # ── C2: default ON (no marker file) ──
        res.check("C2: no marker file exists yet", not os.path.exists(marker_path()))
        api0 = get_json(port, f"/api/sessions/{SESSION}/time-inject")
        res.check("C2: the API says ON by default", api0.get("enabled") is True, str(api0))
        res.check("C2: the switch is rendered", p["switch"] and p["knob"], str(p))
        res.check("C2: ... in the ON state", p["on"], str(p.get("cls")))
        res.check("C2: the row is labelled", p["label"].lower() == "time inject", repr(p["label"]))
        res.check("C2: the status explains what ON does", ON_COPY in p["status"], repr(p["status"]))
        log0 = log_bytes()

        # ── C3: toggle OFF → the marker, the API, the panel ──
        res.check("C3: clicking the switch", c.ev(CLICK_SWITCH) == "ok")
        got = None
        for _ in range(24):
            if os.path.exists(marker_path()):
                got = open(marker_path()).read()
                break
            time.sleep(0.25)
        res.check("C3: the switch wrote the marker file", got is not None, str(got))
        res.check("C3: the marker holds exactly the hook's off value", got == "off", repr(got))
        api1 = get_json(port, f"/api/sessions/{SESSION}/time-inject")
        res.check("C3: the API reports OFF", api1.get("enabled") is False, str(api1))
        p1 = wait_for(c, PANEL, lambda v: not v.get("on") and OFF_COPY in v.get("status", ""))
        p1 = p1 or panel(c)
        res.check("C3: the switch flipped to OFF", not p1["on"], str(p1.get("cls")))
        res.check("C3: the status explains what OFF does", OFF_COPY in p1["status"], repr(p1["status"]))
        res.check("C3: toggling appended no event to the log", log_bytes() == log0,
                  f"{len(log0)} -> {len(log_bytes())} bytes")
        api_other = get_json(port, f"/api/sessions/{SESSION2}/time-inject")
        res.check("C3: the other session is untouched (per-session toggle)",
                  api_other.get("enabled") is True and not os.path.exists(marker_path(SESSION2)),
                  f"{api_other} marker={os.path.exists(marker_path(SESSION2))}")

        # ── C4: the state is the server's, not the component's ──
        reload_page(c)
        time.sleep(1.0)
        select_and_open(c)
        p2 = wait_for(c, PANEL, lambda v: v.get("body") and not v.get("loading"), timeout=10)
        p2 = p2 or panel(c)
        res.check("C4: after a reload the panel still reports OFF",
                  p2["body"] and not p2["on"], str(p2.get("cls")))
        res.check("C4: ... and the marker file is still there", os.path.exists(marker_path()))

        # ── C5/C6: the hook obeys the switch (the whole point) ──
        out_off = run_hook()
        res.check("C6: with the switch OFF the hook injects nothing",
                  '"time"' not in out_off and "Session time" not in out_off, out_off[:200])
        res.check("C6: ... and it reports no effect at all ({})",
                  out_off.strip() == "{}", repr(out_off[:120]))
        res.check("C5: clicking the switch again", c.ev(CLICK_SWITCH) == "ok")
        gone = False
        for _ in range(24):
            if not os.path.exists(marker_path()):
                gone = True
                break
            time.sleep(0.25)
        res.check("C5: ON removes the marker (back to the default, not a rewrite)", gone,
                  f"exists={os.path.exists(marker_path())}")
        api2 = get_json(port, f"/api/sessions/{SESSION}/time-inject")
        res.check("C5: the API reports ON again", api2.get("enabled") is True, str(api2))
        p3 = wait_for(c, PANEL, lambda v: v.get("on") and ON_COPY in v.get("status", ""))
        p3 = p3 or panel(c)
        res.check("C5: the switch shows ON again", p3["on"], str(p3.get("cls")))
        out_on = run_hook()
        res.check("C6: with the switch ON the hook injects the fragment",
                  '"time"' in out_on, out_on[:200])
        res.check("C6: the fragment carries the header, the timestamp and the gap",
                  "Session time" in out_on and "User's last message:" in out_on
                  and "Idle since last activity:" in out_on, out_on[:400])
        res.check("C6: a 4-day gap is humanized the way the hook's own e2e proves",
                  "3 days, 23 hours" in out_on, out_on[:400])
        res.check("C6: ... and earns the re-verify nudge",
                  "re-verify" in out_on, out_on[:400])
        res.check("C6: ... while other fragments pass through untouched",
                  '"essence"' in out_on, out_on[:200])
        res.check("C6: the wrong window is still a no-op",
                  run_hook(window="tool.before").strip() == "{}", run_hook(window="tool.before")[:120])

        # ── C7: nothing in the log changed across every toggle ──
        res.check("C7: the event log is byte-identical after all of it", log_bytes() == log0,
                  f"{len(log0)} -> {len(log_bytes())} bytes")
    finally:
        try:
            if chrome:
                chrome.terminate()
        except Exception:
            pass
        server.terminate()

    print(flush=True)
    if res.fails:
        print(f"FAIL ({len(res.fails)}/{res.n} checks failed): " + ", ".join(res.fails))
        return 1
    print(f"PASS ({res.n} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

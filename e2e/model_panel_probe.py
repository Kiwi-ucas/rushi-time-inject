#!/usr/bin/env python3
"""Probe the v0.5.45 model settings panel + session-card model chip.

Checks, in a real browser:
  - the settings button sits in the sidebar's bottom bar (#status-bar);
  - the panel is a stack of PROVIDER cards (one per base_url/api_key_env),
    field order base_url -> api key -> api_key_env -> model rows;
  - the card's left dot auto-probes reachability when the panel opens;
  - a model row's arrow expands its details;
  - the panel has no "active model" block, no hint line and no Chinese;
  - the new-session dialog offers model + thinking effort.

Local-only diagnostic (e2e/ is gitignored). Usage:
    python3 e2e/model_panel_probe.py [port]
"""
import base64, json, os, re, socket, struct, subprocess, sys, time, urllib.request

HOST = "127.0.0.1"
CHROME = os.path.expanduser(
    "~/Library/Caches/ms-playwright/chromium-1148/chrome-mac/Chromium.app/Contents/MacOS/Chromium")
PROF = "/tmp/rushi-model-panel-profile"

BTN_IN_STATUS_BAR = "!!document.querySelector('#status-bar #model-settings')"
CLICK = "document.querySelector('#model-settings').click(); 'clicked'"
PANEL = """(function(){
  const d = document.querySelector('#ms-dialog');
  if (!d) return JSON.stringify({dialog:false});
  const rows = [...d.querySelectorAll('.ms-list .ms-row')].map(r => ({
    dotLit: !!r.querySelector('.ms-card-dot.on'),
    dotUnknown: !!r.querySelector('.ms-card-dot.unknown'),
    dotTitle: (r.querySelector('.ms-card-dot')||{}).title || '',
    name: (r.querySelector('.ms-name')||{}).innerText || '',
    sub: (r.querySelector('.ms-sub')||{}).innerText || '',
    selected: r.classList.contains('ms-sel'),
  }));
  const form = d.querySelector('.ms-form');
  return JSON.stringify({
    dialog: true,
    rows: rows,
    formLabels: form ? [...form.querySelectorAll('.ns-label')].map(x=>x.innerText.trim()) : [],
    formBaseUrl: form ? (form.querySelector('#ms-base-url')||{}).value : null,
    models: form ? [...form.querySelectorAll('.ms-model-row input.ms-model-id')].map(i=>i.value) : [],
    details: form ? form.querySelectorAll('.ms-model-details').length : 0,
    hasActiveBlock: !!d.querySelector('.ms-active'),
    hasHint: !!d.querySelector('.ms-list-hint'),
    chinese: /[\\u4e00-\\u9fa5]/.test(d.innerText),
    buttons: [...d.querySelectorAll('.ns-btn')].map(b=>b.innerText.trim()),
  });
})()"""
# Second rail row (the DeepSeek provider) — select it and re-read the form.
SELECT_SECOND = "(function(){const r=document.querySelectorAll('#ms-dialog .ms-list .ms-row')[1]; if(!r) return 'nf'; r.click(); return 'ok';})()"
TOGGLE_FIRST_ARROW = "(function(){const b=document.querySelector('#ms-dialog .ms-model-row .ms-mini'); if(!b) return 'nf'; b.click(); return 'ok';})()"
DETAIL_COUNT = "document.querySelectorAll('#ms-dialog .ms-model-details').length"

OPEN_NS = "(function(){const b=document.querySelector('#btn-new'); if(!b) return 'nf'; b.click(); return 'ok';})()"
NS_PANEL = """(function(){
  const d = document.querySelector('#ns-dialog');
  if (!d) return JSON.stringify({open:false});
  return JSON.stringify({
    open: true,
    labels: [...d.querySelectorAll('.ns-label')].map(x=>x.innerText.trim()),
    segs: [...d.querySelectorAll('.ns-seg')].map(sg=>[...sg.querySelectorAll('button')].map(b=>b.innerText.trim())),
  });
})()"""
CLOSE_NS = "(function(){const b=[...document.querySelectorAll('#ns-dialog .ns-btn')].find(x=>/cancel/i.test(x.innerText)); if(b) b.click(); return 'ok';})()"


def ws_connect(u):
    m = re.match(r"ws://([^:/]+)(?::(\d+))?(/.*)$", u)
    host, port, path = m.group(1), m.group(2) or "80", m.group(3)
    s = socket.create_connection((host, int(port)), timeout=15)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(1024)
    return s, buf.split(b"\r\n\r\n", 1)[1]


def _rx(s, n, buf):
    while len(buf) < n:
        buf += s.recv(65536)
    return buf[:n], buf[n:]


def recv(s, buf):
    h, buf = _rx(s, 2, buf)
    op, ln = h[0] & 0x0F, h[1] & 0x7F
    if ln == 126:
        e, buf = _rx(s, 2, buf); ln = struct.unpack(">H", e)[0]
    elif ln == 127:
        e, buf = _rx(s, 8, buf); ln = struct.unpack(">Q", e)[0]
    p, buf = _rx(s, ln, buf)
    return (p.decode("utf-8", "replace"), buf) if op in (0, 1) else (None, buf)


def send(s, text):
    p = text.encode(); ln = len(p)
    hdr = bytes([0x81, 0x80 | ln]) if ln < 126 else bytes([0x81, 0x80 | 126]) + struct.pack(">H", ln)
    mk = os.urandom(4)
    s.sendall(hdr + mk + bytes(b ^ mk[i % 4] for i, b in enumerate(p)))


class Cdp:
    def __init__(self, s, buf):
        self.s, self.buf, self.n = s, buf, 1

    def ev(self, expr):
        mid = self.n; self.n += 1
        send(self.s, json.dumps({"id": mid, "method": "Runtime.evaluate",
                                 "params": {"expression": expr, "returnByValue": True}}))
        for _ in range(150):
            try:
                t, self.buf = recv(self.s, self.buf)
            except socket.timeout:
                return None
            if t is None:
                continue
            m = json.loads(t)
            if m.get("id") == mid:
                return m.get("result", {}).get("result", {}).get("value")
        return None


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8480
    base = f"http://{HOST}:{port}"
    probe = socket.socket(); probe.bind((HOST, 0)); cdp_port = probe.getsockname()[1]; probe.close()
    proc = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
                             f"--remote-debugging-port={cdp_port}", f"--user-data-dir={PROF}",
                             "--window-size=1400,1000", base + "/"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = None
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://{HOST}:{cdp_port}/json", timeout=2) as r:
                    for t in json.load(r):
                        if t.get("type") == "page" and f"127.0.0.1:{port}" in t.get("url", ""):
                            ws_url = t.get("webSocketDebuggerUrl"); break
            except Exception:
                pass
            if ws_url:
                break
            time.sleep(0.5)
        if not ws_url:
            print("FAIL: no CDP target"); return 1
        s, buf = ws_connect(ws_url)
        c = Cdp(s, buf)
        deadline = time.time() + 60
        while time.time() < deadline and not c.ev(BTN_IN_STATUS_BAR):
            time.sleep(0.5)
        print("button in #status-bar:", c.ev(BTN_IN_STATUS_BAR))
        if not c.ev(BTN_IN_STATUS_BAR):
            print("FAIL: #model-settings is not inside #status-bar")
            return 1

        print("click settings:", c.ev(CLICK))
        time.sleep(1.5)
        print("details before toggle:", c.ev(DETAIL_COUNT))
        print("panel:", c.ev(PANEL))
        # The reachability probe runs on open; give it a moment.
        time.sleep(3.5)
        print("panel after probes:", c.ev(PANEL))

        print("select 2nd provider:", c.ev(SELECT_SECOND))
        time.sleep(0.6)
        print("form after switch:", c.ev(PANEL))

        print("toggle arrow:", c.ev(TOGGLE_FIRST_ARROW))
        time.sleep(0.4)
        print("details after toggle:", c.ev(DETAIL_COUNT))

        print("close panel:", c.ev("(function(){const b=[...document.querySelectorAll('#ms-dialog .ns-btn')].find(x=>x.innerText.trim()==='Close'); b && b.click(); return 'ok';})()"))
        time.sleep(0.8)
        print("open new-session:", c.ev(OPEN_NS))
        time.sleep(1.2)
        print("new-session:", c.ev(NS_PANEL))
        print("close new-session:", c.ev(CLOSE_NS))
        return 0
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.exit(main())

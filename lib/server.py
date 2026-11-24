#!/usr/bin/env python3
"""Mission control's local web server.

Shows the dashboard at http://localhost:PORT and lets you message an agent from its session page.
It listens on this machine only (127.0.0.1). A message is typed into the agent's tmux pane only when
the request carries this machine's secret token, which only pages served here contain, and comes from
this server's own pages (Host and Origin checked), so no website or other page can send anything.
Sending reuses your existing SSH shared connections like everything else: it never logs in by itself.

Phone access goes through Tailscale (`mctl phone` sets it up): Tailscale Serve forwards your own devices'
requests here over your private tailnet and tags each with the Tailscale login it came from. Requests
arriving that way are accepted only from your own Tailscale login; anything else from outside is refused,
so even turning on Tailscale Funnel (public) by mistake would not expose the dashboard.
"""
import http.server
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse

MC_DIR = os.path.realpath(os.environ.get("MC_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ARCHIVE = os.environ.get("MC_ARCHIVE_DIR") or os.path.expanduser("~/.local/state/mission-control")
PORT0 = int(os.environ.get("MC_PORT") or 8765)
MY_HOST = (os.environ.get("MC_HOST") or "").lower()   # this machine's name in mission control
LAST_NUDGE = [0.0]
SERVE_EXT = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css",
             ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon"}
os.makedirs(ARCHIVE, exist_ok=True)


def load_token():
    p = os.path.join(ARCHIVE, "token")
    try:
        t = open(p).read().strip()
        if len(t) >= 32:
            return t
    except OSError:
        pass
    t = secrets.token_urlsafe(32)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(t)
    return t


TOKEN = load_token()
PORT = PORT0
_TS = {"t": 0, "name": "", "login": ""}


def tailscale():
    """This machine's Tailscale name and the owner's login (cached 5 min). Empty if Tailscale isn't here."""
    if time.time() - _TS["t"] < 300:
        return _TS
    _TS["t"] = time.time()
    exe = shutil.which("tailscale.exe") or shutil.which("tailscale")
    for c in ("/mnt/c/Program Files/Tailscale/tailscale.exe",):
        if not exe and os.path.exists(c):
            exe = c
    if not exe:
        return _TS
    try:
        d = json.loads(subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=15).stdout or "{}")
        me = d.get("Self") or {}
        _TS["name"] = (me.get("DNSName") or "").rstrip(".").lower()
        user = (d.get("User") or {}).get(str(me.get("UserID")), {})
        _TS["login"] = (user.get("LoginName") or "").lower()
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return _TS


def allowed_logins():
    extra = [x.lower() for x in (os.environ.get("MC_WEB_LOGINS") or "").split() if x]
    return set(extra or ([tailscale()["login"]] if tailscale()["login"] else []))


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "mission-control"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), fmt % args))

    def _allowed_hosts(self):
        h = {"127.0.0.1:%d" % PORT, "localhost:%d" % PORT}
        if tailscale()["name"]:
            h |= {tailscale()["name"], tailscale()["name"] + ":443"}
        return h

    def _allowed_origins(self):
        o = {"http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT}
        if tailscale()["name"]:
            o.add("https://" + tailscale()["name"])
        return o

    def _remote_ok(self):
        """Requests through Tailscale Serve carry Tailscale-User-Login; only your own login may come in."""
        host = self.headers.get("Host", "").lower()
        login = (self.headers.get("Tailscale-User-Login") or "").lower()
        remote = bool(login) or (tailscale()["name"] and host.split(":")[0] == tailscale()["name"])
        if not remote:
            return True
        ok = allowed_logins()
        return bool(login) and (login in ok if ok else False)

    def _reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._reply(code, json.dumps(obj))

    @staticmethod
    def _file(path):
        """A page or data file of the dashboard folder, or None."""
        if path in ("", "/"):
            path = "/dashboard.html"
        fp = os.path.realpath(os.path.join(MC_DIR, path.lstrip("/")))
        if os.path.splitext(fp)[1].lower() not in SERVE_EXT or not fp.startswith(MC_DIR + os.sep) or not os.path.isfile(fp):
            return None
        return fp

    @staticmethod
    def _collector():
        try:
            c = json.load(open(os.path.join(MC_DIR, "state", "collector.json")))
            return str(c.get("host") or ""), float(c.get("time") or 0)
        except (OSError, ValueError):
            return "", 0.0

    def do_GET(self):
        if self.headers.get("Host", "").lower() not in self._allowed_hosts():
            return self._reply(403, "forbidden", "text/plain")   # blocks DNS-rebinding tricks
        if not self._remote_ok():
            return self._reply(403, "Mission control only answers your own devices on your Tailscale network.", "text/plain")
        u = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(u.path)
        if path == "/api/ping":
            return self._json(200, {"ok": True})
        if path == "/api/stamp":   # has this page been rewritten since you loaded it? (pages poll this, it's tiny)
            fp = self._file(urllib.parse.unquote((urllib.parse.parse_qs(u.query).get("p") or ["/"])[0]))
            host, t = self._collector()
            return self._json(200, {"mtime": os.path.getmtime(fp) if fp else 0, "collected": t, "by": host})
        fp = self._file(path)
        if not fp:
            return self._reply(404, "not found", "text/plain")
        ext = os.path.splitext(fp)[1].lower()
        mtime = os.path.getmtime(fp)
        with open(fp, "rb") as f:
            data = f.read()
        if ext == ".html":
            inject = '<script>window.MC_SEND={"token":"%s","port":%d,"mtime":%.3f}</script></head>' % (TOKEN, PORT, mtime)
            data = data.replace(b"</head>", inject.encode(), 1)
        self._reply(200, data, SERVE_EXT[ext])

    def _collect_now(self):
        """The refresh button: wake the collector for a cycle now (only on the machine that collects, at most once a minute)."""
        host, t = self._collector()
        age, now = time.time() - t, time.time()
        if host and MY_HOST and host.lower() != MY_HOST:
            return {"ok": True, "started": False,
                    "message": "%s collects the data; this page updates by itself when its next data arrives." % host}
        if age < 45:
            return {"ok": True, "started": False, "message": "The data is only %d s old." % max(0, age)}
        if now - LAST_NUDGE[0] < 60:
            return {"ok": True, "started": True, "message": "Collecting fresh data now. The page updates when it arrives."}
        LAST_NUDGE[0] = now
        with open(os.path.join(ARCHIVE, "collect.now"), "w") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
        return {"ok": True, "started": True, "message": "Collecting fresh data now (about a minute). The page updates when it arrives."}

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/api/send", "/api/collect"):
            return self._reply(404, "not found", "text/plain")
        origin = self.headers.get("Origin", "").lower()
        if (self.headers.get("Host", "").lower() not in self._allowed_hosts() or not self._remote_ok()
                or origin not in self._allowed_origins()
                or not secrets.compare_digest(self.headers.get("X-MC-Token", ""), TOKEN)):
            return self._json(403, {"ok": False, "message": "Refused: this request didn't come from a mission control page on this machine."})
        if path == "/api/collect":
            return self._json(200, self._collect_now())
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(min(n, 200000)).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"ok": False, "message": "Bad request."})
        key, text, force = str(req.get("key") or ""), str(req.get("text") or ""), bool(req.get("force"))
        if not re.match(r"^[A-Za-z0-9_.\-]{1,120}$", key):
            return self._json(400, {"ok": False, "message": "Unknown session."})
        if not text.strip():
            return self._json(400, {"ok": False, "message": "Nothing to send."})
        if len(text) > 20000:
            return self._json(400, {"ok": False, "message": "That message is too long (20,000 characters at most)."})
        with tempfile.NamedTemporaryFile("w", delete=False, dir=ARCHIVE, prefix="send-", suffix=".txt", encoding="utf-8") as f:
            f.write(text)
            tmp = f.name
        try:
            cmd = ["bash", os.path.join(MC_DIR, "mctl"), "send", "--goal", key, "--file", tmp] + (["--force"] if force else [])
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            rc, out, err = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            rc, out, err = 124, "", "timed out reaching the session"
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        screen = out.split("---screen---", 1)[1].strip("\n") if "---screen---" in out else ""
        with open(os.path.join(ARCHIVE, "sent.log"), "a", encoding="utf-8") as lg:
            lg.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "key": key, "rc": rc, "force": force, "text": text[:2000]}) + "\n")
        if rc == 0:
            return self._json(200, {"ok": True, "screen": screen})
        msg = (err.strip().splitlines() or [""])[-1] or (out.strip().splitlines() or ["failed"])[0]
        if rc == 3:
            msg = "The agent's screen shows a question or approval prompt, so typing now could answer it. Answer it in the terminal, or send anyway."
        return self._json(200, {"ok": False, "code": rc, "message": msg, "screen": screen})


def main():
    global PORT
    srv = None
    for port in range(PORT0, PORT0 + 11):
        try:
            PORT = port
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            srv = None
    if not srv:
        sys.exit("mission control server: no free port between %d and %d" % (PORT0, PORT0 + 10))
    with open(os.path.join(ARCHIVE, "port"), "w") as f:
        f.write(str(PORT))
    sys.stderr.write("%s serving %s at http://localhost:%d\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), MC_DIR, PORT))
    srv.serve_forever()


if __name__ == "__main__":
    main()

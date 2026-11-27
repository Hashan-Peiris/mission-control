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
import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

MC_DIR = os.path.realpath(os.environ.get("MC_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ARCHIVE = os.environ.get("MC_ARCHIVE_DIR") or os.path.expanduser("~/.local/state/mission-control")
PORT0 = int(os.environ.get("MC_PORT") or 8765)
MY_HOST = (os.environ.get("MC_HOST") or "").lower()   # this machine's name in mission control
LAST_NUDGE = [0.0]
LAST_LAUNCH_NUDGE = [0.0]
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
_TS = {"t": 0, "name": "", "login": "", "ip": ""}
SEEN_RELAY = {}   # relay ids already run (with when), so a request can't be replayed
_ROUTE = threading.local()   # how this request's action reached another PC (Tailscale or the shared folder), for the reply


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
        _TS["ip"] = next((x for x in me.get("TailscaleIPs") or [] if "." in x), "")
        if _TS["name"] and _TS["ip"]:   # so your other PCs can reach this one (see relay in mctl)
            subprocess.run([sys.executable, os.path.join(MC_DIR, "lib", "mc.py"), "ts-publish", _TS["name"], _TS["ip"]],
                           capture_output=True, timeout=30)
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
        note = getattr(_ROUTE, "note", "")
        if note and isinstance(obj, dict):
            obj.setdefault("route", note)
        _ROUTE.note = ""
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
        if path == "/api/launch-info":   # the New-session dialog's options, as of the last collection
            try:
                return self._json(200, json.load(open(os.path.join(MC_DIR, "state", "launch.json"))))
            except (OSError, ValueError):
                return self._json(200, {})
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

    @staticmethod
    def _mctl(args, timeout=120):
        try:
            p = subprocess.run(["bash", os.path.join(MC_DIR, "mctl")] + args, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "", "timed out reaching the node"
        m = re.findall(r"^relay-route: (\w+) (\S+)$", p.stderr, re.M)
        if m:
            how, pc = m[-1]
            _ROUTE.note = ("via Tailscale to %s" % pc) if how == "tailscale" else (
                "via OneDrive to %s, slower: it wasn't reachable over Tailscale (a VPN, or Tailscale off)" % pc)
        return p.returncode, p.stdout, re.sub(r"^relay-route: .*\n?", "", p.stderr, flags=re.M)

    @staticmethod
    def _log(entry):
        entry["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(ARCHIVE, "sent.log"), "a", encoding="utf-8") as lg:
            lg.write(json.dumps(entry) + "\n")

    def _launch(self, req):
        """Start a new agent session in tmux (mctl launch); check=True only checks that it could start."""
        f = {k: str(req.get(k) or "").strip() for k in ("host", "name", "dir", "agent", "model", "effort", "cmd")}
        text, check, goal = str(req.get("text") or ""), bool(req.get("check")), bool(req.get("goal"))
        cont = bool(req.get("continue"))
        if cont:   # answer the agent's start-up question, then type the first message once it is ready
            f = {"host": f["host"], "name": f["name"]}
            text, check, goal = "", False, False
            extra = ["--continue", "--answer", str(req.get("answer") or "")[:200], "--line", str(req.get("line") or "")[:600]]
        else:
            extra = []
        if len(text) > 100000:
            return {"ok": False, "message": "That first message is too long (100,000 characters at most)."}
        args = ["launch"] + sum([["--" + k, v] for k, v in f.items() if v], []) + (["--goal"] if goal else []) + (["--check"] if check else []) + extra
        tmp = None
        if text.strip() and not check:
            with tempfile.NamedTemporaryFile("w", delete=False, dir=ARCHIVE, prefix="launch-", suffix=".txt", encoding="utf-8") as fh:
                fh.write(text)
                tmp = fh.name
            args += ["--file", tmp]
        try:
            rc, out, err = self._mctl(args, timeout=150)
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        if not check:
            self._log({"launch": f, "goal": goal, "rc": rc, "text": text[:2000]})
        info = {}
        for line in out.splitlines():
            mm = re.match(r"^---(state|file|line):(.*)$", line)
            if mm:
                info[mm.group(1)] = mm.group(2)
        screen = out.split("---screen---", 1)[1].strip("\n") if "---screen---" in out else ""
        msg = (err.strip().splitlines() or [""])[-1] or (out.strip().splitlines() or [""])[0]
        if check:
            return {"ok": rc == 0, "message": msg}
        if rc in (0, 7, 8, 9) and info.get("state"):   # a new session: collect now so it shows up soon
            host, t = self._collector()
            if not (host and MY_HOST and host.lower() != MY_HOST) and time.time() - LAST_LAUNCH_NUDGE[0] > 60:
                LAST_LAUNCH_NUDGE[0] = time.time()
                with open(os.path.join(ARCHIVE, "collect.now"), "w") as fh:
                    fh.write("launch")
        state = info.get("state", "")
        res = {"ok": rc == 0, "code": rc, "state": state, "screen": screen, "file": info.get("file", ""), "line": info.get("line", "")}
        if rc == 0 and cont:
            res["message"] = "Done: the agent is ready%s." % (" and has your first message" if info.get("line") else "")
        elif rc == 0:
            res["message"] = "Started %s. It appears in the tabs within about a minute." % f["name"]
        elif rc == 8:
            res["message"] = ("The agent is asking something before it starts (see its screen below). Answer it here"
                              "%s." % (" and your first message goes in right after" if info.get("line") else ""))
        elif rc in (7, 9) and state:
            res["message"] = ("The session started, but the agent %s, so your first message wasn't typed." %
                              ("didn't start (see the screen)" if rc == 7 else "wasn't ready in time"))
        else:
            res["message"] = msg or "Couldn't start the session."
        return res

    def _session_action(self, path, req):
        key = str(req.get("key") or "")
        if not re.match(r"^[A-Za-z0-9_.\-]{1,120}$", key):
            return {"ok": False, "message": "Unknown session."}
        if path == "/api/key":
            rc, out, err = self._mctl(["key", "--goal", key, "esc"], timeout=180)
            self._log({"key": key, "press": "esc", "rc": rc})
        elif path == "/api/answer":
            choice, sig = str(req.get("choice") or ""), str(req.get("sig") or "")
            if not re.match(r"^([0-9]{1,2}|y|n|enter)$", choice) or not re.match(r"^[0-9a-f]{10}$", sig):
                return {"ok": False, "message": "That answer isn't one the page offered."}
            rc, out, err = self._mctl(["answer", "--goal", key, "--choice", choice, "--sig", sig], timeout=180)
            self._log({"key": key, "answer": choice, "rc": rc})
            screen = out.split("---screen---", 1)[1].strip("\n") if "---screen---" in out else ""
            if rc == 0:
                return {"ok": True, "message": "Answered. The page shows the change on its next update.", "screen": screen}
            msg = (err.strip().splitlines() or [""])[-1] or "Couldn't answer."
            return {"ok": False, "message": msg if rc != 124 else "The session didn't answer in time.", "screen": screen}
        else:
            rc, out, err = self._mctl(["end", "--goal", key, "--confirm", str(req.get("confirm") or "")], timeout=180)
            self._log({"key": key, "end": True, "rc": rc})
        screen = out.split("---screen---", 1)[1].strip("\n") if "---screen---" in out else ""
        msg = (err.strip().splitlines() or [""])[-1] or (out.strip().splitlines() or [""])[0]
        return {"ok": rc == 0, "message": msg if rc else (out.strip().splitlines() or ["Done."])[-1] if path == "/api/end" else "Pressed Esc.",
                "screen": screen}

    def _contract(self, req):
        """A change to a goal's contract (lib/contract.py via mctl contract-edit). Approving or rejecting an agent's request,
        and "Tell the agent how to ask", also send the agent a short message (you pressed the button)."""
        key = str(req.get("key") or "")
        if not re.match(r"^[A-Za-z0-9_.\-]{1,120}$", key):
            return {"ok": False, "message": "Unknown session."}
        body = {k: str(req.get(k))[:1500] for k in ("op", "name", "rule_op", "val", "value", "text", "pid", "uid", "items", "item", "psig") if req.get(k) is not None}

        def edit(b):
            p = subprocess.run(["bash", os.path.join(MC_DIR, "mctl"), "contract-edit", key], input=json.dumps(b),
                               capture_output=True, text=True, timeout=90)
            return json.loads((p.stdout.strip().splitlines() or ["{}"])[-1])
        try:
            res = edit(body)
        except (subprocess.TimeoutExpired, ValueError) as e:
            return {"ok": False, "message": "Couldn't change the contract (%s)." % e.__class__.__name__}
        self._log({"key": key, "contract": body.get("op"), "ok": res.get("ok")})
        if res.get("ok") and res.get("agent_msg"):
            with tempfile.NamedTemporaryFile("w", delete=False, dir=ARCHIVE, prefix="send-", suffix=".txt", encoding="utf-8") as f:
                f.write(res["agent_msg"])
                tmp = f.name
            try:
                rc, out, err = self._mctl(["send", "--goal", key, "--file", tmp], timeout=200)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            why = (err.strip().splitlines() or ["the session was busy or asking something"])[-1]
            done = {"ask": "asked", "probe": "probed", "remind": "reminded"}.get(body.get("op"))
            if done:   # an item question, a memory check or a reminder: recorded only once it went out
                if rc == 0 and done in ("asked", "probed"):
                    self._check_back(150)
                if rc == 124:   # handed over but no word back (another PC asleep, or its answer lost)
                    return {"ok": False, "message": "%s. It isn't recorded as sent; if the agent got it, its answer still shows up." % why.rstrip(".")}
                if rc != 0:
                    return {"ok": False, "message": "Couldn't reach the agent: %s. Nothing was sent; try again when the session is free." % why.rstrip(".")}
                try:
                    edit(dict(body, op=done))
                except (subprocess.TimeoutExpired, ValueError):
                    pass
                return {"ok": True, "message": res.get("message") or "Sent."}
            res["message"] = (res.get("message") or "") + (" The agent has been told." if rc == 0 else
                                                           " Telling the agent didn't work (%s); send it yourself if needed." % why)
        return {"ok": bool(res.get("ok")), "message": res.get("message") or "Done."}

    def _collect_now(self):
        """The refresh button: wake the collector for a cycle now (only on the machine that collects, at most once a minute)."""
        host, t = self._collector()
        age, now = time.time() - t, time.time()
        if host and MY_HOST and host.lower() != MY_HOST:
            return {"ok": True, "started": False, "short": "%s collects" % host,
                    "message": "%s collects the data; this page updates by itself when its next data arrives." % host}
        if age < 240:   # fresh data at most every 4 minutes
            return {"ok": True, "started": False, "short": "Fresh \u00b7 again in %d min" % max(1, -(-(240 - age) // 60)),
                    "message": "The data is %d min old; fresh data can be asked for every 4 minutes." % (age // 60)}
        if now - LAST_NUDGE[0] < 120:
            return {"ok": True, "started": True, "message": "Collecting fresh data now. The page updates when it arrives."}
        if now - LAST_NUDGE[0] < 240:
            return {"ok": True, "started": False, "short": "Asked already",
                    "message": "Fresh data was asked for at %s and hasn't arrived yet; the collector may be busy." % time.strftime("%H:%M", time.localtime(LAST_NUDGE[0]))}
        LAST_NUDGE[0] = now
        with open(os.path.join(ARCHIVE, "collect.now"), "w") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
        return {"ok": True, "started": True, "message": "Collecting fresh data now (about a minute). The page updates when it arrives."}

    def _check_back(self, delay):
        """After an item question, collect once more in a couple of minutes (if no collection ran meanwhile) so the
        agent's answer reaches the page sooner. Only on the machine that collects."""
        def go():
            host, t = self._collector()
            if (host and MY_HOST and host.lower() != MY_HOST) or time.time() - t < 90 or time.time() - LAST_NUDGE[0] < 90:
                return
            LAST_NUDGE[0] = time.time()
            try:
                with open(os.path.join(ARCHIVE, "collect.now"), "w") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
            except OSError:
                pass
        th = threading.Timer(delay, go)
        th.daemon = True
        th.start()

    def _relay_in(self):
        """A request from another of your PCs (relay in mctl), over your tailnet: your own Tailscale login, a valid
        signature with the shared key, a fresh timestamp and a new id. Runs it like the shared-folder route does."""
        login = (self.headers.get("Tailscale-User-Login") or "").lower()
        if self.headers.get("Host", "").lower() not in self._allowed_hosts() or not login or login not in allowed_logins():
            return self._json(403, {"ok": False, "message": "refused"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(n, 2000000))
            key = open(os.path.join(MC_DIR, "state", "relay.key")).read().strip()
            req = json.loads(body.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return self._json(403, {"ok": False, "message": "refused"})
        good = hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
        rid = str(req.get("id") or "")
        if len(key) < 32 or not secrets.compare_digest(self.headers.get("X-MC-Sig", ""), good) or not re.match(r"^[0-9]{6,12}-[0-9]{1,12}$", rid):
            return self._json(403, {"ok": False, "message": "refused"})
        now = time.time()
        for k in [k for k, t in SEEN_RELAY.items() if now - t > 900]:
            SEEN_RELAY.pop(k, None)
        if rid in SEEN_RELAY or abs(now - float(req.get("time") or 0)) > 180:
            return self._json(403, {"ok": False, "message": "refused (stale or repeated)"})
        SEEN_RELAY[rid] = now
        tmpd = tempfile.mkdtemp(dir=ARCHIVE, prefix="relay-")
        try:
            fp = os.path.join(tmpd, "request.json")
            with open(fp, "wb") as f:
                f.write(body)
            subprocess.run([sys.executable, os.path.join(MC_DIR, "lib", "mc.py"), "relay-run", fp, tmpd], capture_output=True, timeout=180,
                           env=dict(os.environ, MC_DIR=MC_DIR))
            res = json.load(open(os.path.join(tmpd, rid + ".json")))
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            res = {"id": rid, "rc": 124 if isinstance(e, subprocess.TimeoutExpired) else 1, "out": "", "err": "relay: %s\n" % e.__class__.__name__}
        finally:
            shutil.rmtree(tmpd, ignore_errors=True)
        self._log({"relay": rid, "from": req.get("from"), "rc": res.get("rc"), "via": "tailscale"})
        return self._json(200, res)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        _ROUTE.note = ""
        if path == "/api/relay":
            return self._relay_in()
        if path not in ("/api/send", "/api/collect", "/api/launch", "/api/key", "/api/end", "/api/answer", "/api/contract"):
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
        if path == "/api/launch":
            return self._json(200, self._launch(req))
        if path in ("/api/key", "/api/end", "/api/answer"):
            return self._json(200, self._session_action(path, req))
        if path == "/api/contract":
            return self._json(200, self._contract(req))
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
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=200)
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
        question = None
        if rc == 3:
            try:
                question = json.loads(out.split("---question---", 1)[1].split("---screen---", 1)[0].strip() or "null")
            except (IndexError, ValueError):
                question = None
            msg = ("The agent is asking something, so typing now could answer it. Pick an answer here first, or send your message anyway:"
                   if question and question.get("options") else
                   "The agent's screen shows a question, so your message would be taken as the answer. If it is the answer, send it anyway:")
        return self._json(200, {"ok": False, "code": rc, "message": msg, "screen": screen, "question": question})


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

    def ts_refresh():   # keeps this PC's Tailscale name and address current for your other PCs
        while True:
            try:
                tailscale()
            except Exception:  # noqa
                pass
            time.sleep(310)
    threading.Thread(target=ts_refresh, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()

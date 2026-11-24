#!/usr/bin/env python3
"""mission-control analyzer: turns raw collector output into snapshot.json, dashboard.html and alerts.

Standard library only (Python 3.8+). Called by ../mctl; can also be run by hand:
    python3 lib/mc.py analyze | summary | digest-config [--local] | amend TEXT | promote ID | referee-bundle
"""
import base64
import datetime as dt
import hashlib
import html
import json
import os
import re
import socket
import sys
import time
import zlib
import signal
import urllib.request

if hasattr(signal, "SIGPIPE"):
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

MC_DIR = os.environ.get("MC_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(MC_DIR, "state")
LATEST = os.path.join(STATE, "latest")
CONTRACT_DIR = os.path.join(MC_DIR, "contract")
LEGACY_CONTRACT = os.path.join(CONTRACT_DIR, "goal_contract.md")
CONTRACT = LEGACY_CONTRACT   # switched per goal by use_contract()
CUR_GOAL = None
NOW = time.time()

if os.environ.get("DISPLAY_TZ"):
    os.environ["TZ"] = os.environ["DISPLAY_TZ"]
    try:
        time.tzset()
    except AttributeError:
        pass


def env(name, default):
    v = os.environ.get(name, "")
    if v == "":
        return default
    if isinstance(default, bool):
        return v.lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(float(v))
        except ValueError:
            return default
    if isinstance(default, float):
        try:
            return float(v)
        except ValueError:
            return default
    return v


CFG = {
    "session": env("TMUX_SESSION", ""),
    "interval": env("INTERVAL_MIN", 5),
    "hide": [x for x in re.split(r"[\s,]+", os.environ.get("HIDE_SECTIONS", "")) if x],
    "hours": env("HOURS", 48),
    "stall": env("STALL_MIN", 30),
    "low_eff": env("LOW_EFF", 0.25),
    "pending_h": env("PENDING_HOURS", 6.0),
    "ntfy_topic": env("NTFY_TOPIC", ""),
    "ntfy_server": env("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
    "digest_hour": env("DIGEST_HOUR", 8),
    "scan_roots": [x for x in env("SCAN_ROOTS", "").split(":") if x],
    "param_files": [x for x in env("PARAM_FILES", "INCAR,KPOINTS").split(",") if x],
    "scan_hours": env("SCAN_HOURS", 72),
    "goals": env("GOALS", "auto"),
    "nodes": env("NODES", "") or env("SAIPH_HOST", ""),
    "rf": env("RF_HOST", ""),
    "sj": env("SJ_HOST", ""),
    "scythe": env("SCYTHE_HOST", ""),
    # what to tell you to run when a shared connection is missing (your own alias or script)
    "connect": env("CONNECT_CMD", "") or "your SSH connection script",
}
# Hosts mission control must never touch: matched anywhere in an alias, host name or socket name, any case.
NEVER = [w.lower() for w in os.environ.get("NEVER_CONTACT", "").split() if w]


def never_note():
    """'and never contacts Alpha or Beta' for the page footers (empty when nothing is listed)."""
    return (", and never contacts %s" % " or ".join(esc(w.capitalize()) for w in NEVER)) if NEVER else ""


def compute_note():
    have = (["Rockfish"] if CFG["rf"] else []) + (["Skipjack"] if CFG["sj"] else []) + CFG["nodes"].split()
    return "- allowed: %s\n%s" % (", ".join(have) or "(clusters and nodes this goal may use)",
                                   ("- note: %s are never polled by mission control.\n" % " and ".join(w.capitalize() for w in NEVER)) if NEVER else "")
SEV_ORDER = {"red": 0, "amber": 1, "info": 2}
# this PC as a host name in the dashboard (the collector's own WSL; a laptop's when it has taken over)
LOCAL_NAME = (os.environ.get("MC_HOST_OVERRIDE") or socket.gethostname().split(".")[0]).lower()
LOGIN_HOSTS = {"rockfish-login": "Rockfish login node", "skipjack-login": "Skipjack login node"}


# ---------------------------------------------------------------- helpers
def rd(path, default=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return default


def rjson(path, default=None):
    t = rd(path)
    if not t:
        return default
    try:
        return json.loads(t)
    except Exception:
        return default


def wjson(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, default=str)
    os.replace(tmp, path)


def esc(s):
    return html.escape("" if s is None else str(s), quote=True)


def trunc(s, n):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def iso_epoch(ts):
    if not ts or not isinstance(ts, str) or ts in ("Unknown", "None", "N/A"):
        return None
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::(\d\d))?(\.\d+)?\s*(Z|[+-]\d\d:?\d\d)?", ts.strip())
    if not m:
        return None
    y, mo, d, hh, mi = (int(m.group(i)) for i in range(1, 6))
    se = int(m.group(6) or 0)
    frac = float(m.group(7) or 0)
    tz = m.group(8)
    if not tz:  # Slurm prints cluster-local time; Rockfish and Skipjack are US Eastern, like DISPLAY_TZ
        return time.mktime((y, mo, d, hh, mi, se, 0, 0, -1)) + frac
    off = 0
    if tz != "Z":
        t = tz.replace(":", "")
        off = (1 if t[0] == "+" else -1) * (int(t[1:3]) * 3600 + int(t[3:5]) * 60)
    return dt.datetime(y, mo, d, hh, mi, se, tzinfo=dt.timezone.utc).timestamp() + frac - off


def ago(epoch):
    if not epoch:
        return "never"
    s = max(0, NOW - epoch)
    if s < 90:
        return "%d s ago" % s
    if s < 5400:
        return "%d min ago" % (s / 60)
    if s < 172800:
        return "%.1f h ago" % (s / 3600)
    return "%.1f d ago" % (s / 86400)


def hm(epoch):
    if not epoch:
        return ""
    lt = time.localtime(epoch)
    if time.strftime("%Y-%m-%d", lt) == time.strftime("%Y-%m-%d", time.localtime(NOW)):
        return time.strftime("%H:%M", lt)
    return time.strftime("%b %d %H:%M", lt)


def slurm_secs(s):
    if not s:
        return 0.0
    s = s.strip()
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d or 0)
    parts = s.split(":")
    try:
        parts = [float(p) for p in parts]
    except ValueError:
        return 0.0
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return days * 86400 + parts[-3] * 3600 + parts[-2] * 60 + parts[-1]


def hid(text):
    return "U" + hashlib.sha1(re.sub(r"\s+", " ", text.strip())[:2000].encode()).hexdigest()[:6]


def num(v):
    try:
        return float(str(v).lower().replace("d", "e"))
    except ValueError:
        return None


# ---------------------------------------------------------------- contract
DEFAULT_CONTRACT = """# Goal contract: {session}

What the agents may not change without your say-so. The dashboard checks agent edits and
recent input files against this file every collection. Edit it any time.

## Goal
_(empty: mctl fills this from the Codex goal on the first run)_

## Clauses
- C1 [must] Keep the scientific settings below unless an amendment allows a change.
- C2 [must] Every reported number must trace to a job ID and its input files.
- C3 [must] Do not skip validation or convergence checks to save time.

## Watched parameters
Format: `- NAME` (flag any change), or `- NAME <= value`, `>= value`, `== value`, `in A|B|C`.
- ENCUT
- EDIFF
- EDIFFG
- KSPACING
- KPOINTS
- ISMEAR
- SIGMA
- PREC
- GGA
- METAGGA
- ISPIN
- LDAU
- IVDW

## Acceptance criteria
One per line, in plain English. Thresholds in meV/atom, eV/atom, meV/A or eV/A are checked
against real test output (DeePMD `dp test`, test logs, lcurve.out); anything else goes to the referee.
Custom number from any log: `- custom: <label> | <regex with one (number) group> | <= value`

## Compute
{compute}
## Amendments
"""


def slug(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name)).strip("_") or "goal"


def use_contract(key, name):
    """Point the contract functions at this goal's contract/<key>.md (created on first use)."""
    global CONTRACT, CUR_GOAL
    os.makedirs(CONTRACT_DIR, exist_ok=True)
    p = os.path.join(CONTRACT_DIR, key + ".md")
    if not os.path.exists(p) and os.path.exists(LEGACY_CONTRACT) and key == slug(CFG["session"]):
        os.replace(LEGACY_CONTRACT, p)  # the original single-goal contract belongs to this goal
    if not os.path.exists(p):  # a tmux session group (X) replacing its member sessions (X-0, X-1...): keep the best contract
        old = [f for f in glob_list(os.path.join(CONTRACT_DIR, key + "-*.md")) if re.match(re.escape(key) + r"-\d+\.md$", os.path.basename(f))]
        if old:
            best = max(old, key=lambda f: (len(rd(f, "") or ""), os.path.getmtime(f)))
            with open(p, "w", encoding="utf-8") as fo:
                fo.write(rd(best, ""))
    CONTRACT, CUR_GOAL = p, name
    ensure_contract()


def contract_files():
    try:
        return sorted(os.path.join(CONTRACT_DIR, f) for f in os.listdir(CONTRACT_DIR) if f.endswith(".md"))
    except OSError:
        return []


def ensure_contract():
    if not os.path.exists(CONTRACT):
        os.makedirs(os.path.dirname(CONTRACT), exist_ok=True)
        with open(CONTRACT, "w", encoding="utf-8") as f:
            f.write(DEFAULT_CONTRACT.format(session=CUR_GOAL or CFG["session"], compute=compute_note()))
        return
    raw = rd(CONTRACT, "") or ""
    if "## Acceptance criteria" not in raw:
        sec = DEFAULT_CONTRACT[DEFAULT_CONTRACT.index("## Acceptance criteria"):DEFAULT_CONTRACT.index("## Compute")]
        raw = raw.replace("## Compute", sec + "## Compute", 1) if "## Compute" in raw else raw.rstrip("\n") + "\n\n" + sec
        with open(CONTRACT, "w", encoding="utf-8") as f:
            f.write(raw)


def parse_contract(path=None):
    if path is None or path == CONTRACT:
        ensure_contract()
        path = CONTRACT
    text = rd(path, "") or DEFAULT_CONTRACT.format(session=CFG["session"], compute=compute_note())
    sec, body = None, {}
    for line in text.splitlines():
        m = re.match(r"^##\s+(.*)", line)
        if m:
            sec = m.group(1).strip().lower()
            body[sec] = []
            continue
        if sec:
            body[sec].append(line)
    goal = "\n".join(x for x in body.get("goal", []) if x.strip() and not x.strip().startswith("_(")).strip()
    clauses = []
    for line in body.get("clauses", []):
        m = re.match(r"^\s*-\s*(C\d+)\s*(\[[^\]]+\])?\s*(.*)", line)
        if m:
            clauses.append({"id": m.group(1), "tag": (m.group(2) or "").strip("[]"), "text": m.group(3).strip()})
    watch = []
    for line in body.get("watched parameters", []):
        line = re.split(r"\s+#", line, maxsplit=1)[0]
        m = re.match(r"^\s*-\s*`?([A-Za-z_][A-Za-z0-9_]*)`?\s*(abs<=|<=|>=|==|<|>|in\b)?\s*(.*?)\s*$", line)
        if m:
            op, val = m.group(2), m.group(3).strip()
            watch.append({"name": m.group(1).upper(), "op": op, "val": val if op else "", "src": "contract" if op else ""})
    criteria = []
    for line in body.get("acceptance criteria", []):
        m = re.match(r"^\s*-\s+(.*\S)", line)
        if m:
            txt = re.split(r"\s+#\s", m.group(1), maxsplit=1)[0].strip()
            src = "goal text" if "# inferred" in m.group(1) else "contract"
            for c in parse_criteria(txt):
                criteria.append(dict(c, src=src))
    amends = []
    for line in body.get("amendments", []):
        m = re.match(r"^\s*-\s*([^|]+)\|\s*(A\d+)\s*\|\s*(.*)", line)
        if m:
            amends.append({"when": m.group(1).strip(), "id": m.group(2), "text": m.group(3).strip()})
    mtime = os.path.getmtime(path) if os.path.exists(path) else 0
    return {"goal": goal, "clauses": clauses, "watch": watch, "amends": amends, "path": path, "criteria": criteria,
            "mtime": mtime, "raw": text}


def rule_violated(rule, value):
    op, ref = rule.get("op"), rule.get("val", "")
    if not op:
        return False
    if op == "in":
        allowed = [x.strip().upper() for x in ref.split("|") if x.strip()]
        return str(value).upper() not in allowed
    a, b = num(value), num(ref)
    if a is None or b is None:
        return op == "==" and str(value).upper() != ref.upper()
    if op == "abs<=":
        return abs(a) > abs(b) + 1e-15
    return not {"<=": a <= b + 1e-15, ">=": a >= b - 1e-15, "==": abs(a - b) <= 1e-12 * max(1, abs(b)),
                "<": a < b, ">": a > b}[op]


# How "more accurate" runs for common settings, used to turn values stated in the goal into rules.
IMPLICIT_OPS = {"ENCUT": ">=", "EDIFF": "<=", "EDIFFG": "abs<=", "KSPACING": "<=", "SIGMA": "<=", "ECUTWFC": ">=",
                "ECUTRHO": ">=", "CONV_THR": "<=", "FORC_CONV_THR": "<="}
# Settings that legitimately differ between structures, so mixed values alone are not suspicious.
VARIES = {"KPOINTS", "MAGMOM", "NBANDS", "NELECT", "ISPIN", "LDAUU", "LDAUL", "LDAUJ", "NCORE", "NPAR", "KPAR",
          "NSIM", "LORBIT", "NEDOS", "SYSTEM", "ISIF", "IBRION", "NSW", "POTIM", "ICHARG", "ISTART", "LWAVE", "LCHARG"}
NUM_RE = r"([-+]?(?:\d+\.?\d*|\.\d+)(?:[eEdD][-+]?\d+)?)"


def values_near(key, text):
    out = []
    for m in re.finditer(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])[\s=:]*(?:[A-Za-z]{1,6}[\s=:]+){0,3}%s" % (re.escape(key), NUM_RE), text, re.I):
        v = num(m.group(1))
        if v is not None:
            out.append(v)
    return out


def infer_rules(goal_text, watch):
    rules = {}
    for w in watch:
        if w.get("op"):
            continue
        vals = values_near(w["name"], goal_text or "")
        if vals:
            rules[w["name"]] = {"name": w["name"], "op": IMPLICIT_OPS.get(w["name"], "=="), "val": "%g" % vals[0], "src": "goal text"}
    return rules


def authorized(key, value, texts):
    v = num(value)
    for t in texts:
        for x in values_near(key, t):
            if v is not None and abs(x - v) <= 1e-12 * max(1.0, abs(v)):
                return True
            if v is None and str(value).upper() in t.upper():
                return True
    return False


UNIT_SCALE = {"mev": 1e-3, "ev": 1.0}
CRIT_NUM = re.compile(r"(?i)([0-9]*\.?[0-9]+(?:e-?\d+)?)\s*(mev|ev)\s*/\s*(atoms?|at|\u00e5|a|ang|angstroms?)\b")
CRIT_OP_LE = re.compile(r"(?i)(below|under|less than|lower than|smaller than|at most|no more than|not exceed|within|better than|<=|\u2264|<)")
CRIT_OP_GE = re.compile(r"(?i)(above|over|greater than|more than|at least|>=|\u2265|>)")


def parse_criteria(text):
    """Turn plain-English thresholds into checkable metrics (one per number+unit). Unparsed text is kept for the referee."""
    m = re.match(r"(?i)custom:\s*([^|]+)\|\s*(.+?)\s*\|\s*(<=|>=|==|<|>)\s*([-+0-9.eE]+)\s*$", text)
    if m:
        try:
            re.compile(m.group(2))
            return [{"text": text, "parsed": True, "metric": "custom:" + m.group(1).strip(), "regex": m.group(2), "op": m.group(3),
                     "value": float(m.group(4)), "unit": "", "scale": 1.0, "label": m.group(1).strip()}]
        except re.error:
            return [{"text": text, "parsed": False}]
    out, prev = [], 0
    nums = list(CRIT_NUM.finditer(text))
    for n in nums:
        seg_start = prev
        seg = text[prev: n.end()]
        prev = n.end()
        unit_e, per = n.group(2).lower(), n.group(3).lower()
        per_atom = per.startswith("at")
        low = seg.lower()
        if "force" in low:
            q = "force"
        elif "virial" in low:
            q = "virial"
        elif "energy" in low or per_atom:
            q = "energy"
        else:
            q = "force"
        if q == "force":
            per_atom = False
        stat = "mae" if re.search(r"\bmae\b|mean absolute", low) else "rmse"
        before = text[max(seg_start, n.start() - 60): n.start()]
        op = ">=" if CRIT_OP_GE.search(before) and not CRIT_OP_LE.search(before) else "<="
        unit = "%s/%s" % ("meV" if unit_e == "mev" else "eV", "atom" if per_atom else "\u00c5")
        piece = seg.strip(" ,;.").lstrip()
        piece = re.sub(r"(?i)^(and|or|with|while)\s+", "", piece)
        out.append({"text": piece if len(nums) > 1 else text, "parsed": True, "metric": "%s_%s%s" % (q, stat, "_atom" if per_atom else ""),
                    "op": op, "value": float(n.group(1)) * UNIT_SCALE[unit_e], "unit": unit, "scale": UNIT_SCALE[unit_e],
                    "label": "%s %s%s" % (q, stat.upper(), " per atom" if per_atom else "")})
    return out or [{"text": text, "parsed": False}]


def infer_criteria(goal_text):
    out = []
    for sent in re.split(r"(?<=[.;\n])\s+", goal_text or ""):
        prev = 0
        for n in CRIT_NUM.finditer(sent):
            head = sent[prev: n.start()]
            cut = max([head.rfind(d) + len(d) for d in (", ", "; ", " and ", "(", ": ") if head.rfind(d) >= 0] or [0])
            piece = (head[cut:] + sent[n.start(): n.end()]).strip(" ,;()")
            prev = n.end()
            if not (CRIT_OP_LE.search(piece) or CRIT_OP_GE.search(piece)):
                continue
            for c in parse_criteria(piece):
                if c["parsed"]:
                    out.append(dict(c, src="goal text"))
    return out


def fmt_metric(v, c):
    if v is None:
        return "\u2013"
    x = v / (c.get("scale") or 1.0)
    return ("%.3g %s" % (x, c.get("unit", ""))).strip()


def tail2(p):
    parts = [x for x in re.split(r"[\\/]+", str(p).strip().rstrip("/")) if x and x != "."]
    return "/".join(parts[-2:]).lower()


def fill_goal_if_empty(contract, goal_text):
    if contract["goal"] or not goal_text:
        return
    raw = contract["raw"]
    stamp = time.strftime("%Y-%m-%d")
    new = re.sub(r"(## Goal\s*\n)(?:_\(empty[^\n]*\n)?",
                 lambda m: m.group(1) + goal_text.strip() + "\n\n_(auto-filled from the Codex goal on %s)_\n" % stamp,
                 raw, count=1)
    if not contract["criteria"]:
        crit_lines = "".join("- %s   # inferred from the goal text; edit if wrong\n" % c["text"].replace("\n", " ")
                             for c in infer_criteria(goal_text))
        if crit_lines:
            new = new.replace("## Compute", crit_lines.rstrip("\n") + "\n\n## Compute", 1) if "## Compute" in new else new + crit_lines
    for name, r in infer_rules(goal_text, contract["watch"]).items():
        new = re.sub(r"(?m)^(\s*-\s*`?%s`?)\s*$" % re.escape(name),
                     lambda m: "%s %s %s   # inferred from the goal text; edit if wrong" % (m.group(1), r["op"], r["val"]), new, count=1)
    if new != raw:
        with open(CONTRACT, "w", encoding="utf-8") as f:
            f.write(new)


def next_amend_id(contract):
    ids = [int(a["id"][1:]) for a in contract["amends"] if a["id"][1:].isdigit()]
    return "A%d" % (max(ids) + 1 if ids else 1)


def cmd_amend(text, src=""):
    c = parse_contract()
    aid = next_amend_id(c)
    line = "- %s | %s | %s%s\n" % (time.strftime("%Y-%m-%d %H:%M"), aid, text.strip().replace("\n", " "),
                                    (" (from %s)" % src) if src else "")
    raw = c["raw"]
    if not raw.endswith("\n"):
        raw += "\n"
    if "## Amendments" not in raw:
        raw += "\n## Amendments\n"
    raw += line
    with open(CONTRACT, "w", encoding="utf-8") as f:
        f.write(raw)
    print("Added %s to %s" % (aid, CONTRACT))


def known_goals():
    ov = rjson(os.path.join(STATE, "snapshot.json"), {}) or {}
    return ov.get("goals") or []


def resolve_goal(arg):
    """Pick a goal by key, name or unique prefix; with no argument, the only goal."""
    gl = known_goals()
    if not gl:
        sys.exit("No goals known yet. Run: mctl run")
    if not arg:
        if len(gl) == 1:
            return gl[0]
        sys.exit("Several goals are running; say which with --goal NAME:\n  " + "\n  ".join("%s (%s on %s)" % (g["key"], g["name"], g["node"]) for g in gl))
    a = arg.lower()
    exact = [g for g in gl if a in (g["key"].lower(), g["name"].lower())]
    pref = [g for g in gl if g["key"].lower().startswith(a) or g["name"].lower().startswith(a)]
    hit = exact or pref
    if len(hit) != 1:
        sys.exit("No single goal matches %r. Goals: %s" % (arg, ", ".join(g["key"] for g in gl)))
    return hit[0]


def pop_goal_arg(args):
    out, goal, i = [], None, 0
    while i < len(args):
        if args[i] in ("--goal", "-g") and i + 1 < len(args):
            goal = args[i + 1]
            i += 2
            continue
        out.append(args[i])
        i += 1
    return goal, out


def cmd_promote(uid):
    for g in known_goals():
        snap = rjson(os.path.join(STATE, "goals", g["key"] + ".json"), {}) or {}
        for u in snap.get("instructions", []):
            if u["id"].lower() == uid.lower():
                use_contract(g["key"], g["name"])
                cmd_amend(u["text"], u["id"])
                return
    print("No instruction %s in the latest snapshot. Run: mctl status" % uid)
    sys.exit(1)


# ---------------------------------------------------------------- digest config
def cmd_digest_config(local=False, login=None):
    watch, customs = [], []
    for p in contract_files() or [None]:
        c = parse_contract(p) if p else parse_contract(os.path.join(CONTRACT_DIR, "_none_.md"))
        for w in c["watch"]:
            if w["name"] not in watch:
                watch.append(w["name"])
        for cr in c["criteria"]:
            if cr.get("regex") and cr["label"] not in [x["label"] for x in customs]:
                customs.append({"label": cr["label"], "regex": cr["regex"]})
    g = CFG["goals"].strip()
    sessions = ["*"] if (local or login or g in ("", "auto", "*")) else [x for x in re.split(r"[\s,]+", g) if x]
    cfg = {
        "tmux_sessions": sessions,
        "hours": CFG["hours"],
        "watch": watch,
        "param_files": CFG["param_files"],
        "scan_roots": [] if (local or login) else CFG["scan_roots"],
        "scan_hours": CFG["scan_hours"],
        "scan_every_min": 30,
        "claude": True,
        "label": LOCAL_NAME if local else ("login" if login else "node"),
        "custom_metrics": customs,
        "msg_since": {v["path"]: {"epoch": v.get("epoch"), "seq": v.get("seq", 0)}
                      for v in (rjson(os.path.join(CONV_DIR, "index.json"), {}) or {}).values()
                      if (v.get("host") == LOCAL_NAME) == bool(local)},
    }
    if login:  # a shared login node: only agents running right now, and (Rockfish) which nodes your connection script reaches from it
        cfg.update({"live_only": True, "login": True, "report_masters": login.startswith("rockfish")})
    if local:
        cfg["cache"] = "~/.cache/mission-control/digest_cache_local.json"
    sys.stdout.write(base64.b64encode(json.dumps(cfg).encode()).decode())


# ---------------------------------------------------------------- parsing raw inputs
def parse_sections(text):
    secs, cur, fields = {}, None, {}
    for line in (text or "").splitlines():
        if line.startswith("### "):
            cur = line[4:].strip()
            secs[cur] = []
            continue
        if cur is None:
            continue
        if line.startswith("#fields "):
            fields[cur] = line[8:].split("|")
            continue
        secs[cur].append(line)
    return secs, fields


def parse_cluster(name, text, err, status):
    out = {"name": name, "ok": False, "jobs": [], "acct": [], "sinfo": [], "error": None, "node": {}}
    if status is not None and status[0] != 0:
        lines = [l for l in (err or "").replace("\r", "").strip().splitlines() if l.strip()]
        out["error"] = trunc(lines[-1] if lines else ("timed out" if status[0] == 124 else "exit %s" % status[0]), 200)
    secs, fields = parse_sections(text)
    if "squeue" not in secs:
        if not out["error"]:
            out["error"] = "no data"
        return out
    out["ok"] = True
    host = secs.get("host", [])
    out["login"] = host[0] if host else ""
    f = fields.get("squeue", [])
    for line in secs.get("squeue", []):
        p = line.split("|")
        if len(p) != len(f):
            continue
        out["jobs"].append(dict(zip(f, p)))
    f = fields.get("sacct", [])
    allocs = {}
    for line in secs.get("sacct", []):
        p = line.split("|")
        if len(p) != len(f):
            continue
        r = dict(zip(f, p))
        jid = r.get("JobID", "")
        base = jid.split(".")[0]
        if "." not in jid:
            allocs[base] = r
            r["_steps_cpu"] = 0.0
        else:
            a = allocs.get(base)
            if a is not None:
                a["_steps_cpu"] += slurm_secs(r.get("TotalCPU"))
    out["acct"] = list(allocs.values())
    nodes = {}
    for sec, lines in secs.items():
        if sec != "node" and not sec.startswith("node:"):
            continue
        node = {}
        for line in lines:
            k, _, v = line.partition("=")
            node[k.strip()] = v.strip()
        sc = node.get("scontrol", "")
        if sc:
            for key in ("State", "BootTime", "NodeAddr", "Partitions"):
                mm = re.search(r"\b%s=(\S+)" % key, sc)
                if mm:
                    node[key.lower()] = mm.group(1)
            mm = re.search(r"\bReason=(.+?)(?=\s+[A-Z][A-Za-z]+=|$)", sc)
            if mm:
                node["reason"] = mm.group(1).strip()
            if re.search(r"Node \S+ not found|Invalid node name", sc, re.I):
                node["state"] = "not a Slurm node"
            elif re.search(r"command not found|No such file", sc, re.I):
                node.pop("state", None)
        nodes[node.get("name") or sec.partition(":")[2]] = node
    out["nodes"] = nodes
    node = next(iter(nodes.values()), {})
    out["node"] = node
    for line in secs.get("sinfo", []):
        p = line.split("|")
        if len(p) == 3:
            out["sinfo"].append({"partition": p[0], "avail": p[1], "nodes": p[2]})
    return out


def read_status():
    st = {}
    for line in (rd(os.path.join(LATEST, "status"), "") or "").splitlines():
        p = line.split()
        if len(p) >= 2:
            try:
                st[p[0]] = (int(p[1]), int(p[2]) if len(p) > 2 else 0)
            except ValueError:
                pass
    return st


# ---------------------------------------------------------------- analysis
class Flags:
    def __init__(self):
        self.items = []

    def add(self, key, sev, area, title, detail="", action="", needs_you=False):
        self.items.append({"key": key, "sev": sev, "area": area, "title": title, "detail": detail,
                           "action": action, "needs_you": needs_you or sev == "red"})

    def sorted(self):
        return sorted(self.items, key=lambda f: (SEV_ORDER.get(f["sev"], 3), f["area"]))


def goal_from_db(goal_db):
    goals = []
    for t in goal_db or []:
        if "goal" not in (t.get("table") or "").lower() or not t.get("rows"):
            continue
        cols = [c.lower() for c in t["columns"]]
        for row in t["rows"]:
            r = dict(zip(cols, row))
            text_col = next((c for c in ("objective", "goal", "text", "description", "prompt", "title", "content") if r.get(c)), None)
            if not text_col:
                strs = [(len(v), c) for c, v in r.items() if isinstance(v, str)]
                text_col = max(strs)[1] if strs else None
            status = next((r[c] for c in ("status", "state") if r.get(c)), "")
            budget = next((r[c] for c in cols if "budget" in c and isinstance(r.get(c), (int, float))), None)
            used = next((r[c] for c in cols if ("used" in c or "usage" in c or "spent" in c) and isinstance(r.get(c), (int, float))), None)
            upd = next((r[c] for c in cols if "updat" in c or "modif" in c), None)
            goals.append({"text": r.get(text_col, "") if text_col else "", "status": str(status), "budget": budget,
                          "used": used, "updated": upd, "thread": next((r[c] for c in cols if "thread" in c), None)})
    return goals


def pending_wait(sess):
    """If the agent's latest tool call is a sleep/wait that hasn't returned, when does it end?"""
    tools = (sess or {}).get("tools") or []
    if not tools:
        return None
    t = tools[-1]
    if t.get("exit") is not None:
        return None
    start = iso_epoch(t.get("ts"))
    last = iso_epoch((sess or {}).get("last_ts"))
    if not start or (last and last - start > 120):  # something happened after it
        return None
    cmd = t.get("cmd") or ""
    name = (t.get("tool") or "").lower()
    m = re.search(r'"(?:duration_ms|sleep_ms|wait_ms|timeout_ms)"\s*:\s*(\d+)', cmd)
    dur = int(m.group(1)) / 1000.0 if m else None
    if dur is None:
        m = re.search(r"(?:^|[;&|\s])sleep\s+(\d+)([smh]?)\b", cmd)
        if m:
            dur = int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]
    if name not in ("sleep", "wait", "clock", "pause") and dur is None:
        return None
    if dur is None or dur < 300:
        return None
    return {"start": start, "until": start + dur, "tool": name or "sleep"}


def goal_db_multi(goals):
    return len({str(g.get("thread")) for g in goals if g.get("thread")}) > 1


def label_sessions(sessions):
    fam = [s for s in sessions if s.get("role") == "goal"]
    fam.sort(key=lambda s: (s.get("first_ts") or "9", -(s.get("calls") or 0)))
    main = None
    for s in fam:
        src = json.dumps(s.get("meta", {})).lower()
        if s.get("kind") == "codex" and "subagent" not in src and "parent" not in src:
            main = s
            break
    if not main and fam:
        main = fam[0]
    n = 0
    for s in fam:
        if s is main:
            s["label"] = "main"
        else:
            n += 1
            s["label"] = "sub-%d" % n
    for s in sessions:
        if "label" not in s:
            sid = str(s.get("meta", {}).get("id") or s.get("meta", {}).get("sessionId") or os.path.basename(s.get("path", "")))[-6:]
            s["label"] = "%s %s" % (s.get("kind", "?"), sid)
    return main, fam


def conn_problem(name, msg):
    m = (msg or "").lower()
    if "can't look up" in m or "temporary failure in name resolution" in m:
        return ("red", "No connection to %s: this PC can't look up cluster addresses" % name,
                "DNS isn't working inside WSL (common after a reboot or a VPN change), so %s can't connect either. "
                "In Windows PowerShell run: wsl --shutdown. Reopen Ubuntu (mission control restarts by itself) and run %s. "
                "If it happens after every reboot, put dnsTunneling=true under [wsl2] in C:\\Users\\<you>\\.wslconfig, then wsl --shutdown once."
                % (CFG["connect"], CFG["connect"]), True)
    if "no shared connection" in m or "no shared skipjack connection" in m:
        return ("red", "No shared connection to %s" % name,
                "Run %s in WSL. Mission control never logs in by itself; it picks the connection up on its next cycle." % CFG["connect"], True)
    if "connection reset by peer" in m or "kex_exchange_identification" in m:
        return ("red", "%s is refusing connections from this machine" % name,
                "Often a temporary lockout after repeated failed logins, or maintenance. It usually clears by itself; "
                "if %s also fails, wait a while before retrying." % CFG["connect"], True)
    if any(k in m for k in ("permission denied", "authentication", "host key verification", "too many authentication")):
        return ("red", "No shared connection to %s" % name,
                "Run %s in WSL. Mission control picks the connection up on its next cycle (within %d min)." % (CFG["connect"], CFG["interval"]), True)
    if any(k in m for k in ("timed out", "timeout", "banner exchange")):
        return ("amber", "%s isn't answering" % name,
                "It timed out, so this is on the cluster or network side (%s hits the same). Mission control keeps retrying." % CFG["connect"], False)
    if any(k in m for k in ("no route to host", "could not resolve", "name or service not known", "connection refused", "network is unreachable")):
        return ("red", "%s is unreachable" % name, "", True)
    return ("red", "Can't reach %s" % name, "Run %s in WSL. Mission control retries on its next cycle." % CFG["connect"], True)


def load_shared():
    """Everything that is the same for every goal: cluster tables, node digests, connection health."""
    status = read_status()
    stamp = (rd(os.path.join(LATEST, "STAMP"), "") or "").strip()
    collected = None
    if stamp:
        try:
            collected = dt.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
        except ValueError:
            collected = None
    names = [n for n in CFG["nodes"].split() if n]
    try:
        for fn in sorted(os.listdir(LATEST)):
            mm = re.match(r"node_(.+)\.json$", fn)
            if mm and mm.group(1) not in names:
                names.append(mm.group(1))
    except OSError:
        pass
    configured = set(n for n in CFG["nodes"].split() if n)
    logins = {}
    try:
        for fn in sorted(os.listdir(LATEST)):
            mm = re.match(r"host_(.+)\.json$", fn)
            if mm:
                logins[mm.group(1)] = {"doc": rjson(os.path.join(LATEST, fn), None),
                                       "err": rd(os.path.join(LATEST, "host_%s.err" % mm.group(1)), "") or ""}
    except OSError:
        pass
    nodes = {}
    for n in names:
        doc = rjson(os.path.join(LATEST, "node_%s.json" % n), None)
        err = rd(os.path.join(LATEST, "node_%s.err" % n), "") or ""
        nodes[n] = {"doc": doc, "err": err}
    local = rjson(os.path.join(LATEST, "local.json"), None)
    clusters = []
    for name, host in (("Rockfish", CFG["rf"]), ("Skipjack", CFG["sj"])):
        if not host:
            continue
        key = name.lower()
        clusters.append(parse_cluster(name, rd(os.path.join(LATEST, key + ".txt"), ""),
                                      rd(os.path.join(LATEST, key + ".err"), ""), status.get(key)))
    scythe_txt = rd(os.path.join(LATEST, "scythe.txt"), "") if CFG["scythe"] else ""
    flags, hosts = Flags(), []
    rfc = next((c for c in clusters if c["name"] == "Rockfish"), None)
    for c in clusters:
        hosts.append({"name": c["name"], "ok": c["ok"], "detail": c.get("error") or c.get("login", "")})
        if not c["ok"]:
            sev, title, action, ny = conn_problem(c["name"], c.get("error"))
            flags.add("conn:" + c["name"], sev, "connection", title, c.get("error") or "", action, needs_you=ny)
    for n, nd in nodes.items():
        doc = nd["doc"]
        if doc is not None and doc.get("ok", False):
            hosts.append({"name": n, "ok": True, "detail": "%.1f s" % doc.get("elapsed_s", 0)})
            continue
        if n not in configured:
            continue  # a node found automatically: no alarms if it can't be read
        err = [l for l in nd["err"].replace("\r", "").strip().splitlines() if l.strip()]
        msg = trunc((doc or {}).get("error") or (err[-1] if err else "no data from %s" % n), 220)
        hosts.append({"name": n, "ok": False, "detail": msg})
        node = ((rfc or {}).get("nodes") or {}).get(n) or {}
        if rfc is not None and not rfc["ok"] and CFG.get("rf") and (rfc.get("error") or "")[:40] in msg:
            flags.add("conn:" + n, "info", "connection", "%s is reached through Rockfish, so it waits on that connection" % n, msg)
        else:
            sev, title, action, ny = conn_problem(n, msg)
            detail = msg
            bits = []
            if node.get("ssh_port"):
                bits.append("from the Rockfish login node its ssh port is %s" % node["ssh_port"])
            if node.get("state"):
                bits.append("Slurm state %s%s" % (node["state"], (" (%s)" % node["reason"]) if node.get("reason") else ""))
            if node.get("boottime"):
                bits.append("booted %s" % node["boottime"])
            if node.get("my_jobs_on_node") not in (None, ""):
                bits.append("%s of your jobs on it" % node["my_jobs_on_node"])
            if bits:
                detail = msg + " \u00b7 " + "; ".join(bits)
            if ("unreachable" in title or "answering" in title) and rfc is not None and rfc["ok"]:
                title = "%s is unreachable from Rockfish" % n
                sev, ny = "red", True
                action = ("Your Rockfish connection works, so this is the node itself, not your login. "
                          "If %s went down or rebooted, the tmux sessions running goals there are gone with it. "
                          "Check with your cluster's support team, or run `sinfo -n %s` on Rockfish." % (n, n))
            elif "unreachable" in title:
                action = "The node isn't answering. If it went down or rebooted, the tmux sessions running goals there are gone with it."
            flags.add("conn:" + n, sev, "connection", title, detail, action, needs_you=ny)
        flags.items[-1]["node"] = n
    if collected and NOW - collected > 3 * CFG["interval"] * 60 + 120:
        flags.add("stale", "amber", "connection", "Dashboard data is %s" % ago(collected).replace(" ago", " old"),
                  "The collector loop may have stopped.", "In WSL run: mctl start")
    return {"status": status, "collected": collected, "nodes": nodes, "logins": logins, "local": local, "clusters": clusters,
            "scythe": scythe_txt, "conn_flags": flags.items, "hosts": hosts}


def merge_params(parts):
    out = {"values": {}, "examples": {}, "files": 0, "dirs": 0, "roots": [], "truncated": False, "metric_files": [],
           "train_inputs": [], "cached": True, "t": 0}
    for p in parts:
        for k, vals in (p.get("values") or {}).items():
            dst = out["values"].setdefault(k, {})
            for v, n in vals.items():
                dst[v] = dst.get(v, 0) + n
        for k, exs in (p.get("examples") or {}).items():
            dst = out["examples"].setdefault(k, {})
            for v, lst in exs.items():
                dst.setdefault(v, lst)
        out["files"] += p.get("files") or 0
        out["dirs"] += p.get("dirs") or 0
        out["roots"] += p.get("roots") or []
        out["truncated"] = out["truncated"] or bool(p.get("truncated"))
        out["metric_files"] += p.get("metric_files") or []
        out["train_inputs"] += p.get("train_inputs") or []
        out["cached"] = out["cached"] and bool(p.get("cached"))
        out["t"] = max(out["t"], p.get("t") or 0)
    return out


def goal_doc(goal):
    """This goal's slice of its node's digest, in the shape the per-goal analysis expects."""
    doc = goal.get("doc") or {}
    if not doc:
        return {"ok": False, "tmux": {}, "sessions": []}
    name = goal["name"]
    if doc.get("multi"):
        sessions = [x for x in doc.get("sessions", []) if x.get("goal_session") == name]
        cwds = list((doc.get("goal_cwds") or {}).get(name) or [])
        byroot = (doc.get("params") or {}).get("by_root") or {}

        def related(r):
            return any(r == c or r.startswith(c.rstrip("/") + "/") or c.startswith(r.rstrip("/") + "/") for c in cwds)
        roots = [r for r in byroot if related(r)] or (list(byroot) if CFG["scan_roots"] else [])
        params = merge_params([byroot[r] for r in roots])
    else:
        sessions = list(doc.get("sessions", []))
        cwds = list(doc.get("goal_cwds") or [])
        params = doc.get("params") or {}
    dec = [d for d in doc.get("decisions", []) if any(str(d.get("file", "")).startswith(c) for c in cwds)]
    git = {c: v for c, v in (doc.get("git") or {}).items() if c in cwds}
    return {"ok": doc.get("ok", False), "tmux": goal.get("tm") or {}, "sessions": sessions, "goal_db": doc.get("goal_db"),
            "node": doc.get("node"), "params": params, "decisions": dec, "git": git, "goal_cwds": cwds,
            "elapsed_s": doc.get("elapsed_s", 0)}


def analyze_goal(gdesc, shared):
    flags = Flags()
    gname, gkey, gnode = gdesc["name"], gdesc["key"], gdesc["node"]
    use_contract(gkey, gname)
    contract = parse_contract()
    collected = shared["collected"]
    clusters = shared["clusters"]
    scythe_txt = shared["scythe"]
    hosts = shared["hosts"]
    for f in shared["conn_flags"]:
        if f.get("node") and f["node"] != gnode:
            continue
        flags.items.append(dict(f))
    ndoc = goal_doc(gdesc)  # this goal's data from its node
    local = None
    if gdesc.get("unreachable"):
        flags.add("goal:unseen", "info", "goal", "Can't see this goal right now",
                  "%s is unreachable%s." % (gnode, (", last seen " + ago(gdesc["last_seen"])) if gdesc.get("last_seen") else ""))
    if gdesc.get("ended"):
        flags.add("goal:ended", "amber", "goal", "tmux session %s has ended" % gname,
                  "Last seen %s on %s." % (ago(gdesc.get("last_seen")), gnode), "If that wasn't intended, the agent may have crashed.")

    # ---- sessions and goal
    sessions = list((ndoc or {}).get("sessions", []))
    for s in (local or {}).get("sessions", []):
        s["host"] = LOCAL_NAME
        sessions.append(s)
    uniq, seen_sid = [], set()
    for s in sessions:
        s.setdefault("host", gnode)
        sid = s.get("meta", {}).get("id") or s.get("meta", {}).get("sessionId") or s.get("path")
        if sid in seen_sid:
            continue
        seen_sid.add(sid)
        s["last_epoch"] = max(iso_epoch(s.get("last_ts")) or 0, s.get("mtime") or 0) or None
        uniq.append(s)
    sessions = uniq
    main, fam = label_sessions(sessions)
    tm = (ndoc or {}).get("tmux", {})
    goals = goal_from_db((ndoc or {}).get("goal_db"))
    fam_ids = {str(x.get("meta", {}).get("id")) for x in fam if x.get("meta", {}).get("id")}
    goal = next((g for g in goals if g.get("thread") and str(g["thread"]) in fam_ids), None)
    if goal is None and len(goals) == 1 and not goals[0].get("thread"):
        goal = goals[0]  # a goal table without thread ids: only usable when there is no ambiguity
    goal_text = (goal or {}).get("text") or ""
    if not goal_text and main:
        typed = [u for u in main.get("user", []) if u.get("typed")] or main.get("user", [])
        goal_text = typed[0]["text"] if typed else ""
    if goal_text:
        fill_goal_if_empty(contract, goal_text)
    agent_up = bool(tm.get("codex_running") or tm.get("claude_running"))
    goal_status = (goal or {}).get("status") or ("running" if agent_up else "unknown")
    gs = goal_status.lower()
    paused = any(k in gs for k in ("pause", "complete", "done", "budget", "clear", "fail"))

    if ndoc and ndoc.get("ok"):
        if not tm.get("found"):
            flags.add("tmux:missing", "red", "goal", "tmux session %s not found on %s" % (gname, gnode),
                      "Sessions there: " + ", ".join(tm.get("available", [])[:6]) if tm.get("available") else tm.get("error", ""),
                      "Check whether the node rebooted or the session was renamed.")
        elif not tm.get("codex_running") and not tm.get("claude_running"):
            flags.add("agent:notrunning", "red", "goal", "No Codex or Claude process is running in %s" % gname,
                      "The tmux session exists but the agent has exited.", "Open the session and resume the goal.")
    tail = tm.get("pane_tail", [])
    # a status line with "esc to interrupt" means the agent is busy (e.g. "Reviewing approval request" by Codex's own
    # reviewer), not asking you anything
    tail_txt = "\n".join(l for l in tail[-25:] if "esc to interrupt" not in l)
    rv = re.search(r"Reviewing approval request \((?:(\d+)h )?(\d+)m", "\n".join(tail[-6:]))
    if rv and int(rv.group(1) or 0) * 60 + int(rv.group(2)) >= 15:
        flags.add("pane:review-slow", "amber", "goal", "Codex's approval reviewer has been checking one request for %s"
                  % ("%sh %sm" % (rv.group(1), rv.group(2)) if rv.group(1) else "%s min" % rv.group(2)),
                  "The agent waits until the reviewer decides. It isn't asking you anything.",
                  "If it stays stuck, press Esc in the session (mctl peek %s to look first)." % gkey)
    m = re.search(r"(Allow (this|command)|Approve|approval|Do you want to|\[y/N\]|\(y/n\)|Press enter to continue|"
                  r"waiting for (your )?(input|approval)|Run this command\?|Proceed\?)", tail_txt, re.I)
    if m:
        line = next((l for l in reversed(tail[-25:]) if m.group(0).lower() in l.lower()), m.group(0))
        flags.add("pane:approval", "red", "goal", "The agent is waiting for you", trunc(line.strip(), 220),
                  "Open the tmux session to answer. Peek with: mctl peek %s" % gkey)
    last_act = main.get("last_epoch") if main else None
    full_tail = "\n".join(tail[-12:])
    ctx_m = re.search(r"Context (\d+)% left", full_tail)
    warn_m = re.search(r"\u26a0\s*(\d+) warning", full_tail)
    busy = "esc to interrupt" in full_tail
    pw = pending_wait(main)
    sleep_until = None
    if pw and not paused and not m:
        if NOW < pw["until"] + 1200:
            sleep_until = pw["until"]
            flags.add("goal:sleeping", "info", "goal", "Agent is sleeping until %s, as scheduled" % hm(pw["until"]),
                      "It started a %s of %s at %s to wait for its jobs; it wakes on its own." % (
                          pw["tool"], ("%.1f h" % ((pw["until"] - pw["start"]) / 3600.0)), hm(pw["start"])))
        else:
            flags.add("goal:overslept", "amber", "goal", "Agent should have woken at %s" % hm(pw["until"]),
                      "Its scheduled %s ended %d min ago and nothing has happened since." % (pw["tool"], (NOW - pw["until"]) / 60),
                      "mctl peek %s to see the screen." % gkey)
    if ctx_m and int(ctx_m.group(1)) <= 15:
        flags.add("goal:context", "amber", "integrity", "Agent context is %s%% free; a compaction is coming" % ctx_m.group(1),
                  "Compaction is when long sessions tend to lose constraints.", "Glance at the plan and contract after it compacts.")
    if warn_m:
        flags.add("goal:tuiwarn", "info", "goal", "Codex shows %s warning(s) in the session" % warn_m.group(1),
                  "Press F2 in the tmux session to read it, or mctl peek %s." % gkey)
    if main and last_act and not paused and not m and not sleep_until and not pw and not (busy and NOW - last_act < 6 * 3600):
        idle_min = (NOW - last_act) / 60
        if idle_min > 3 * CFG["stall"]:
            flags.add("goal:stalled", "red", "goal", "Main agent silent for %d min" % idle_min,
                      "No new events in its session log.", "mctl peek to see the screen.")
        elif idle_min > CFG["stall"]:
            flags.add("goal:quiet", "amber", "goal", "Main agent quiet for %d min" % idle_min,
                      "It may be waiting on long jobs, or stuck.")
    comp_recent = []
    for s in fam:
        for c in s.get("compactions", []):
            e = iso_epoch(c)
            if e and NOW - e < 6 * 3600:
                comp_recent.append((e, s["label"]))
    if comp_recent:
        e, lab = max(comp_recent)
        flags.add("goal:compaction:%d" % int(e // 3600), "amber", "integrity",
                  "%s's context was compacted at %s" % (lab, hm(e)),
                  "Long sessions can lose the goal's constraints after compaction (known Codex issue).",
                  "Glance at the plan and recent edits below.")
    for s in fam:
        calls = s.get("tools", [])[-30:]
        fails = [c for c in calls if c.get("exit") not in (None, 0)]
        if len(calls) >= 10 and len(fails) / len(calls) > 0.3:
            flags.add("goal:errors:" + s["label"], "amber", "goal",
                      "%s: %d of its last %d commands failed" % (s["label"], len(fails), len(calls)),
                      trunc(fails[-1].get("cmd", ""), 160))
    quota = None
    for s in fam or sessions:
        rl = s.get("rate") or {}
        for k, w in rl.items() if isinstance(rl, dict) else []:
            if isinstance(w, dict) and w.get("used_percent") is not None:
                wm = w.get("window_minutes") or 0
                lab = "weekly" if wm >= 7 * 1440 - 60 else ("%dh" % round(wm / 60) if wm else k)
                if quota is None or w["used_percent"] > quota["pct"]:
                    quota = {"pct": float(w["used_percent"]), "window": lab, "resets": w.get("resets_in_seconds")}
    if quota and quota["pct"] >= 85:
        flags.add("quota", "amber", "goal", "Codex %s quota at %d%%" % (quota["window"], quota["pct"]),
                  "The goal will pause when it runs out.")
    if goal and isinstance(goal.get("budget"), (int, float)) and goal.get("budget") and isinstance(goal.get("used"), (int, float)):
        frac = goal["used"] / goal["budget"]
        if frac > 0.85:
            flags.add("goal:budget", "amber", "goal", "Goal token budget %d%% used" % (frac * 100))

    # ---- full conversation (agent + you), from the local store
    conv = load_conversation(fam or ([main] if main else [])) if not gdesc.get("pseudo") else []
    conv_days = []
    if conv:
        try:
            conv_days = write_conv_data(gkey, conv)
        except Exception as e:
            sys.stderr.write("conversation data %s: %s\n" % (gkey, e))
    conv_counts = {}
    for m in conv:
        conv_counts[m["r"]] = conv_counts.get(m["r"], 0) + 1
    agent_msgs = [m for m in conv if m["r"] == "agent"]
    if not agent_msgs and main:  # store still empty: fall back to the digest's recent messages
        agent_msgs = [{"t": a.get("ts"), "e": iso_epoch(a.get("ts")) or 0, "r": "agent", "w": main.get("label"), "x": a.get("text", "")}
                      for a in main.get("agent", [])]
    last_you = next((m for m in reversed(conv) if m["r"] == "user"), None)

    # ---- plan
    plan, plan_ts, plan_expl = None, None, ""
    for s in ([main] if main else []) + fam:
        if s and s.get("plan"):
            plan, plan_ts, plan_expl = s["plan"], s.get("plan_ts"), s.get("plan_expl") or ""
            break
    plan_done = sum(1 for p in plan or [] if str(p.get("status", "")).lower() in ("completed", "done", "complete"))
    plan_now = next((p["step"] for p in plan or [] if "progress" in str(p.get("status", "")).lower()), "")

    # ---- jobs and compute
    submitted = {}
    for s in sessions:
        for ev in s.get("notable", []):
            if ev.get("jobid"):
                submitted[ev["jobid"]] = s.get("label", "?")
    res = []
    tot_alloc = tot_idle = tot_failed = tot_lost = gpu_h = 0.0
    low_eff_jobs, quick_fails = [], {}
    running_cores = 0
    for c in clusters:
        r = sum(1 for j in c["jobs"] if j.get("State") == "RUNNING")
        pd = sum(1 for j in c["jobs"] if j.get("State") == "PENDING")
        cores = sum(int(j.get("CPUs") or 0) for j in c["jobs"] if j.get("State") == "RUNNING")
        running_cores += cores
        alloc = idle = failed = lost = 0.0
        nfail = 0
        for a in c["acct"]:
            el = float(a.get("ElapsedRaw") or 0)
            ncpu = float(a.get("AllocCPUS") or 0)
            ch = el * ncpu / 3600.0
            stt = (a.get("State") or "").split()[0]
            tres = a.get("ReqTRES") or ""
            mg = re.search(r"gres/gpu(?::[^=,]+)?=(\d+)", tres)
            if mg:
                gpu_h += el * int(mg.group(1)) / 3600.0
            if stt in ("RUNNING", "PENDING", "REQUEUED", "RESIZING", "SUSPENDED") or ch <= 0:
                continue
            if stt in ("NODE_FAIL", "BOOT_FAIL", "PREEMPTED"):
                lost += ch  # the cluster killed it (outage, hardware, preemption): not the agent's waste
                continue
            alloc += ch
            used = max(slurm_secs(a.get("TotalCPU")), a.get("_steps_cpu", 0.0)) / 3600.0
            if stt in ("FAILED", "OUT_OF_MEMORY", "DEADLINE"):
                failed += ch
                nfail += 1
                if el < 300:
                    stem = re.sub(r"[\d_\-.]+$", "", a.get("JobName", "")) or a.get("JobName", "")
                    quick_fails.setdefault((c["name"], stem), []).append(a)
            else:
                idle_part = max(0.0, ch - used)
                idle += idle_part
                eff = used / ch if ch else 1
                if eff < CFG["low_eff"] and el > 600 and ncpu > 1 and stt == "COMPLETED":
                    low_eff_jobs.append((c["name"], a, eff))
        tot_alloc += alloc
        tot_idle += idle
        tot_failed += failed
        tot_lost += lost
        long_pd = []
        for j in c["jobs"]:
            if j.get("State") == "PENDING":
                sub = iso_epoch(j.get("Submit"))
                if sub and NOW - sub > CFG["pending_h"] * 3600:
                    long_pd.append(j)
        if long_pd:
            j = long_pd[0]
            flags.add("pd:%s:%s" % (c["name"], j.get("JobID")), "amber", "compute",
                      "%d job(s) pending over %g h on %s" % (len(long_pd), CFG["pending_h"], c["name"]),
                      "e.g. %s (%s), reason %s" % (j.get("JobID"), trunc(j.get("Name"), 40), j.get("Reason")),
                      "Consider moving them to a less busy partition or cluster.")
        seen = {}
        for j in c["jobs"]:
            if j.get("State") in ("RUNNING", "PENDING"):
                k = (j.get("Name"), j.get("WorkDir"))
                seen.setdefault(k, []).append(j.get("JobID"))
        for (nm, wd), ids in seen.items():
            if len(ids) > 1 and nm:
                flags.add("dup:%s:%s" % (c["name"], nm), "amber", "compute",
                          "Possible duplicate jobs on %s: %s" % (c["name"], trunc(nm, 40)),
                          "%d jobs share a name and folder: %s" % (len(ids), ", ".join(ids[:6])),
                          "Check that an agent didn't submit the same calculation twice.")
        res.append({"name": c["name"], "ok": c["ok"], "running": r, "pending": pd, "cores": cores,
                    "alloc_ch": round(alloc, 1), "idle_ch": round(idle, 1), "failed_ch": round(failed, 1), "lost_ch": round(lost, 1),
                    "nfail": nfail, "error": c.get("error"), "sinfo": c.get("sinfo", [])[:12],
                    "jobs": [{"id": j.get("JobID"), "name": j.get("Name"), "state": j.get("State"), "part": j.get("Partition"),
                              "used": j.get("TimeUsed"), "limit": j.get("TimeLimit"), "cpus": j.get("CPUs"),
                              "reason": j.get("Reason"), "by": submitted.get(j.get("JobID", "").split("_")[0], "")}
                             for j in c["jobs"]][:60]})
    for (cl, stem), lst in quick_fails.items():
        if len(lst) < 3:
            continue
        # A burst of quick failures needs you while it may still be going on (an agent resubmitting a broken
        # setup). Once a later job with the same name runs fine it's over; an old burst is only worth a note.
        cobj = next((c for c in clusters if c["name"] == cl), {"acct": [], "jobs": []})
        end_of = lambda a: iso_epoch(a.get("End")) or iso_epoch(a.get("Start")) or 0  # noqa: E731
        last = max(lst, key=end_of)
        t_last = end_of(last)
        dirs = {a.get("WorkDir") for a in lst if a.get("WorkDir")}
        same_name_ok, maybe_replacement = [], []
        rows = [(a.get("JobID"), a.get("JobName"), (a.get("State") or "").split()[0] if a.get("State") else "",
                 a.get("ExitCode") or "0:0", iso_epoch(a.get("Start")) or 0, a.get("WorkDir")) for a in cobj.get("acct", [])]
        rows += [(j.get("JobID"), j.get("Name"), j.get("State"), "0:0", iso_epoch(j.get("Start")) or 0, j.get("WorkDir"))
                 for j in cobj.get("jobs", [])]
        for jid, name, stt, ec, st0, wd in rows:
            good = (stt == "COMPLETED" and ec == "0:0") or (stt == "RUNNING" and NOW - st0 > 600)
            if not good or st0 <= t_last:
                continue
            if (name or "").startswith(stem):
                same_name_ok.append(jid)
            elif wd and wd in dirs and st0 - t_last < 3 * 3600:
                maybe_replacement.append("%s (%s, %s since %s)" % (jid, trunc(name, 40), stt.lower(), hm(st0)))
        age = NOW - t_last if t_last else 0
        if same_name_ok or age > 24 * 3600:
            continue
        ongoing = age < 2 * 3600
        repl = (" A job started from the same folder soon after: %s." % maybe_replacement[0]) if maybe_replacement else ""
        flags.add("qf:%s:%s" % (cl, stem), "red" if ongoing else "amber", "compute",
                  "%d jobs named %s* failed within 5 min on %s" % (len(lst), trunc(stem, 30), cl),
                  "Last failure: %s at %s (%s), exit %s.%s" % (last.get("JobID"), hm(t_last), ago(t_last), last.get("ExitCode"), repl),
                  "An agent may be resubmitting a broken setup. Check its last edits." if ongoing else
                  "No new failures for %s, so nothing is looping now.%s" % (
                      ago(t_last).replace(" ago", ""), " If that job is the replacement, there's nothing to do." if repl else ""))
    if low_eff_jobs:
        low_eff_jobs.sort(key=lambda x: x[2])
        cl, a, eff = low_eff_jobs[0]
        flags.add("loweff:%s" % cl, "amber", "compute",
                  "%d finished job(s) used under %d%% of their CPUs" % (len(low_eff_jobs), CFG["low_eff"] * 100),
                  "Worst: %s %s on %s at %d%% of %s cores" % (a.get("JobID"), trunc(a.get("JobName"), 30), cl, eff * 100, a.get("AllocCPUS")),
                  "Ask for fewer cores, or fix the parallel settings.")
    waste_pct = (tot_idle + tot_failed) / tot_alloc * 100 if tot_alloc >= 1 else None  # under 1 core-h a percentage is noise
    if waste_pct is not None and waste_pct > 30 and tot_alloc > 20:
        flags.add("waste", "amber", "compute", "%d%% of the last 48 h of compute was idle or failed" % waste_pct,
                  "%.0f of %.0f core-hours" % (tot_idle + tot_failed, tot_alloc))

    node = (ndoc or {}).get("node") or {}
    if node:
        load = (node.get("load") or [0])[0]
        ncpu = node.get("ncpu") or 1
        shared_host = gnode in LOGIN_HOSTS or gnode == LOCAL_NAME   # a login node or this PC: not ours to keep busy
        if not paused and agent_up and not shared_host and load / ncpu < 0.05 and ncpu >= 8:
            flags.add("node:idle:" + gnode, "info", "compute", "%s CPUs are nearly idle (load %.1f of %d cores)" % (gnode, load, ncpu),
                      "Fine if all the work is in Slurm queues.")
        for g in ([] if shared_host else node.get("gpus", [])):
            if g.get("mem_used", 0) > 1024 and g.get("util", 0) < 10:
                flags.add("gpu:idle:%s:%s" % (gnode, g.get("i")), "amber", "compute",
                          "%s GPU %s holds %.0f GB but is %d%% busy" % (gnode, g.get("i"), g["mem_used"] / 1024, g.get("util", 0)),
                          "A process may be stuck or waiting.")

    # ---- integrity
    watch = {w["name"]: w for w in contract["watch"]}
    goal_basis = contract["goal"] or goal_text
    for k, r in infer_rules(goal_basis, contract["watch"]).items():
        watch[k] = r
    first_instr = goal_text.strip()[:400]
    perm_texts = [a["text"] for a in contract["amends"]] + [
        u["text"] for s in sessions for u in s.get("user", []) if u.get("typed") and u["text"].strip()[:400] != first_instr]
    params = (ndoc or {}).get("params") or {}
    ptable = []
    for k in [w["name"] for w in contract["watch"]]:
        vals = (params.get("values") or {}).get(k) or {}
        if not vals:
            continue
        rule = watch.get(k, {})
        row = {"name": k, "rule": (("%s %s" % (rule.get("op"), rule.get("val"))).strip() + (" (from goal)" if rule.get("src") == "goal text" else "")) if rule.get("op") else "any change",
               "values": [], "bad": False}
        if not rule.get("op") and k in VARIES:
            row["rule"] = "varies by structure"
        for v, n in sorted(vals.items(), key=lambda kv: -kv[1]):
            bad = rule_violated(rule, v) and not authorized(k, v, perm_texts)
            row["values"].append({"v": v, "n": n, "bad": bad, "ex": (params.get("examples", {}).get(k, {}).get(v) or [""])[0]})
            row["bad"] = row["bad"] or bad
        ptable.append(row)
        if row["bad"]:
            badv = [x for x in row["values"] if x["bad"]]
            flags.add("param:%s" % k, "red", "integrity",
                      "%s breaks the contract in %d recent input file(s)" % (k, sum(x["n"] for x in badv)),
                      "Rule %s; found %s (e.g. %s)" % (row["rule"], ", ".join("%s \u00d7%d" % (x["v"], x["n"]) for x in badv[:3]), trunc(badv[0]["ex"], 120)),
                      "Reject it in the TUI, or allow it with: mctl amend \"...\"")
        elif len(row["values"]) > 1 and not rule.get("op") and k not in VARIES:
            minority = [x for x in row["values"][1:] if not authorized(k, x["v"], perm_texts)]
            if minority:
                flags.add("param-mixed:%s" % k, "amber", "integrity",
                          "%s has %d different values in recent inputs" % (k, len(row["values"])),
                          ", ".join("%s \u00d7%d" % (x["v"], x["n"]) for x in row["values"][:4]),
                          "If only one value is valid, add a rule to the contract, e.g. - %s == %s" % (k, row["values"][0]["v"]))
    changes = []
    for s in sessions:
        for p in s.get("patches", []):
            e = iso_epoch(p.get("ts"))
            if e and NOW - e > CFG["hours"] * 3600:
                continue
            olds, news = {}, {}
            for sign, fpath, body, keep in p.get("lines", []):
                for k in watch:
                    mm = re.search(r"(?<![A-Za-z0-9_])%s\s*[=:]\s*([^\s,;#!]+)" % re.escape(k), body, re.I)
                    if mm:
                        (olds if sign == "-" else news).setdefault((k, fpath), mm.group(1))
            for (k, fpath), nv in news.items():
                ov = olds.get((k, fpath))
                if ov is not None and ov.lower() == nv.lower():
                    continue
                bad = rule_violated(watch[k], nv) and not authorized(k, nv, perm_texts)
                changes.append({"ts": p.get("ts"), "who": s.get("label"), "param": k, "old": ov, "new": nv, "file": fpath, "bad": bad})
    for ch in changes[-12:]:
        k = ch["param"]
        ok = authorized(k, ch["new"], perm_texts)
        if ch["bad"]:
            sev = "red"
        elif ch["old"] is None:
            continue
        else:
            sev = "info" if ok else "amber"
        rule = watch.get(k, {})
        flags.add("chg:%s:%s:%s" % (k, ch["file"], ch["new"]), sev, "integrity",
                  "%s changed %s \u2192 %s by %s" % (k, ch["old"] or "(new)", ch["new"], ch["who"]),
                  "%s at %s%s" % (trunc(ch["file"], 90), hm(iso_epoch(ch["ts"])),
                                   (" \u00b7 breaks %s %s%s" % (k, ("%s %s" % (rule.get("op"), rule.get("val"))), " from your goal" if rule.get("src") == "goal text" else " in the contract")) if ch["bad"] else
                                   (" \u00b7 you allowed %s = %s in an instruction" % (k, ch["new"]) if ok else "")),
                  "" if sev == "info" else "Reject it in the TUI, or allow it with: mctl amend \"%s may be %s because ...\"" % (k, ch["new"]))
    red_chg = {f["key"].split(":")[1] for f in flags.items if f["key"].startswith("chg:") and f["sev"] == "red"}
    for f in list(flags.items):
        if f["key"].startswith("param:") and f["key"][6:] in red_chg:
            flags.items.remove(f)
            for g in flags.items:
                if g["key"].startswith("chg:%s:" % f["key"][6:]) and g["sev"] == "red":
                    g["detail"] += " \u00b7 now in %s" % f["title"].split(" in ", 1)[-1]
                    break
    shortcuts = []
    for s in fam or sessions:
        for sc in s.get("shortcuts", []):
            e = iso_epoch(sc.get("ts"))
            if e and NOW - e < 24 * 3600:
                shortcuts.append((e, s.get("label"), sc))
    shortcuts.sort(reverse=True)
    for e, lab, sc in shortcuts[:4]:
        flags.add("sc:%s:%s" % (lab, hashlib.sha1(sc["context"].encode()).hexdigest()[:8]), "amber", "integrity",
                  "Possible shortcut wording from %s: \u201c%s\u201d" % (lab, sc["match"]),
                  "%s \u00b7 \u2026%s\u2026" % (hm(e), trunc(sc["context"], 200)),
                  "Worth a look: check it didn't trade accuracy for speed.")
    behind = [s.get("label") for s in sessions if s.get("catching_up")]
    if behind:
        flags.add("catchup", "info", "goal", "Still reading older session logs (%s)" % ", ".join(behind[:3]),
                  "Long sessions are read in chunks; the picture completes within a few cycles.")
    # ---- acceptance criteria: thresholds checked against real test output
    criteria = list(contract["criteria"])
    have = {c.get("metric") for c in criteria if c.get("parsed")}
    for c in infer_criteria(contract["goal"] or goal_text):
        if c["metric"] not in have:
            criteria.append(c)
            have.add(c["metric"])
    measured = []
    for s in sessions:
        for m in s.get("metrics", []):
            measured.append(dict(m, epoch=iso_epoch(m.get("ts")) or s.get("last_epoch"), who=s.get("label"), host=s.get("host")))
    for src_doc in (ndoc, local):
        for m in ((src_doc or {}).get("params") or {}).get("metric_files", []):
            measured.append(dict(m, epoch=m.get("mtime"), who="file"))
    measured.sort(key=lambda m: m.get("epoch") or 0)
    claims = [dict(cl, who=s.get("label"), epoch=iso_epoch(cl.get("ts"))) for s in (fam or sessions) for cl in s.get("claims", [])]
    done_claims = [dict(d, who=s.get("label"), epoch=iso_epoch(d.get("ts"))) for s in (fam or sessions) for d in s.get("done_claims", [])]
    crit_rows = []
    for c in criteria:
        row = {"text": c["text"], "src": c.get("src"), "parsed": c.get("parsed"), "status": "referee"}
        if c.get("parsed"):
            k = c["metric"]
            test_ev = [m for m in measured if k in m.get("metrics", {}) and m.get("src") != "training curve"]
            curve_ev = [m for m in measured if (k + "_val") in m.get("metrics", {})]
            ev = test_ev or curve_ev
            key = k if test_ev else k + "_val"
            row.update(metric=k, rule="%s %s" % (c["op"], fmt_metric(c["value"], c)), unit=c["unit"])
            if not ev:
                row.update(status="no evidence", latest=None)
            else:
                last = ev[-1]
                v = last["metrics"][key]
                ok = rule_violated({"op": c["op"], "val": str(c["value"])}, v) is False
                best = min(m["metrics"][key] for m in ev) if c["op"] in ("<=", "<") else max(m["metrics"][key] for m in ev)
                row.update(status="met" if ok else "not met", latest=fmt_metric(v, c), best=fmt_metric(best, c),
                           when=last.get("epoch"), source=("%s \u00b7 %s" % (last.get("src"), trunc(last.get("cmd") or last.get("file") or "", 90))),
                           n_test=last.get("n_test"), from_curve=not bool(test_ev), count=len(ev))
                for cl in claims:
                    if cl.get("metric") == k and cl.get("epoch") and c["op"] in ("<=", "<") and cl["value"] < best * 0.9:
                        flags.add("claim:%s:%s" % (k, int(cl["epoch"])), "amber", "integrity",
                                  "%s reports %s, but the best measured %s is %s" % (cl["who"], fmt_metric(cl["value"], c), c["label"], fmt_metric(best, c)),
                                  "\u2026%s\u2026" % trunc(cl["context"], 200), "Ask it to show the dp test output behind that number.")
                if not ok:
                    sev = "info"
                    late_done = [d for d in done_claims if d.get("epoch") and (not last.get("epoch") or d["epoch"] >= last["epoch"] - 3600)]
                    if late_done or paused:
                        sev = "red"
                    flags.add("crit:%s" % k, sev, "integrity",
                              ("Agent says the target is met, but %s is %s (needs %s)" % (c["label"], fmt_metric(v, c), row["rule"])) if sev == "red" else
                              "Not met yet: %s is %s (needs %s)" % (c["label"], fmt_metric(v, c), row["rule"]),
                              "Latest evidence: %s, %s%s" % (row["source"], hm(last.get("epoch")), " (training curve only, no dp test yet)" if row["from_curve"] else ""),
                              "Don't accept the goal as done until a dp test on held-out data meets it." if sev == "red" else "")
            if done_claims and row["status"] == "no evidence":
                flags.add("crit-noev:%s" % k, "red", "integrity",
                          "Agent says the target is met, but no test output for %s was found" % c["label"],
                          trunc(done_claims[-1]["text"], 200), "Ask it to run dp test on the held-out set and keep the output.")
        crit_rows.append(row)
    # test-set hygiene
    train_sets = []
    for src_doc in (ndoc, local):
        for ti in ((src_doc or {}).get("params") or {}).get("train_inputs", []):
            train_sets.append(ti)
    train_tails = {tail2(p): ti["file"] for ti in train_sets for p in ti["train"]}
    for m in measured:
        if m.get("src") == "training curve":
            continue
        tested = list(m.get("systems") or [])
        ta = m.get("test_args") or {}
        if ta.get("system"):
            tested.append(ta["system"])
        hits = [t for t in tested if tail2(t) in train_tails]
        if hits:
            flags.add("testontrain:%s" % tail2(hits[0]), "amber", "integrity",
                      "A model was tested on data that is in its training set",
                      "%s is listed in training_data of %s" % (trunc(hits[0], 80), trunc(train_tails[tail2(hits[0])], 80)),
                      "Accuracy on training data overstates the real error. Ask for dp test on held-out systems.", needs_you=True)
        nt = m.get("n_test") or (ta.get("n") if ta else None)
        if nt is not None and nt < 50 and m.get("epoch") and NOW - m["epoch"] < CFG["hours"] * 3600:
            flags.add("smalltest:%s" % int(m["epoch"]), "amber", "integrity", "A test used only %d frames" % nt,
                      trunc(m.get("cmd") or m.get("file") or "", 160), "Small test sets make the error estimate unreliable.")
    if not contract["goal"] and not goal_text:
        flags.add("contract:empty", "info", "integrity", "The contract has no goal text yet",
                  "Fill contract/goal_contract.md so the checks know what matters.")

    # ---- instructions you typed
    instr = []
    promoted = " ".join(a["text"] for a in contract["amends"])
    for s in sessions:
        typed = [u for u in s.get("user", []) if u.get("typed")] or s.get("user", [])
        for u in typed:
            uid = hid(u["text"])
            instr.append({"id": uid, "ts": u.get("ts"), "who": s.get("label"), "host": s.get("host"),
                          "text": u["text"], "promoted": uid in promoted})
    instr.sort(key=lambda u: iso_epoch(u.get("ts")) or 0, reverse=True)
    seen_ids, uniq = set(), []
    for u in instr:
        if u["id"] in seen_ids:
            continue
        seen_ids.add(u["id"])
        uniq.append(u)
    instr = uniq[:60]

    # ---- timeline
    tl = []
    for s in fam or ([main] if main else []):
        for ev in s.get("notable", []):
            if ev.get("kind") == "monitor":
                continue
            tl.append({"ts": ev.get("ts"), "who": s.get("label"), "kind": ev.get("kind"), "text": ev.get("cmd", ""),
                       "exit": ev.get("exit"), "jobid": ev.get("jobid"), "watch": ev.get("watch")})
    for u in instr[:30]:
        if u.get("host") == gnode:
            tl.append({"ts": u["ts"], "who": "you", "kind": "you", "text": u["text"]})
    for d in (ndoc or {}).get("decisions", []):
        for line in d.get("tail", [])[-8:]:
            if line.strip():
                mm = re.search(r"(\d{4}-\d\d-\d\d[T ]\d\d:\d\d(:\d\d)?)", line)
                txt = re.sub(r"^\s*[-*]\s*(\d{4}-\d\d-\d\d[T ]\d\d:\d\d(:\d\d)?)?\s*\|?\s*", "", line.strip())
                tl.append({"ts": mm.group(1).replace(" ", "T") if mm else None, "who": "decision", "kind": "decision", "text": txt})
    for t in tl:
        t["epoch"] = iso_epoch(t.get("ts")) or 0
    tl.sort(key=lambda t: t["epoch"], reverse=True)
    tl = tl[:40]

    # ---- history + sparklines
    os.makedirs(os.path.join(STATE, "history"), exist_ok=True)
    hist_path = os.path.join(STATE, "history", gkey + ".jsonl")
    hist_line = {"t": int(NOW), "cores": running_cores,
                 "jobs_r": sum(r["running"] for r in res), "jobs_pd": sum(r["pending"] for r in res),
                 "load": (node.get("load") or [None])[0] if node else None,
                 "gpu": max([g.get("util", 0) for g in node.get("gpus", [])] or [None]) if node else None,
                 "calls": sum(s.get("calls", 0) for s in fam), "tokens": sum(((s.get("tokens") or {}).get("total_tokens") or 0) for s in fam)}
    hist = []
    for line in (rd(hist_path, "") or "").splitlines()[-2000:]:
        try:
            hist.append(json.loads(line))
        except ValueError:
            pass
    if not hist or NOW - hist[-1].get("t", 0) > 60:
        hist.append(hist_line)
        with open(hist_path, "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(h) for h in hist[-2000:]) + "\n")
    day = [h for h in hist if NOW - h.get("t", 0) < 86400]

    ref_path = os.path.join(STATE, "referee", gkey + ".md")
    referee = rd(ref_path, "")
    ref_mtime = os.path.getmtime(ref_path) if referee else None

    title_goal = trunc(re.sub(r"\s+", " ", contract["goal"] or goal_text or gname), 90)
    snap = {
        "generated": NOW, "collected": collected, "session": gname, "key": gkey, "node_name": gnode, "title": title_goal,
        "unreachable": bool(gdesc.get("unreachable")), "ended": bool(gdesc.get("ended")), "pseudo": bool(gdesc.get("pseudo")),
        "last_seen": gdesc.get("last_seen"),
        "goal": {"status": goal_status, "text": contract["goal"] or goal_text, "db": goals[:5], "paused": paused,
                 "codex_running": agent_up, "tmux_found": tm.get("found"), "notmux": bool(tm.get("notmux")),
                 "agent_kind": (main or {}).get("kind") or ("claude" if tm.get("claude_running") else ("codex" if tm.get("codex_running") else None)),
                 "last_activity": last_act, "main_calls": (main or {}).get("calls"), "quota": quota,
                 "model": (main or {}).get("meta", {}).get("ctx_model"), "effort": (main or {}).get("meta", {}).get("ctx_effort"),
                 "cwd": ((ndoc or {}).get("goal_cwds") or [""])[0],
                 "sleep_until": sleep_until, "context_left": int(ctx_m.group(1)) if ctx_m else None,
                 "warnings": int(warn_m.group(1)) if warn_m else 0, "busy": busy},
        "plan": {"steps": plan or [], "done": plan_done, "now": plan_now, "ts": plan_ts, "expl": plan_expl},
        "flags": flags.sorted(), "resources": res, "node": node, "scythe": scythe_txt,
        "compute": {"alloc_ch": round(tot_alloc, 1), "idle_ch": round(tot_idle, 1), "failed_ch": round(tot_failed, 1), "lost_ch": round(tot_lost, 1),
                    "waste_pct": None if waste_pct is None else round(waste_pct, 1), "gpu_h": round(gpu_h, 1),
                    "running_cores": running_cores},
        "params": {"table": ptable, "scan": {k: params.get(k) for k in ("files", "dirs", "roots", "truncated", "note", "cached", "t")}},
        "changes": changes[-20:], "instructions": instr, "timeline": tl, "criteria": crit_rows,
        "measured": [{k: m.get(k) for k in ("epoch", "src", "cmd", "file", "metrics", "n_test", "who")} for m in measured[-30:]],
        "sessions": [{"label": s.get("label"), "host": s.get("host"), "kind": s.get("kind"), "role": s.get("role"),
                      "model": s.get("meta", {}).get("ctx_model") or s.get("meta", {}).get("model_provider"),
                      "effort": s.get("meta", {}).get("ctx_effort"),
                      "cwd": s.get("meta", {}).get("cwd") or s.get("meta", {}).get("ctx_cwd"),
                      "last": s.get("last_epoch"), "calls": s.get("calls"), "errors": s.get("errors"),
                      "tokens": (s.get("tokens") or {}).get("total_tokens"), "path": s.get("path")}
                     for s in sorted(sessions, key=lambda s: -(s.get("last_epoch") or 0))][:40],
        "agent_words": [{"ts": m["t"], "epoch": m["e"], "who": m.get("w") or "main", "text": m["x"], "mdl": m.get("md") or ""}
                        for m in agent_msgs[-5:]][::-1],
        "conv": {"count": len(conv), "agent": len([m for m in conv if m["r"] == "agent"]), "page": gkey + ".conversation.html" if conv_days else None,
                 "days": conv_days, "counts": conv_counts,
                 "last_you": {"ts": last_you["t"], "epoch": last_you["e"], "text": last_you["x"]} if last_you else None},
        "pane": tail[-70:], "tmux": {k: tm.get(k) for k in ("found", "panes", "agent_cmds", "error", "notmux", "pid", "cwd", "target", "members")},
        "hosts": hosts, "history": day, "contract": {k: contract[k] for k in ("clauses", "watch", "amends", "path")},
        "referee": {"text": referee, "mtime": ref_mtime}, "git": (ndoc or {}).get("git"),
    }
    return snap


# ---------------------------------------------------------------- full conversations
CONV_DIR = os.path.join(STATE, "conversations")


def conv_key(host, path):
    return hashlib.sha1(("%s:%s" % (host, path)).encode()).hexdigest()[:12]


def msg_id(m):
    return "%s|%s|%s" % (m.get("ts"), m.get("role"), hashlib.sha1((m.get("text") or "").encode("utf-8", "replace")).hexdigest()[:10])


def update_conversations(shared):
    """Append new agent/user messages from every digest to state/conversations/<session>.jsonl."""
    os.makedirs(CONV_DIR, exist_ok=True)
    idx_p = os.path.join(CONV_DIR, "index.json")
    idx = rjson(idx_p, {}) or {}
    docs = [(n, nd["doc"]) for n, nd in list(shared["nodes"].items()) + list(shared.get("logins", {}).items())] + [(LOCAL_NAME, shared["local"])]
    changed = False
    for host, doc in docs:
        if not doc or not doc.get("ok"):
            continue
        for sess in doc.get("sessions", []):
            new = sess.get("msgs_new") or []
            if not new:
                continue
            path = sess.get("path")
            k = conv_key(host, path)
            ent = idx.get(k) or {"host": host, "path": path, "epoch": None, "seq": 0, "count": 0}
            fp = os.path.join(CONV_DIR, k + ".jsonl")
            epoch = sess.get("msg_epoch")
            if ent.get("epoch") == epoch:
                add = [m for m in new if m["seq"] > ent.get("seq", 0)]
            else:  # the digest rebuilt its state: keep what we have, add only messages we haven't seen
                have = set()
                for line in (rd(fp, "") or "").splitlines():
                    try:
                        have.add(msg_id(json.loads(line)))
                    except ValueError:
                        pass
                add = [m for m in new if msg_id(m) not in have]
            if add:
                with open(fp, "a", encoding="utf-8") as f:
                    for m in add:
                        row = {"ts": m.get("ts"), "role": m.get("role"), "text": m.get("text")}
                        if m.get("mdl"):
                            row["mdl"] = m["mdl"]
                        f.write(json.dumps(row) + "\n")
                ent["count"] = ent.get("count", 0) + len(add)
                ent["last_ts"] = add[-1].get("ts")
            ent["epoch"], ent["seq"] = epoch, max([m["seq"] for m in new] + [ent.get("seq", 0) if ent.get("epoch") == epoch else 0])
            idx[k] = ent
            changed = True
    if changed:
        wjson(idx_p, idx)
    return idx


def load_conversation(sessions):
    msgs = []
    for x in sessions:
        k = conv_key(x.get("host"), x.get("path"))
        rows = []
        for line in (rd(os.path.join(CONV_DIR, k + ".jsonl"), "") or "").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue

        def is_review(m):
            t = (m.get("text") or "").lstrip()
            return (m.get("role") == "agent" and t.startswith("{") and '"risk_level"' in t[:200]) or \
                (m.get("role") == "user" and t.startswith("The following is the Codex agent history"))
        # Codex's automatic approval reviewer runs as its own session: its "user" turns are prompts, not you
        reviewer = sum(1 for m in rows if is_review(m)) * 2 >= max(1, len([m for m in rows if m.get("role") in ("agent", "user")]))
        label = "approval reviewer" if reviewer else x.get("label")
        sub = not reviewer and label not in (None, "main")
        for m in rows:
            role = "review" if (reviewer and m.get("role") in ("agent", "user")) or is_review(m) else m.get("role")
            if sub and role == "user":
                role = "task"   # a sub-agent's "user" turns are instructions from the main agent
            elif sub and role == "agent":
                role = "sub"
            msgs.append({"t": m.get("ts"), "e": iso_epoch(m.get("ts")) or 0, "r": role, "w": label, "x": m.get("text") or "", "md": m.get("mdl") or ""})
    msgs.sort(key=lambda m: m["e"])
    return msgs


TEX_SYM = {"times": "\u00d7", "cdot": "\u00b7", "approx": "\u2248", "le": "\u2264", "leq": "\u2264", "ge": "\u2265", "geq": "\u2265",
           "pm": "\u00b1", "to": "\u2192", "rightarrow": "\u2192", "sim": "~", "ne": "\u2260", "neq": "\u2260", "infty": "\u221e",
           "Delta": "\u0394", "delta": "\u03b4", "mu": "\u03bc", "sigma": "\u03c3", "alpha": "\u03b1", "beta": "\u03b2",
           "gamma": "\u03b3", "theta": "\u03b8", "lambda": "\u03bb", "pi": "\u03c0", "epsilon": "\u03b5", "AA": "\u00c5",
           "circ": "\u00b0", "degree": "\u00b0", "%": "%", ",": "\u2009", ";": " ", "!": "", " ": " "}


def tex(x):
    """Just enough TeX for numbers and units in agent messages: \\(10^{-6}\\) -> 10<sup>-6</sup>."""
    x = re.sub(r"\\(?:mathrm|text|mathit|mathbf|operatorname)\{([^{}]*)\}", r"\1", x)
    x = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"\1/\2", x)
    x = re.sub(r"\\sqrt\{([^{}]*)\}", "\u221a\\1", x)
    x = re.sub(r"\\([A-Za-z]+|[,;! %])", lambda k: TEX_SYM.get(k.group(1), k.group(1)), x)
    x = re.sub(r"\^\{([^{}]*)\}", r"<sup>\1</sup>", x)
    x = re.sub(r"_\{([^{}]*)\}", r"<sub>\1</sub>", x)
    x = re.sub(r"\^([A-Za-z0-9+\-])", r"<sup>\1</sup>", x)
    x = re.sub(r"_([A-Za-z0-9])", r"<sub>\1</sub>", x)
    return '<span class="math">%s</span>' % x.replace("{", "").replace("}", "")


def md_inline(t):
    # code spans first (their contents stay literal), held as placeholders so **bold** can wrap them
    codes = []

    def hold(m):
        codes.append("<code>%s</code>" % esc(m.group(1)))
        return "\x00%d\x00" % (len(codes) - 1)
    part = esc(re.sub(r"`([^`]+)`", hold, (t or "").replace("\x00", "")))
    part = re.sub(r"\\\((.+?)\\\)|\\\[(.+?)\\\]", lambda m: tex(m.group(1) or m.group(2)), part)
    part = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", part)
    part = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", part)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], part)


def md_table(rows):
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    head = None
    if len(cells) >= 2 and all(re.match(r"^:?-{2,}:?$", c) for c in cells[1] if c):
        head, cells = cells[0], cells[2:]
    h = '<table class="mdt">'
    if head:
        h += "<tr>%s</tr>" % "".join("<th>%s</th>" % md_inline(c) for c in head)
    h += "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % md_inline(c) for c in r) for r in cells)
    return h + "</table>"


def md(text):
    """Just enough Markdown for agent messages: paragraphs, lists, headings, tables, code, bold."""
    out, para, buf, in_code, tbl = [], [], [], False, []

    def flush():
        if para:
            out.append("<p>%s</p>" % "<br>".join(md_inline(x) for x in para))
            del para[:]
        if tbl:
            out.append(md_table(tbl))
            del tbl[:]
    for ln in (text or "").split("\n"):
        if not in_code and ln.strip().startswith("|") and ln.strip().count("|") >= 2:
            if para:
                out.append("<p>%s</p>" % "<br>".join(md_inline(x) for x in para))
                del para[:]
            tbl.append(ln)
            continue
        if tbl:
            out.append(md_table(tbl))
            del tbl[:]
        if ln.strip().startswith("```"):
            if in_code:
                out.append("<pre>%s</pre>" % esc("\n".join(buf)))
                buf, in_code = [], False
            else:
                flush()
                in_code = True
            continue
        if in_code:
            buf.append(ln)
            continue
        if not ln.strip():
            flush()
            continue
        mh = re.match(r"\s*#{1,4}\s+(.*)", ln)
        ml = re.match(r"\s*(?:[-*]|\d+[.)])\s+(.*)", ln)
        if mh:
            flush()
            out.append('<div class="mdh">%s</div>' % md_inline(mh.group(1)))
        elif ml:
            flush()
            out.append('<div class="mdli">\u2022 %s</div>' % md_inline(ml.group(1)))
        else:
            para.append(ln)
    if in_code:
        out.append("<pre>%s</pre>" % esc("\n".join(buf)))
    flush()
    return "".join(out)


CONV_JS = r"""
(function(){
var D=window.MC_CONV||[],L=document.getElementById('list'),Q=document.getElementById('q'),O=document.getElementById('order'),KEY=document.body.getAttribute('data-key');
var ROLES=['agent','sub','user','task','event','review'],pending=[];
var NAME={agent:'Agent',sub:'Sub-agent',user:'You',task:'Task for',event:'Event',review:'Approval reviewer'},AV={agent:'A',task:'→',review:'✓'};
var TEX={times:'×',cdot:'·',approx:'≈',le:'≤',leq:'≤',ge:'≥',geq:'≥',pm:'±',to:'→',rightarrow:'→',sim:'~',ne:'≠',neq:'≠',infty:'∞',
 Delta:'Δ',delta:'δ',mu:'μ',sigma:'σ',alpha:'α',beta:'β',gamma:'γ',theta:'θ',lambda:'λ',pi:'π',epsilon:'ε',AA:'Å',circ:'°',degree:'°',',':' ',';':' ','!':'',' ':' ','%':'%'};
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function tex(x){x=x.replace(/\\(?:mathrm|text|mathit|mathbf|operatorname)\{([^{}]*)\}/g,'$1').replace(/\\frac\{([^{}]*)\}\{([^{}]*)\}/g,'$1/$2').replace(/\\sqrt\{([^{}]*)\}/g,'√$1')
 .replace(/\\([A-Za-z]+|[,;! %])/g,function(a,k){return TEX.hasOwnProperty(k)?TEX[k]:k;}).replace(/\^\{([^{}]*)\}/g,'<sup>$1</sup>').replace(/_\{([^{}]*)\}/g,'<sub>$1</sub>')
 .replace(/\^([A-Za-z0-9+\-])/g,'<sup>$1</sup>').replace(/_([A-Za-z0-9])/g,'<sub>$1</sub>');return '<span class="math">'+x.replace(/[{}]/g,'')+'</span>';}
function inl(s){var cd=[],p=esc(String(s).replace(/\u0000/g,'').replace(/`([^`]+)`/g,function(a,c){cd.push('<code>'+esc(c)+'</code>');return '\u0000'+(cd.length-1)+'\u0000';}));
 p=p.replace(/\\\((.+?)\\\)|\\\[(.+?)\\\]/g,function(a,b,c){return tex(b||c);}).replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>').replace(/\[([^\]]+)\]\(([^)]+)\)/g,'$1');
 return p.replace(/\u0000(\d+)\u0000/g,function(a,i){return cd[+i];});}
function tbl(rows){var cs=rows.map(function(r){return r.trim().replace(/^\||\|$/g,'').split('|').map(function(c){return c.trim();});});var h=null;
 if(cs.length>=2&&cs[1].every(function(c){return !c||/^:?-{2,}:?$/.test(c);})){h=cs[0];cs=cs.slice(2);}
 return '<table class="mdt">'+(h?'<tr>'+h.map(function(c){return '<th>'+inl(c)+'</th>';}).join('')+'</tr>':'')+cs.map(function(r){return '<tr>'+r.map(function(c){return '<td>'+inl(c)+'</td>';}).join('')+'</tr>';}).join('')+'</table>';}
function md(t){var o=[],p=[],b=[],c=false,tb=[];function fp(){if(p.length){o.push('<p>'+p.map(inl).join('<br>')+'</p>');p=[];}}function ft(){if(tb.length){o.push(tbl(tb));tb=[];}}function f(){fp();ft();}
 t.split('\n').forEach(function(l){if(!c&&/^\s*\|/.test(l)&&(l.match(/\|/g)||[]).length>=2){fp();tb.push(l);return;}ft();
 if(/^\s*```/.test(l)){if(c){o.push('<pre>'+esc(b.join('\n'))+'</pre>');b=[];c=false;}else{f();c=true;}return;}
 if(c){b.push(l);return;} if(!l.trim()){f();return;} var h=l.match(/^\s*#{1,4}\s+(.*)/),li=l.match(/^\s*(?:[-*]|\d+[.)])\s+(.*)/);
 if(h){f();o.push('<div class="mdh">'+inl(h[1])+'</div>');}else if(li){f();o.push('<div class="mdli">• '+inl(li[1])+'</div>');}else p.push(l);});
 if(c)o.push('<pre>'+esc(b.join('\n'))+'</pre>');f();return o.join('');}
function day(e){return new Date(e*1000).toLocaleDateString(undefined,{weekday:'long',month:'short',day:'numeric',year:'numeric'});}
function tm(e){return new Date(e*1000).toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'});}
function who(m){var n=NAME[m.r]||m.r;if(m.r==='task')return n+' '+esc(m.w||'sub-agent');if(m.r==='sub'||(m.r==='agent'&&m.w&&m.w!=='main'))n+=' · '+esc(m.w);return n;}
function av(m){if(m.r==='sub')return 'S'+String(m.w||'').replace(/[^0-9]/g,'');return AV[m.r]||'?';}
function on(r){var el=document.getElementById('f_'+r);return !el||el.checked;}
function idOf(m){return m.e+'|'+m.r+'|'+m.x.length+'|'+m.x.slice(0,60);}
function atBottom(){return window.innerHeight+window.scrollY>=document.body.scrollHeight-160;}
function toLatest(){if(O.value==='new')window.scrollTo(0,0);else window.scrollTo(0,document.body.scrollHeight);}
function render(){var q=(Q.value||'').toLowerCase();
 var items=D.filter(function(m){return on(m.r)&&(!q||m.x.toLowerCase().indexOf(q)>=0);}).concat(q?[]:pending);
 if(O.value==='new')items=items.slice().reverse();
 var h=[],lastDay='',pk=null,pe=0;
 items.forEach(function(m){var d=day(m.e);if(d!==lastDay){h.push('<div class="day"><span>'+d+'</span></div>');lastDay=d;pk=null;}
  if(m.r==='event'){h.push('<div class="evt"><span>'+inl(m.x)+' · '+tm(m.e)+'</span></div>');pk=null;return;}
  var k=m.r+'|'+(m.w||''),cont=k===pk&&Math.abs(m.e-pe)<600;pk=k;pe=m.e;
  var long=m.x.length>2200||m.x.split('\n').length>26;
  h.push('<div class="row r-'+m.r+(cont?' cont':'')+(m.pending?' pending':'')+'">'+(m.r==='user'?'':'<div class="av">'+av(m)+'</div>')+
   '<div class="bub">'+(cont?'':'<div class="mh"><span class="who">'+who(m)+'</span><span class="tm">'+tm(m.e)+'</span>'+(m.md?'<span class="mdl">'+esc(m.md)+'</span>':'')+'</div>')+
   '<div class="mb'+(long?' clip':'')+'" title="'+tm(m.e)+'">'+md(m.x)+'</div>'+(long?'<button class="more">Show all</button>':'')+'</div></div>');});
 L.innerHTML=h.join('')||'<div class="empty">No messages match.</div>';
 document.getElementById('n').textContent=D.length.toLocaleString()+' messages';}
L.addEventListener('click',function(ev){var b=ev.target.closest('.more');if(!b)return;var mb=b.previousElementSibling;var c=mb.classList.toggle('clip');b.textContent=c?'Show all':'Show less';});
var tmr;Q.addEventListener('input',function(){clearTimeout(tmr);tmr=setTimeout(render,200);});
try{var so=localStorage.getItem('mc.order');if(so)O.value=so;}catch(e){}
O.addEventListener('change',function(){try{localStorage.setItem('mc.order',O.value);}catch(e){}render();toLatest();});
ROLES.forEach(function(r){var el=document.getElementById('f_'+r);if(el)el.addEventListener('change',function(){el.parentNode.classList.toggle('off',!el.checked);render();});});
document.getElementById('jump').addEventListener('click',toLatest);
document.addEventListener('mc-sent',function(ev){pending.push({t:'',e:Date.now()/1000,r:'user',w:'',x:ev.detail.text,pending:true});render();toLatest();});
D.sort(function(a,b){return a.e-b.e;});render();toLatest();
// opened with mctl (http): pick up new messages every minute without reloading the page
if(/^https?:/.test(location.protocol)){var pad=function(n){return (n<10?'0':'')+n;};
 window.mcConvPoll=function(){var d=new Date(),f='conv/'+encodeURIComponent(KEY)+'/'+d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+'.js';
  fetch(f,{cache:'no-store'}).then(function(r){return r.ok?r.text():'';}).then(function(t){var i=t.indexOf('MC_CONV,'),j=t.lastIndexOf(');');if(i<0||j<0)return;
   var arr=JSON.parse(t.slice(i+8,j)),seen={},add=0,bottom=atBottom();D.forEach(function(m){seen[idOf(m)]=1;});
   arr.forEach(function(m){if(!seen[idOf(m)]){D.push(m);add++;if(m.r==='user')pending=pending.filter(function(p){return p.x.trim()!==m.x.trim();});}});
   if(add){D.sort(function(a,b){return a.e-b.e;});render();if(bottom)toLatest();}}).catch(function(){});};
 setInterval(window.mcConvPoll,30000);document.addEventListener('visibilitychange',function(){if(!document.hidden)window.mcConvPoll();});}
})();
"""

CONV_CSS = """
.ctl{position:sticky;top:var(--abh,48px);z-index:10;background:color-mix(in srgb,var(--bg) 88%,transparent);-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);
padding:10px 0;display:flex;flex-wrap:wrap;gap:8px;align-items:center;border-bottom:1px solid var(--line);margin-bottom:4px}
.ctl input[type=search]{flex:1;min-width:200px;padding:8px 14px;border-radius:99px;border:1px solid var(--line);background:var(--card);color:var(--fg);font-size:14px}
.ctl select,.ctl button{padding:6px 12px;border-radius:99px;border:1px solid var(--line);background:var(--card);color:var(--fg);font-size:13px}
.chip{--c:var(--c-agent);display:inline-flex;align-items:center;gap:6px;font-size:12.5px;padding:4px 10px;border-radius:99px;border:1px solid var(--line);background:var(--card);cursor:pointer;user-select:none}
.chip input{display:none}.chip .sq{width:10px;height:10px;border-radius:50%;background:var(--c)}.chip b{font-weight:600;color:var(--mut)}.chip.off{opacity:.45}.chip.off .sq{background:none;border:1px solid var(--c)}
.chip.r-agent{--c:var(--c-agent)}.chip.r-sub{--c:var(--c-sub)}.chip.r-user{--c:var(--c-you)}.chip.r-task{--c:var(--c-task)}.chip.r-event{--c:var(--c-event)}.chip.r-review{--c:var(--c-review)}
.composer{position:sticky;bottom:0;z-index:8;padding:16px 0 12px;background:linear-gradient(to bottom,transparent,var(--bg) 30%)}
.composer .sendbox{margin:0;padding:10px 12px;border:1px solid var(--line);border-radius:20px;background:var(--card);box-shadow:0 6px 24px rgba(0,0,0,.16)}
.composer .sendbox textarea{border:none;background:transparent;padding:6px 4px;min-height:40px}.composer .sendbox textarea:focus{outline:none}
.composer #sendres{max-height:40vh;overflow:auto}
@media (max-width:640px){.ctl{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none}.ctl::-webkit-scrollbar{display:none}
.ctl input[type=search]{min-width:0;flex:0 0 150px}.ctl .chip,.ctl select,.ctl button{flex:none}.composer{padding-bottom:max(12px,env(safe-area-inset-bottom))}}
"""
ROLE_CHIPS = [("agent", "Agent"), ("sub", "Sub-agents"), ("user", "You"), ("task", "Tasks for sub-agents"), ("event", "Events"), ("review", "Approval reviews")]


# Every block on the pages can be hidden: click its x (this browser), or `mctl hide NAME` (every machine).
SECTIONS = [("latest", "Agent's latest words"), ("cards", "Key numbers"), ("needs", "Needs you"), ("criteria", "Acceptance criteria"),
            ("plan", "Plan"), ("resources", "Resources"), ("integrity", "Scientific integrity"), ("watchdog", "Watchdog"),
            ("timeline", "What happened"), ("instructions", "Your instructions"), ("screen", "Live screen"), ("slurm", "Slurm jobs"),
            ("sessions", "Agent sessions"), ("referee", "Referee review"), ("contract", "Contract"), ("others", "Other agent logs")]
SEC_LABEL = dict(SECTIONS)


def sec(scope, name, label=None):
    """Attributes that make a block hideable. scope: g = session pages, o = overview."""
    return ' data-sec="%s:%s" data-label="%s"' % (scope, esc(name), esc(label or SEC_LABEL.get(name, name)))


def hide_css():
    rules = ",".join('[data-sec$=":%s"]' % re.sub(r'["\\\]\[]', "", n) for n in CFG["hide"])
    return ("<style>%s{display:none!important}</style>" % rules) if rules else ""


HIDE_CSS = """
[data-sec]{position:relative}.hide-x{position:absolute;top:-9px;right:-9px;z-index:3;width:22px;height:22px;border-radius:50%;border:1px solid var(--line);
background:var(--card);color:var(--mut);font-size:14px;line-height:19px;text-align:center;padding:0;cursor:pointer;opacity:0;transition:opacity .15s;box-shadow:var(--shadow)}
[data-sec]:hover>.hide-x,summary:hover>.hide-x{opacity:1}.hide-x:hover{color:var(--red);border-color:var(--red)}
summary>.hide-x{position:static;float:right;margin-top:-1px}@media (hover:none){.hide-x{opacity:.7}}
.hbar{display:flex;flex-wrap:wrap;align-items:center;gap:6px;font-size:12.5px;margin:-6px 0 14px}
.hbar button{font-size:12px;padding:3px 10px;border-radius:99px;border:1px dashed var(--line);background:none;color:var(--mut);cursor:pointer}.hbar button:hover{color:var(--blu);border-color:var(--blu)}
"""

HIDE_JS = r"""<script>
(function(){
var K='mc.hidden',H={};try{H=JSON.parse(localStorage.getItem(K)||'{}')||{};}catch(e){}
function save(){try{localStorage.setItem(K,JSON.stringify(H));}catch(e){}}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');}
var els=[];
function scan(){els=[].slice.call(document.querySelectorAll('[data-sec]'));els.forEach(function(el){if(el.getAttribute('data-hx'))return;el.setAttribute('data-hx','1');var b=document.createElement('button');b.className='hide-x';b.type='button';b.title='Hide “'+el.getAttribute('data-label')+'” (Show again from the Hidden bar)';b.textContent='×';
 b.addEventListener('click',function(ev){ev.preventDefault();ev.stopPropagation();H[el.getAttribute('data-sec')]=el.getAttribute('data-label');save();apply();});
 var host=el.tagName==='DETAILS'?el.querySelector('summary'):el;if(host)host.appendChild(b);});apply();}
function apply(){var hid=[];els.forEach(function(el){var h=!!H[el.getAttribute('data-sec')];el.style.display=h?'none':'';if(h)hid.push(el);});
 var bar=document.getElementById('hidden-bar');if(!bar)return;
 if(!hid.length){bar.style.display='none';bar.innerHTML='';return;}
 bar.style.display='';bar.innerHTML='<span class="mut">Hidden here:</span>'+hid.map(function(el){return '<button type="button" data-k="'+esc(el.getAttribute('data-sec'))+'" title="Show it again">'+esc(el.getAttribute('data-label'))+' ↺</button>';}).join('')+(hid.length>1?'<button type="button" data-k="*">Show all</button>':'');}
document.addEventListener('click',function(ev){var b=ev.target.closest?ev.target.closest('#hidden-bar button'):null;if(!b)return;var k=b.getAttribute('data-k');
 if(k==='*'){els.forEach(function(el){delete H[el.getAttribute('data-sec')];});}else{delete H[k];}save();apply();});
window.mcHideScan=scan;scan();
})();
</script>"""


def hidden_note():
    if not CFG["hide"]:
        return ""
    return '<br>Hidden on every machine (config.env HIDE_SECTIONS): %s \u00b7 <span class="mono">mctl unhide NAME</span> brings one back' % esc(
        ", ".join(SEC_LABEL.get(n, n) for n in CFG["hide"]))


HUE = {}   # session key -> palette index 0..7 (state/hues.json keeps it for the session's whole life)


def assign_hues(keys):
    """Give every session its own colour and keep it: a new session takes the least-used free colour."""
    path = os.path.join(STATE, "hues.json")
    db = rjson(path, {}) or {}
    db = {k: v for k, v in db.items() if isinstance(v, list) and len(v) == 2 and (k in keys or NOW - v[1] < 14 * 86400)}
    for k in keys:
        if k not in db:  # the least-used colour, as far round the colour wheel from the others as possible
            used = [db[x][0] for x in keys if x in db]
            dist = lambda i: min([min((i - u) % 8, (u - i) % 8) for u in used] or [8])  # noqa: E731
            idx = min(range(8), key=lambda i: (used.count(i), -dist(i), (zlib.crc32(k.encode()) + i) % 8))
            db[k] = [idx, NOW]
        db[k][1] = NOW
        HUE[k] = db[k][0]
    try:
        wjson(path, db)
    except OSError:
        pass


def hue(key):
    """CSS custom property for a session's colour."""
    return "--h:var(--hu%d)" % HUE.get(key, zlib.crc32((key or "").encode()) % 8)


def appbar(nav, cur=None, where="root", conv=False, collected=None):
    """Sticky bar on every page: every agent session, grouped by host, one click away."""
    pre = "goals/" if where == "root" else ""
    home = "dashboard.html" if where == "root" else "../dashboard.html"
    order, groups = [], {}
    for n in nav:
        h = n.get("node") or "?"
        if h not in groups:
            groups[h] = []
            order.append(h)
        groups[h].append(n)
    parts = []
    for h in order:
        parts.append('<span class="hg">%s</span>' % esc(LOGIN_HOSTS.get(h, h)))
        for n in groups[h]:
            href = pre + n["key"] + (".conversation.html" if conv and n.get("conv") else ".html")
            dot = "gray" if n.get("dim") else {"red": "red", "amber": "amber"}.get(n.get("sev"), "")
            parts.append('<a class="np%s%s" style="%s" href="%s" title="%s on %s"><span class="dot %s"></span>%s</a>' % (
                " cur" if n["key"] == cur else "", " dim" if n.get("dim") else "", hue(n["key"]), esc(href), esc(n["name"]), esc(h), dot,
                esc(trunc(n["name"], 30))))
    return ('<div class="appbar"><div class="in"><a class="brand" href="%s"><i></i><span>Mission control</span></a><nav class="sw">%s</nav>%s</div></div>' % (
        home, "".join(parts) or '<span class="small mut">no agent sessions found</span>', fresh_pill(collected) if collected is not None else ""))


def write_conv_data(gkey, msgs):
    """Per-day data files (only changed days are rewritten, to keep OneDrive quiet). Returns the day names."""
    base = os.path.join(MC_DIR, "goals", "conv", gkey)
    os.makedirs(base, exist_ok=True)
    days = {}
    for m in msgs:
        d = time.strftime("%Y-%m-%d", time.localtime(m["e"])) if m["e"] else "undated"
        days.setdefault(d, []).append({k: m[k] for k in ("t", "e", "r", "w", "x", "md") if m.get(k) or k in ("t", "e", "r", "x")})
    for d, lst in days.items():
        body = "(window.MC_CONV=window.MC_CONV||[]).push.apply(window.MC_CONV,%s);\n" % json.dumps(lst, ensure_ascii=False)
        fp = os.path.join(base, d + ".js")
        if rd(fp) != body:
            with open(fp, "w", encoding="utf-8") as f:
                f.write(body)
    return sorted(days)


def write_conv_page(snap, nav):
    gkey, gname, cv = snap["key"], snap["session"], snap.get("conv") or {}
    counts = cv.get("counts") or {}
    chips = "".join('<label class="chip r-%s%s"><input type="checkbox" id="f_%s"%s><span class="sq"></span>%s <b>%s</b></label>' % (
        r, "" if r != "review" else " off", r, "" if r == "review" else " checked", lab, "{:,}".format(counts[r]))
        for r, lab in ROLE_CHIPS if counts.get(r))
    scripts = "".join('<script src="conv/%s/%s.js"></script>' % (esc(gkey), esc(d)) for d in cv.get("days") or [])
    page = ('<!doctype html><html lang="en"><head>%s<title>%s \u00b7 conversation</title><style>%s%s</style></head><body class="convpage" data-key="%s" style="%s">%s<main class="chatpage">'
            '<header><div><h1>%s</h1><p class="sub">%s on %s \u00b7 <span id="n"></span> \u00b7 new messages appear by themselves when opened with mctl</p></div>'
            '<a class="np" href="%s.html">\u2190 Session page</a></header>'
            '<div class="ctl"><input id="q" type="search" placeholder="Search the conversation">%s'
            '<select id="order"><option value="chat">Chat order</option><option value="new">Newest first</option></select><button id="jump">Latest \u2193</button></div>'
            '<div id="list" class="chat"></div><div class="composer">%s</div></main>%s<script>%s</script>%s</body></html>') % (
        HEAD_META, esc(gname), CSS, CONV_CSS, esc(gkey), hue(gkey), appbar(nav, gkey, "goals", True, snap.get("collected")), esc(gname),
        esc((snap["goal"].get("agent_kind") or "agent").capitalize()), esc(LOGIN_HOSTS.get(snap.get("node_name"), snap.get("node_name") or "?")),
        esc(gkey), chips, send_box(snap, compact=True), scripts, CONV_JS, SEND_JS + REFRESH_JS)
    fp = os.path.join(MC_DIR, "goals", gkey + ".conversation.html")
    if rd(fp) != page:
        with open(fp, "w", encoding="utf-8") as f:
            f.write(page)


# ---------------------------------------------------------------- goals: discovery and orchestration
def discover_goals(shared):
    goals = []
    for n, nd in list(shared["nodes"].items()) + list(shared.get("logins", {}).items()):
        doc = nd["doc"]
        if not doc or not doc.get("ok"):
            continue
        tmx = doc.get("tmux") or {}
        if not doc.get("multi"):
            tmx = {(tmx.get("session") or CFG["session"]): tmx} if tmx else {}
        for sess, tm in tmx.items():
            goals.append({"name": sess, "node": n, "tm": tm, "doc": doc})
    ld = shared["local"]
    if ld and ld.get("ok") and ld.get("multi"):
        for sess, tm in (ld.get("tmux") or {}).items():
            goals.append({"name": sess, "node": LOCAL_NAME, "tm": tm, "doc": ld})
    names = [g["name"] for g in goals]
    for g in goals:
        g["key"] = slug(g["name"]) if names.count(g["name"]) == 1 else slug(g["node"] + "-" + g["name"])
    seen_p = os.path.join(STATE, "goals_seen.json")
    seen = rjson(seen_p, {}) or {}
    live = {g["key"] for g in goals}
    live_names = {g["name"] for g in goals}
    for g in goals:
        live_names.update((g.get("tm") or {}).get("members") or [])
    valid_nodes = set(shared["nodes"]) | set(shared.get("logins", {})) | ({LOCAL_NAME} if shared["local"] else set())
    for g in goals:
        seen[g["key"]] = {"name": g["name"], "node": g["node"], "last_seen": NOW}
    for k, v in list(seen.items()):
        if k in live:
            continue
        if v.get("name") in live_names or v.get("node") not in valid_nodes:
            del seen[k]  # renamed key for a goal we can see, or a node no longer watched
            continue
        nd = shared["nodes"].get(v.get("node")) or shared.get("logins", {}).get(v.get("node")) or ({"doc": shared["local"]} if v.get("node") == LOCAL_NAME else {})
        node_ok = bool((nd.get("doc") or {}).get("ok"))
        age = NOW - (v.get("last_seen") or 0)
        if age >= 7 * 86400:
            del seen[k]
        elif not node_ok:
            goals.append({"name": v["name"], "node": v["node"], "key": k, "tm": {}, "doc": None, "unreachable": True, "last_seen": v.get("last_seen")})
        elif age < 86400:
            goals.append({"name": v["name"], "node": v["node"], "key": k, "tm": {}, "doc": None, "ended": True, "last_seen": v.get("last_seen")})
    if not goals and CFG["session"]:
        n0 = (CFG["nodes"].split() or [LOCAL_NAME])[0]
        if not ((shared["nodes"].get(n0) or {}).get("doc") or {}).get("ok"):
            goals.append({"name": CFG["session"], "node": n0, "key": slug(CFG["session"]), "tm": {}, "doc": None,
                          "unreachable": True, "last_seen": None})
    wjson(seen_p, seen)
    return goals


GOAL_AREAS = ("goal", "integrity")
PRIVATE_IP = re.compile(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)")


def node_from_socket(path):
    """Host name in a shared-connection socket path such as ~/.ssh/mypc-shared/node7-reconnected.sock."""
    if not re.search(r"(/\.ssh/([^/]+/)?|^/tmp/([^/]+/)?)[^/]+$", path or ""):
        return None  # only where mission control's nested route looks for sockets
    b = os.path.basename(path).lower()
    for suf in ("-reconnected.sock", ".sock", ".socket", ".ctl"):
        if b.endswith(suf):
            b = b[:-len(suf)]
            break
    for pre in ("cm-", "ssh-", "control-", "mux-", "master-", "cm_", "ssh_"):
        if b.startswith(pre):
            b = b[len(pre):]
    if "@" in b:
        b = b.split("@", 1)[1]
    b = re.split(r"[:_]", b)[0].split(".")[0]
    if not re.match(r"^[a-z][a-z0-9-]{1,30}$", b) or re.match(r"^[0-9a-f]{12,}$", b):
        return None
    return b


def clean_conflict_copies():
    """When two machines write the same file, OneDrive keeps both ('dashboard-MyLaptop.html'). For files
    mission control rebuilds every cycle the extra copy is clutter, so remove it. Contracts and the
    conversation store are never touched."""
    hosts = {LOCAL_NAME}
    try:
        for fn in os.listdir(MC_DIR):
            mm = re.match(r"config\.(.+)\.env$", fn)
            if mm and mm.group(1) != "local":
                hosts.add(mm.group(1))
    except OSError:
        return
    rx = re.compile(r"^(.+)-(%s)(-\d+)?(\.[A-Za-z0-9]+)$" % "|".join(re.escape(h) for h in hosts if h), re.I)
    rebuilt = {MC_DIR: {"dashboard.html"},
               STATE: {"snapshot.json", "collector.json", "goals_seen.json", "alerts.json", "doctor.log", "conn_doctor.txt"}}
    dirs = [MC_DIR, STATE, LATEST, os.path.join(STATE, "goals"), os.path.join(MC_DIR, "goals")]
    cdir = os.path.join(MC_DIR, "goals", "conv")
    if os.path.isdir(cdir):
        dirs += [os.path.join(cdir, k) for k in os.listdir(cdir)]
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for fn in names:
            mm = rx.match(fn)
            if not mm:
                continue
            base = mm.group(1) + mm.group(4)
            if d in rebuilt and base not in rebuilt[d]:
                continue
            if os.path.isfile(os.path.join(d, fn)) and os.path.exists(os.path.join(d, base)):
                try:
                    os.remove(os.path.join(d, fn))
                except OSError:
                    pass


def update_auto_nodes(shared):
    """Nodes your connection script holds a shared connection to on the Rockfish login node become agent nodes automatically:
    candidates are probed by the next Slurm snapshot, and kept if they have a private address and answer on ssh."""
    doc = (shared.get("logins", {}).get("rockfish-login") or {}).get("doc") or {}
    if not doc.get("ok"):
        return
    sj_label = next((l for l in os.environ.get("SJ_TARGET", "").lower().split(".") if l and l != "login"), "skipjack")
    bad = NEVER + ["login", "skipjack", sj_label, "rockfish", "dtn", "github", "gitlab", "bitbucket"]
    bad += [w.lower() for w in os.environ.get("EXCLUDE_HOSTS", "").split() if w]
    skip = {"rf", "sj", "mt", "bl", (CFG.get("rf") or "").lower(), (CFG.get("sj") or "").lower(), str(doc.get("host", "")).lower()}
    configured = set(CFG["nodes"].split())
    cands = []
    for sock in doc.get("ssh_masters") or []:
        n = node_from_socket(sock)
        if not n or n in skip or n in configured or any(w in n for w in bad) or n in cands:
            continue
        cands.append(n)
    rfc = next((c for c in shared["clusters"] if c["name"] == "Rockfish"), None) or {}
    info = rfc.get("nodes") or {}
    keep = [n for n in cands if PRIVATE_IP.match((info.get(n) or {}).get("resolves") or "") and (info.get(n) or {}).get("ssh_port") == "open"]
    for fn, vals in (("auto_candidates", cands), ("auto_nodes", keep)):
        body = " ".join(vals) + "\n"
        if (rd(os.path.join(STATE, fn), "") or "") != body:
            with open(os.path.join(STATE, fn), "w") as f:
                f.write(body)


def analyze():
    shared = load_shared()
    try:
        update_conversations(shared)
    except Exception as e:  # never let history bookkeeping break the dashboard
        sys.stderr.write("conversation store: %s\n" % e)
    try:
        update_auto_nodes(shared)
    except Exception as e:
        sys.stderr.write("auto nodes: %s\n" % e)
    try:
        clean_conflict_copies()
    except Exception as e:
        sys.stderr.write("conflict copies: %s\n" % e)
    goals = discover_goals(shared)
    for d in (os.path.join(STATE, "goals"), os.path.join(MC_DIR, "goals")):
        os.makedirs(d, exist_ok=True)
    if not goals:  # no agent sessions anywhere: still compute shared resources for the overview
        n0 = (CFG["nodes"].split() or ["node"])[0]
        goals = [{"name": "(no goals)", "node": n0, "key": "_none", "tm": {}, "doc": None, "pseudo": True}]
    snaps = []
    for g in goals:
        snap = analyze_goal(g, shared)
        snaps.append(snap)
    real = [x for x in snaps if not x.get("pseudo")]
    node_rank = {n: i for i, n in enumerate(CFG["nodes"].split())}
    real.sort(key=lambda x: (node_rank.get(x["node_name"], 50 if x["node_name"] not in LOGIN_HOSTS and x["node_name"] != LOCAL_NAME else
                                           (80 if x["node_name"] in LOGIN_HOSTS else 90)), x["node_name"], x["session"].lower()))
    nav = [{"key": x["key"], "name": x["session"], "node": x["node_name"], "sev": goal_sev(x["flags"]),
            "dim": bool(x.get("ended") or x.get("unreachable")), "conv": bool((x.get("conv") or {}).get("page"))} for x in real]
    assign_hues([n["key"] for n in nav])
    for x in real:
        x["nav"] = nav
        wjson(os.path.join(STATE, "goals", x["key"] + ".json"), x)
        with open(os.path.join(MC_DIR, "goals", x["key"] + ".html"), "w", encoding="utf-8") as f:
            f.write(render(x))
        if (x.get("conv") or {}).get("page"):
            try:
                write_conv_page(x, nav)
            except Exception as e:
                sys.stderr.write("conversation page %s: %s\n" % (x["key"], e))
    snaps = real + [x for x in snaps if x.get("pseudo")]
    keep = {x["key"] for x in real}
    keep_conv = {x["key"] for x in real if (x.get("conv") or {}).get("page")}
    try:  # pages and history files of sessions no longer listed (renamed or ended more than a day ago)
        gdir = os.path.join(MC_DIR, "goals")
        for fn in os.listdir(gdir):
            mm = re.match(r"(.+?)(\.conversation)?\.html$", fn)
            if mm and (mm.group(1) not in keep or (mm.group(2) and mm.group(1) not in keep_conv)):
                os.remove(os.path.join(gdir, fn))
        cdir = os.path.join(gdir, "conv")
        for k in (os.listdir(cdir) if os.path.isdir(cdir) else []):
            if k not in keep:
                import shutil
                shutil.rmtree(os.path.join(cdir, k), ignore_errors=True)
    except OSError as e:
        sys.stderr.write("cleanup: %s\n" % e)
    ov = overview(shared, snaps)
    ov["nav"] = nav
    wjson(os.path.join(STATE, "snapshot.json"), ov)
    with open(os.path.join(MC_DIR, "dashboard.html"), "w", encoding="utf-8") as f:
        f.write(render_overview(ov))
    alerts(ov)
    return ov


def overview(shared, snaps):
    real = [x for x in snaps if not x.get("pseudo")]
    base = snaps[0]
    flags, seen = [], set()
    for f in shared["conn_flags"]:
        if f["key"] not in seen:
            seen.add(f["key"])
            flags.append(dict(f))
    for x in snaps:
        for f in x["flags"]:
            if f["area"] == "compute" and f["key"] not in seen:
                seen.add(f["key"])
                flags.append(dict(f))
    for x in real:
        for f in x["flags"]:
            if f["area"] in GOAL_AREAS:
                g = dict(f, key="g:%s:%s" % (x["key"], f["key"]), title="%s: %s" % (x["session"], f["title"]), goal=x["key"])
                flags.append(g)
    flags.sort(key=lambda f: (SEV_ORDER.get(f["sev"], 3), f["area"]))
    cards = []
    for x in real:
        gf = [f for f in x["flags"] if f["area"] in GOAL_AREAS or (f["area"] == "connection" and f.get("node"))]
        crit = [r for r in x.get("criteria", []) if r.get("parsed")]
        top = next((f["title"] for f in sorted(gf, key=lambda f: SEV_ORDER.get(f["sev"], 3)) if f["sev"] in ("red", "amber")), "")
        cards.append({"key": x["key"], "name": x["session"], "node": x["node_name"], "title": x["title"],
                      "status": x["goal"]["status"], "running": x["goal"].get("codex_running"), "tmux_found": x["goal"].get("tmux_found"),
                      "last": x["goal"].get("last_activity"), "plan_done": x["plan"]["done"], "plan_total": len(x["plan"]["steps"]),
                      "sleep_until": x["goal"].get("sleep_until"), "context_left": x["goal"].get("context_left"),
                      "latest_words": (x.get("agent_words") or [{}])[0].get("text"), "latest_epoch": (x.get("agent_words") or [{}])[0].get("epoch"),
                      "latest_update": next((w.get("text") for w in (x.get("agent_words") or []) if len(w.get("text") or "") >= 400), None),
                      "latest_update_epoch": next((w.get("epoch") for w in (x.get("agent_words") or []) if len(w.get("text") or "") >= 400), None),
                      "kind": x["goal"].get("agent_kind"), "notmux": x["goal"].get("notmux"), "model": x["goal"].get("model"),
                      "effort": x["goal"].get("effort"),
                      "fkeys": [f["key"] for f in x["flags"]], "last_seen": x.get("last_seen"), "conv": (x.get("conv") or {}).get("page"),
                      "target": (x.get("tmux") or {}).get("target") or x["session"],
                      "conv_count": (x.get("conv") or {}).get("count") or 0,
                      "g": {k: x["goal"].get(k) for k in ("codex_running", "sleep_until", "paused", "status", "busy", "last_activity")},
                      "plan_now": x["plan"]["now"], "crit_met": sum(1 for r in crit if r["status"] == "met"), "crit_total": len(crit),
                      "crit_noev": sum(1 for r in crit if r["status"] == "no evidence"),
                      "reds": sum(1 for f in gf if f["sev"] == "red"), "ambers": sum(1 for f in gf if f["sev"] == "amber"),
                      "top": top, "unreachable": x.get("unreachable"), "ended": x.get("ended"), "sev": worst_sev(gf)})
    nodes = {n: (nd["doc"] or {}).get("node") for n, nd in shared["nodes"].items() if (nd["doc"] or {}).get("ok")}
    others = []
    for src, doc in [(n, nd["doc"]) for n, nd in list(shared["nodes"].items()) + list(shared.get("logins", {}).items())] + [(LOCAL_NAME, shared["local"])]:
        for x in (doc or {}).get("sessions", []) if doc and doc.get("ok") else []:
            if x.get("role") == "goal":
                continue
            le = max(iso_epoch(x.get("last_ts")) or 0, x.get("mtime") or 0)
            others.append({"host": src, "kind": x.get("kind"), "cwd": (x.get("meta") or {}).get("cwd") or (x.get("meta") or {}).get("ctx_cwd"),
                           "last": le or None, "calls": x.get("calls"), "role": x.get("role")})
    others.sort(key=lambda o: -(o["last"] or 0))
    return {"generated": NOW, "collected": shared["collected"], "goals": cards, "flags": flags, "hosts": shared["hosts"],
            "resources": base["resources"], "compute": base["compute"], "history": base.get("history") or [], "nodes": nodes,
            "others": others[:30], "instructions": [dict(u, goal=x["key"]) for x in real for u in x.get("instructions", [])]}


def session_card(g):
    gray = g["unreachable"] or g["ended"]
    stxt, scls = session_state(g.get("g") or {}, g.get("fkeys"), g["unreachable"], g["ended"], g["node"], g.get("last_seen"))
    sev = "gray" if gray else {"red": "red", "amber": "amber"}.get(g["sev"], "")
    o = ['<a class="scard %s" style="%s" href="goals/%s.html"%s><div class="sh"><span class="dot %s"></span><span class="sn" title="%s">%s</span>%s</div>' % (
        sev, hue(g["key"]), esc(g["key"]), sec("o", "s:" + g["key"], "Session " + g["name"]), sev, esc(g["name"]), esc(g["name"]), kind_badge(g.get("kind")))]
    o.append('<div class="st %s">%s</div>' % (scls, esc(stxt)))
    if g.get("title") and g["title"] != g["name"]:
        o.append('<div class="obj" title="%s">%s</div>' % (esc(trunc(g["title"], 600)), esc(trunc(g["title"], 160))))
    w, we = g.get("latest_words"), g.get("latest_epoch")
    if w and len(w) < 200 and g.get("latest_update"):
        w, we = g["latest_update"], g.get("latest_update_epoch") or we
    if w:
        o.append('<div class="words"><div class="wt"><span class="mut">%s \u00b7</span> %s</div></div>' % (esc(ago(we)), esc(trunc(re.sub(r"[*`#]", "", w).replace("\n", " "), 420))))
    meta = []
    if g.get("last") and not gray:
        meta.append("Active <b>%s</b>" % esc(ago(g["last"])))
    if g.get("context_left") is not None:
        meta.append('Context <b class="%s">%d%%</b> free' % ("bad-t" if g["context_left"] <= 15 else "", g["context_left"]))
    if g["plan_total"]:
        meta.append("Plan <b>%d/%d</b>" % (g["plan_done"], g["plan_total"]))
    if g["crit_total"]:
        if g.get("crit_noev") == g["crit_total"]:
            meta.append("Criteria <b>no test yet</b>")
        else:
            meta.append('Criteria <b class="%s">%d/%d met</b>' % ("done" if g["crit_met"] == g["crit_total"] else "bad-t", g["crit_met"], g["crit_total"]))
    if g.get("conv_count"):
        meta.append("<b>%s</b> messages" % "{:,}".format(g["conv_count"]))
    if g.get("model"):
        meta.append('<span class="mdl" style="font-size:12px">%s</span>' % esc(" \u00b7 ".join(x for x in (g["model"], g.get("effort")) if x)))
    if g["reds"] or g["ambers"]:
        meta.append("%s%s" % (('<span class="pill bad">%d red</span> ' % g["reds"]) if g["reds"] else "",
                              ('<span class="pill warn">%d amber</span>' % g["ambers"]) if g["ambers"] else ""))
    o.append('<div class="meta">%s</div>' % "".join("<span>%s</span>" % m for m in meta))
    if g.get("top") and not gray:
        o.append('<div class="small %s" style="margin-top:6px">%s</div>' % ("bad-t" if g["reds"] else "mut", esc(trunc(g["top"], 120))))
    o.append("</a>")
    return "".join(o)


def render_overview(ov):
    reds = [f for f in ov["flags"] if f["sev"] == "red"]
    goals = ov["goals"]
    o = ['<!doctype html><html lang="en"><head>%s<title>%smission control</title><style>%s</style>%s</head><body class="ov">' % (
             HEAD_META, ("(%d) " % len(reds)) if reds else "", CSS, hide_css())]
    o.append(appbar(ov.get("nav") or [], None, "root", False, ov.get("collected")))
    o.append("<main>")
    needs = [f for f in ov["flags"] if f.get("needs_you")]
    hosts_with = []
    for g in goals:
        if g["node"] not in hosts_with:
            hosts_with.append(g["node"])
    live = [g for g in goals if not g["unreachable"] and not g["ended"]]
    chips = "".join('<span class="hc" title="%s"><span class="dot %s"></span>%s</span>' % (
        esc(h.get("detail") or ""), "" if h["ok"] else "red", esc(h["name"])) for h in ov["hosts"])
    o.append('<header><div><h1>Mission control</h1><p class="sub">%d agent session%s running on %s%s</p></div><div class="chips">%s</div></header>' % (
        len(live), "" if len(live) == 1 else "s", esc(", ".join(LOGIN_HOSTS.get(h, h) for h in hosts_with)) or "no hosts",
        (" \u00b7 <b class='bad-t'>%d need%s you</b>" % (len(needs), "s" if len(needs) == 1 else "")) if needs else "", chips))
    o.append('<div id="hidden-bar" class="hbar" style="display:none"></div>')
    if needs:
        o.append('<section class="card"%s><h2>Needs you</h2>' % sec("o", "needs"))
        for i, f in enumerate(needs[:20]):
            if i == 4:
                o.append("<details style='border:none;padding:0;margin:4px 0 0;box-shadow:none'><summary class='small'>%d more</summary>" % (len(needs[:20]) - 4))
            h = flag_html(f)
            if f.get("goal"):
                h = '<a class="gc" href="goals/%s.html">%s</a>' % (esc(f["goal"]), h)
            o.append(h)
        if len(needs[:20]) > 4:
            o.append("</details>")
        o.append("</section>")
    else:
        o.append('<div class="quiet"><span class="dot"></span>Nothing needs you right now.</div>')
    if not goals:
        o.append('<div class="card"><div class="empty">No Codex or Claude session is running on the hosts mission control can see.</div></div>')
    for h in hosts_with:
        hg = [g for g in goals if g["node"] == h]
        nd = (ov.get("nodes") or {}).get(h) or {}
        extra = ""
        if nd.get("ncpu"):
            extra = " \u00b7 load %.0f of %d cores" % ((nd.get("load") or [0])[0], nd["ncpu"])
        o.append('<div%s><div class="hosth"><b>%s</b>%d session%s%s</div><div class="sgrid">' % (
            sec("o", "host:" + h, "All sessions on " + LOGIN_HOSTS.get(h, h)), esc(LOGIN_HOSTS.get(h, h)), len(hg), "" if len(hg) == 1 else "s", esc(extra)))
        o.extend(session_card(g) for g in hg)
        o.append("</div></div>")
    o.append('<div style="height:16px"></div><section class="card"%s>%s</section>' % (sec("o", "resources"), resources_html(ov["resources"], ov["nodes"], ov["compute"], ov["history"])))
    comp = [f for f in ov["flags"] if f["area"] in ("compute", "connection") and not f.get("needs_you")]
    if comp:
        o.append('<section class="card"%s><h2>Watchdog</h2>%s</section>' % (sec("o", "watchdog"), "".join(flag_html(f) for f in comp)))
    if ov["others"]:
        o.append("<details" + sec("o", "others") + "><summary>Other agent logs seen recently (not running now): %d</summary><table><tr><th>Host</th><th>Kind</th><th>Last active</th><th>Calls</th><th>Folder</th></tr>" % len(ov["others"]))
        for x in ov["others"]:
            o.append("<tr><td>%s</td><td>%s</td><td class=mut>%s</td><td>%s</td><td class='mut small mono'>%s</td></tr>" % (
                esc(x["host"]), esc(x["kind"]), esc(ago(x["last"])), esc(x["calls"]), esc(trunc(x["cwd"], 60))))
        o.append("</table></details>")
    hs = " \u00b7 ".join('%s <span style="color:var(--%s)">%s</span>' % (esc(h["name"]), "grn" if h["ok"] else "red", "ok" if h["ok"] else "down") for h in ov["hosts"])
    o.append('<footer>Collectors: %s<br>Collected %s \u00b7 new data every %d min \u00b7 every running Codex or Claude session is found automatically '
             '(tmux or not) on the watched nodes, the Rockfish and Skipjack login nodes and this PC<br>'
             'Mission control types into a session only when you press Send on its page%s.<br>'
             'Hover over any block and click its \u00d7 to hide it in this browser; <span class="mono">mctl hide NAME</span> hides it on every machine.%s</footer></main>%s</body></html>' % (
                 hs, esc(time.strftime("%Y-%m-%d %H:%M", time.localtime(ov["collected"]))) if ov.get("collected") else "never", CFG["interval"], never_note(), hidden_note(), HIDE_JS + REFRESH_JS))
    return "\n".join(o)


# ---------------------------------------------------------------- alerts
def push(title, body, priority="default", tags=""):
    if not CFG["ntfy_topic"]:
        return False
    try:
        req = urllib.request.Request("%s/%s" % (CFG["ntfy_server"], CFG["ntfy_topic"]), data=body.encode("utf-8"),
                                     headers={"Title": title.encode("ascii", "replace").decode(), "Priority": priority, "Tags": tags})
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:
        sys.stderr.write("ntfy push failed: %s\n" % e)
        return False


def alerts(ov):
    path = os.path.join(STATE, "alerts.json")
    st = rjson(path, {}) or {}
    seen = st.get("seen", {})
    current = {f["key"]: f for f in ov["flags"] if f["sev"] in ("red", "amber")}
    for k in list(seen):
        if k not in current:
            del seen[k]
    new = [f for k, f in current.items() if k not in seen]
    for f in new:
        seen[f["key"]] = NOW
    if new and CFG["ntfy_topic"]:
        reds = [f for f in new if f["sev"] == "red"]
        lines = ["%s %s" % ("\u2757" if f["sev"] == "red" else "\u26a0", f["title"]) for f in new[:6]]
        if len(new) > 6:
            lines.append("+%d more" % (len(new) - 6))
        push("Mission control: %d new" % len(new), "\n".join(lines), "high" if reds else "default", "rotating_light" if reds else "warning")
    today = time.strftime("%Y-%m-%d")
    if CFG["ntfy_topic"] and time.localtime().tm_hour >= CFG["digest_hour"] and st.get("digest") != today:
        lines = []
        for g in ov["goals"]:
            lines.append("%s: %s, last activity %s, plan %d/%d%s" % (
                g["name"], "node unreachable" if g["unreachable"] else (g["status"] or "?"), ago(g["last"]), g["plan_done"], g["plan_total"],
                (", criteria %d/%d" % (g["crit_met"], g["crit_total"])) if g["crit_total"] else ""))
        c = ov["compute"]
        lines.append("Jobs: %d running, %d pending" % (sum(r["running"] for r in ov["resources"]), sum(r["pending"] for r in ov["resources"])))
        lines.append("Compute 48h: %.0f core-h, %s idle/failed" % (c["alloc_ch"], ("%d%%" % c["waste_pct"]) if c["waste_pct"] is not None else "n/a"))
        lines.append("Flags: %d red, %d amber" % (sum(1 for f in ov["flags"] if f["sev"] == "red"), sum(1 for f in ov["flags"] if f["sev"] == "amber")))
        if push("Morning brief: %d goal(s)" % len(ov["goals"]), "\n".join(lines), "default", "sunrise"):
            st["digest"] = today
    st["seen"] = seen
    wjson(path, st)


# ---------------------------------------------------------------- rendering
CSS = """
:root{--bg:#f1f3fd;--card:#fff;--fg:#171a33;--mut:#5a618b;--faint:#8a90b4;--line:rgba(40,52,130,.13);--bar:#e2e6f8;--shadow:0 1px 2px rgba(30,40,110,.06);
--violet:#6c4de0;--sky:#0f78cf;--teal:#08917e;--lime:#4a870f;--amber:#ad6500;--orange:#d0520b;--rose:#d02c5c;--pink:#b8338f;--gold:#9c7200;--slate:#6670a6;
--red:var(--rose);--redbg:#fde9ef;--amb:var(--amber);--ambbg:#fff1db;--grn:var(--lime);--grnbg:#e9f5dc;--blu:var(--sky);--blubg:#e2f0fd;--pur:var(--violet);--purbg:#eeeafe;--tealbg:#dcf5f0;
--c-agent:var(--violet);--t-agent:#e8e0ff;--c-you:#2563eb;--t-you:#eaf1fe;--c-sub:var(--teal);--t-sub:#d3f2ea;--c-task:#2f9c8c;--t-task:#e3f5f1;
--c-event:var(--amber);--t-event:#fff3df;--c-review:var(--slate);--t-review:#f0f1f8;
--chatbg:#e7eaf7;--hu0:#7048e8;--hu1:#1c7ed6;--hu2:#0a8fa9;--hu3:#099268;--hu4:#5c940d;--hu5:#a87700;--hu6:#e8590c;--hu7:#d6336c;--h:var(--violet);--s:var(--slate)}
@media (prefers-color-scheme:dark){:root{--bg:#0b1130;--card:#131a3d;--fg:#edf0ff;--mut:#a1aad5;--faint:#6b75a6;--line:rgba(140,160,255,.15);--bar:#222b59;--shadow:none;
--violet:#b39dff;--sky:#62bfff;--teal:#2fdcc0;--lime:#a6e35c;--amber:#ffc25a;--orange:#ff9a5c;--rose:#ff6f92;--pink:#f78ad6;--gold:#ffd54a;--slate:#94a0d4;
--redbg:#3b1733;--ambbg:#33291d;--grnbg:#1a2f2b;--blubg:#142a5c;--purbg:#271f5c;--tealbg:#0f3141;
--c-you:#2d6ae0;--t-agent:#251657;--t-you:#14275b;--t-sub:#0a3b3b;--c-task:#35b09f;--t-task:#0f3140;--t-event:#2e2a32;--t-review:#1a2147;
--chatbg:#060a1d;--hu0:#b197fc;--hu1:#74b0ff;--hu2:#3bd9f0;--hu3:#38d9a9;--hu4:#a9e34b;--hu5:#ffd43b;--hu6:#ff9f5a;--hu7:#f783c4}}
*{box-sizing:border-box}html{scroll-padding-top:110px}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 "Segoe UI",-apple-system,Roboto,Helvetica,Arial,sans-serif}
a{color:var(--blu)}main{max-width:1120px;margin:0 auto;padding:18px 18px 48px}h1{font-size:22px;font-weight:650;margin:0;word-break:break-word}h2{font-size:15px;font-weight:650;margin:0 0 10px}
.sub{color:var(--mut);font-size:13px;margin:3px 0 0}header{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;margin:6px 0 16px}
.appbar{position:sticky;top:0;z-index:20;background:var(--card);border-bottom:1px solid var(--line);box-shadow:var(--shadow)}
.appbar .in{max-width:1120px;margin:0 auto;padding:0 18px;height:48px;display:flex;align-items:center;gap:14px}
.brand{font-weight:700;font-size:14px;color:var(--fg);text-decoration:none;white-space:nowrap;display:flex;align-items:center;gap:7px}.brand span{display:inline}.brand i{width:10px;height:10px;border-radius:3px;background:var(--pur);display:inline-block;transform:rotate(45deg)}
.sw{flex:1;min-width:0;display:flex;align-items:center;gap:6px;overflow-x:auto;white-space:nowrap;scrollbar-width:thin;padding:6px 0}
.hg{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--faint);margin:0 2px 0 8px}.hg:first-child{margin-left:0}
.np{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;padding:4px 10px;border-radius:99px;text-decoration:none;color:var(--fg);background:var(--bg);border:1px solid var(--line)}
.np:hover{border-color:var(--blu)}.np.cur{background:var(--blubg);border-color:var(--blu);color:var(--blu);font-weight:600}.np.dim{opacity:.6}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex:none;background:var(--grn)}.dot.amber{background:var(--amb)}.dot.red{background:var(--red)}.dot.gray{background:var(--faint)}
.dot.pulse{box-shadow:0 0 0 0 currentColor;animation:pl 2s infinite}@keyframes pl{0%{box-shadow:0 0 0 0 rgba(120,180,90,.5)}70%{box-shadow:0 0 0 6px rgba(120,180,90,0)}100%{box-shadow:0 0 0 0 rgba(120,180,90,0)}}
.pill{display:inline-block;font-size:12px;padding:3px 10px;border-radius:99px;white-space:nowrap}.ok{background:var(--grnbg);color:var(--grn)}.warn{background:var(--ambbg);color:var(--amb)}.bad{background:var(--redbg);color:var(--red)}.neu{background:var(--bar);color:var(--mut)}.you{background:var(--blubg);color:var(--blu)}.dec{background:var(--purbg);color:var(--pur)}.tl2{background:var(--tealbg);color:var(--teal)}
.chips{display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-end}.hc{display:inline-flex;align-items:center;gap:6px;font-size:12px;padding:3px 10px;border-radius:99px;background:var(--card);border:1px solid var(--line);white-space:nowrap}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:14px}.m{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;box-shadow:var(--shadow)}
.m .l{font-size:12px;color:var(--mut)}.m .v{font-size:21px;font-weight:650;margin:2px 0;line-height:1.25}.m .s{font-size:12px;color:var(--mut);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:14px;box-shadow:var(--shadow)}.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}.grid2 .card{margin-bottom:0}
.flag{border-radius:10px;padding:10px 12px;margin:8px 0;border-left:4px solid transparent}.flag.red{background:var(--redbg);color:var(--red);border-left-color:var(--red)}.flag.amber{background:var(--ambbg);color:var(--amb);border-left-color:var(--amb)}.flag.info{background:var(--bar);color:var(--fg);border-left-color:var(--faint)}
.flag .t{font-weight:600;font-size:14px}.flag .d{font-size:13px;margin-top:2px;word-break:break-word}.flag .a{font-size:12px;margin-top:4px;opacity:.9}.flag code{font-size:12px}
.quiet{display:flex;align-items:center;gap:8px;background:var(--grnbg);color:var(--grn);border-radius:12px;padding:10px 14px;margin-bottom:14px;font-size:14px}
.row{display:flex;align-items:center;gap:10px;font-size:13px;margin:7px 0}.row .n{width:92px;flex:none}.row .x{flex:1;min-width:0}.row .r{flex:none;color:var(--mut);text-align:right}
.bar{height:7px;background:var(--bar);border-radius:4px;overflow:hidden}.bar i{display:block;height:7px;border-radius:4px;background:var(--grn)}.bar i.w{background:var(--amb)}.bar i.b{background:var(--red)}
table{width:100%;border-collapse:collapse;font-size:13px}th{text-align:left;font-weight:600;color:var(--mut);border-bottom:1px solid var(--line);padding:5px 6px}td{border-bottom:1px solid var(--line);padding:5px 6px;vertical-align:top}
.tl{font-size:13px}.tl .e{display:flex;gap:10px;padding:6px 0;border-bottom:1px solid var(--line)}.tl .tm{width:84px;flex:none;color:var(--mut)}.tl .w{width:74px;flex:none}.tl .tx{flex:1;min-width:0;word-break:break-word}
.mono,pre,code{font-family:"Cascadia Mono",Consolas,ui-monospace,monospace}pre{font-size:12px;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;overflow:auto;max-height:520px;white-space:pre-wrap;word-break:break-word}
details{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 16px;margin-bottom:14px;box-shadow:var(--shadow)}summary{cursor:pointer;font-weight:600;font-size:14px}
.step{display:flex;gap:8px;font-size:13px;padding:4px 0}.step .i{width:18px;flex:none;text-align:center}.done{color:var(--grn)}.prog{color:var(--blu);font-weight:600}.pend{color:var(--mut)}
.mut{color:var(--mut)}.small{font-size:12px}.bad-t{color:var(--red);font-weight:600}.empty{color:var(--mut);font-size:13px;padding:6px 0}
footer{color:var(--faint);font-size:12px;margin-top:18px;line-height:1.7}svg.sp{vertical-align:middle}
.mb{word-break:break-word}.mb p{margin:6px 0}.mdh{font-weight:650;margin:10px 0 4px}.mdli{margin:2px 0 2px 10px}.mb code{font-size:12.5px;background:var(--card);border:1px solid var(--line);padding:0 4px;border-radius:4px}.mb pre{background:var(--card);max-height:none}
.mb table.mdt{display:block;width:fit-content;max-width:100%;overflow-x:auto;margin:8px 0;font-size:13px;border:1px solid var(--line);border-radius:8px;border-collapse:separate;border-spacing:0;background:var(--card)}
.mb table.mdt th{background:var(--bg);color:var(--fg)}.mb table.mdt th,.mb table.mdt td{padding:5px 12px;border-bottom:1px solid var(--line);white-space:nowrap}.mb table.mdt tr:last-child td{border-bottom:none}
.chat{max-width:920px;margin:0 auto}.row{display:flex;align-items:flex-end;gap:8px;margin:14px 0}.row.cont{margin-top:4px}.row.r-user{justify-content:flex-end}
.av{--c:var(--c-agent);flex:none;width:30px;height:30px;border-radius:50%;background:var(--c);color:#fff;font-size:11.5px;font-weight:700;display:flex;align-items:center;justify-content:center}
.row.cont .av{visibility:hidden}
.bub{--c:var(--c-agent);--t:var(--t-agent);min-width:0;max-width:min(82%,780px);background:var(--t);border:1px solid var(--line);border-radius:20px 20px 20px 6px;padding:9px 15px 10px;box-shadow:var(--shadow)}
.row.cont .bub{border-top-left-radius:8px}.bub .mh{display:flex;gap:8px;align-items:baseline;font-size:12px;margin-bottom:3px}.bub .who{color:var(--c);font-weight:650}.bub .tm{color:var(--mut)}
.bub .mb>p:first-child{margin-top:0}.bub .mb>p:last-child{margin-bottom:0}
.r-sub .av,.r-sub .bub{--c:var(--c-sub);--t:var(--t-sub)}.r-task .av,.r-task .bub{--c:var(--c-task);--t:var(--t-task)}.r-review .av,.r-review .bub{--c:var(--c-review);--t:var(--t-review)}.r-review .bub{font-size:13px}
.r-user .bub{background:var(--c-you);color:#fff;border-color:transparent;border-radius:20px 20px 6px 20px;max-width:min(76%,680px)}.r-user.cont .bub{border-top-right-radius:8px}
.r-user .bub .who,.r-user .bub .tm{color:rgba(255,255,255,.82)}.r-user .mb code{background:rgba(255,255,255,.16);border-color:transparent;color:#fff}
.r-user .mb pre{background:rgba(0,0,0,.24);border-color:transparent;color:#fff}.r-user .more{color:#fff;border-color:rgba(255,255,255,.55)}
.row.pending .bub{opacity:.8}.row.pending .tm:after{content:" · sent"}
.evt{display:flex;align-items:center;gap:10px;margin:16px 0;font-size:12px}.evt:before,.evt:after{content:"";flex:1;height:1px;background:var(--line)}
.evt span{padding:3px 12px;border-radius:99px;background:var(--t-event);color:var(--amb)}
.day{display:flex;justify-content:center;margin:24px 0 8px}.day span{font-size:11px;font-weight:650;color:var(--mut);background:var(--card);border:1px solid var(--line);border-radius:99px;padding:3px 12px;text-transform:uppercase;letter-spacing:.05em}
.math{white-space:nowrap}.mdl{color:var(--faint);font-size:11px;letter-spacing:.01em}.bub .mh .mdl{margin-left:auto;padding-left:10px}.r-user .bub .mdl{color:rgba(255,255,255,.6)}.math sup,.math sub{font-size:.75em}
.sendbox textarea{border-radius:14px}details.scr{border:none;box-shadow:none;padding:4px 0;margin:0;background:none}details.scr summary{font-weight:500;color:var(--mut)}
@media (max-width:640px){main{padding:12px 10px 40px}h1{font-size:19px}.brand span{display:none}.bub{max-width:88%;padding:8px 12px}.r-user .bub{max-width:86%}
.av{width:24px;height:24px;font-size:10px}.row{gap:6px}.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.sgrid{grid-template-columns:1fr}.grid2{grid-template-columns:1fr}
header{flex-direction:column}.chips{justify-content:flex-start}.ctl{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none}.ctl::-webkit-scrollbar{display:none}
.ctl input[type=search]{min-width:0;flex:0 0 150px}.ctl .chip,.ctl select,.ctl button{flex:none}.card,details{padding:12px}.card table{display:block;overflow-x:auto}}
.msg{--c:var(--c-agent);--t:var(--t-agent);background:var(--t);border:1px solid var(--line);border-left:4px solid var(--c);border-radius:10px;padding:10px 14px;margin:10px 0}
.m-agent{--c:var(--c-agent);--t:var(--t-agent)}.m-user{--c:var(--c-you);--t:var(--t-you);margin-left:48px}.m-sub{--c:var(--c-sub);--t:var(--t-sub)}.m-task{--c:var(--c-task);--t:var(--t-task);margin-left:24px}
.m-event{--c:var(--c-event);--t:var(--t-event);padding:6px 12px;font-size:13px}.m-review{--c:var(--c-review);--t:var(--t-review);font-size:13px}
.mh{display:flex;align-items:baseline;gap:8px;margin-bottom:2px;font-size:12.5px}.who{color:var(--c);font-weight:650}.mh .tm{color:var(--mut)}
.clip{max-height:340px;overflow:hidden;-webkit-mask-image:linear-gradient(#000 75%,transparent);mask-image:linear-gradient(#000 75%,transparent)}
.card .mb.clip{max-height:620px}.more{margin-top:6px;background:none;border:1px solid var(--line);color:var(--c);border-radius:99px;padding:2px 12px;font-size:12px;cursor:pointer}
.scard{display:block;text-decoration:none;color:inherit;background:var(--card);border:1px solid var(--line);border-left:5px solid var(--grn);border-radius:12px;padding:12px 14px;box-shadow:var(--shadow);transition:border-color .15s,transform .15s}
.scard:hover{border-color:var(--blu);transform:translateY(-1px)}.scard.amber{border-left-color:var(--amb)}.scard.red{border-left-color:var(--red)}.scard.gray{border-left-color:var(--faint)}
.scard .sh{display:flex;align-items:center;gap:8px}.scard .sn{font-weight:650;font-size:15px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.kind{font-size:11px;font-weight:600;padding:2px 8px;border-radius:99px;background:var(--bar);color:var(--mut)}.kind.codex{background:var(--purbg);color:var(--pur)}.kind.claude{background:var(--ambbg);color:var(--amb)}
.scard .st{font-size:13px;margin:4px 0 2px;font-weight:600}.st.ok{background:none;color:var(--grn)}.st.warn{background:none;color:var(--amb)}.st.bad{background:none;color:var(--red)}.st.neu{background:none;color:var(--mut)}
.scard .obj{font-size:12.5px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.scard .words{font-size:13px;margin:8px 0;padding:8px 10px;background:var(--t-agent);border-left:3px solid var(--c-agent);border-radius:8px}.scard .words .wt{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.scard .meta{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;color:var(--mut)}.scard .meta b{color:var(--fg);font-weight:600}
.hosth{display:flex;align-items:baseline;gap:8px;margin:18px 0 8px;font-size:13px;color:var(--mut)}.hosth b{color:var(--fg);font-size:14px}
.sgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px}
.objective{font-size:13px;color:var(--mut);margin:6px 0 0;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;max-width:820px;cursor:pointer}
.objective.open{display:block;-webkit-line-clamp:unset;white-space:pre-wrap;overflow:auto;max-height:60vh;color:var(--fg)}
.xp{cursor:pointer}.xp .xf{display:none}.xp.open .xs{display:none}.xp.open .xf{display:inline;word-break:break-all}
a.gc{display:block;text-decoration:none;color:inherit}a.gc:hover .flag{filter:brightness(.98)}
.sendbox{margin-top:12px;padding-top:12px;border-top:1px solid var(--line)}.sendbox textarea{width:100%;min-height:48px;resize:vertical;padding:9px 12px;border-radius:10px;
border:1px solid var(--line);background:var(--t-you);color:var(--fg);font:inherit;font-size:14px;border-left:4px solid var(--c-you)}.sendbox textarea:focus{outline:2px solid var(--c-you);outline-offset:1px}
.sendbox textarea:disabled{opacity:.6;background:var(--bg)}.sendrow{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-top:6px}
.sendbtn{padding:6px 18px;border-radius:99px;border:none;background:var(--c-you);color:#fff;font-weight:600;cursor:pointer}.sendbtn:disabled{opacity:.45;cursor:default}
.sentok{color:var(--grn);margin-top:8px}.sendscr{max-height:260px;margin-top:6px}
/* colour: each session has its own hue (--h), each block its own (--s) */
body{background:radial-gradient(1000px 360px at 50% -150px,color-mix(in srgb,var(--h) 26%,transparent),transparent 72%) no-repeat,var(--bg)}
body.ov{background:radial-gradient(700px 320px at 15% -120px,color-mix(in srgb,var(--violet) 24%,transparent),transparent 72%) no-repeat,
radial-gradient(700px 320px at 85% -120px,color-mix(in srgb,var(--teal) 18%,transparent),transparent 72%) no-repeat,var(--bg)}
.appbar{background:color-mix(in srgb,var(--card) 86%,transparent);-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);border-bottom-color:color-mix(in srgb,var(--h) 30%,var(--line))}
.brand i{width:12px;height:12px;border-radius:3px;background:conic-gradient(var(--hu0),var(--hu1),var(--hu2),var(--hu3),var(--hu4),var(--hu5),var(--hu6),var(--hu7),var(--hu0))}
.np{--h:var(--slate);background:color-mix(in srgb,var(--h) 10%,transparent);border-color:color-mix(in srgb,var(--h) 42%,transparent)}
.np:hover{border-color:var(--h)}.np.cur{background:color-mix(in srgb,var(--h) 28%,transparent);border-color:var(--h);color:var(--fg)}
body[style] h1{color:color-mix(in srgb,var(--h) 72%,var(--fg))}
.card[data-sec],details[data-sec]{background:color-mix(in srgb,var(--s) 6%,var(--card));border-color:color-mix(in srgb,var(--s) 30%,transparent)}
.card[data-sec] h2:first-of-type,details[data-sec]>summary{color:color-mix(in srgb,var(--s) 62%,var(--fg))}
.card[data-sec] h2:first-of-type:before,details[data-sec]>summary:after{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--s);
box-shadow:0 0 0 3px color-mix(in srgb,var(--s) 22%,transparent);margin:0 9px 1px 1px;vertical-align:middle}
details[data-sec]>summary:after{margin:0 0 1px 9px}
[data-sec$=":latest"],[data-sec$=":sessions"]{--s:var(--violet)}[data-sec$=":needs"]{--s:var(--rose)}.calm[data-sec$=":needs"]{--s:var(--lime)}
[data-sec$=":criteria"],[data-sec$=":referee"]{--s:var(--pink)}[data-sec$=":plan"],[data-sec$=":instructions"]{--s:var(--sky)}
[data-sec$=":resources"],[data-sec$=":slurm"]{--s:var(--teal)}[data-sec$=":integrity"],[data-sec$=":contract"]{--s:var(--gold)}
[data-sec$=":watchdog"]{--s:var(--orange)}[data-sec$=":timeline"]{--s:var(--lime)}[data-sec$=":screen"],[data-sec$=":others"]{--s:var(--slate)}
.m{--s:var(--slate);background:color-mix(in srgb,var(--s) 11%,var(--card));border-color:color-mix(in srgb,var(--s) 34%,transparent);box-shadow:inset 0 3px 0 var(--s)}
.m .l{color:color-mix(in srgb,var(--s) 70%,var(--fg));font-weight:600}
.flag{--fc:var(--slate);background:color-mix(in srgb,var(--fc) 9%,var(--card));border:1px solid color-mix(in srgb,var(--fc) 24%,transparent);border-left:3px solid var(--fc);color:var(--fg)}
.flag.red{--fc:var(--rose)}.flag.amber{--fc:var(--amber)}.flag.info{--fc:var(--slate)}
.flag.red,.flag.amber,.flag.info{color:var(--fg);background:color-mix(in srgb,var(--fc) 8%,var(--card));border-left-color:var(--fc)}
.flag.red{background:color-mix(in srgb,var(--fc) 14%,var(--card))}
.flag .t{color:color-mix(in srgb,var(--fc) 82%,var(--fg))}.flag .d{opacity:.92}.flag .a{color:var(--mut);opacity:1}
.quiet{background:color-mix(in srgb,var(--lime) 13%,var(--card));border:1px solid color-mix(in srgb,var(--lime) 32%,transparent)}
.bar{height:8px;border-radius:99px}.bar i{height:8px;border-radius:99px;background:linear-gradient(90deg,var(--teal),var(--lime) 50%,var(--amber) 78%,var(--orange));background-size:var(--bs,100%) 100%}
.bar i.eff{background:var(--lime)}.bar i.w{background:var(--amber)}.bar i.b{background:var(--rose)}
.scard{--h:var(--slate);background:color-mix(in srgb,var(--h) 9%,var(--card));border:1px solid color-mix(in srgb,var(--h) 34%,transparent);border-top:3px solid var(--h)}
.scard:hover{border-color:var(--h);border-top-color:var(--h)}.scard.amber,.scard.red,.scard.gray{border-left-color:color-mix(in srgb,var(--h) 34%,transparent)}
.scard.red{box-shadow:0 0 0 2px color-mix(in srgb,var(--rose) 60%,transparent)}.scard.gray{filter:saturate(.45);opacity:.85}
.scard .sn{color:color-mix(in srgb,var(--h) 72%,var(--fg))}
.hc{background:color-mix(in srgb,var(--card) 80%,transparent)}
.kind.claude{background:color-mix(in srgb,var(--orange) 16%,transparent);color:var(--orange)}
button.fresh{display:inline-flex;align-items:center;gap:7px;flex:none;font:inherit;font-size:12px;border:1px solid transparent;cursor:pointer}
button.fresh svg{opacity:.7;flex:none}button.fresh:hover{border-color:currentColor}button.fresh:hover svg{opacity:1}
button.fresh:focus-visible{outline:2px solid currentColor;outline-offset:2px}
.fresh.spin svg{opacity:1;animation:mcspin .9s linear infinite}@keyframes mcspin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.fresh.spin svg{animation-duration:2.5s}}
/* every session tab stays visible: the bar wraps onto a second row when they don't fit (phones scroll instead) */
.appbar .in{height:auto;min-height:48px}.sw{flex-wrap:wrap;overflow-x:visible;row-gap:6px}
@media (max-width:640px){.appbar .in{flex-wrap:wrap;column-gap:10px;padding-top:6px}.appbar .brand span{display:inline}
.appbar button.fresh{margin-left:auto}.sw{order:3;flex:1 1 100%;flex-wrap:nowrap;overflow-x:auto;padding:4px 0 8px}}
body.convpage{--bg:var(--chatbg)}.card[data-sec$=":latest"]{background:var(--chatbg)}
.bub .mb,.msg .mb{line-height:1.6}.bub{border-color:color-mix(in srgb,var(--c) 38%,transparent)}.r-user .bub{border-color:transparent}.bub .who{color:color-mix(in srgb,var(--c) 78%,var(--fg))}
.av{background:color-mix(in srgb,var(--c) 26%,var(--card));color:color-mix(in srgb,var(--c) 70%,var(--fg));box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--c) 45%,transparent)}
.mb code{font-family:Consolas,"Cascadia Mono",ui-monospace,monospace;font-size:.9em;font-weight:400;padding:1px 5px;border:none;border-radius:5px;
background:color-mix(in srgb,var(--fg) 7%,transparent);color:color-mix(in srgb,var(--fg) 80%,var(--c,var(--mut)))}
.mb b{font-weight:600}
@media (prefers-color-scheme:dark){.bub .mb,.msg .mb{color:color-mix(in srgb,var(--fg) 88%,var(--bg))}.bub .mb b{color:var(--fg)}}
""" + HIDE_CSS


def spark(values, w=120, h=26, color="var(--blu)"):
    vals = [v for v in values if isinstance(v, (int, float))]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    pts = []
    for i, v in enumerate(vals):
        x = i * (w - 2) / (len(vals) - 1) + 1
        y = h - 2 - (v - lo) * (h - 4) / rng
        pts.append("%.1f,%.1f" % (x, y))
    return ('<svg class="sp" width="%d" height="%d" viewBox="0 0 %d %d" aria-hidden="true"><polyline fill="none" stroke="%s" '
            'stroke-width="1.5" points="%s"/></svg>' % (w, h, w, h, color, " ".join(pts)))


def bar(frac, cls=""):
    frac = max(0.0, min(1.0, frac or 0))
    return '<div class="bar"><i class="%s" style="width:%.0f%%;--bs:%.0f%%"></i></div>' % (cls, frac * 100, 100 / max(frac, .01))


def flag_html(f):
    return ('<div class="flag %s"><div class="t">%s</div>%s%s</div>' % (
        f["sev"], esc(f["title"]), ('<div class="d">%s</div>' % esc(f["detail"])) if f["detail"] else "",
        ('<div class="a">%s</div>' % esc(f["action"])) if f["action"] else ""))


KIND_PILL = {"submit": ("submit", "ok"), "cancel": ("cancel", "warn"), "alloc": ("srun", "ok"), "delete": ("delete", "bad"),
             "edit": ("edit", "neu"), "git": ("git", "neu"), "agent": ("agent", "dec"), "goal": ("goal", "dec"),
             "ssh": ("ssh", "neu"), "plan": ("plan", "dec"), "error": ("error", "bad"), "event": ("event", "warn"),
             "you": ("you", "you"), "decision": ("decision", "dec"), "other": ("cmd", "neu")}


def resources_html(resources, nodes, c, hist):
    o = ['<h2>Resources <span class="small mut">(from the scheduler, not the agents)</span></h2>']
    for nname, node in nodes.items():
        if not node:
            continue
        load = (node.get("load") or [0])[0]
        ncpu = node.get("ncpu") or 1
        frac = load / ncpu
        o.append('<div class="row"><span class="n">%s CPU</span><span class="x">%s</span><span class="r">load %.1f / %d</span></div>' % (
            esc(nname), bar(frac, "b" if frac > 1.2 else ""), load, ncpu))
        if node.get("mem_total_gb"):
            used = node["mem_total_gb"] - node.get("mem_avail_gb", 0)
            o.append('<div class="row"><span class="n">%s RAM</span><span class="x">%s</span><span class="r">%.0f / %.0f GB</span></div>' % (
                esc(nname), bar(used / node["mem_total_gb"], "w" if used / node["mem_total_gb"] > .9 else ""), used, node["mem_total_gb"]))
        for gp in node.get("gpus", []):
            u = gp.get("util", 0)
            o.append('<div class="row"><span class="n">%s GPU %s</span><span class="x">%s</span><span class="r">%d%% \u00b7 %.0f GB</span></div>' % (
                esc(nname), esc(gp.get("i")), bar(u / 100.0, "w" if u < 10 and gp.get("mem_used", 0) > 1024 else ""), u, gp.get("mem_used", 0) / 1024))
    for r in resources:
        if not r["ok"]:
            o.append('<div class="row"><span class="n">%s</span><span class="x bad-t">unreachable</span><span class="r small">%s</span></div>' % (esc(r["name"]), esc(trunc(r.get("error"), 50))))
            continue
        eff = 1 - (r["idle_ch"] + r["failed_ch"]) / r["alloc_ch"] if r["alloc_ch"] else None
        o.append('<div class="row"><span class="n">%s</span><span class="x">%s</span><span class="r">%dR %dPD \u00b7 %d cores</span></div>' % (
            esc(r["name"]), bar(eff if eff is not None else 0, "eff" if (eff or 1) > .7 else "w") if eff is not None else '<span class="small mut">no finished jobs in 48 h</span>',
            r["running"], r["pending"], r["cores"]))
        if r["alloc_ch"]:
            o.append('<div class="small mut" style="margin:-4px 0 6px 102px">bar = share of 48 h core-hours used well \u00b7 %.0f core-h, %.0f idle, %.0f failed</div>' % (
                r["alloc_ch"], r["idle_ch"], r["failed_ch"]))
    if len(hist) > 2:
        o.append('<div class="row small mut" style="margin-top:10px"><span class="n">24 h cores</span><span class="x">%s</span><span class="r">%d now</span></div>' % (
            spark([h.get("cores") for h in hist]), c.get("running_cores", 0)))
    return "\n".join(o)


FRESH_JS = r"""<script>
(function(){
function fmt(s){if(s<60)return 'just now';var m=Math.round(s/60);if(m<60)return m+' min ago';var h=Math.floor(m/60);if(h<48)return h+' h '+(m%60?(m%60)+' min ':'')+'ago';return Math.round(h/24)+' days ago';}
function tick(){[].forEach.call(document.querySelectorAll('.fresh'),function(el){
 var c=+el.getAttribute('data-collected'),iv=+el.getAttribute('data-interval')||5,age=Math.max(0,Date.now()/1000-c),cls,txt,tip;
 var when=new Date(c*1000).toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'});
 if(age<(iv*2+2)*60){cls='ok';txt='Updated '+fmt(age);tip='Collected at '+when+' (every '+iv+' min). '+
  (window.MC_SEND?'The page updates itself when new data arrives. Click to collect fresh data now.':'The page reloads itself every 2 min. Click to reload now.');}
 else if(age<3600){cls='warn';txt='Data '+fmt(age).replace(' ago',' old');tip='No new data since '+when+'. The collector may be stopped or busy: run mctl on the collecting machine.';}
 else{cls='bad';txt='Stale: last data '+fmt(age);tip='No new data since '+when+'. The collector looks stopped: open Ubuntu on the PC that collects (it restarts itself) or run mctl.';}
 el.className='pill fresh '+cls+(el.classList.contains('spin')?' spin':'');(el.querySelector('.ft')||el).textContent=txt;
 if(!el.classList.contains('spin'))el.title=tip;});}
window.mcFreshTick=tick;tick();setInterval(tick,15000);
})();
</script>"""


HEAD_META = ('<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
             '<meta name="theme-color" content="#22221f"><meta name="apple-mobile-web-app-capable" content="yes">'
             '<meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="Mission control">'
             '<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 32 32%27%3E%3Crect x=%277%27 y=%277%27 '
             'width=%2718%27 height=%2718%27 rx=%274%27 transform=%27rotate(45 16 16)%27 fill=%27%23534ab7%27/%3E%3C/svg%3E">')

REFRESH_JS = r"""<script>
(function(){
// Keeps the page current. Opened with mctl (http://localhost or your phone): asks the server every 15 s whether
// this page was rewritten and, if so, swaps the new content in place (scroll, open blocks and what you are typing
// stay). Opened as a plain file: reloads every 2 min while visible. The refresh button asks for fresh data now.
function xk(e){return 'mcx:'+location.pathname+':'+e.id;}
function abh(){var a=document.querySelector('.appbar');if(a)document.documentElement.style.setProperty('--abh',a.offsetHeight+'px');}
abh();window.addEventListener('resize',abh);
function applyX(){[].forEach.call(document.querySelectorAll('[data-x]'),function(e){try{if(sessionStorage.getItem(xk(e))==='1')e.classList.add('open');}catch(_){}});}
document.addEventListener('click',function(ev){var e=ev.target.closest&&ev.target.closest('[data-x]');if(!e||String(window.getSelection()).length)return;
 e.classList.toggle('open');try{sessionStorage.setItem(xk(e),e.classList.contains('open')?'1':'0');}catch(_){}});
applyX();
var S=window.MC_SEND||null,served=!!(S&&/^https?:/.test(location.protocol)),mt=(S&&S.mtime)||0,conv=document.body.classList.contains('convpage'),
 build=(document.querySelector('meta[name=mc-build]')||{}).content,loaded=Date.now(),inflight=null,btn=document.getElementById('mc-refresh');
function blocked(){var a=document.activeElement;return !!window.MC_BUSY||String(window.getSelection()).length>0||!!(a&&a.tagName==='SELECT');}
function hasText(){return [].some.call(document.querySelectorAll('textarea,input[type=search]'),function(t){return t.value.trim();});}
function atBottom(){return window.innerHeight+window.scrollY>=document.documentElement.scrollHeight-240;}
function spin(on){if(btn)btn.classList.toggle('spin',!!on);}
function stamp(){return fetch('/api/stamp?p='+encodeURIComponent(location.pathname),{cache:'no-store'}).then(function(r){return r.json();});}
function swap(h){
 var d=new DOMParser().parseFromString(h,'text/html'),nb=(d.querySelector('meta[name=mc-build]')||{}).content;
 if(nb&&build&&nb!==build){if(blocked()||hasText())return false;location.reload();return true;}   // the page code itself changed
 var na=d.querySelector('.appbar nav'),oa=document.querySelector('.appbar nav');if(na&&oa)oa.innerHTML=na.innerHTML;
 var nf=d.querySelector('.fresh'),of=document.querySelector('.fresh');if(nf&&of)of.setAttribute('data-collected',nf.getAttribute('data-collected'));
 document.title=d.title;if(window.mcFreshTick)window.mcFreshTick();abh();
 if(conv){if(window.mcConvPoll)window.mcConvPoll();return true;}
 var nm=d.querySelector('main'),om=document.querySelector('main');if(!nm||!om)return false;
 var y=window.scrollY,act=document.activeElement,aid=act&&act.id,s0=null,s1=null,open={};
 if(aid&&/^(TEXTAREA|INPUT)$/.test(act.tagName)){try{s0=act.selectionStart;s1=act.selectionEnd;}catch(_){}}
 [].forEach.call(om.querySelectorAll('details'),function(x,i){open[x.getAttribute('data-sec')||('#'+i)]=x.open;});
 nm=document.importNode(nm,true);
 [].forEach.call(nm.querySelectorAll('details'),function(x,i){var k=x.getAttribute('data-sec')||('#'+i);if(k in open)x.open=open[k];});
 var sb=document.getElementById('sendbox'),nsb=nm.querySelector('#sendbox');if(sb&&nsb)nsb.parentNode.replaceChild(sb,nsb);   // keeps what you typed
 om.parentNode.replaceChild(nm,om);
 applyX();if(window.mcHideScan)window.mcHideScan();window.scrollTo(0,y);
 if(aid){var e=document.getElementById(aid);if(e&&document.activeElement!==e){try{e.focus({preventScroll:true});if(s0!==null)e.setSelectionRange(s0,s1);}catch(_){}}}
 return true;}
function pull(){if(inflight)return inflight;
 inflight=fetch(location.href,{cache:'no-store'}).then(function(r){return r.ok?r.text():null;}).then(function(h){inflight=null;return !!h&&!blocked()&&swap(h);})
  .catch(function(){inflight=null;return false;});return inflight;}
function check(){if(!served||document.hidden)return;
 stamp().then(function(j){if(j.mtime>mt){var m=j.mtime;pull().then(function(ok){if(ok)mt=m;});}}).catch(function(){});}
function fallback(){if(served||document.hidden||blocked()||hasText()||(conv&&!atBottom()))return;if(Date.now()-loaded>110000)location.reload();}
setInterval(function(){served?check():fallback();},served?15000:30000);
document.addEventListener('visibilitychange',function(){if(!document.hidden){served?check():fallback();}});
if(btn)btn.addEventListener('click',function(){
 if(!served){location.reload();return;}
 spin(true);
 stamp().then(function(j){mt=Math.max(mt,0);return pull().then(function(ok){if(ok)mt=j.mtime;return j;});}).then(function(j){
  if(Date.now()/1000-(j.collected||0)<60){spin(false);return;}   // the data is already fresh
  return fetch('/api/collect',{method:'POST',headers:{'Content-Type':'application/json','X-MC-Token':S.token},body:'{}'}).then(function(r){return r.json();}).then(function(c){
   if(c.message)btn.title=c.message;if(!c.started){spin(false);return;}
   var n=0,iv=setInterval(function(){n++;stamp().then(function(s){
    if(s.mtime>mt){clearInterval(iv);var m=s.mtime;pull().then(function(ok){if(ok)mt=m;spin(false);btn.title='Refresh now';});}
    else if(n>40){clearInterval(iv);spin(false);}}).catch(function(){});},5000);});
 }).catch(function(){spin(false);});});
})();
</script>"""

SEND_JS = r"""<script>
(function(){
var box=document.getElementById('sendbox');if(!box)return;
var ta=document.getElementById('sendtext'),btn=document.getElementById('sendbtn'),hint=document.getElementById('sendhint'),res=document.getElementById('sendres'),S=window.MC_SEND;
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function off(msg){ta.disabled=true;btn.disabled=true;hint.innerHTML=msg;}
if(box.getAttribute('data-notmux')==='1'){off('This agent runs outside tmux, so it can\u2019t receive messages from here.');return;}
if(!S||!S.token){off('Read-only copy. To message the agent, open the dashboard by typing <b>mctl</b> in WSL (it opens <span class="mono">http://localhost:8765</span>).');return;}
function send(force){var t=ta.value;if(!t.trim())return;btn.disabled=true;window.MC_BUSY=true;res.innerHTML='<div class="small mut">Sending\u2026</div>';
 fetch('/api/send',{method:'POST',headers:{'Content-Type':'application/json','X-MC-Token':S.token},body:JSON.stringify({key:box.getAttribute('data-key'),text:t,force:!!force})})
 .then(function(r){return r.json();}).then(function(d){btn.disabled=false;window.MC_BUSY=false;
  var when=new Date().toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'}),scr=d.screen?'<pre class="sendscr">'+esc(d.screen)+'</pre>':'';
  if(d.ok){var sent=ta.value;ta.value='';ta.style.height='';
   if(box.getAttribute('data-compact')==='1')res.innerHTML='<div class="small sentok">\u2713 Sent at '+when+'</div>'+(scr?'<details class="scr"><summary class="small">The agent\u2019s screen a few seconds later</summary>'+scr+'</details>':'');
   else res.innerHTML='<div class="small sentok">\u2713 Sent at '+when+'. The agent\u2019s screen a few seconds later:</div>'+scr;
   try{document.dispatchEvent(new CustomEvent('mc-sent',{detail:{text:sent}}));}catch(e){}}
  else if(d.code===3){res.innerHTML='<div class="flag amber"><div class="t">Not sent yet</div><div class="d">'+esc(d.message)+'</div><div class="a"><button type="button" class="more" id="sendforce">Send anyway</button></div></div>'+scr;
   document.getElementById('sendforce').onclick=function(){send(true);};}
  else{res.innerHTML='<div class="flag red"><div class="t">Not sent</div><div class="d">'+esc(d.message||'failed')+'</div></div>'+scr;}})
 .catch(function(e){btn.disabled=false;window.MC_BUSY=false;res.innerHTML='<div class="flag red"><div class="t">Not sent</div><div class="d">The local mission control server didn\u2019t answer. Type mctl in WSL to restart it.</div></div>';});}
btn.addEventListener('click',function(){send(false);});
ta.addEventListener('input',function(){ta.style.height='auto';ta.style.height=Math.min(ta.scrollHeight,220)+'px';});
ta.addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();send(false);}});
})();
</script>"""

# Pages carry a fingerprint of their code, so an open page reloads fully (rather than swapping content) after an update.
BUILD = hashlib.md5((CSS + CONV_CSS + CONV_JS + HIDE_JS + FRESH_JS + REFRESH_JS + SEND_JS).encode("utf-8")).hexdigest()[:12]
HEAD_META += '<meta name="mc-build" content="%s">' % BUILD


def send_box(s, compact=False):
    g = s["goal"]
    where = "%s on %s" % (s["session"], LOGIN_HOSTS.get(s.get("node_name"), s.get("node_name") or "?"))
    return ('<div class="sendbox" id="sendbox" data-key="%s" data-notmux="%s"' + (' data-compact="1"' if compact else '') +
            '><textarea id="sendtext" rows="2" placeholder="Message the agent, just as you would type it in its terminal (%s)"></textarea>'
            '<div class="sendrow"><span id="sendhint" class="small mut">Enter sends \u00b7 Shift+Enter for a new line \u00b7 goes to tmux %s</span>'
            '<button type="button" id="sendbtn" class="sendbtn">Send</button></div><div id="sendres"></div></div>') % (
        esc(s["key"]), "1" if g.get("notmux") else "0", esc(where), esc(where))


def fresh_pill(collected):
    """How old the data on the page is, counted live in the browser (the page itself is a snapshot)."""
    if not collected:
        return '<span class="pill bad" title="Mission control hasn\u2019t collected anything yet.">No data yet</span>'
    return ('<button class="pill fresh ok" id="mc-refresh" type="button" data-collected="%d" data-interval="%d" title="Refresh now">'
            '<span class="ft">Updated %s</span><svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.4" '
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 11.5a8 8 0 1 1-2.4-5.7"/><path d="M20.5 3.5v5h-5"/></svg>'
            '</button>%s' % (int(collected), CFG["interval"], esc(ago(collected)), FRESH_JS))


def goal_sev(flags):
    return worst_sev([f for f in flags if f["area"] in ("goal", "integrity") or (f["area"] == "connection" and f.get("node"))])


def worst_sev(flags):
    sev = [f["sev"] for f in flags]
    return "red" if "red" in sev else ("amber" if "amber" in sev else "ok")


def session_state(g, flag_keys, unreachable=False, ended=False, node="", last_seen=None):
    """One line saying what the agent is doing right now, and its color."""
    fk = set(flag_keys or [])
    if unreachable:
        return "Can't reach %s right now" % LOGIN_HOSTS.get(node, node), "neu"
    if ended:
        return "Session ended%s" % ((" " + ago(last_seen)) if last_seen else ""), "neu"
    if not g.get("codex_running"):
        return "Agent not running", "bad"
    if "pane:approval" in fk:
        return "Waiting for you", "bad"
    if "goal:stalled" in fk:
        return "Silent for %s" % ago(g.get("last_activity")).replace(" ago", ""), "bad"
    if "goal:overslept" in fk:
        return "Should have woken up by now", "warn"
    if g.get("sleep_until"):
        return "Sleeping until %s (scheduled check)" % hm(g["sleep_until"]), "ok"
    if g.get("paused"):
        return (g.get("status") or "paused").capitalize(), "warn"
    if g.get("busy") or (g.get("last_activity") and NOW - g["last_activity"] < 15 * 60):
        return "Working", "ok"
    if g.get("last_activity"):
        return "Idle · last active %s" % ago(g["last_activity"]), "neu"
    return "Running", "ok"


def kind_badge(kind):
    return '<span class="kind %s">%s</span>' % (esc(kind or ""), esc((kind or "agent").capitalize()))


def render(s):
    g, c, p = s["goal"], s["compute"], s["plan"]
    reds = [f for f in s["flags"] if f["sev"] == "red"]
    ambers = [f for f in s["flags"] if f["sev"] == "amber"]
    cv = s.get("conv") or {}
    o = []
    o.append('<!doctype html><html lang="en"><head>%s<title>%s%s \u00b7 mission control</title><style>%s</style>%s</head><body style="%s">'
             % (HEAD_META, ("(%d) " % len(reds)) if reds else "", esc(s["session"]), CSS, hide_css(), hue(s.get("key"))))
    o.append(appbar(s.get("nav") or [], s.get("key"), "goals", False, s.get("collected")))
    o.append("<main>")
    stxt, scls = session_state(g, [f["key"] for f in s["flags"]], s.get("unreachable"), s.get("ended"), s.get("node_name"), s.get("last_seen"))
    where = LOGIN_HOSTS.get(s.get("node_name"), s.get("node_name") or "?")
    meta = ["%s on %s" % ("no tmux (pid %s)" % esc((s.get("tmux") or {}).get("pid") or "?") if g.get("notmux") else "tmux " + esc(s["session"]), esc(where))]
    if g.get("model"):
        meta.append('<span class="mdl" title="Main agent\u2019s model and reasoning level">%s</span>' % esc(" \u00b7 ".join(x for x in (g["model"], g.get("effort")) if x)))
    subs, revs = {}, {}
    for x in s.get("sessions") or []:
        if x.get("role") == "goal" and x.get("label") not in (None, "main") and x.get("model"):
            k = " \u00b7 ".join(v for v in (x["model"], x.get("effort")) if v)
            grp = revs if "review" in x["model"] else subs   # Codex's approval reviewer runs as its own session
            grp[k] = grp.get(k, 0) + 1
    for grp, name, tip in ((subs, "sub-agents", "Sub-agents\u2019 models and reasoning levels"),
                           (revs, "approval reviewer", "Codex\u2019s automatic approval reviewer")):
        if grp:
            meta.append('<span class="mdl" title="%s">%s: %s</span>' % (tip, name, esc(", ".join(
                "%s%s" % (k, " \u00d7%d" % n if n > 1 else "") for k, n in sorted(grp.items(), key=lambda kv: -kv[1])))))
    if g.get("cwd"):  # shortened; click (or tap) for the whole path, selectable for copying
        meta.append('<span class="mono xp" id="x-cwd" data-x title="Click to show the full folder"><span class="xs">%s</span><span class="xf">%s</span></span>'
                    % (esc(trunc(g["cwd"], 70)), esc(g["cwd"])))
    goal_full = ((s.get("goal") or {}).get("text") or s.get("title") or "").strip()
    o.append('<header><div style="min-width:0"><div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap"><h1>%s</h1>%s<span class="pill %s">%s</span></div>'
             '<p class="sub">%s</p>%s</div>%s</header>' % (
                 esc(s["session"]), kind_badge(g.get("agent_kind")), scls, esc(stxt), " \u00b7 ".join(meta),
                 ('<p class="objective" id="x-goal" data-x title="Click to show the whole goal"><b>Goal:</b> %s</p>' % esc(trunc(goal_full, 20000)))
                 if goal_full and goal_full != s["session"] else "",
                 ""))
    cv = s.get("conv") or {}
    words = s.get("agent_words") or []
    o.append('<div id="hidden-bar" class="hbar" style="display:none"></div>')
    o.append('<section class="card"' + sec("g", "latest") + '><div style="display:flex;justify-content:space-between;gap:10px;align-items:baseline;flex-wrap:wrap">'
             '<h2>%s</h2>%s</div>' % (
                 ('<a href="%s" style="color:inherit;text-decoration:none">Agent\u2019s latest words</a>' % esc(cv["page"])) if cv.get("page") else "Agent\u2019s latest words",
                 ('<a class="pill dec" style="text-decoration:none" href="%s">Full conversation \u00b7 %s messages \u2192</a>' % (esc(cv["page"]), "{:,}".format(cv["count"])))
                 if cv.get("page") else '<span class="small mut">full history builds up from the next collection</span>'))
    if words:
        # the tail of the chat: older ones folded, then the latest (two if the newest is only a short "next I'll..." line)
        shown = 1 if len(words[0].get("text") or "") >= 400 else min(2, len(words))
        ly = cv.get("last_you")

        def ep_of(w):
            return w.get("epoch") or iso_epoch(w.get("ts")) or 0
        items = [dict(w, role="agent") for w in words]
        if ly:
            items.append({"role": "user", "epoch": ly.get("epoch"), "text": ly.get("text") or ""})
        items.sort(key=ep_of)
        newest_ep = sorted(ep_of(w) for w in words)[-shown]
        cut = next((i for i, w in enumerate(items) if w["role"] == "agent" and ep_of(w) >= newest_ep), len(items))
        if ly and items[-1]["role"] == "user":
            cut = min(cut, len(items) - 1)   # your last message stays visible when it is the newest

        def bubble(w):
            ep = ep_of(w)
            t = w.get("text") or ""
            long = len(t) > 2200 or t.count("\n") > 30
            more = ('<button class="more" onclick="var m=this.previousElementSibling,c=m.classList.toggle(\'clip\');'
                    'this.textContent=c?\'Show all\':\'Show less\'">Show all</button>') if long else ""
            if w["role"] == "user":
                return ('<div class="row r-user"><div class="bub"><div class="mh"><span class="who">You</span><span class="tm">%s · %s</span></div>'
                        '<div class="mb">%s</div></div></div>' % (esc(hm(ep)), esc(ago(ep)), md(trunc(t, 1500))))
            sub = w.get("who") not in (None, "main")
            mt = w.get("mdl") or ""
            return ('<div class="row r-%s"><div class="av">%s</div><div class="bub"><div class="mh"><span class="who">%s</span><span class="tm">%s%s</span>%s</div>'
                    '<div class="mb%s">%s</div>%s</div></div>' % (
                        "sub" if sub else "agent", "S" if sub else "A", esc("Sub-agent \u00b7 " + w.get("who") if sub else "Agent"),
                        esc(hm(ep)), (" \u00b7 " + esc(ago(ep))) if ep else "", ('<span class="mdl">%s</span>' % esc(mt)) if mt else "",
                        " clip" if long else "", md(t), more))
        o.append('<div class="chat">')
        if cut > 0:
            o.append("<details style='border:none;padding:0;margin:4px 0 0;box-shadow:none;background:none'><summary class='small'>Earlier messages</summary>")
            o.extend(bubble(w) for w in items[:cut])
            if cv.get("page"):
                o.append('<div class="small" style="margin:6px 0 0 38px"><a href="%s">All %s messages →</a></div>' % (esc(cv["page"]), "{:,}".format(cv["count"])))
            o.append("</details>")
        o.extend(bubble(w) for w in items[cut:])
        o.append("</div>")
    else:
        o.append('<div class="empty">No agent messages captured yet.</div>')
    o.append(send_box(s))
    o.append("</section>")

    # metric cards
    gs = g["status"] or "unknown"
    gcls = "bad-t" if (not g.get("codex_running") and g.get("tmux_found") is not None) else ""
    cards = [
        ("Goal", '<span class="%s">%s</span>' % (gcls, esc(gs.capitalize())),
         ("agent running (no tmux)" if g.get("notmux") else "agent running in tmux") if g.get("codex_running") else ("agent not running" if g.get("tmux_found") else "tmux not seen")),
        ("Last agent activity", (esc("asleep until " + hm(g["sleep_until"])) if g.get("sleep_until") else
                                 esc(ago(g.get("last_activity")).replace(" ago", ""))) if g.get("last_activity") else "\u2013",
         "main agent \u00b7 %s tool calls%s" % ("{:,}".format(g["main_calls"]) if g.get("main_calls") else "0",
                                               (" \u00b7 Codex quota %d%% %s" % (g["quota"]["pct"], g["quota"]["window"])) if g.get("quota") else "")),
        ("Plan", ("%d / %d" % (p["done"], len(p["steps"]))) if p["steps"] else "\u2013",
         esc(trunc(p["now"], 60)) if p["now"] else ("no plan published" if not p["steps"] else "")),
        ("Slurm jobs now", "%d R \u00b7 %d PD" % (sum(r["running"] for r in s["resources"]), sum(r["pending"] for r in s["resources"])),
         " \u00b7 ".join("%s %dR %dPD" % (r["name"], r["running"], r["pending"]) for r in s["resources"]) or "no clusters"),
        ("Idle or failed compute, 48 h", ("%d%%" % c["waste_pct"]) if c["waste_pct"] is not None else "\u2013",
         "%.0f of %.0f core-h%s%s" % (c["idle_ch"] + c["failed_ch"], c["alloc_ch"], (" \u00b7 %.0f GPU-h" % c["gpu_h"]) if c["gpu_h"] else "",
                                       (" \u00b7 node failure cost %.0f more" % c["lost_ch"]) if c.get("lost_ch") else "")),
        ("Acceptance criteria", ("%d / %d met" % (sum(1 for r in s.get("criteria", []) if r["status"] == "met"),
                                                 sum(1 for r in s.get("criteria", []) if r.get("parsed")))) if any(r.get("parsed") for r in s.get("criteria", [])) else "\u2013",
         (" \u00b7 ".join("%s %s" % (r.get("latest") or "no data", "\u2713" if r["status"] == "met" else "") for r in s.get("criteria", []) if r.get("parsed"))[:90])
         or "add them in the contract"),
    ]
    o.append('<section class="cards"%s>' % sec("g", "cards"))
    tile_hue = {"Goal": "lime", "Last agent activity": "sky", "Plan": "violet", "Slurm jobs now": "teal",
                "Idle or failed compute, 48 h": "orange", "Acceptance criteria": "pink"}
    for l, v, sub in cards:
        o.append('<div class="m" style="--s:var(--%s)"><div class="l">%s</div><div class="v">%s</div><div class="s">%s</div></div>'
                 % (tile_hue.get(l, "slate"), esc(l), v, sub))
    o.append("</section>")

    # needs you
    needs = [f for f in s["flags"] if f.get("needs_you")]
    o.append('<section class="card%s"%s><h2>Needs you</h2>' % ("" if needs else " calm", sec("g", "needs")))
    if needs:
        o.extend(flag_html(f) for f in needs)
    else:
        o.append('<div class="empty">Nothing needs you right now. %d amber item(s) below are worth a glance.</div>' % len(ambers)
                 if ambers else '<div class="empty">Nothing needs you right now.</div>')
    o.append("</section>")

    # plan + resources
    if s.get("criteria"):
        o.append('<section class="card"' + sec("g", "criteria") + '><h2>Acceptance criteria <span class="small mut">(checked against real test output)</span></h2><table>'
                 '<tr><th></th><th>Criterion</th><th>Latest</th><th>Best</th><th>Evidence</th></tr>')
        for r in s["criteria"]:
            if r["status"] == "met":
                ic, cls = "\u2713", "done"
            elif r["status"] == "not met":
                ic, cls = "\u2717", "bad-t"
            elif r["status"] == "no evidence":
                ic, cls = "?", "mut"
            else:
                ic, cls = "\u2696", "mut"
            ev = ("%s, %s%s%s" % (esc(r.get("source")), esc(ago(r.get("when"))), (" \u00b7 %d frames" % r["n_test"]) if r.get("n_test") else "",
                                   " \u00b7 training curve only" if r.get("from_curve") else "")) if r.get("latest") else (
                "no test output found yet" if r["parsed"] else "not a number: the referee judges this one")
            o.append('<tr><td class="%s" style="font-size:16px">%s</td><td>%s%s<div class="small mut">%s</div></td><td class="%s">%s</td><td>%s</td><td class="small mut">%s</td></tr>' % (
                cls, ic, esc(r["text"]), ' <span class="pill neu">from goal</span>' if r.get("src") == "goal text" else "",
                esc(r.get("rule") or ""), cls if r["status"] == "not met" else "", esc(r.get("latest") or "\u2013"), esc(r.get("best") or "\u2013"), ev))
        o.append("</table></section>")
    o.append('<section class="grid2"><div class="card"%s><h2>Plan</h2>' % sec("g", "plan"))
    if p["steps"]:
        for st in p["steps"]:
            stt = str(st.get("status", "")).lower()
            if stt in ("completed", "done", "complete"):
                cls, ic = "done", "\u2713"
            elif "progress" in stt:
                cls, ic = "prog", "\u25b6"
            else:
                cls, ic = "pend", "\u25cb"
            o.append('<div class="step %s"><span class="i">%s</span><span>%s</span></div>' % (cls, ic, esc(st.get("step"))))
        o.append('<div class="small mut" style="margin-top:6px">Updated %s%s</div>' % (
            esc(ago(iso_epoch(p["ts"]))), (" \u00b7 " + esc(trunc(p["expl"], 200))) if p["expl"] else ""))
    else:
        o.append('<div class="empty">The agent hasn\u2019t published a plan in this window. Codex shows one when it calls update_plan.</div>')
    o.append('</div><div class="card"%s>' % sec("g", "resources"))
    o.append(resources_html(s["resources"], {s.get("node_name") or "node": s.get("node") or {}}, c, s.get("history") or []))
    o.append("</div></section>")
    o.append('<div style="height:14px"></div>')

    # integrity
    integ = [f for f in s["flags"] if f["area"] == "integrity" and not f.get("needs_you")]
    o.append('<section class="card"%s><h2>Scientific integrity</h2>' % sec("g", "integrity"))
    if integ:
        o.extend(flag_html(f) for f in integ)
    elif not any(f["area"] == "integrity" for f in s["flags"]):
        o.append('<div class="empty">No contract breaks, unexplained parameter changes or shortcut wording found.</div>')
    pt = s["params"]["table"]
    if pt:
        o.append('<table style="margin-top:8px"><tr><th>Parameter</th><th>Rule</th><th>Values in recent input files</th></tr>')
        for row in pt:
            vals = ", ".join(('<span class="%s" title="%s">%s \u00d7%d</span>' % ("bad-t" if v["bad"] else "", esc(v["ex"]), esc(v["v"]), v["n"])) for v in row["values"][:6])
            o.append("<tr><td class=mono>%s</td><td class=mut>%s</td><td>%s</td></tr>" % (esc(row["name"]), esc(row["rule"]), vals))
        o.append("</table>")
    sc = s["params"]["scan"]
    o.append('<div class="small mut" style="margin-top:8px">Input scan: %s file(s) changed in the last %g h under %s%s. Rules live in <span class=mono>contract/goal_contract.md</span>.</div>' % (
        sc.get("files") or 0, CFG["scan_hours"], esc(", ".join(sc.get("roots") or []) or "the goal folder"), " (scan cut short)" if sc.get("truncated") else ""))
    o.append("</section>")

    # other flags (goal / compute amber+info not shown above)
    other = [f for f in s["flags"] if f["area"] in ("goal", "compute", "connection") and not f.get("needs_you")]
    if other:
        o.append('<section class="card"%s><h2>Watchdog</h2>' % sec("g", "watchdog"))
        o.extend(flag_html(f) for f in other)
        o.append("</section>")

    # timeline
    o.append('<section class="card"' + sec("g", "timeline") + '><h2>What happened <span class="small mut">(you, agents, decisions; newest first)</span></h2><div class="tl">')
    if s["timeline"]:
        for t in s["timeline"]:
            lab, cls = KIND_PILL.get(t.get("kind"), (t.get("kind") or "cmd", "neu"))
            extra = ""
            if t.get("exit") not in (None, 0):
                extra += ' <span class="pill bad">exit %s</span>' % esc(t["exit"])
            if t.get("jobid"):
                extra += ' <span class="pill ok">job %s</span>' % esc(t["jobid"])
            if t.get("watch"):
                extra += ' <span class="pill warn">%s</span>' % esc(", ".join(t["watch"][:3]))
            o.append('<div class="e"><span class="tm">%s</span><span class="w"><span class="pill %s">%s</span></span><span class="tx"><span class="small mut">%s</span> %s%s</span></div>' % (
                esc(hm(t.get("epoch"))), cls, esc(lab), esc(t.get("who") if t.get("who") not in ("you", "decision") else ""),
                esc(trunc(t.get("text"), 320)), extra))
    else:
        o.append('<div class="empty">No notable agent actions in the window.</div>')
    o.append("</div></section>")

    # instructions
    o.append('<section class="card"' + sec("g", "instructions") + '><h2>Your instructions to the agents</h2><div class="small mut" style="margin-bottom:6px">Captured from the session logs. Make one binding with <span class=mono>mctl promote &lt;ID&gt;</span>.</div>')
    if s["instructions"]:
        o.append("<table><tr><th>ID</th><th>When</th><th>To</th><th>Instruction</th></tr>")
        for u in s["instructions"][:25]:
            o.append('<tr><td class=mono>%s%s</td><td class=mut style="white-space:nowrap">%s</td><td class=mut>%s</td><td>%s</td></tr>' % (
                esc(u["id"]), ' <span class="pill ok">in contract</span>' if u["promoted"] else "", esc(hm(iso_epoch(u.get("ts")))),
                esc(u.get("who")), esc(trunc(u["text"], 400))))
        o.append("</table>")
    else:
        o.append('<div class="empty">None captured yet.</div>')
    o.append("</section>")

    # collapsibles

    if g.get("notmux"):
        o.append('<details' + sec("g", "screen") + '><summary>Live screen</summary><div class="empty">This agent runs outside tmux, so there is no screen to show. '
                 'Its messages and actions above come from its session log.</div></details>')
    else:
        o.append("<details" + sec("g", "screen") + "><summary>Live screen: tmux %s (last %d lines)</summary><pre>%s</pre></details>" % (
            esc(s["session"]), len(s["pane"]), esc("\n".join(s["pane"])) or "(nothing captured)"))
    if s["resources"]:
        o.append("<details" + sec("g", "slurm") + "><summary>Slurm jobs</summary><table><tr><th>Cluster</th><th>Job</th><th>Name</th><th>State</th><th>Partition</th><th>Time</th><th>Cores</th><th>Submitted by</th></tr>")
        for r in s["resources"]:
            for j in r["jobs"]:
                o.append("<tr><td>%s</td><td class=mono>%s</td><td>%s</td><td>%s</td><td>%s</td><td class=mut>%s / %s</td><td>%s</td><td class=mut>%s</td></tr>" % (
                    esc(r["name"]), esc(j["id"]), esc(trunc(j["name"], 40)), esc(j["state"]) + ((" <span class=mut>(%s)</span>" % esc(j["reason"])) if j["state"] == "PENDING" else ""),
                    esc(j["part"]), esc(j["used"]), esc(j["limit"]), esc(j["cpus"]), esc(j["by"] or "\u2013")))
        o.append("</table></details>")
    o.append("<details" + sec("g", "sessions") + "><summary>Agent sessions (%d)</summary><table><tr><th>Agent</th><th>Host</th><th>Kind</th><th>Last active</th><th>Calls</th><th>Errors</th><th>Tokens</th><th>Folder</th></tr>" % len(s["sessions"]))
    for x in s["sessions"]:
        o.append("<tr><td>%s%s</td><td>%s</td><td>%s</td><td class=mut>%s</td><td>%s</td><td>%s</td><td class=mut>%s</td><td class='mut small mono'>%s</td></tr>" % (
            esc(x["label"]), ' <span class="pill dec">goal</span>' if x["role"] == "goal" else "", esc(x["host"]), esc(x["kind"]),
            esc(ago(x["last"])), esc(x["calls"]), esc(x["errors"]), esc("{:,}".format(x["tokens"]) if x.get("tokens") else "\u2013"), esc(trunc(x["cwd"], 50))))
    o.append("</table></details>")
    ref = s.get("referee") or {}
    if ref.get("text"):
        o.append("<details" + sec("g", "referee") + "><summary>Referee review (%s)</summary><pre>%s</pre></details>" % (esc(ago(ref.get("mtime"))), esc(trunc(ref["text"], 12000))))
    ct = s["contract"]
    o.append("<details" + sec("g", "contract") + "><summary>Contract: %d clauses, %d watched parameters, %d amendments</summary>" % (len(ct["clauses"]), len(ct["watch"]), len(ct["amends"])))
    for cl in ct["clauses"]:
        o.append('<div class="small" style="margin:4px 0"><b>%s</b> %s</div>' % (esc(cl["id"]), esc(cl["text"])))
    for a in ct["amends"]:
        o.append('<div class="small" style="margin:4px 0"><b>%s</b> <span class=mut>%s</span> %s</div>' % (esc(a["id"]), esc(a["when"]), esc(a["text"])))
    o.append("</details>")

    hs = " \u00b7 ".join('%s <span style="color:var(--%s)">%s</span>' % (esc(h["name"]), "grn" if h["ok"] else "red", "ok" if h["ok"] else "down") for h in s["hosts"])
    o.append('<footer>Collectors: %s<br>Collected %s \u00b7 rendered %s \u00b7 refreshes every 2 min in the browser, new data every %d min<br>'
             'Mission control types into a session only when you press Send above%s.<br>'
             'Hover over any block and click its \u00d7 to hide it in this browser; <span class="mono">mctl hide NAME</span> hides it on every machine.%s</footer>' % (
                 hs, esc(time.strftime("%Y-%m-%d %H:%M", time.localtime(s["collected"]))) if s.get("collected") else "never",
                 esc(time.strftime("%H:%M", time.localtime(s["generated"]))), CFG["interval"], never_note(), hidden_note()))
    o.append("</main>%s%s%s</body></html>" % (HIDE_JS, SEND_JS, REFRESH_JS))
    return "\n".join(o)


# ---------------------------------------------------------------- shared-connection discovery
def _proc_peer_ips(pid):
    import socket as so
    inodes = set()
    try:
        for fd in os.listdir("/proc/%d/fd" % pid):
            try:
                m = re.match(r"socket:\[(\d+)\]", os.readlink("/proc/%d/fd/%s" % (pid, fd)))
            except OSError:
                continue
            if m:
                inodes.add(m.group(1))
    except OSError:
        return []
    out = []
    for fn, v6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
        for line in (rd(fn, "") or "").splitlines()[1:]:
            f = line.split()
            if len(f) < 10 or f[3] != "01" or f[9] not in inodes:
                continue
            hx = f[2].split(":")[0]
            try:
                b = bytes.fromhex(hx)
                if not v6:
                    ip = so.inet_ntop(so.AF_INET, b[::-1])
                else:
                    b = b"".join(b[i:i + 4][::-1] for i in range(0, 16, 4))
                    ip = so.inet_ntop(so.AF_INET6, b)
                    if ip.startswith("::ffff:"):
                        ip = ip[7:]
                out.append(ip)
            except (ValueError, OSError):
                pass
    return out


def _ssh_masters():
    """Every live ssh control master owned by this user: (socket path, pid, remote IPs). Reads /proc only."""
    uid = os.getuid()
    listening = {}
    for line in (rd("/proc/net/unix", "") or "").splitlines()[1:]:
        f = line.split()
        try:
            if len(f) >= 8 and int(f[3], 16) & 0x10000:
                listening[f[6]] = f[7]
        except ValueError:
            continue
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            if os.stat("/proc/" + d).st_uid != uid or (rd("/proc/%s/comm" % d, "") or "").strip() != "ssh":
                continue
            inodes = set()
            for fd in os.listdir("/proc/%s/fd" % d):
                try:
                    m = re.match(r"socket:\[(\d+)\]", os.readlink("/proc/%s/fd/%s" % (d, fd)))
                except OSError:
                    continue
                if m:
                    inodes.add(m.group(1))
        except OSError:
            continue
        paths = [listening[i] for i in inodes if i in listening]
        if not paths:
            continue
        path = paths[0]
        final = re.sub(r"\.[A-Za-z0-9]{16}$", "", path)  # ssh binds a temporary name, then renames it
        if final != path and os.path.exists(final):
            path = final
        cmd = (rd("/proc/%s/cmdline" % d, "") or "").replace("\0", " ")
        out.append((path, int(d), _proc_peer_ips(int(d)), cmd))
    # A ProxyJump master talks to its target through a separate `ssh -W [host]:port jump` process over pipes.
    # That process's command names the real target; find it by the pipes the two share (or by parentage).
    def fd_links(pid):
        links = set()
        try:
            for fd in os.listdir("/proc/%s/fd" % pid):
                try:
                    links.add(os.readlink("/proc/%s/fd/%s" % (pid, fd)))
                except OSError:
                    pass
        except OSError:
            pass
        return links
    proxies = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            if os.stat("/proc/" + d).st_uid != uid:
                continue
        except OSError:
            continue
        cmdl = (rd("/proc/%s/cmdline" % d, "") or "").replace("\0", " ")
        if re.search(r"(^|\s)-W\s", cmdl) or "ProxyCommand" in cmdl:
            st = rd("/proc/%s/stat" % d, "") or ""
            try:
                ppid = int(st.rsplit(")", 1)[1].split()[1])
            except (IndexError, ValueError):
                ppid = -1
            proxies.append((ppid, {x for x in fd_links(d) if x.startswith("pipe:") or x.startswith("socket:")}, cmdl))
    res = []
    for path, pid, ips, cmd in out:
        mine = {x for x in fd_links(pid) if x.startswith("pipe:") or x.startswith("socket:")}
        extra = " ".join(c for pp, links, c in proxies if pp == pid or (links & mine))
        res.append((path, pid, ips, (cmd + " " + extra).strip()))
    return res


def glob_list(pattern):
    import glob as _g
    return _g.glob(pattern)


def cmd_discover(args):
    """Map live ssh master sockets (opened by your connection script or anything else) to host aliases.
    Local only: reads /proc and the local ssh config; never contacts a remote host."""
    import socket as so
    import subprocess
    rest = [a for a in args[1:] if a]
    nodes = rest[rest.index("--nodes") + 1:] if "--nodes" in rest else []
    aliases = (rest[: rest.index("--nodes")] if "--nodes" in rest else rest) + nodes
    found = _ssh_masters()
    # Hard rule: never touch a connection to a NEVER_CONTACT host (or anything in EXCLUDE_HOSTS).
    excl_words = list(NEVER) + [w.lower() for w in os.environ.get("EXCLUDE_HOSTS", "").split() if w]

    def word_rx(words):  # an empty list must match nothing (an empty alternation would match everything)
        return re.compile(r"(?<![A-Za-z0-9])(%s)(?![A-Za-z0-9])" % "|".join(re.escape(w) for w in words), re.I) if words else re.compile(r"(?!x)x")
    excl_rx = word_rx(excl_words)
    never_rx = re.compile("|".join(re.escape(w) for w in NEVER), re.I) if NEVER else re.compile(r"(?!x)x")
    excl_ips = set()
    cfg_txt = ""
    for f in [os.path.expanduser("~/.ssh/config")] + sorted(glob_list(os.path.expanduser("~/.ssh/config.d/*"))):
        cfg_txt += (rd(f, "") or "") + "\n"
    host_aliases = [h for line in cfg_txt.splitlines() if line.strip().lower().startswith("host ")
                    for h in line.split()[1:] if not any(c in h for c in "*?!")]
    for h in host_aliases:
        try:
            g = subprocess.run(["ssh", "-G", h], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=5).stdout
        except Exception:
            g = ""
        if excl_rx.search(h) or never_rx.search(g) or h.lower() in excl_words:
            hn_x = next((ln.split(None, 1)[1].strip() for ln in g.splitlines() if ln.lower().startswith("hostname ")), h)
            try:
                for ai in so.getaddrinfo(hn_x, None):
                    excl_ips.add(ai[4][0])
            except Exception:
                pass
            excl_words.append(h.lower())
            excl_rx = word_rx(excl_words)
    blocked = {path for path, pid, ips, cmd in found if (set(ips) & excl_ips) or excl_rx.search(os.path.basename(path)) or excl_rx.search(cmd)
               or never_rx.search(os.path.basename(path))}
    found = [x for x in found if x[0] not in blocked]
    masters = {path: ips for path, pid, ips, cmd in found}
    cmds = {path: cmd for path, pid, ips, cmd in found}
    if blocked:
        sys.stderr.write("discover: %d connection(s) to excluded hosts (NEVER_CONTACT/EXCLUDE_HOSTS) ignored\n" % len(blocked))
    sys.stderr.write("discover: %d live ssh shared connection(s)\n" % len(masters))
    rev_cache = {}

    def rev(ip):
        if ip not in rev_cache:
            try:
                rev_cache[ip] = so.gethostbyaddr(ip)[0].lower()
            except Exception:
                rev_cache[ip] = ""
        return rev_cache[ip]

    for sk, ips in masters.items():
        sys.stderr.write("  %s -> %s\n" % (sk, ", ".join("%s (%s)" % (ip, rev(ip) or "?") for ip in ips) or "no TCP peer"))
    for a in aliases:
        try:
            g = subprocess.run(["ssh", "-G", a], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=5).stdout
        except Exception:
            g = ""
        kv = {}
        for line in g.splitlines():
            k, _, v = line.partition(" ")
            kv.setdefault(k.lower(), v.strip())
        hn = kv.get("hostname", a)
        cfg_cp = os.path.expanduser(kv.get("controlpath", "none"))
        if cfg_cp != "none" and "%" not in cfg_cp and cfg_cp in masters:
            sys.stderr.write("  %s: its ssh config ControlPath is live\n" % a)
            continue
        ips = set()
        try:
            for ai in so.getaddrinfo(hn, None):
                ips.add(ai[4][0])
        except Exception:
            pass
        if excl_rx.search(a) or excl_rx.search(hn) or (ips & excl_ips):
            sys.stderr.write("  %s: excluded host, never used\n" % a)
            continue
        hit = next((sk for sk, pi in masters.items() if ips & set(pi)), None)
        if not hit:  # e.g. a node reached with ProxyJump: no direct TCP peer, but its name is in the socket path or command
            first = hn.lower().split(".")[0]
            names = {a.lower(), hn.lower()} | ({first} if not first.isdigit() else set())
            names = {n for n in names if len(n) >= 3}
            rx = re.compile(r"(?<![A-Za-z0-9])(%s)(?![A-Za-z0-9])" % "|".join(re.escape(n) for n in names)) if names else None
            if rx:
                hit = next((sk for sk in masters if rx.search(os.path.basename(sk).lower()) or rx.search(cmds.get(sk, "").lower())), None)
        sys.stderr.write("  %s (%s): %s\n" % (a, hn, ("using " + hit) if hit else "no live shared connection found"))
        if hit:
            print("%s=%s" % (a, hit))


# ---------------------------------------------------------------- CLI
def cmd_summary():
    ov = rjson(os.path.join(STATE, "snapshot.json"))
    if not ov or "goals" not in ov:
        print("No snapshot yet. Run: mctl run")
        return
    for g in ov["goals"]:
        state = "node unreachable" if g["unreachable"] else ("session ended" if g["ended"] else
                                                               ("%s, agent %s" % (g["status"], "running" if g["running"] else "NOT running")))
        print("%s  (%s on %s)" % (trunc(g["title"], 70), g["key"], g["node"]))
        print("  %s \u00b7 last activity %s%s%s" % (state, ago(g["last"]), (" \u00b7 plan %d/%d" % (g["plan_done"], g["plan_total"])) if g["plan_total"] else "",
                                               (" \u00b7 criteria %d/%d met" % (g["crit_met"], g["crit_total"])) if g["crit_total"] else ""))
    for r in ov["resources"]:
        print("  %-9s %s" % (r["name"], ("%dR %dPD, %d cores" % (r["running"], r["pending"], r["cores"])) if r["ok"] else "unreachable"))
    c = ov["compute"]
    if c["waste_pct"] is not None:
        print("  48 h compute %.0f core-h, %d%% idle or failed" % (c["alloc_ch"], c["waste_pct"]))
    for f in ov["flags"]:
        if f["sev"] != "info":
            print("  [%s] %s" % (f["sev"].upper(), f["title"]))
    print("  data collected %s" % ago(ov.get("collected")))


def cmd_goal_summary(s):
    g, p, c = s["goal"], s["plan"], s["compute"]
    print("%s" % s["title"])
    print("  goal %s \u00b7 agent %s \u00b7 last activity %s" % (g["status"], "running" if g.get("codex_running") else "NOT running", ago(g.get("last_activity"))))
    if p["steps"]:
        print("  plan %d/%d%s" % (p["done"], len(p["steps"]), ("  now: " + trunc(p["now"], 70)) if p["now"] else ""))
    for r in s["resources"]:
        print("  %-9s %s" % (r["name"], ("%dR %dPD, %d cores" % (r["running"], r["pending"], r["cores"])) if r["ok"] else "unreachable"))
    if c["waste_pct"] is not None:
        print("  48 h compute %.0f core-h, %d%% idle or failed" % (c["alloc_ch"], c["waste_pct"]))
    for r in s.get("criteria", []):
        print("  criterion %-9s %s%s" % (r["status"], trunc(r["text"], 60), (" (latest %s)" % r["latest"]) if r.get("latest") else ""))
    for f in s["flags"]:
        if f["sev"] != "info":
            print("  [%s] %s" % (f["sev"].upper(), f["title"]))
    print("  data collected %s" % ago(s.get("collected")))


def cmd_referee_bundle(goal_arg=None):
    gg = resolve_goal(goal_arg)
    s = rjson(os.path.join(STATE, "goals", gg["key"] + ".json"), {}) or {}
    use_contract(gg["key"], gg["name"])
    c = parse_contract()
    lines = ["# Referee bundle (%s)" % time.strftime("%Y-%m-%d %H:%M"), "", "## Contract", c["raw"], "",
             "## Your instructions (newest first)"]
    lines += ["- %s %s: %s" % (u["id"], u.get("ts"), trunc(u["text"], 600)) for u in s.get("instructions", [])[:20]]
    lines += ["", "## Agent plan"] + ["- [%s] %s" % (x.get("status"), x.get("step")) for x in s.get("plan", {}).get("steps", [])]
    lines += ["", "## Watched parameter values in recent input files"]
    for row in s.get("params", {}).get("table", []):
        lines.append("- %s (%s): %s" % (row["name"], row["rule"], ", ".join("%s x%d" % (v["v"], v["n"]) for v in row["values"])))
    lines += ["", "## Parameter changes in agent edits"] + ["- %s %s: %s %s -> %s in %s" % (x["ts"], x["who"], x["param"], x["old"], x["new"], x["file"]) for x in s.get("changes", [])]
    lines += ["", "## Recent agent actions"] + ["- %s %s [%s] %s" % (hm(t.get("epoch")), t.get("who"), t.get("kind"), trunc(t.get("text"), 300)) for t in s.get("timeline", [])[:30]]
    lines += ["", "## Agent's latest words"] + [trunc(a.get("text"), 1500) for a in s.get("agent_words", [])]
    lines += ["", "## Acceptance criteria (judge the ones marked referee yourself, from the evidence)"]
    lines += ["- [%s] %s%s" % (r["status"], r["text"], (" | latest %s, best %s, %s" % (r.get("latest"), r.get("best"), r.get("source"))) if r.get("latest") else "")
              for r in s.get("criteria", [])]
    lines += ["", "## Measured results (newest last)"] + ["- %s %s %s: %s" % (hm(m.get("epoch")), m.get("src"), trunc(m.get("cmd") or m.get("file") or "", 120),
                                                                         ", ".join("%s=%.4g" % kv for kv in (m.get("metrics") or {}).items())) for m in s.get("measured", [])[-15:]]
    lines += ["", "## Current flags"] + ["- [%s] %s: %s" % (f["sev"], f["title"], f["detail"]) for f in s.get("flags", [])]
    os.makedirs(os.path.join(STATE, "referee"), exist_ok=True)
    path = os.path.join(STATE, "referee", gg["key"] + "_bundle.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("%s\t%s" % (gg["key"], path))


def cmd_sections():
    print("Blocks you can hide (mctl hide NAME [NAME...], on every machine):")
    for n, l in SECTIONS:
        print("  %-13s %s%s" % (n, l, "   [hidden]" if n in CFG["hide"] else ""))
    snap = rjson(os.path.join(STATE, "snapshot.json"), {}) or {}
    for g in snap.get("goals", []):
        n = "s:" + g["key"]
        print("  %-13s session card %s on the overview%s" % (n, g["name"], "   [hidden]" if n in CFG["hide"] else ""))
    hosts = sorted(set(g["node"] for g in snap.get("goals", [])))
    for h in hosts:
        n = "host:" + h
        print("  %-13s every session on %s%s" % (n, h, "   [hidden]" if n in CFG["hide"] else ""))
    print("Or hover over a block in the browser and click its x (that browser only).")


def main():
    a = sys.argv[1:]
    goal_arg, a = pop_goal_arg(a)
    if not a or a[0] == "analyze":
        s = analyze()
        r = sum(1 for f in s["flags"] if f["sev"] == "red")
        m = sum(1 for f in s["flags"] if f["sev"] == "amber")
        print("dashboard updated: %d goal(s), %d red, %d amber" % (len(s["goals"]), r, m))
    elif a[0] == "summary":
        if goal_arg:
            gg = resolve_goal(goal_arg)
            cmd_goal_summary(rjson(os.path.join(STATE, "goals", gg["key"] + ".json"), {}))
        else:
            cmd_summary()
    elif a[0] == "sections":
        cmd_sections()
    elif a[0] == "send-target":
        gg = resolve_goal(a[1] if len(a) > 1 else goal_arg)
        snap = rjson(os.path.join(STATE, "goals", gg["key"] + ".json"), {}) or {}
        tm = snap.get("tmux") or {}
        target = tm.get("target") or gg.get("target") or gg["name"]
        panes = [p for p in tm.get("panes") or [] if not p.get("dead")]
        pick = (next((p for p in panes if re.search(r"codex|claude|node", p.get("cmd") or "", re.I)), None)
                or next((p for p in panes if p.get("active")), None) or (panes[0] if panes else None))
        spec = "=%s:%s" % (target, pick["id"]) if pick and pick.get("id") else "=%s:" % target
        print("\t".join([gg["node"], spec, gg["key"], "1" if tm.get("notmux") else "0"]))
    elif a[0] == "goals":
        for g in known_goals():
            print("%-28s %-10s %s" % (g["key"], g["node"], "node unreachable" if g["unreachable"] else ("ended" if g["ended"] else g["status"])))
    elif a[0] == "goal-info":
        gg = resolve_goal(a[1] if len(a) > 1 else goal_arg)
        print("%s\t%s\t%s" % (gg["node"], gg.get("target") or gg["name"], gg["key"]))
    elif a[0] == "digest-config":
        cmd_digest_config("--local" in a, a[a.index("--login") + 1] if "--login" in a and a.index("--login") + 1 < len(a) else None)
    elif a[0] == "amend":
        if len(a) < 2:
            sys.exit("usage: mctl amend [--goal NAME] \"text\"")
        gg = resolve_goal(goal_arg)
        use_contract(gg["key"], gg["name"])
        cmd_amend(" ".join(a[1:]))
    elif a[0] == "promote":
        if len(a) < 2:
            sys.exit("usage: mctl promote U1a2b3c")
        cmd_promote(a[1])
    elif a[0] == "referee-bundle":
        cmd_referee_bundle(goal_arg)
    elif a[0] == "discover":
        cmd_discover(a[1:])
    elif a[0] == "contract":
        if goal_arg or len(known_goals()) == 1:
            gg = resolve_goal(goal_arg)
            use_contract(gg["key"], gg["name"])
            print(CONTRACT)
        else:
            for p in contract_files():
                print(p)
    else:
        sys.exit("unknown command: %s" % a[0])


if __name__ == "__main__":
    main()

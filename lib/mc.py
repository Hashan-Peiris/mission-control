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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import question as QN  # noqa: E402  (finds a question or approval prompt on an agent's screen)
import contract as CT  # noqa: E402  (keeps each goal's contract in step with the goal; automatic changes only tighten)
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
CSTATE_DIR = os.path.join(MC_DIR, "state", "contract")   # per goal: decisions, values being learned, recent automatic changes
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
    # tagged auto: a guess (every host mission control watches) that the goal's own words may narrow
    return "- allowed: %s\n%s" % ((", ".join(have) + "   # auto: every watched host; narrow it if this goal uses fewer") if have else
                                   "(clusters and nodes this goal may use)",
                                   ("- note: %s are never polled by mission control.\n" % " and ".join(w.capitalize() for w in NEVER)) if NEVER else "")
SEV_ORDER = {"red": 0, "amber": 1, "info": 2, "ok": 4}
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


def dur(secs):
    """A length of time in words: 40 s, 12 min, 2.5 h, 1.5 d."""
    s = max(0, float(secs or 0))
    return "%d s" % s if s < 90 else ("%d min" % (s / 60) if s < 5400 else ("%.1f h" % (s / 3600) if s < 172800 else "%.1f d" % (s / 86400)))


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
    old_p = os.path.join(CONTRACT_DIR, "old", key + ".md")
    if not os.path.exists(p) and os.path.exists(old_p):   # tidied away while the goal was gone: bring it back
        os.replace(old_p, p)
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


def norm_pv(v):
    """A setting's value as VASP reads it: no quotes or code punctuation, any case, .TRUE./T/True alike, 0.10 = 0.1."""
    x = str(v).strip().strip("\"'`()[]{},;:\\")
    if re.fullmatch(r"\.?(t|true)\.?", x, re.I):
        return "TRUE"
    if re.fullmatch(r"\.?(f|false)\.?", x, re.I):
        return "FALSE"
    n = num(x)
    return "%g" % n if n is not None else x.upper()


def pv_equal(a, b):
    a, b = norm_pv(a), norm_pv(b)
    if a == b:
        return True
    # VASP reads text settings by their first letters (PREC = Acc is Accurate), and long lines get cut off
    return len(a) >= 3 and len(b) >= 3 and a.isalpha() and b.isalpha() and (a.startswith(b) or b.startswith(a))


def rule_violated(rule, value):
    op, ref = rule.get("op"), rule.get("val", "")
    if not op:
        return False
    if op == "in":
        return not any(pv_equal(value, x) for x in ref.split("|") if x.strip())
    a, b = num(norm_pv(value)), num(norm_pv(ref))
    if a is None or b is None:
        return op == "==" and not pv_equal(value, ref)
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


DONE_RX = re.compile(r"(?i)\b(?:goal|criteri\w*|targets?|thresholds?|requirements?)\s+(?:is|are|was|were|has been|have been|now|all)\s+"
                     r"(?:all\s+|fully\s+|now\s+)?(?:met|achieved|reached|satisfied|fulfilled|complete[d]?|passed)\b|"
                     r"\b(?:meets|satisfies|is below|is under) the (?:target|threshold|criteri\w*)\b|"
                     r"\bgoal (?:is )?(?:complete|done|accomplished)\b|\ball (?:acceptance )?criteria (?:are )?(?:met|satisfied|pass(?:ed)?)\b")
COND_RX = re.compile(r"(?i)\b(?:once|when|whenever|until|unless|if|after|before|whether|so that|ensure|make sure|check|verify|confirm)\b[^.!?]{0,70}$")


def is_done_claim(text):
    """The agent stating the goal or a target is met, not "once the requirements are satisfied" or "check that ..."."""
    text = text or ""
    return any(not COND_RX.search(text[max(0, m.start() - 80): m.start()]) for m in DONE_RX.finditer(text))


ML_ERR_RX = re.compile(r"\b(rmse|mae|errors?|accuracy|accurate|tests?|tested|testing|validation|validate|held[- ]out|deviation|dp test|"
                       r"predict\w*|model|potential)\b", re.I)


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
        if not ML_ERR_RX.search(text[max(0, seg_start - 80): n.end()]) or re.search(r"\bmax(?:imum)?\b", low):
            continue   # no error, test or model nearby (e.g. a DFT force convergence), or a maximum error (dp test gives MAE/RMSE): referee
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
def cmd_digest_config(local=False, login=None, probe=False):
    watch, customs = [], []
    for p in contract_files() or [None]:
        c = parse_contract(p) if p else parse_contract(os.path.join(CONTRACT_DIR, "_none_.md"))
        for w in c["watch"]:
            if w["name"] not in watch:
                watch.append(w["name"])
        for cr in c["criteria"]:
            if cr.get("regex") and cr["label"] not in [x["label"] for x in customs]:
                customs.append({"label": cr["label"], "regex": cr["regex"]})
    for n in CT.SCAN_NAMES:   # every setting the known codes use, so values can be learned before anyone lists them
        if n not in watch:
            watch.append(n)
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
        cfg["probe"] = probe   # which agents, accounts and models this PC has (New session): only when publishing to other PCs
    sys.stdout.write(base64.b64encode(json.dumps(cfg).encode()).decode())


# ---------------------------------------------------------------- contract upkeep (see lib/contract.py)
def watched_hosts_text(shared):
    every = {}
    for h in (os.environ.get("HOSTS") or "").split():
        m = re.match(r"^([A-Za-z0-9._-]+)(?:@[^/\s]+)?(?:/(\d+))?$", h)
        if m and m.group(2):
            every[m.group(1)] = m.group(2)
    names = (["Rockfish"] if CFG["rf"] else []) + (["Skipjack"] if CFG["sj"] else [])
    names += [n + (" (every %s min)" % every[n] if n in every else "") for n in shared["nodes"]]
    return ", ".join(names)


def write_contract(raw):
    tmp = CONTRACT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(raw)
    os.replace(tmp, CONTRACT)


def contract_upkeep(gkey, goal_text, codex_goal, ndoc, shared):
    """Bring this goal's contract up to date from its own instructions, its run files and what mission control knows."""
    st = CT.load_state(CSTATE_DIR, gkey)
    st.setdefault("first_seen", NOW)
    raw = rd(CONTRACT, "") or ""
    specs = {}
    for c, files in ((ndoc or {}).get("specs") or {}).items():
        for name, sp in files.items():
            specs[name if name not in specs else "%s (%s)" % (name, os.path.basename(c.rstrip("/")))] = sp
    params = (ndoc or {}).get("params") or {}
    hosts = ["Rockfish", "Skipjack"] + list(shared["nodes"]) + [w.capitalize() for w in NEVER]
    ctx = {"goal_text": goal_text, "codex_goal": codex_goal, "specs": specs, "values": params.get("values") or {},
           "codes": params.get("codes") or {}, "hosts": [h for i, h in enumerate(hosts) if h.lower() not in [x.lower() for x in hosts[:i]]],
           "watched": watched_hosts_text(shared), "never": [w.capitalize() for w in NEVER], "requests": (ndoc or {}).get("requests") or [],
           "implicit": IMPLICIT_OPS, "varies": VARIES, "infer_criteria": infer_criteria, "now": NOW}
    try:
        new, notes = CT.upkeep(raw, ctx, st)
    except Exception as e:  # never let contract upkeep break the dashboard
        sys.stderr.write("contract upkeep %s: %s\n" % (gkey, e))
        return st, []
    if new != raw:
        write_contract(new)
    if notes:
        st["log"] = (st.get("log") or [])[-40:] + [{"t": NOW, "x": n} for n in notes]
    if (ndoc or {}).get("ok"):
        st["specs_seen"] = {n: sp.get("mtime") for n, sp in specs.items()}
        st["codes"] = ctx["codes"]
    CT.save_state(CSTATE_DIR, gkey, st)
    return st, notes


def prop_view(p, meta):
    """A waiting change, in parts for the page: the change, what it is for, why, and the agent's request id."""
    change = p["action"].split(" ", 1)[1] if " " in p["action"] else p["action"]
    why, scope, rid = meta.get("why"), meta.get("scope"), meta.get("agent") or ""
    if why is None:   # recorded before requests were kept in parts: read them back from the sentence
        m = re.match(r"^The agent asks \(([^)]*)\):\s*(.*?)(?:\s+for\s+(.*?))?\.\s+Why:\s*(.*)$", p["text"])
        if m:
            rid, scope, why = m.group(1), m.group(3) or "", m.group(4)
        else:
            return {"change": change, "text": p["text"]}
    return {"change": change, "scope": scope or "", "why": why or "", "rid": rid}


def clip_html(text, n=260):
    """Text as HTML; past about n characters the rest folds behind "more"."""
    text = str(text or "")
    if len(text) <= n + 60:
        return esc(text)
    cut = (text[:n].rsplit(" ", 1)[0] if " " in text[:n] else text[:n]).rstrip(" .,;:")
    return ('<details class="dd"><summary><span class="lead">%s\u2026</span><span class="full">%s</span> <span class="mtog"></span></summary></details>'
            % (esc(cut), esc(text)))


def prop_html(pv, gkey):
    o = []
    if pv.get("change"):
        o.append('<div class="pchg">%s</div>' % clip_html(pv["change"], 200))
    if pv.get("items"):
        lis = "".join('<li><span class="ptx">%s</span><span class="pbt">%s%s</span>%s</li>' % (
            clip_html(it["text"], 240),
            act_btn({"label": "Add", "op": "approve", "pid": it["pid"], "psig": it["psig"],
                     "confirm": "Add this acceptance criterion to the contract?\n\n%s\n\nThe agent is told." % trunc(it["text"], 600)}, "lnk"),
            act_btn({"label": "Reject", "op": "reject", "pid": it["pid"], "psig": it["psig"], "danger": True,
                     "confirm": "Reject this criterion?\n\n%s" % trunc(it["text"], 600)}, "lnk"),
            ('<div class="pmeta"><b>Why</b>%s</div>' % clip_html(it["why"], 160)) if it.get("why") and (
                k + 1 == len(pv["items"]) or pv["items"][k + 1].get("why") != it["why"]) else "") for k, it in enumerate(pv["items"]))
        o.append('<div data-ckey="%s"><ol class="plist">%s</ol><div class="actmsg"></div></div>' % (esc(gkey), lis))
    if pv.get("scope"):
        o.append('<div class="pmeta"><b>For</b>%s</div>' % clip_html(pv["scope"], 200))
    if pv.get("why"):
        o.append('<div class="pmeta"><b>Why</b>%s</div>' % clip_html(pv["why"], 220))
    if pv.get("text"):
        o.append('<div class="pmeta">%s</div>' % clip_html(pv["text"], 260))
    if pv.get("rid"):
        o.append('<div class="pmeta faint">%s %s from the agent</div>' % ("Requests" if "," in pv["rid"] else "Request", esc(pv["rid"])))
    return "".join(o)


def contract_flags(flags, contract, cst, res, comp, node, gnode, codex_goal):
    raw = contract["raw"]
    cp = CT.parse_compute(raw)
    groups = {}   # proposed acceptance criteria, one card per agent request
    for p in CT.proposals(raw):   # changes that would loosen a rule: yours to decide
        meta = (cst.get("proposals") or {}).get(p["id"]) or {}
        agent = bool(meta.get("agent")) or p["text"].startswith(("The agent asks", "The agent proposes"))
        psig = CT.sig(p["id"], p["action"])[:8]   # the page sends it back, so a stale page can't decide a different request
        if p["action"].lower().startswith("criterion "):
            groups.setdefault("all", []).append((p, meta, psig))
            continue
        pv = prop_view(p, meta)
        what = pv["change"]
        flags.add("prop:%s" % p["id"], "red" if agent else "amber", "goal",
                  "The agent asks to change a rule" if agent else "A change would loosen the contract", "",
                  "Allow it for this request only, change the rule for good, or reject it.", needs_you=True, prop=pv,
                  acts=[{"label": "Allow for this request", "op": "approve_once", "pid": p["id"], "psig": psig,
                         "confirm": "Allow %s for this request only?\n\nThe rule stays as it is; the approval is recorded as an amendment%s." % (
                             what, " and the agent is told" if agent else "")},
                        {"label": "Change the rule", "op": "approve", "pid": p["id"], "psig": psig,
                         "confirm": "Change the contract rule for good: %s?%s" % (what, "\n\nThe agent is told." if agent else "")},
                        {"label": "Reject", "op": "reject", "pid": p["id"], "psig": psig, "danger": True,
                         "confirm": "Reject %s (%s)?%s" % (p["id"], what, "\n\nThe agent is told." if agent else "")}])
    for _, lst in groups.items():   # every proposed criterion in one card, whichever request it came in
        n, pids, sigs = len(lst), ",".join(p["id"] for p, _, _ in lst), ",".join(x for _, _, x in lst)
        whys = {m.get("why") or "" for _, m, _ in lst}
        rids = sorted({m.get("agent") or "" for _, m, _ in lst} - {""})
        flags.add("prop:crit", "amber", "goal",
                  "The agent proposes %s" % ("an acceptance criterion" if n == 1 else "%d acceptance criteria" % n), "",
                  "Add %s to the contract, or reject %s." % (("it", "it") if n == 1 else ("them", "them")), needs_you=True,
                  prop={"rid": ", ".join(rids), "why": whys.pop() if len(whys) == 1 else "",
                        "items": [{"pid": p["id"], "text": p["action"][10:], "psig": x, "why": (m.get("why") or "") if len({mm.get("why") or "" for _, mm, _ in lst}) > 1 else ""}
                                  for p, m, x in lst]},
                  acts=[{"label": "Add all %d" % n if n > 1 else "Add it", "op": "approve", "pid": pids, "psig": sigs,
                         "confirm": "Add %s to the contract?\n\nThe agent is told." % ("these %d acceptance criteria" % n if n > 1 else "this acceptance criterion")},
                        {"label": "Reject all" if n > 1 else "Reject", "op": "reject", "pid": pids, "psig": sigs, "danger": True,
                         "confirm": "Reject %s?\n\nThe agent is told." % ("all %d proposed criteria" % n if n > 1 else "this criterion")}])
    allowed = [x.lower() for x in cp["allowed"]] if cp["allowed"] else None
    forbid = [x.split(" (")[0].strip().lower() for x in cp["forbid"]]
    for r in res:   # jobs this goal's agents submitted on a cluster the contract doesn't allow
        mine = [j for j in r.get("jobs") or [] if j.get("by") and j.get("state") in ("RUNNING", "PENDING")]
        nm = str(r.get("name") or "").lower()
        if mine and (nm in forbid or (allowed is not None and nm not in allowed)):
            flags.add("compute:notallowed:%s" % nm, "red", "compute", "Jobs on %s, which the contract doesn't allow" % r["name"],
                      "%d running or waiting: %s" % (len(mine), ", ".join(str(j.get("id")) for j in mine[:6])),
                      "Ask the agent to move them, or change the Compute section of the contract.")
    if cp["budget"] and cp["budget"][1].lower() in ("su", "core-h", "sus") and comp.get("alloc_ch"):
        frac = comp["alloc_ch"] / cp["budget"][0]
        if frac >= 0.8:
            flags.add("compute:budget", "red" if frac >= 1 else "amber", "compute",
                      "%.0f%% of the %g %s budget used in the last 48 h alone" % (frac * 100, cp["budget"][0], cp["budget"][1]),
                      "%.0f core-hours in 48 h (the dashboard sees 48 h of accounting)." % comp["alloc_ch"], "Check the spending with the agent.")
    cap = cp["cpu_cap"].get(str(gnode).lower())
    if cap and (node or {}).get("ncpu"):
        pct = (node.get("load") or [0])[0] / node["ncpu"] * 100
        if pct > cap:
            flags.add("compute:cap:%s" % gnode, "amber", "compute", "%s CPU at %.0f%%, over the %g%% cap in the contract" % (gnode, pct, cap),
                      "Load %.1f on %d cores." % ((node.get("load") or [0])[0], node["ncpu"]), "Ask the agent to ease off, or raise the cap.")
    gsec = "\n".join(l for _, l in CT.section_lines(raw, "Goal"))
    if codex_goal and contract["goal"] and "_(auto-filled from the Codex goal" not in gsec and \
            re.sub(r"\s+", " ", contract["goal"]).strip() != re.sub(r"\s+", " ", codex_goal).strip():
        flags.add("contract:goal-changed", "info", "integrity", "The Codex goal differs from the goal you wrote in the contract",
                  trunc(codex_goal, 220), "Keep yours, or use the Codex goal text.",
                  acts=[{"label": "Use the Codex goal", "op": "goal_sync", "confirm": "Replace the contract's goal text with the Codex goal?"}])
    if not contract["criteria"] and NOW - (cst.get("first_seen") or NOW) > 86400:
        flags.add("contract:nocrit", "amber", "integrity", "No acceptance criteria yet",
                  "Without them the checks can't tell finished from unfinished work.",
                  "Add one in the Scientific integrity card (e.g. force error <= 30 meV/A), or ask the agent to propose some.")


def cmd_contract_edit(key, req):
    """One change to a goal's contract from the dashboard (JSON on stdin). Prints {"ok", "message", "agent_msg"}."""
    def out(ok, msg, agent_msg=None):
        print(json.dumps({"ok": ok, "message": msg, "agent_msg": agent_msg}))
        return 0 if ok else 1
    g = next((x for x in known_goals() if x["key"] == key), None)
    if not g:
        return out(False, "No session called %s." % key)
    use_contract(key, g["name"])
    raw = rd(CONTRACT, "") or ""
    st = CT.load_state(CSTATE_DIR, key)
    snap = rjson(os.path.join(STATE, "goals", key + ".json"), {}) or {}
    op = str(req.get("op") or "")
    name = str(req.get("name") or "").upper().strip()
    if name and not re.match(r"^[A-Z_][A-Z0-9_]{0,40}$", name):
        return out(False, "That isn't a parameter name.")
    agent_msg, msg, was, bound = None, "", "", ""
    try:
        if op == "set":
            rop, val = str(req.get("rule_op") or "=="), str(req.get("val") or "").strip()
            if rop not in ("==", "<=", ">=", "<", ">", "in", "abs<=") or not val or "|" in val and rop != "in" or len(val) > 80 or "#" in val:
                return out(False, "Write the rule as NAME, a comparison and a value.")
            raw, msg = CT.apply_action(raw, "set %s %s %s" % (name, rop, val))
            raw, aid = CT.add_amendment(raw, "Rule set on the dashboard: %s" % msg)
        elif op == "allow":
            raw, msg = CT.apply_action(raw, "allow %s %s" % (name, str(req.get("value") or "").strip()))
            raw, aid = CT.add_amendment(raw, "Allowed on the dashboard: %s" % msg)
        elif op == "unlock":
            e = CT.watch_entries(raw).get(name)
            if not e or not e["op"]:
                return out(False, "%s has no rule." % name)
            raw = CT.set_watch(raw, name)
            was = "%s %s" % (e["op"], e["val"])
            st.setdefault("unlocked", []).append(name)
            raw, aid = CT.add_amendment(raw, "Unlocked %s on the dashboard (was %s %s)" % (name, e["op"], e["val"]))
            msg = "%s is unlocked (any change is still shown)" % name
        elif op == "unwatch":
            raw, msg = CT.apply_action(raw, "unwatch %s" % name)
            st.setdefault("unlocked", []).append(name)
        elif op == "keep":
            raw = CT.make_firm(raw, name)
            msg = "%s is now your own rule" % name
        elif op == "criterion":
            text = re.sub(r"\s+", " ", str(req.get("text") or "")).strip()
            if not text or len(text) > 300:
                return out(False, "Write the criterion in one line.")
            raw = CT.add_criterion(raw, text)
            msg = "Added the criterion"
        elif op == "uncriterion":
            raw = CT.remove_criterion(raw, str(req.get("text") or ""))
            st.setdefault("dropped_criteria", []).append(CT.crit_key(str(req.get("text") or "")))
            msg = "Removed the criterion"
        elif op == "amend":
            text = re.sub(r"\s+", " ", str(req.get("text") or "")).strip()
            if not text:
                return out(False, "Nothing to add.")
            raw, aid = CT.add_amendment(raw, trunc(text, 1200))
            msg = "Added to the contract as %s" % aid
        elif op == "bind":
            uid = str(req.get("uid") or "")
            u = next((x for x in snap.get("instructions", []) if x["id"].lower() == uid.lower()), None)
            if not u:
                return out(False, "That instruction isn't in the latest data any more.")
            raw, aid = CT.add_amendment(raw, "%s (from %s)" % (trunc(u["text"], 1200), u["id"]))
            bound = u["text"]
            msg = "Your instruction %s is now binding (%s)" % (u["id"], aid)
        elif op in ("ask", "asked", "ack", "unack"):
            # items on the page; these never touch the contract file. "ask" only writes the message: the server sends it
            # and then records it with "asked", so an ask that didn't go out is not shown as asked.
            flags = {f.get("id"): f for f in snap.get("flags") or [] if f.get("id")}
            ipath = os.path.join(STATE, "items", key + ".json")
            ist = rjson(ipath, {}) or {}
            if op in ("ask", "asked"):
                ids = [x for x in re.split(r"[,\s]+", str(req.get("items") or "")) if re.match(r"^I[0-9a-f]{5}$", x)]
                items = [flags[x] for x in ids if x in flags]
                if not items:
                    return out(False, "Those items aren't on the page any more.")
                q = re.sub(r"\s+", " ", str(req.get("text") or "")).strip()
                if op == "asked":
                    for f in items:
                        ist.setdefault("asks", {})[f["id"]] = {"key": f["key"], "t": time.time(), "q": q}
                    ist["asks"] = dict(sorted(ist["asks"].items(), key=lambda kv: kv[1].get("t", 0))[-200:])
                    items_save(ipath, ist)
                    return out(True, "Recorded.")
                lines = ["- %s \u00b7 %s%s" % (f["id"], f["title"], (" \u00b7 " + trunc(f.get("detail") or "", 260)) if f.get("detail") else "") for f in items[:25]]
                agent_msg = ("[mission control] The user asks about %s on the dashboard:\n%s\n\n%s\n\nAnswer each on its own line, starting with "
                             "its id: \"%s: fixed | intended | explained | working | need_decision - short reason\". If one needs a contract "
                             "change, also file a request in .mc/requests.jsonl (param, value, why, scope; or criterion and why to propose "
                             "an acceptance criterion).") % (
                    "this item" if len(items) == 1 else "these %d items" % len(items), "\n".join(lines),
                    "Question: " + q if q else "Question: is this intended? If it is a mistake, fix it; if it needs a contract change, ask for it.",
                    items[0]["id"])
                zz = flags.get(item_id("goal:sleeping"))
                return out(True, "Asked the agent about %s; its answer shows under %s when it replies%s." % (
                    "this item" if len(items) == 1 else "%d items" % len(items), "it" if len(items) == 1 else "each one",
                    (" (%s, so it answers then)" % zz["title"].split(",")[0].replace("Agent is", "it is")) if zz else ""), agent_msg)
            else:
                f = flags.get(str(req.get("item") or ""))
                if not f:
                    return out(False, "That item isn't on the page any more.")
                if op == "ack":
                    if f["key"].startswith(NO_ACK):
                        return out(False, "The agent waits for your decision on this one: approve or reject it.")
                    ist.setdefault("acks", {})[f["key"]] = {"sig": item_sig(f), "note": trunc(str(req.get("text") or "").strip(), 300), "t": time.time()}
                    msg = "Dismissed. It stays hidden until it changes (7 days at most), under Dismissed in its section"
                else:
                    (ist.get("acks") or {}).pop(f["key"], None)
                    msg = "Brought back"
            items_save(ipath, ist)
            return out(True, msg + ".")
        elif op in ("probe", "probed", "remind", "reminded", "pin", "pinned"):   # memory: ask what it remembers, remind it, pin the rules
            mem = snap.get("memory") or {}
            if op == "probe":
                return out(True, "Asked the agent what it remembers; its answer is scored on the Memory card.",
                           "[mission control] Memory check from the user. From what you remember (don't open files for this), reply "
                           "with one line per item, starting each line with its label:\nGOAL: the goal in one line\nRULE: each setting "
                           "you must keep, with its value\nLIMIT: each compute limit (clusters, budget)\nCRITERION: each acceptance "
                           "criterion\nINSTRUCTION: each standing instruction from the user\nNEXT: your next three steps\nThen carry on "
                           "with your work.")
            if op == "remind":
                full = mem.get("items_full") or []
                if not full:
                    return out(False, "The contract has no rules or limits to remind it of yet.")
                comps = [c for c in mem.get("comps") or [] if c.get("who") in (None, "", "main")]
                last, pr = (comps[-1] if comps else {}), (mem.get("probe") or {})
                miss = (pr.get("hard_missing") if pr.get("answered") and pr.get("at", 0) > (last.get("e") or 0) else last.get("hard_missing")) or []
                text = "[mission control] Reminder from the user: these still apply to this goal.\n" + "\n".join("- " + x for x in full)
                if miss:
                    text += "\n\nYour last summary or memory check left out: %s." % "; ".join(miss)
                return out(True, "Sent the agent its contract items%s." % ((" (%d it had left out)" % len(miss)) if miss else ""),
                           text + "\nKeep them in your working notes, and carry on.")
            if op == "pin":
                comps = [c for c in mem.get("comps") or [] if c.get("who") in (None, "", "main")]
                want = (comps[-1].get("unseen") if comps else None) or [x for x in mem.get("items_full") or []]
                if not want:
                    return out(False, "Nothing to pin: the contract has no rules or limits yet.")
                return out(True, "Asked the agent to pin %d contract item%s in AGENTS.md." % (len(want), "" if len(want) == 1 else "s"),
                           "[mission control] Request from the user: add these contract items to AGENTS.md in your working folder, in a "
                           "short \"Contract (from mission control)\" section, keeping everything else in the file as it is, so they stay in "
                           "your context after every compaction:\n" + "\n".join("- " + x for x in want) + "\nThen carry on with your work.")
            ms = mem_state(key)
            k = {"probed": "probes", "reminded": "reminds", "pinned": "pins"}[op]
            ms[k] = (ms.get(k) or [])[-9:] + [time.time()]
            mem_save(key, ms)
            return out(True, "Recorded.")
        elif op.startswith("term_"):   # words on the Terms card; never touch the contract file
            tst, known = terms_state(key), terms_known()
            rep = snap.get("terms") or {}
            rows = {r["id"]: r for r in (rep.get("terms") or []) + (rep.get("sugg") or [])}
            text = re.sub(r"\s+", " ", str(req.get("text") or "")).strip().strip("`*\"“”.,;:!?")
            tids = [x for x in re.split(r"[,\s]+", str(req.get("items") or req.get("item") or "")) if re.match(r"^T[0-9a-f]{5}$", x)]
            watch = tst.setdefault("watch", {})
            if text and op in ("term_add", "term_ask", "term_asked", "term_hide"):
                if len(text) < 2 or len(text) > 60:
                    return out(False, "Pick a word or a short phrase (up to 60 characters).")
                n = term_norm(text)
                if op != "term_hide":
                    watch.setdefault(n, {"term": text, "t": time.time()})
                    known.discard(n)
                tids.append(term_id(key, n))
                rows.setdefault(term_id(key, n), {"id": term_id(key, n), "n": n, "term": text, "meaning": ""})
            for t in tids:   # a suggestion picked from the card becomes a word to ask about
                r = rows.get(t)
                if r and op in ("term_add", "term_ask") and not r.get("meaning"):
                    watch.setdefault(r["n"], {"term": r["term"], "t": time.time()})
            pick = [rows[t] for t in tids if t in rows]
            if op == "term_add":
                terms_save(key, tst)
                terms_known_save(known)
                nw = sum(1 for n in watch if not any(r.get("n") == n and r.get("meaning") for r in rows.values()))
                return out(True, "Saved “%s”. %d word%s wait for a meaning: Ask agent on the Terms card asks about them together." % (
                    pick[0]["term"] if pick else text, nw, "" if nw == 1 else "s"))
            if op in ("term_ask", "term_asked"):
                want = {r["n"]: r for r in pick}
                for n, w in watch.items():   # and every other word still waiting, so the agent is asked once
                    r = next((x for x in rows.values() if x.get("n") == n), None)
                    if not (r and r.get("meaning")) and n not in known:
                        want.setdefault(n, r or {"id": term_id(key, n), "n": n, "term": w.get("term") or n})
                want = list(want.values())[:25]
                if not want:
                    return out(False, "No words are waiting for a meaning. Select a word in the chat, or pick one on the Terms card.")
                if op == "term_asked":
                    tst.setdefault("asked", {}).update({r["n"]: time.time() for r in want})
                    terms_save(key, tst)
                    return out(True, "Recorded.")
                terms_save(key, tst)
                terms_known_save(known)
                lines = "\n".join("- %s · “%s”%s" % (r["id"], r["term"], (" (it was recorded as: %s)" % trunc(r["meaning"], 200)) if r.get("meaning") else "")
                                  for r in want)
                return out(True, "Asked the agent about %d word%s; each answer shows on the Terms card when it replies." % (len(want), "" if len(want) == 1 else "s"),
                           "[mission control] The user asks what %s mean%s in this project:\n%s\n\nAnswer each on its own line, starting with its id: "
                           "\"%s: what it means, in plain words\". Also add each one to .mc/glossary.jsonl in your working folder, one JSON line per "
                           "word: {\"term\": \"...\", \"meaning\": \"...\", \"by\": \"main\" (or your sub-agent name), \"ts\": \"<ISO time>\"}. If a "
                           "meaning changed during the project, say what it meant before and when it changed. Then carry on with your work." % (
                               "this word" if len(want) == 1 else "these words", "s" if len(want) == 1 else "", lines, want[0]["id"]))
            r = pick[0] if pick else None
            if op in ("term_question", "term_questioned", "term_same", "term_hide", "term_unhide", "term_accept", "term_keep") and not r:
                return out(False, "That word isn't on the page any more.")
            if op == "term_question":
                bits = []
                if r.get("pending"):
                    bits.append("Pinned meaning (the contract uses this word): %s\nNewer meaning (%s, %s): %s" % (r["pinned"], r["pending"]["by"], hm(r["pending"]["t"]), r["pending"]["meaning"]))
                elif r.get("changed"):
                    c = r["changed"]
                    bits.append("Earlier meaning (%s, %s): %s\nNewer meaning (%s, %s): %s" % (c["by0"], hm(c["t0"]), c["from"], c["by"], hm(c["t"]), r["meaning"]))
                if r.get("differ"):
                    bits.append("Different meanings recorded:\n" + "\n".join("- %s: %s" % (d["by"], d["meaning"]) for d in r["differ"]))
                if r.get("shift"):
                    sh = r["shift"]
                    bits.append("It is used differently lately.\nThen (%s, %s): “%s”\nNow (%s, %s): “%s”" % (
                        sh["then"]["who"], hm(sh["then"]["t"]), sh["then"]["text"], sh["now"]["who"], hm(sh["now"]["t"]), sh["now"]["text"]))
                if r.get("mainsub"):
                    ms = r["mainsub"]
                    bits.append("The main agent and a sub-agent use it in different settings.\nMain agent: “%s”\n%s: “%s”" % (
                        ms["main"]["text"], ms["sub"]["who"].capitalize(), ms["sub"]["text"]))
                if not bits and r.get("meaning"):
                    bits.append("Recorded meaning (%s): %s" % (r["by"], r["meaning"]))
                return out(True, "Asked the agent whether “%s” still means the same; its answer shows on the Terms card." % r["term"],
                           "[mission control] The user asks whether “%s” still means the same thing in this project.\n%s\n\nAnswer on one line, "
                           "starting with its id: \"%s: <what it means now>; same meaning as before\" or \"%s: <what it means now>; changed: <what "
                           "changed and why>\". If the main agent and a sub-agent mean different things, say which is right. If it changed, add the "
                           "new meaning with a \"why\" to .mc/glossary.jsonl. Then carry on with your work." % (r["term"], "\n\n".join(bits), r["id"], r["id"]))
            if op == "term_questioned":
                tst.setdefault("questioned", {})[r["n"]] = time.time()
                terms_save(key, tst)
                return out(True, "Recorded.")
            if op == "term_same":
                tst.setdefault("same", {})[r["n"]] = time.time()
                terms_save(key, tst)
                return out(True, "Noted: same meaning. The hint stays away for 30 days.")
            if op in ("term_hide", "term_unhide"):
                (known.add if op == "term_hide" else known.discard)(r["n"])
                terms_known_save(known)
                return out(True, "Hidden on every session's Terms card (“Hidden” brings it back)." if op == "term_hide" else "Brought back.")
            if op in ("term_accept", "term_keep"):
                p = r.get("pending")
                if not p or p.get("sig") != str(req.get("psig") or ""):
                    return out(False, "That meaning changed or was already decided since this page was made. The page refreshes in a moment; try again then.")
                if op == "term_accept":
                    tst.setdefault("pinned", {})[r["n"]] = {"meaning": p["meaning"], "t": time.time(), "by": "you"}
                    terms_save(key, tst)
                    return out(True, "The new meaning of “%s” is pinned." % r["term"])
                tst.setdefault("kept", {}).setdefault(r["n"], []).append(p["sig"])
                terms_save(key, tst)
                return out(True, "Kept the pinned meaning of “%s”." % r["term"],
                           "[mission control] The user keeps the pinned meaning of \u201c%s\u201d for this goal, because the contract relies on it.\n"
                           "Pinned meaning: %s\nYour newer meaning: %s\nPlease keep using \u201c%s\u201d in its pinned meaning, and give "
                           "the newer idea a word of its own. Add a line with the pinned meaning to .mc/glossary.jsonl. Then carry on with your "
                           "work." % (r["term"], r["pinned"], p["meaning"], r["term"]))
            if op in ("term_setup", "term_setup_sent"):
                if op == "term_setup_sent":
                    tst["setup"] = time.time()
                    terms_save(key, tst)
                    return out(True, "Recorded.")
                seed = [x["term"] for x in rep.get("sugg") or []][:12] + [r2["term"] for r2 in rep.get("terms") or [] if r2.get("waiting")][:12]
                return out(True, "Asked the agent to keep a word list; its words show on the Terms card as it adds them.",
                           "[mission control] Request from the user: keep a word list for this project, so the user can follow your reports.\n"
                           "1. When you coin a term or use an ordinary word in a special sense (a model label, a stage name, words like \"qualified\" "
                           "or \"sealed\"), add one JSON line to .mc/glossary.jsonl in your working folder: {\"term\": \"...\", \"meaning\": \"what it "
                           "means, in plain words, one or two sentences\", \"by\": \"main\" (or your sub-agent name), \"ts\": \"<ISO time>\"}.\n"
                           "2. If a term's meaning changes, add a new line with the new meaning and a \"why\" field. Never edit or delete old lines.\n"
                           "3. Define any new term in plain words the first time you use it in a message to the user.\n"
                           "4. Add rules 1 to 3 to AGENTS.md in your working folder, in a short \"Word list (from mission control)\" section, keeping "
                           "everything else as it is, so they survive compactions and reach your sub-agents.\n"
                           "5. Now add the terms you already use regularly (up to about 30)%s.\nThen carry on with your work." % (
                               (", including: " + ", ".join(seed)) if seed else ""))
            return out(False, "Unknown change.")
        elif op in ("approve", "reject", "approve_once"):
            pids = [x for x in re.split(r"[,\s]+", str(req.get("pid") or "")) if x][:40]
            sigs = [x for x in re.split(r"[,\s]+", str(req.get("psig") or "")) if x]
            if sigs:   # the page says which request it showed: refuse if that request changed since
                cur = {p["id"]: CT.sig(p["id"], p["action"])[:8] for p in CT.proposals(raw)}
                if len(sigs) != len(pids) or any(cur.get(i) != g for i, g in zip(pids, sigs)):
                    return out(False, "That request changed or was already decided since this page was made. The page refreshes in a moment; try again then.")
            msgs, told = [], []
            for pid in pids or [""]:
                raw, m1, info = CT.decide(raw, st, pid, op != "reject", once=op == "approve_once")
                msgs.append(m1)
                if info.get("agent"):
                    told.append("Your request %s was approved by the user: %s." % (info["agent"], m1) if op != "reject" else
                                "Your request %s (%s) was not approved.%s" % (info["agent"], info["action"].split(" ", 1)[1],
                                                                             "" if info["action"].lower().startswith("criterion") else
                                                                             " Keep to the contract as it is."))
            msg = msgs[0] if len(msgs) == 1 else "%s %d changes: %s" % ("Rejected" if op == "reject" else "Approved", len(msgs), "; ".join(msgs))
            if told:
                agent_msg = "[mission control] " + ("\n".join(told) if len(told) > 1 else told[0])
        elif op == "goal_sync":
            cg = (snap.get("ccard") or {}).get("codex_goal") or ""
            if not cg:
                return out(False, "No Codex goal text known for this session.")
            ls, secs = CT._split(raw)
            r = CT._range(secs, "goal", len(ls))
            if not r:
                return out(False, "The contract has no Goal section.")
            ls[r[0]:r[1]] = ["", cg, "", "_(auto-filled from the Codex goal on %s; kept in step with it)_" % CT.today(), ""]
            raw = "\n".join(ls)
            msg = "The goal text now follows the Codex goal"
        elif op == "tell":
            agent_msg = CT.protocol_text(raw, (snap.get("goal") or {}).get("cwd") or "")
            msg = "Told the agent how to ask"
        else:
            return out(False, "Unknown change.")
    except ValueError as e:
        return out(False, str(e))
    if op in ("set", "allow", "keep") and name in (st.get("unlocked") or []):
        st["unlocked"] = [x for x in st["unlocked"] if x != name]
    # Tell the agent what changed (you pressed the button), so it can plan with the new rule; CONTRACT_TELL_AGENT=0 turns it off
    if agent_msg is None and os.environ.get("CONTRACT_TELL_AGENT", "1") != "0":
        tell = {"set": "%s is now a rule (%s). Values outside it break the contract." % (name, msg),
                "allow": ("%s%s." % (msg[:1].upper(), msg[1:])) if msg else "",
                "unlock": "%s is no longer locked (it was %s). You may choose its value; say what you chose and why." % (name, was),
                "unwatch": "%s is no longer watched." % name,
                "criterion": "New acceptance criterion: %s" % str(req.get("text") or "").strip(),
                "uncriterion": "This acceptance criterion was removed: %s" % str(req.get("text") or "").strip(),
                "amend": "This is now part of the contract: %s" % str(req.get("text") or "").strip()[:1200],
                "bind": "This instruction of mine is now part of the contract: %s" % bound[:1200]}.get(op)
        if tell:
            agent_msg = "[mission control] Contract change from the user: " + tell
    write_contract(raw)
    st["log"] = (st.get("log") or [])[-40:] + [{"t": time.time(), "x": "you: " + msg}]
    CT.save_state(CSTATE_DIR, key, st)
    return out(True, msg + ".", agent_msg)


# ---------------------------------------------------------------- items on the page: ask the agent, its answers, your OK
# Every flag gets a short id (I3fa21). "Ask agent" sends the agent the items with their ids; it answers one line each
# ("I3fa21: intended - ...") in its reply or in .mc/answers.jsonl, and the answer shows under the item. "Dismiss" quiets an
# item you have judged fine until it changes. Each item also says what makes it go away by itself.
ANSWER_RX = re.compile(r"(?im)^\W{0,6}(I[0-9a-f]{5})\W{0,3}\s*[:\-\u2013\u2014]?\s*\**\s*(fixed|intended|explained|working|need[_ ]decision|won'?t fix|wontfix)\b"
                       r"\**\s*[:\-\u2013\u2014.,]?\s*(.*)$")
ANSWER_WORDS = {"fixed": "fixed it", "intended": "intended", "explained": "explained", "working": "working on it",
                "need_decision": "needs your decision", "wontfix": "won\u2019t change it"}
NO_ACK = ("prop:",)   # the agent's requests wait for your decision (Reject is how you turn one down)


def items_save(path, ist):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as fh:
        json.dump(ist, fh)
    os.replace(path + ".tmp", path)


def item_id(key):
    return "I" + hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:5]


def item_sig(f):   # what a dismissal holds for: the item and its kind of finding (counts may change); for a question, that question
    extra = ((f.get("q") or {}).get("sig") or f.get("detail") or "") if f["key"].startswith("pane:") else ""
    return hashlib.sha1((f["key"] + re.sub(r"\d+", "#", f["title"]) + extra).encode("utf-8", "replace")).hexdigest()[:10]


def clears_text(key, hours):
    k = key.split(":", 1)[0]
    return {
        "chg": "Goes away %g h after the edit, or once the value keeps to the contract (allow it, or the agent changes it back)." % hours,
        "param": "Goes away once no recent input file breaks the rule (allow the value, or the agent fixes the files).",
        "param-mixed": "Goes away when you lock the setting, or when the recent files agree.",
        "prop": "Goes away when you approve or reject it.",
        "pane": "Goes away when the question on the agent\u2019s screen is answered.",
        "pd": "Goes away when the jobs start or leave the queue.",
        "held": "Goes away when the jobs are released or cancelled.",
        "never": "Goes away when the jobs are cancelled or resubmitted.",
        "dup": "Goes away when one of the two jobs ends.",
        "waste": "Goes away when idle or failed time in the last %g h is back under the limit." % hours,
        "loweff": "Goes away when that job is older than %g h, or new jobs use their CPUs." % hours,
        "qf": "Goes away when the failures are older than %g h." % hours,
        "sc": "Goes away when the wording is older than %g h." % hours,
        "claim": "Goes away when a measured value backs the claim, or the claim is older than %g h." % hours,
        "crit": "Goes away when the latest test output meets the criterion.",
        "crit-noev": "Goes away when test output for it is found.",
        "contract": "Goes away when the contract has what it is missing.",
        "compute": "Goes away when the jobs, budget or load are back within the contract.",
        "node": "Goes away when the node is busy again.",
        "gpu": "Goes away when the GPU is busy again.",
        "storage": "Goes away 6 h after the spike.",
        "goal": "Goes away when the agent\u2019s state changes.",
        "term": "Goes away when you accept the new meaning or keep the pinned one.",
        "memory": "Goes away when a newer summary or memory check keeps every rule and limit, after a reminder, or 48 h after the compaction.",
        "drift": "Goes away when the agent finishes a plan step or does something else.",
        "agent": "Goes away when an agent runs in the session again.",
        "tmux": "Goes away when the tmux session is back.",
        "quota": "Goes away when the quota window resets.",
        "catchup": "Goes away when the older logs are read.",
        "stale": "Goes away when fresh data arrives.",
        "conn": "Goes away when the connection is back.",
    }.get(k, "Goes away when a collection no longer sees it.")


def item_answers(sessions, ndoc):
    out = {}

    def put(iid, status, note, t, src, extra=None):
        st = status.lower().replace(" ", "_").replace("\u2019", "").replace("'", "")
        st = "wontfix" if st in ("wont_fix", "wontfix") else st
        if st not in ANSWER_WORDS:
            return
        if iid not in out or t >= out[iid]["t"]:
            out[iid] = dict({"status": st, "note": trunc(note.strip().strip("|*` ").strip(), 400), "t": t, "src": src}, **(extra or {}))
    for s in sessions:
        for a in s.get("agent", []) or []:
            for m in ANSWER_RX.finditer(a.get("text") or ""):
                put(m.group(1), m.group(2), m.group(3), iso_epoch(a.get("ts")) or 0, "chat")
        for a in s.get("item_answers", []) or []:   # remote_digest keeps these from the full reply (long replies are shortened)
            put(str(a.get("id") or ""), str(a.get("status") or ""), str(a.get("note") or ""), iso_epoch(a.get("ts")) or 0, "chat")
    for r in (ndoc or {}).get("answers") or []:
        iid = str(r.get("item") or "")
        if re.match(r"^I[0-9a-f]{5}$", iid):
            put(iid, str(r.get("status") or ""), str(r.get("note") or ""), iso_epoch(r.get("ts")) or NOW, "file",
                {"param": r.get("param") or "", "value": r.get("value") or ""})
    return out


def items_decorate(flags, gkey, sessions, ndoc):
    path = os.path.join(STATE, "items", gkey + ".json")
    ist = rjson(path, {}) or {}
    answers = item_answers(sessions, ndoc)
    for f in flags.items:
        f["gkey"], f["id"], f["clears"] = gkey, item_id(f["key"]), clears_text(f["key"], CFG["hours"])
        if f["id"] in answers:
            f["answer"] = answers[f["id"]]
        ask = (ist.get("asks") or {}).get(f["id"])
        if ask and NOW - ask.get("t", 0) < 2 * 86400 and not (f.get("answer") and f["answer"]["t"] >= ask["t"] - 60):
            f["asked"] = ask["t"]
        ack = (ist.get("acks") or {}).get(f["key"])
        if ack and not f["key"].startswith(NO_ACK) and ack.get("sig") == item_sig(f) and NOW - ack.get("t", 0) < 7 * 86400:
            f["acked"] = {"note": ack.get("note", ""), "t": ack["t"], "was": f["sev"], "needs": bool(f.get("needs_you"))}
            f["sev"], f["needs_you"] = "ok", False
    return answers


def item_bar(f):
    """Under each item on a session page: the agent's answer, what makes it go away, Ask agent, Dismiss."""
    if not f.get("gkey") or not f.get("id"):
        return ""
    o = ['<div class="ibar" data-ckey="%s">' % esc(f["gkey"])]
    a = f.get("answer")
    if a:
        o.append('<div class="ans"><b>Agent</b> <span class="mut">%s</span> \u00b7 <span class="as-%s">%s</span>%s</div>' % (
            esc(hm(a["t"])), esc(a["status"]), esc(ANSWER_WORDS.get(a["status"], a["status"])), (" \u2014 " + esc(a["note"])) if a.get("note") else ""))
    elif f.get("asked"):
        o.append('<div class="ans mut">Asked the agent at %s; its answer shows here.</div>' % esc(hm(f["asked"])))
    if f.get("acked"):
        o.append('<div class="ans mut ackline">Dismissed by you %s%s</div>' % (esc(ago(f["acked"]["t"])), (" \u2014 " + esc(f["acked"]["note"])) if f["acked"].get("note") else ""))
    o.append('<div class="clr">%s</div><div class="ibtn">' % esc(f.get("clears") or ""))
    o.append('<button type="button" class="lnk" data-cop="ask" data-citems="%s">Ask agent</button>' % esc(f["id"]))
    if f.get("acked"):
        o.append('<button type="button" class="lnk" data-cop="unack" data-citem="%s">Bring back</button>' % esc(f["id"]))
    elif not f["key"].startswith(NO_ACK):
        o.append('<button type="button" class="lnk" data-cop="ack" data-citem="%s">%s</button>' % (
            esc(f["id"]), "Accept the answer" if a and a["status"] in ("intended", "explained", "wontfix") else "Dismiss"))
    o.append('<span class="iid" title="Item id, for asking about it">%s</span></div><div class="actmsg"></div></div>' % esc(f["id"]))
    return "".join(o)


def flag_list_html(items):
    """A section's items: open ones first, the ones you dismissed folded away below (Bring back undoes it)."""
    live = [f for f in items if not f.get("acked")]
    gone = [f for f in items if f.get("acked")]
    o = [flag_html(f) for f in live]
    if gone:
        if not live:
            o.append('<div class="empty nothing">Nothing open.</div>')
        o.append('<details class="fold dism"><summary>Dismissed (%d)</summary>%s</details>' % (len(gone), "".join(flag_html(f) for f in gone)))
    return "".join(o)


def ask_all_btn(items, gkey, label):
    ids = [f["id"] for f in items if f.get("id") and f.get("sev") in ("red", "amber")]
    if len(ids) < 2 or not gkey:
        return ""
    return '<span class="cacts askall" data-ckey="%s"><button type="button" class="lnk" data-cop="ask" data-citems="%s">%s (%d)</button><span class="actmsg"></span></span>' % (
        esc(gkey), esc(",".join(ids)), esc(label), len(ids))


# ---------------------------------------------------------------- storage of the working folder (scanned every ~30 min)
STEP_GB = 50          # milestones: every 50 GB the folder crosses, up or down
SPIKE_GB = 10         # warning: this much growth in 30 minutes
SPIKE_FILES = 50000   # ... or this many new files in 30 minutes (or the count doubling, from 2,000 files up)
GB = 1e9


def fmt_b(b):
    b = float(b or 0)
    for unit, d in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if abs(b) >= d:
            return "%.3g %s" % (b / d, unit)
    return "%d B" % b


def fmt_n(n):
    n = float(n or 0)
    return "%.3g M" % (n / 1e6) if n >= 1e6 else ("{:,.0f}".format(n))


def storage_update(gkey, scan, flags):
    """Keep the folder's size history, note each 50 GB crossed, and warn on sudden growth. Returns what the page shows."""
    path = os.path.join(STATE, "storage", gkey + ".json")
    h = rjson(path, {}) or {}
    if not scan or not scan.get("t"):
        return dict(h, scanning=bool((scan or {}).get("scanning_since"))) if h.get("samples") else None
    if h.get("root") != scan.get("root"):   # a different working folder: start a new history
        h = {"root": scan.get("root"), "samples": [], "events": [], "spikes": []}
    smp = h.setdefault("samples", [])
    if not smp or scan["t"] > smp[-1][0] + 1:
        prev = smp[-1] if smp else None
        smp.append([scan["t"], scan.get("bytes", 0), scan.get("files", 0), 1 if scan.get("partial") else 0])
        del smp[:-600]
        if prev and not scan.get("partial"):
            lo, hi = int(prev[1] // (STEP_GB * GB)), int(scan["bytes"] // (STEP_GB * GB))
            if hi != lo:
                h.setdefault("events", []).append([scan["t"], hi * STEP_GB if hi > lo else lo * STEP_GB, "up" if hi > lo else "down"])
                del h["events"][:-20]
            # compare with the sample about 30 min earlier (20 min to 3 h back), as a rate per 30 min
            base = next((x for x in reversed(smp[:-1]) if 20 * 60 <= scan["t"] - x[0] <= 3 * 3600 and not (x[3:] or [0])[0]), None)
            if base:
                dt = scan["t"] - base[0]
                db, dn = (scan["bytes"] - base[1]) * 1800.0 / dt, (scan["files"] - base[2]) * 1800.0 / dt
                grew_types = []
                for k, b, n in scan.get("types") or []:
                    old = next((x for x in h.get("types") or [] if x[0] == k), None)
                    if b - (old[1] if old else 0) > GB:
                        grew_types.append((b - (old[1] if old else 0), k))
                if db >= SPIKE_GB * GB or dn >= SPIKE_FILES or (base[2] >= 2000 and scan["files"] - base[2] >= base[2]):
                    h.setdefault("spikes", []).append({"t": scan["t"], "db": scan["bytes"] - base[1], "dn": scan["files"] - base[2], "mins": dt / 60.0,
                                                       "types": [[k, d] for d, k in sorted(grew_types, reverse=True)[:3]]})
                    del h["spikes"][:-10]
        h.update(types=scan.get("types") or [], biggest=scan.get("biggest") or [], partial=scan.get("partial"), secs=scan.get("secs"),
                 files=scan.get("files"), bytes=scan.get("bytes"), t=scan["t"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w") as f:
            json.dump(h, f)
        os.replace(path + ".tmp", path)
    sp = (h.get("spikes") or [])[-1:] or []
    if sp and NOW - sp[0]["t"] < 6 * 3600:   # a sudden spike stays on the page for 6 h
        x = sp[0]
        what = []
        if x["db"] >= SPIKE_GB * GB * x["mins"] / 30.0:
            what.append("grew %s" % fmt_b(x["db"]))
        if x["dn"] >= SPIKE_FILES * x["mins"] / 30.0 or x["dn"] > 0 and not what:
            what.append("gained %s files" % fmt_n(x["dn"]))
        flags.add("storage:spike", "amber", "compute", "The working folder %s in %.0f min" % (" and ".join(what) or "jumped", x["mins"]),
                  "At %s. Now %s in %s files%s." % (hm(x["t"]), fmt_b(h.get("bytes")), fmt_n(h.get("files")),
                                                     ("; mostly " + ", ".join("%s (+%s)" % (k, fmt_b(d)) for k, d in x["types"])) if x["types"] else ""),
                  "Check what the agent is writing (Resources, Working folder).")
    return dict(h, scanning=bool(scan.get("scanning_since")))


def storage_html(st):
    if not st or not st.get("samples"):
        return ""
    smp = st["samples"]
    day = [x for x in smp if smp[-1][0] - x[0] <= 48 * 3600]
    base = next((x for x in reversed(smp[:-1]) if 20 * 60 <= smp[-1][0] - x[0] <= 3 * 3600), None)
    bits = []
    if base:
        d = smp[-1][1] - base[1]
        bits.append("%s%s in %.0f min" % ("+" if d >= 0 else "\u2212", fmt_b(abs(d)), (smp[-1][0] - base[0]) / 60))
    ev = (st.get("events") or [])[-1:]
    if ev:
        bits.append("%s %d GB %s" % ("passed" if ev[0][2] == "up" else "fell below", ev[0][1], ago(ev[0][0])))
    bits.append("scanned %s%s" % (hm(st.get("t")), " (partial)" if st.get("partial") else ""))
    o = ['<div class="row" style="margin-top:10px"><span class="n">Working folder</span><span class="x">%s</span><span class="r">%s \u00b7 %s files</span></div>' % (
        spark([x[1] for x in day]), fmt_b(st.get("bytes")), fmt_n(st.get("files")))]
    tot = float(st.get("bytes") or 0) or 1
    rows = "".join('<tr><td class=mono>%s</td><td>%s</td><td class=mut>%.0f%%</td><td class=mut>%s files</td></tr>' % (
        esc(k), fmt_b(b), 100 * b / tot, fmt_n(n)) for k, b, n in (st.get("types") or [])[:8])
    root = (st.get("root") or "").rstrip("/") + "/"
    big = "".join('<li><span class=mono>%s</span> <span class=mut>%s</span></li>' % (esc(trunc(p[len(root):] if p.startswith(root) else p, 90)), fmt_b(b))
                  for p, b in (st.get("biggest") or [])[:4])
    evs = "".join('<li>%s %d GB <span class=mut>%s</span></li>' % ("Passed" if e[2] == "up" else "Fell below", e[1], esc(hm(e[0])))
                  for e in reversed((st.get("events") or [])[-6:]))
    o.append('<details class="stor"><summary class="small mut">%s</summary><table class="small" style="margin-top:6px"><tr><th>Type</th><th>Size</th><th></th><th></th></tr>%s</table>'
             '%s%s<div class="small mut" style="margin-top:6px">Folder <span class=mono>%s</span>. Checked every 30 min in the background; warns at %d GB '
             'or %s new files in 30 min and marks every %d GB.</div></details>' % (
                 esc(" \u00b7 ".join(bits)), rows,
                 ('<div class="small" style="margin-top:8px">Biggest files</div><ul class="clist small">%s</ul>' % big) if big else "",
                 ('<div class="small" style="margin-top:8px">%d GB steps</div><ul class="clist small">%s</ul>' % (STEP_GB, evs)) if evs else "",
                 esc(st.get("root") or ""), SPIKE_GB, "{:,}".format(SPIKE_FILES), STEP_GB))
    return "".join(o)


def tidy_contracts(goals):
    """Move contracts of goals gone for 3 days (and older copies from renamed sessions) to contract/old/."""
    keep = {g["key"] for g in goals}
    seen = rjson(os.path.join(STATE, "goals_seen.json"), {}) or {}
    keep |= {k for k, v in seen.items() if NOW - ((v or {}).get("last_seen") or 0) < 3 * 86400}
    for f in contract_files():
        k = os.path.basename(f)[:-3]
        if k in keep or k == "goal_contract" or NOW - os.path.getmtime(f) < 86400:
            continue
        os.makedirs(os.path.join(CONTRACT_DIR, "old"), exist_ok=True)
        os.replace(f, os.path.join(CONTRACT_DIR, "old", os.path.basename(f)))
        sys.stderr.write("contract: moved %s to contract/old/ (no such goal for 3 days)\n" % os.path.basename(f))


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

    def add(self, key, sev, area, title, detail="", action="", needs_you=False, q=None, acts=None, **extra):
        self.items = [f for f in self.items if f["key"] != key]   # one entry per item: the newest wins
        self.items.append(dict({"key": key, "sev": sev, "area": area, "title": title, "detail": detail,
                                "action": action, "needs_you": needs_you or sev == "red", "q": q, "acts": acts or None}, **extra))

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
        if s.get("kind") == "codex" and "subagent" not in src and "parent" not in src and not (s.get("meta") or {}).get("agent_role"):
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
    # Your other PCs (laptops that stand by) publish their own sessions to the shared folder
    pcdir = os.path.join(STATE, "pcs")
    try:
        for fn in sorted(os.listdir(pcdir)):
            mm = re.match(r"([a-z0-9][a-z0-9._-]{0,60})\.json$", fn)
            fp = os.path.join(pcdir, fn)
            if not mm or mm.group(1) == LOCAL_NAME or mm.group(1) in nodes or time.time() - os.path.getmtime(fp) > 1800:
                continue
            doc = rjson(fp, None)
            if doc and doc.get("ok", True) and doc.get("tmux"):   # only a PC with agent sessions shows up
                nodes[mm.group(1)] = {"doc": doc, "err": ""}
    except OSError:
        pass
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
           "train_inputs": [], "cached": True, "t": 0, "codes": {}}
    for p in parts:
        for k, n in (p.get("codes") or {}).items():
            out["codes"][k] = out["codes"].get(k, 0) + n
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
    specs = {c: v for c, v in (doc.get("specs") or {}).items() if c in cwds}
    stor = None
    for root, v in (doc.get("storage") or {}).items():   # the scanned folder that holds this goal's main working folder
        if cwds and (cwds[0] == root or cwds[0].startswith(root.rstrip("/") + "/")) and v:
            stor = v
            break
    reqs = [dict(r, cwd=c) for c, lst in (doc.get("requests") or {}).items() if c in cwds for r in lst]
    answers = [dict(r, cwd=c) for c, lst in (doc.get("answers") or {}).items() if c in cwds for r in lst]
    gloss = [dict(e, cwd=c) for c, v in (doc.get("glossary") or {}).items() if c in cwds for e in ((v or {}).get("entries") or [])]
    return {"glossary": gloss, "ok": doc.get("ok", False), "tmux": goal.get("tm") or {}, "sessions": sessions, "goal_db": doc.get("goal_db"),
            "node": doc.get("node"), "params": params, "decisions": dec, "git": git, "goal_cwds": cwds,
            "elapsed_s": doc.get("elapsed_s", 0), "specs": specs, "requests": reqs, "answers": answers, "storage": stor}


# ---------------------------------------------------------------- memory across compactions, and drift in long runs
# When an agent compacts, it carries on from a summary it wrote of the work so far. Each summary is checked against what
# should survive it (the contract's rules and limits, its criteria, your standing instructions); the hours before and after
# are compared (contract breaks, failed commands, repeated work, time back to real work); long runs are watched for hours
# without plan progress and for command loops. "Check what it remembers" asks the agent to list them from memory.
MEM_WORDS = {"ENCUT": ["cutoff", "cut-off", "energy cutoff", "plane-wave cutoff"], "EDIFF": ["electronic convergence"],
             "EDIFFG": ["force convergence", "ionic convergence"], "KSPACING": ["k-point spacing", "k-spacing"],
             "ISMEAR": ["smearing method"], "SIGMA": ["smearing width"], "PREC": ["precision"], "ISPIN": ["spin polarization"]}
MEM_STOP = set(("the a an and or not of to in on for with by is are be this that these those it its as at from into than then also only "
                "each every all any your you their our we they will must should can may use using used keep kept dont don't please "
                "make sure when what which there here have has had more most less very just").split())
CONSTRAINT_RX = re.compile(r"\b(?:standing (?:order|directive|instruction)|from now on|going forward|non-negotiable|must|mustn'?t|"
                           r"should never|may not|not allowed|no more than|at most|at least)\b"
                           r"|(?:^|[.!:\n]\s*)(?:please\s+|also\s+)?(?:don'?t|do not|never|always|only|avoid|make sure|use only|keep)\b", re.I)
PROBE_RX = re.compile(r"(?im)^\W{0,4}(GOAL|RULE|LIMIT|CRITERION|INSTRUCTION|NEXT)\s*:\s*(.+)$")


def _mwords(t):
    return {w for w in re.findall(r"[a-z][a-z0-9_+\-]{3,}", str(t).lower()) if w not in MEM_STOP}


def mem_items(contract, instr, goal_text):
    """What should survive a compaction."""
    items = []
    tags = {n: e for n, e in CT.watch_entries(contract["raw"]).items()}
    for w in contract["watch"]:   # rules from you or the goal's words; those learned from the run files live in the files
        if w["op"] and not w["name"].startswith("POT_") and "learned" not in str((tags.get(w["name"]) or {}).get("tag") or "").lower():
            items.append({"kind": "rule", "label": "%s %s %s" % (w["name"], w["op"], w["val"]), "name": w["name"], "op": w["op"], "val": w["val"]})
    cp = CT.parse_compute(contract["raw"])
    if cp["allowed"]:
        hs = [x.split(" (")[0].strip() for x in cp["allowed"]]
        items.append({"kind": "limit", "label": "only %s" % ", ".join(hs), "hosts": hs})
    for f in cp["forbid"]:
        items.append({"kind": "limit", "label": "not %s" % f, "hosts": [f.split(" (")[0].strip()]})
    if cp["budget"]:
        items.append({"kind": "limit", "label": "budget %s %s" % ("{:,.0f}".format(cp["budget"][0]), cp["budget"][1]), "num": cp["budget"][0]})
    for _, t in CT.criteria_texts(contract["raw"]):
        items.append({"kind": "criterion", "label": trunc(t, 80), "text": t})
    gw = _mwords(goal_text)
    seen = []
    for u in instr:   # newest first: standing instructions (constraints), not the goal itself
        t = u["text"].strip()
        if not (12 <= len(t) <= 700) or not CONSTRAINT_RX.search(t) or "?" in t:   # questions aren't standing instructions
            continue
        w = _mwords(t)
        if not w or (gw and len(w & gw) >= 0.8 * len(w)) or any(len(w & s) >= 0.8 * len(w) for s in seen):
            continue
        seen.append(w)
        items.append({"kind": "instruction", "label": trunc(t, 80), "text": t})
        if len(seen) >= 10:
            break
    for a in contract["amends"]:   # instructions you made binding
        if "(from U" in a["text"]:
            items.append({"kind": "instruction", "label": trunc(a["text"], 80), "text": a["text"]})
    return items


def mem_check(text, items):
    """kept / partial / missing for each item, as found in a summary (or a memory-check reply)."""
    low = str(text or "").lower()
    nums = []
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?\s*[kK]?\b|" + CT.NUM, low):
        tok = m.group(0).replace(",", "").strip()
        mult = 1000 if tok.lower().endswith("k") else 1
        x = num(tok.rstrip("kK").strip())
        if x is not None:
            nums.append(x * mult)
    words = _mwords(low)
    out = []
    for it in items:
        st = "missing"
        if it["kind"] == "rule":
            vals = it["val"].split("|") if it["op"] == "in" else [it["val"]]
            hit = False
            for nm in [it["name"]] + MEM_WORDS.get(it["name"], []):
                for m in re.finditer(r"(?<![a-z0-9_])%s(?![a-z0-9_])" % re.escape(nm.lower()), low):
                    hit = True
                    win = low[max(0, m.start() - 30): m.end() + 45]
                    for v in vals:
                        want = num(v)
                        if want is not None:
                            for mm in re.finditer(CT.NUM, win):
                                x = num(mm.group(0))
                                if x is not None and abs(x - want) <= 1e-9 * max(1.0, abs(want)):
                                    st = "kept"
                        elif v.strip().lower() and re.sub(r"^\.|\.$", "", v.strip().lower())[:6] in win:
                            st = "kept"
                    if st == "kept":
                        break
                if st == "kept":
                    break
            if st != "kept" and hit:
                st = "partial"
        elif it["kind"] == "limit":
            if it.get("hosts"):
                st = "kept" if all(re.search(r"\b%s\b" % re.escape(h.lower()), low) for h in it["hosts"]) else "missing"
            elif it.get("num"):
                st = "kept" if any(abs(x - it["num"]) < 0.5 for x in nums) else "missing"
        else:
            w = _mwords(it["text"])
            if w:
                frac = len(w & words) / float(len(w))
                st = "kept" if frac >= 0.5 else ("partial" if frac >= 0.3 else "missing")
        out.append(st)
    return out


def mem_state(gkey):
    return rjson(os.path.join(STATE, "memory", gkey + ".json"), {}) or {}


def mem_save(gkey, st):
    p = os.path.join(STATE, "memory", gkey + ".json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w") as f:
        json.dump(st, f)
    os.replace(p + ".tmp", p)


def _norm_cmd(c):
    return re.sub(r"\s+", " ", str(c or "")).strip()[:300]


def memory_report(gkey, flags, conv, main, fam, contract, instr, goal_text, changes, plan_done, plan_total):
    """The Memory card's data, and its flags."""
    ms = mem_state(gkey)
    items = mem_items(contract, instr, goal_text)
    sessions = [s for s in ([main] if main else []) + [x for x in fam if x is not main] if s]
    info, kept_texts = {}, {}
    for s in sessions:
        kept_texts.update(s.get("kept_texts") or {})
        for ci in s.get("compact_info") or []:
            e = iso_epoch(ci.get("ts"))
            if e:
                info[int(e)] = dict(ci, who=s.get("label") or "main")
    comps = []
    for m in conv:
        if m["r"] == "compact" and m["e"]:
            ci = next((v for k, v in info.items() if abs(k - m["e"]) < 120), {})
            comps.append({"e": m["e"], "who": m["w"] or "main", "text": m["x"], "pre": ci.get("pre"), "win": ci.get("win"),
                          "enc": ci.get("enc"), "kept_t": [kept_texts[k] for k in ci.get("kept") or [] if k in kept_texts]})
    for s in sessions:   # compactions without a readable summary (Codex's encrypted ones, or from before summaries were kept)
        for ts in s.get("compactions") or []:
            e = iso_epoch(ts)
            if e and not any(abs(c["e"] - e) < 120 for c in comps):
                ci = info.get(int(e)) or {}
                comps.append({"e": e, "who": s.get("label") or "main", "text": "", "pre": ci.get("pre"), "win": ci.get("win"),
                              "enc": ci.get("enc"), "kept_t": [kept_texts[k] for k in ci.get("kept") or [] if k in kept_texts]})
    comps.sort(key=lambda c: c["e"])
    mainc = [c for c in comps if c["who"] in (None, "", "main")]
    ev = sorted([dict(x, e=iso_epoch(x.get("ts")) or 0) for x in (main or {}).get("notable", []) if x.get("ts")], key=lambda x: x["e"])
    work = [x for x in ev if x.get("kind") in ("edit", "submit", "plan")]
    # every command the agent ran (time, kind, hash, exit), when the digest keeps it; else the notable ones
    cl = [{"e": c[0], "kind": c[1], "h": c[2], "exit": c[3]} for c in (main or {}).get("cmdlog") or [] if isinstance(c, list) and len(c) >= 4]
    if not cl:
        cl = [{"e": x["e"], "kind": x.get("kind"), "h": _norm_cmd(x.get("cmd")), "exit": x.get("exit")} for x in ev]
    runs = [c for c in cl if c["kind"] not in ("plan", "monitor", "edit")]
    first_cmd = min([x["e"] for x in cl] or [NOW])
    for i, c in enumerate(mainc):
        if c["text"] or c.get("kept_t"):
            res = mem_check(c["text"] + "\n" + "\n".join(c.get("kept_t") or []), items)   # what it carries on with
            c["kept"] = sum(1 for r in res if r == "kept")
            c["missing"] = [it["label"] for it, r in zip(items, res) if r == "missing"]
            c["partial"] = [it["label"] for it, r in zip(items, res) if r == "partial"]
            hard = [it["label"] for it, r in zip(items, res) if r != "kept" and it["kind"] in ("rule", "limit")]
            if c["text"]:
                c["hard_missing"] = hard
            else:   # encrypted summary: these may still be in it, so they are "not seen", not "left out"
                c["unseen"] = hard
        lo = mainc[i - 1]["e"] if i else 0
        hi = mainc[i + 1]["e"] if i + 1 < len(mainc) else NOW
        w = min(3 * 3600, c["e"] - lo if lo else 3 * 3600, max(60, hi - c["e"]))
        win_b, win_a = (c["e"] - w, c["e"]), (c["e"], c["e"] + w)
        inside = lambda e, wn: wn[0] <= e < wn[1]
        c["w_h"] = w / 3600.0
        if win_b[0] < first_cmd or win_b[0] < NOW - CFG["hours"] * 3600:   # the logs don't reach back that far: no comparison
            c["breaks"] = c["fails"] = c["repeats"] = None
        else:
            c["breaks"] = [sum(1 for ch in changes if ch.get("bad") and inside(iso_epoch(ch.get("ts")) or 0, wn)) for wn in (win_b, win_a)]
            c["fails"] = [sum(1 for x in cl if x.get("exit") not in (None, 0, "0") and inside(x["e"], wn)) for wn in (win_b, win_a)]
            before = {x["h"] for x in runs if c["e"] - 6 * 3600 <= x["e"] < c["e"]}
            c["repeats"] = sum(1 for x in runs if inside(x["e"], win_a) and x["h"] in before)
        nxt = next((x for x in work if x["e"] >= c["e"]), None)
        c["back"] = (nxt["e"] - c["e"]) if nxt and (i + 1 >= len(mainc) or nxt["e"] < mainc[i + 1]["e"]) else None
    # the latest memory check (asked with the button) and its reply
    probe = None
    for t in sorted(ms.get("probes") or [], reverse=True)[:1]:
        rep = next((m for m in conv if m["r"] == "agent" and m["e"] >= t - 5 and len(PROBE_RX.findall(m["x"])) >= 2), None)
        probe = {"t": t, "answered": bool(rep)}
        if rep:
            body = "\n".join(v for k, v in PROBE_RX.findall(rep["x"]) if k.upper() != "NEXT")
            res = mem_check(body, items)
            probe.update(at=rep["e"], kept=sum(1 for r in res if r == "kept"),
                         missing=[it["label"] for it, r in zip(items, res) if r != "kept"],
                         hard_missing=[it["label"] for it, r in zip(items, res) if r != "kept" and it["kind"] in ("rule", "limit")])
    remind = max(ms.get("reminds") or [0]) or None
    # plan progress over time (kept here, since the agent's log only has its latest plan)
    ph = ms.setdefault("plan_hist", [])
    if plan_total and (not ph or ph[-1][1:] != [plan_done, plan_total]):
        ph.append([NOW, plan_done, plan_total])
        del ph[:-60]
        mem_save(gkey, ms)
    # ---- flags
    last = mainc[-1] if mainc else None
    if last and NOW - last["e"] < 48 * 3600:
        newer = probe if probe and probe.get("answered") and probe.get("at", 0) > last["e"] else None
        if newer:
            if newer["hard_missing"]:
                flags.add("memory:probe:%d" % int(newer["t"]), "amber", "integrity",
                          "The agent's memory check left out %d contract item%s" % (len(newer["hard_missing"]), "" if len(newer["hard_missing"]) == 1 else "s"),
                          "Missing: %s" % "; ".join(newer["hard_missing"]),
                          "Remind it of the contract%s." % (", or have it pin the contract in AGENTS.md so compactions can't drop it" if last.get("enc") else ""),
                          acts=[{"label": "Remind the agent", "op": "remind", "confirm": "Send the agent its contract items again?"}] +
                               ([{"label": "Pin them in AGENTS.md", "op": "pin", "confirm": "Ask the agent to add the contract items to AGENTS.md in its folder?"}]
                                if last.get("enc") else []))
        elif last.get("hard_missing") and not (remind and remind > last["e"]):
            flags.add("memory:forgot:%d" % int(last["e"]), "amber", "integrity",
                      "After the compaction at %s, the agent's summary leaves out %d contract item%s" % (
                          hm(last["e"]), len(last["hard_missing"]), "" if len(last["hard_missing"]) == 1 else "s"),
                      "Missing: %s" % "; ".join(last["hard_missing"]),
                      "Remind it, or check what it remembers.",
                      acts=[{"label": "Remind the agent", "op": "remind", "confirm": "Send the agent its contract items again, noting what its summary left out?"},
                            {"label": "Check what it remembers", "op": "probe", "confirm": "Ask the agent to list its goal, rules, limits and next steps from memory?"}])
        b0, b1 = last["breaks"] or (0, 0)
        f0, f1 = last["fails"] or (0, 0)
        worse = []
        if b1 >= 1 and b1 > b0:
            worse.append("contract breaks %d → %d" % (b0, b1))
        if f1 >= 3 and f1 >= 3 * max(1, f0):
            worse.append("failed commands %d → %d" % (f0, f1))
        if (last["repeats"] or 0) >= 5:
            worse.append("%d earlier commands run again" % last["repeats"])
        if worse:
            flags.add("memory:after:%d" % int(last["e"]), "amber", "integrity",
                      "The agent works differently since the compaction at %s" % hm(last["e"]),
                      "In %.0f h before and after: %s." % (last["w_h"], "; ".join(worse)), "Look at its recent edits and commands, or check what it remembers.")
    newer_probe = bool(last and probe and probe.get("answered") and probe.get("at", 0) > last["e"])
    if last and last.get("enc") and last.get("unseen") and NOW - last["e"] < 48 * 3600 and not newer_probe:
        n = len(last["unseen"])
        flags.add("memory:unpinned:%s" % hashlib.sha1("|".join(last["unseen"]).encode()).hexdigest()[:6], "amber", "integrity",
                  ("1 contract item relies" if n == 1 else "%d contract items rely" % n) + " on Codex's encrypted summary",
                  "Not in what stayed word for word after the compaction at %s (your messages and AGENTS.md): %s" % (hm(last["e"]), "; ".join(last["unseen"])),
                  "Ask the agent to pin them in AGENTS.md (read again after every compaction), or check what it remembers.",
                  acts=[{"label": "Pin them in AGENTS.md", "op": "pin", "confirm": "Ask the agent to add these contract items to AGENTS.md in its folder?"},
                        {"label": "Check what it remembers", "op": "probe", "confirm": "Ask the agent to list its goal, rules, limits and next steps from memory?"}])
    # hours of work with no plan step finished
    if ph and plan_total and plan_done < plan_total:
        since = next((ph[i][0] for i in range(len(ph) - 1, 0, -1) if ph[i][1] > ph[i - 1][1]), ph[0][0])
        busy = [x for x in ev if x["e"] >= since and x.get("kind") not in ("monitor",)]
        if NOW - since >= 12 * 3600 and len(busy) >= 40:
            flags.add("drift:stall", "amber", "goal", "No plan step finished in %s" % ago(since).replace(" ago", ""),
                      "%d actions since, while the plan stays at %d of %d." % (len(busy), plan_done, plan_total),
                      "Ask the agent what is holding it up.")
    # the same command over and over, with no edit, submission or plan step in between
    recent = [x for x in cl if x["e"] >= NOW - 2 * 3600]
    if recent and not any(x["kind"] in ("edit", "submit", "plan") for x in recent):
        cnt = {}
        for x in recent:
            if x["kind"] not in ("monitor", "plan"):
                cnt[x["h"]] = cnt.get(x["h"], 0) + 1
        top = max(cnt.items(), key=lambda kv: kv[1]) if cnt else None
        if top and top[1] >= 8:
            txt = next((t.get("cmd") for t in reversed((main or {}).get("tools") or []) if hashlib.sha1(
                re.sub(r"\s+", " ", t.get("cmd") or "").strip()[:2000].encode("utf-8", "replace")).hexdigest()[:6] == top[0]), "") or top[0]
            flags.add("drift:loop", "amber", "goal", "The agent ran the same command %d times in 2 h" % top[1],
                      trunc(txt, 160), "It may be stuck in a loop. Look at the live screen, or ask it.")
    return {"items": [it["label"] for it in items], "items_full": [it.get("text") or it["label"] for it in items], "n_items": len(items), "comps": [
        dict({k: c.get(k) for k in ("e", "who", "pre", "win", "kept", "missing", "partial", "hard_missing", "unseen", "breaks", "fails", "repeats",
                                    "back", "w_h", "enc")}, has_text=bool(c["text"]), n_kept=len(c.get("kept_t") or [])) for c in comps[-12:]],
            "probe": probe, "remind": remind, "pinned": max(ms.get("pins") or [0]) or None}


def memory_html(s):
    mem = s.get("memory") or {}
    comps = [c for c in mem.get("comps") or [] if c.get("who") in (None, "", "main")]
    nsub = len([c for c in mem.get("comps") or [] if c.get("who") not in (None, "", "main")])
    pr = mem.get("probe")
    if not comps and not pr:
        return ""
    last = comps[-1] if comps else None
    bits = ["%d compaction%s" % (len(comps), "" if len(comps) == 1 else "s")] if comps else []
    if last:
        bits.append("last %s" % ago(last["e"]))
    if nsub:
        bits.append("%d in sub-agents" % nsub)
    o = ['<section class="card"%s><h2>Memory <span class="h2n">%s</span></h2>' % (sec("g", "memory"), esc(" \u00b7 ".join(bits)))]
    o.append('<div class="small mut" style="margin:-2px 0 8px">Each compaction leaves the agent with a summary it wrote itself. Kept counts the '
             'contract\u2019s rules and limits, its criteria and your standing instructions (%d now) found in that summary.%s</div>' % (
                 mem.get("n_items", 0), " Codex encrypts its summaries: for those, only what stayed word for word (your messages and AGENTS.md) "
                 "can be checked, and Check what it remembers is the direct test." if any(c.get("enc") for c in comps) else ""))
    if pr:
        if pr.get("answered"):
            o.append('<div class="mrow"><b>Memory check</b> %s: kept %d of %d%s</div>' % (
                esc(hm(pr["at"])), pr["kept"], mem.get("n_items", 0),
                (' \u00b7 left out: ' + clip_html("; ".join(pr["missing"]), 160)) if pr.get("missing") else " \u00b7 nothing left out"))
        else:
            o.append('<div class="mrow"><b>Memory check</b> asked at %s; its answer is scored here.</div>' % esc(hm(pr["t"])))
    if mem.get("remind"):
        o.append('<div class="mrow mut">Reminded of the contract at %s.</div>' % esc(hm(mem["remind"])))
    if mem.get("pinned"):
        o.append('<div class="mrow mut">Asked to pin the contract in AGENTS.md at %s.</div>' % esc(hm(mem["pinned"])))
    o.append('<div class="cacts" data-ckey="%s" style="margin:8px 0 10px">%s %s<span class="actmsg"></span></div>' % (
        esc(s["key"]),
        act_btn({"label": "Check what it remembers", "op": "probe", "confirm": "Ask the agent to list its goal, rules, limits, criteria and next steps from memory? Its answer is scored here."}),
        act_btn({"label": "Remind it of the contract", "op": "remind", "confirm": "Send the agent its contract items again?"}) +
        (" " + act_btn({"label": "Pin them in AGENTS.md", "op": "pin", "confirm": "Ask the agent to add the contract items to AGENTS.md in its folder, so they survive compactions?"})
         if any(c.get("enc") for c in comps) else "")))
    if comps:
        rows = []
        for c in reversed(comps[-8:]):
            full = (" \u00b7 %d%% full" % round(100.0 * c["pre"] / c["win"])) if c.get("pre") and c.get("win") else ""
            if c.get("has_text"):
                kept = "%d of %d" % (c["kept"] or 0, mem.get("n_items", 0))
                left = clip_html("; ".join(c.get("missing") or []), 120) if c.get("missing") else '<span class="mut">nothing</span>'
            elif c.get("enc"):
                kept = 'encrypted<div class="faint">%d of %d in the %d message%s kept word for word</div>' % (
                    c.get("kept") or 0, mem.get("n_items", 0), c.get("n_kept") or 0, "" if c.get("n_kept") == 1 else "s")
                left = ('<span class="mut">not seen: </span>' + clip_html("; ".join(c.get("missing") or []), 120)) if c.get("missing") else '<span class="mut">nothing</span>'
            else:
                kept, left = '<span class="mut">\u2013</span>', '<span class="mut">summary not recorded</span>'
            after = []
            if c.get("breaks") is None:
                after.append('<span class="mut">no logs from before</span>')
            else:
                b0, b1 = c["breaks"]
                f0, f1 = c["fails"]
                after.append('<span class="%s">breaks %d\u2192%d</span>' % ("bad-t" if b1 > b0 else "", b0, b1))
                after.append('<span class="%s">failed %d\u2192%d</span>' % ("bad-t" if f1 >= 3 and f1 >= 3 * max(1, f0) else "", f0, f1))
                if c.get("repeats"):
                    after.append("%d repeated" % c["repeats"])
            if c.get("back") is not None:
                after.append("back to work in %s" % dur(c["back"]))
            rows.append('<tr><td>%s<span class="mut">%s</span></td><td>%s</td><td>%s</td><td class="small">%s</td></tr>' % (
                esc(hm(c["e"])), esc(full), kept, left, " \u00b7 ".join(after)))
        o.append('<table class="small mtab"><tr><th>Compaction</th><th>Kept</th><th>Left out</th><th>%.0f h before \u2192 after</th></tr>%s</table>' % (
            min(3, max((c.get("w_h") or 3) for c in comps[-8:])), "".join(rows)))
    o.append("</section>")
    return "".join(o)


# ---- terms: what the agents' own words mean, who used and defined each first, and when a meaning moves.
# Meanings come from the agent's word list (.mc/glossary.jsonl in its folder) and its answers when you ask
# ("T1a2b3: meaning"). History is kept here, since the file on the node may be rewritten. The usage hint is only a hint:
# word statistics can tell that a term now shows up in a different setting, not whether its meaning changed.
TERMS_DIR = os.path.join(STATE, "terms")
TERM_STOP = MEM_STOP | set(("was were been being which who whom how why where not but if so such onto over under about after before between "
                            "during per via our them their his her would could might does did done any both other same new old one two "
                            "three first last next out off now still yet again once result results check checks checked run runs running "
                            "i'm i'll i've it's that's").split())
TERM_COMMON = set(("long-term short-term follow-up read-only up-to-date so-called re-run step-by-step real-time end-to-end built-in well-known "
                   "one-time non-zero first-principles ab-initio state-of-the-art open-source on-the-fly per-atom per-structure high-energy "
                   "low-energy short-range long-range two-step one-step full-size small-scale large-scale time-step check-in e-mail x-ray "
                   "self-consistent single-point half-hour half-day day-to-day in-place out-of-date sub-agent sub-agents co-author").split())
TERM_SKIP = re.compile(r"(?i)^(?:gpt|claude|opus|sonnet|haiku|codex|gemini|o\d|vasp|python|cuda|lammps|ubuntu|wsl)\b|^[ahlv]\d{2,3}s?$|^mi\d{3}|^\d|^x\d")
SUGG_RX = re.compile(r"(?<![\w/.\-@])([A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+){1,3}|[A-Z][a-z]*\d+[A-Za-z]*)(?![\w/\-@]|\.\w)")
TERM_WORD = re.compile(r"[a-z][a-z0-9]{2,}")
TERM_SHIFT_MAX = 0.2      # early vs recent (and main vs sub-agent) context similarity, relative to its own spread
TERM_HINTS_MAX = 3        # usage hints shown per session at most


def term_norm(t):
    t = re.sub(r"[`*\"“”‘’]+", "", str(t or "")).strip().strip(".,;:!?()[]{}'")
    return re.sub(r"\s+", " ", t).lower()[:80]


def term_id(gkey, n):
    return "T" + hashlib.sha1(("%s|%s" % (gkey, n)).encode("utf-8", "replace")).hexdigest()[:5]


def term_rx(n):
    parts = [re.escape(w) for w in re.split(r"[\s\-]+", n) if w]
    return re.compile(r"(?<![\w-])" + r"[\s\-]?".join(parts) + r"(?![\w-])", re.I) if parts else None


def terms_state(gkey):
    return rjson(os.path.join(TERMS_DIR, gkey + ".json"), {}) or {}


def terms_save(gkey, st):
    items_save(os.path.join(TERMS_DIR, gkey + ".json"), st)


def terms_known():
    return set((rjson(os.path.join(TERMS_DIR, "_known.json"), {}) or {}).get("known") or [])


def terms_known_save(known):
    items_save(os.path.join(TERMS_DIR, "_known.json"), {"known": sorted(known)[-3000:]})


def term_who(lab, role="agent"):
    """Who wrote it, in words: you, the main agent, or a sub-agent."""
    lab = str(lab or "").strip()
    if role == "user":
        return "you"
    if lab.lower() in ("", "main", "main agent"):
        return "main agent"
    s = "sub-agent " + lab[4:] if re.match(r"(?i)^sub-\w", lab) else lab if "agent" in lab.lower() else "sub-agent " + lab
    return s + (" (its task from the main agent)" if role == "task" else "")


def term_line(text, m):
    a = text.rfind("\n", 0, m.start())
    b = text.find("\n", m.end())
    line = re.sub(r"\s+", " ", text[a + 1: b if b > 0 else len(text)]).strip().lstrip("-*#>| ").strip()
    if len(line) > 240:   # keep the word in view
        i = max(0, line.lower().find(m.group(0).lower()) - 100)
        line = ("…" if i else "") + line[i:i + 230] + "…"
    return line


def term_bag(text, m, n, w=6):
    own = set(re.split(r"[\s\-]+", n))
    a = text[:m.start()].split()[-w:]
    b = text[m.end():].split()[:w]
    return {k: 1 for k in TERM_WORD.findall(" ".join(a + b).lower()) if k not in TERM_STOP and k not in own}


def bag_cos(a, b):
    if not a or not b:
        return 0.0
    d = sum(v * b.get(k, 0) for k, v in a.items())
    return d / ((sum(v * v for v in a.values()) ** .5) * (sum(v * v for v in b.values()) ** .5))


def bag_sum(bags):
    c = {}
    for bg in bags:
        for k, v in bg.items():
            c[k] = c.get(k, 0) + v
    return c


def use_ratio(A, B):
    """How alike two groups of uses are, relative to how alike each group is with itself (halves of it)."""
    if len(A) < 4 or len(B) < 4:
        return None
    a0, a1, b0, b1 = bag_sum(A[::2]), bag_sum(A[1::2]), bag_sum(B[::2]), bag_sum(B[1::2])
    base = (bag_cos(a0, a1) + bag_cos(b0, b1)) / 2
    if base < 0.25:
        return None   # too varied to say anything
    return round(((bag_cos(a0, b0) + bag_cos(a1, b1)) / 2) / base, 2), round(base, 2)


def meaning_alike(a, b):
    wa = set(TERM_WORD.findall(a.lower())) - TERM_STOP
    wb = set(TERM_WORD.findall(b.lower())) - TERM_STOP
    return not wa or not wb or len(wa & wb) / float(len(wa | wb)) >= 0.5


def terms_report(gkey, flags, conv, sessions, ndoc, contract_raw):
    st = terms_state(gkey)
    before = json.dumps(st, sort_keys=True)
    known = terms_known()
    ents, watch = st.setdefault("entries", []), st.setdefault("watch", {})
    have = {e.get("sig") for e in ents}

    def add_entry(term, meaning, by, t, why, src):
        n, meaning = term_norm(term), re.sub(r"\s+", " ", str(meaning or "")).strip()
        if not n or not meaning or len(n) > 60:
            return
        sig = hashlib.sha1(("%s|%s|%s" % (n, meaning.lower(), by)).encode("utf-8", "replace")).hexdigest()[:10]
        if sig in have:
            return
        have.add(sig)
        ents.append({"n": n, "term": trunc(str(term).strip().strip("`*\"“”"), 80), "meaning": trunc(meaning, 900), "by": by,
                     "t": t or NOW, "why": trunc(str(why or ""), 600), "src": src, "sig": sig})
    for e in (ndoc or {}).get("glossary") or []:
        add_entry(e.get("term"), e.get("meaning"), term_who(e.get("by")), iso_epoch(e.get("ts")), e.get("why"), "list")
    ids = {term_id(gkey, n): n for n in list(watch) + [e["n"] for e in ents]}
    for s in sessions:   # answers to "what does this mean", only for words asked about here
        for a in s.get("term_answers") or []:
            n = ids.get(str(a.get("id") or ""))
            if n:
                add_entry((watch.get(n) or {}).get("term") or next((e["term"] for e in ents if e["n"] == n), n), a.get("meaning"),
                          term_who(s.get("label")), iso_epoch(a.get("ts")), "", "answer")
    ents.sort(key=lambda e: e.get("t") or 0)
    st["entries"] = ents[-1500:]

    names = {}
    for e in ents:
        names[e["n"]] = e["term"]
    for n, w in watch.items():
        names.setdefault(n, w.get("term") or n)
    msgs = [m for m in conv if m.get("r") in ("agent", "sub", "user", "task") and m.get("x")]
    low = [m["x"].lower() for m in msgs]
    craw = contract_raw or ""
    pinned, kept = st.setdefault("pinned", {}), st.setdefault("kept", {})
    asked, questioned, same = st.get("asked") or {}, st.get("questioned") or {}, st.get("same") or {}
    out, hints = [], []
    for n, term in names.items():
        rx = term_rx(n)
        if not rx:
            continue
        hist = []
        for e in ents:
            if e["n"] == n and not (hist and hist[-1]["meaning"].lower() == e["meaning"].lower() and hist[-1]["by"] == e["by"]):
                hist.append({"meaning": e["meaning"], "by": e["by"], "t": e["t"], "why": e.get("why") or "", "src": e["src"], "sig": e["sig"]})
        key0 = re.split(r"[\s\-]+", n)[0]
        first, hits = None, []
        for m, xl in zip(msgs, low):
            if key0 not in xl:
                continue
            mm = rx.search(m["x"])
            if not mm:
                continue
            if first is None:
                first = {"who": term_who(m.get("w"), m["r"]), "t": m["e"], "text": term_line(m["x"], mm)}
            if m["r"] in ("agent", "sub"):
                hits.append((m, mm))
        subs = {}
        for m, _ in hits:
            if m["r"] == "sub":
                w = term_who(m.get("w"))
                subs[w] = subs.get(w, 0) + 1
        cur = hist[-1] if hist else None
        latest_by = {}
        for h in hist:
            latest_by[h["by"]] = h
        differ = None
        if len(latest_by) > 1:
            vals = list(latest_by.values())
            if any(not meaning_alike(vals[0]["meaning"], v["meaning"]) for v in vals[1:]):
                differ = [{"by": v["by"], "meaning": v["meaning"], "t": v["t"]} for v in vals]
        changed = None
        if cur and len(hist) > 1:
            # its own author moved it (two agents disagreeing shows as "defined differently" instead)
            prev = next((h for h in reversed(hist[:-1]) if h["by"] == cur["by"] and not meaning_alike(h["meaning"], cur["meaning"])), None)
            if prev:
                changed = {"from": prev["meaning"], "by0": prev["by"], "t0": prev["t"], "t": cur["t"], "by": cur["by"], "why": cur.get("why") or ""}
        tid = term_id(gkey, n)
        incon = bool(craw and rx.search(craw))
        pend = None
        if incon and hist:
            if n not in pinned:   # the first meaning seen for a word the contract uses becomes its pinned meaning
                pinned[n] = {"meaning": hist[0]["meaning"], "t": NOW, "by": "first meaning"}
            pm = pinned[n]["meaning"]
            if cur and not meaning_alike(pm, cur["meaning"]) and cur["sig"] not in (kept.get(n) or []):
                pend = {"meaning": cur["meaning"], "by": cur["by"], "t": cur["t"], "sig": cur["sig"], "why": cur.get("why") or ""}
                flags.add("term:%s" % n, "amber", "integrity", "“%s” has a new meaning, and the contract uses it" % term,
                          "Pinned meaning: %s\nNew meaning (%s, %s): %s" % (pm, cur["by"], hm(cur["t"]), cur["meaning"]),
                          "Accept the new meaning, or keep the pinned one (the agent is told).", needs_you=True,
                          acts=[{"label": "Accept new meaning", "op": "term_accept", "item": tid, "psig": cur["sig"],
                                 "confirm": "Pin the new meaning of “%s”? Contract rules that use the word then mean the new thing." % term},
                                {"label": "Keep the pinned meaning", "op": "term_keep", "item": tid, "psig": cur["sig"],
                                 "confirm": "Tell the agent to keep using “%s” in its pinned meaning?" % term}])
        shift = mainsub = None
        if len(hits) >= 16 and NOW - (same.get(n) or 0) > 30 * 86400 and NOW - (questioned.get(n) or 0) > 14 * 86400:
            k = max(4, len(hits) // 4)
            if hits[-k][0]["e"] - hits[k - 1][0]["e"] >= 3 * 86400:
                r = use_ratio([term_bag(m["x"], mm, n) for m, mm in hits[:k]], [term_bag(m["x"], mm, n) for m, mm in hits[-k:]])
                if r and r[0] < TERM_SHIFT_MAX and r[1] >= 0.3:
                    m0, mm0 = hits[0]
                    m1, mm1 = hits[-1]
                    shift = {"score": r[0], "then": {"who": term_who(m0.get("w"), m0["r"]), "t": m0["e"], "text": term_line(m0["x"], mm0)},
                             "now": {"who": term_who(m1.get("w"), m1["r"]), "t": m1["e"], "text": term_line(m1["x"], mm1)}}
        mh = [(m, mm) for m, mm in hits if m["r"] == "agent"][-40:]
        sh = [(m, mm) for m, mm in hits if m["r"] == "sub"][-40:]
        if len(mh) >= 4 and len(sh) >= 4 and NOW - (same.get(n) or 0) > 30 * 86400:
            r = use_ratio([term_bag(m["x"], mm, n) for m, mm in mh], [term_bag(m["x"], mm, n) for m, mm in sh])
            if r and r[0] < TERM_SHIFT_MAX:
                mainsub = {"score": r[0], "main": {"who": "main agent", "t": mh[-1][0]["e"], "text": term_line(mh[-1][0]["x"], mh[-1][1])},
                           "sub": {"who": term_who(sh[-1][0].get("w")), "t": sh[-1][0]["e"], "text": term_line(sh[-1][0]["x"], sh[-1][1])}}
        row = {"id": tid, "n": n, "term": term, "meaning": cur["meaning"] if cur else "", "by": cur["by"] if cur else "",
               "t": cur["t"] if cur else None, "src": cur["src"] if cur else "", "why": (cur or {}).get("why") or "",
               "hist": [{k: h[k] for k in ("meaning", "by", "t", "why")} for h in hist[-6:]],
               "first_use": first, "first_def": {"who": hist[0]["by"], "t": hist[0]["t"]} if hist else None,
               "main": sum(1 for m, _ in hits if m["r"] == "agent"), "subs": subs, "last": hits[-1][0]["e"] if hits else None,
               "contract": incon, "pinned": pinned[n]["meaning"] if incon and n in pinned else None, "pending": pend,
               "changed": changed, "differ": differ, "shift": shift, "mainsub": mainsub, "waiting": not hist,
               "asked": asked.get(n), "hidden": n in known and not pend, "watched": (watch.get(n) or {}).get("t")}
        if shift:
            hints.append((shift["score"], row))
        out.append(row)
    for _, row in sorted(hints, key=lambda x: x[0])[TERM_HINTS_MAX:]:
        row["shift"] = None   # only the clearest few
    rank = lambda r: (0 if r["pending"] else 1 if r["differ"] else 2 if r["changed"] and NOW - (r["t"] or 0) < 14 * 86400 else
                      3 if r["shift"] or r["mainsub"] else 4 if r["waiting"] else 5, -(r["last"] or r["t"] or 0))
    out.sort(key=rank)

    # words the agents use a lot that nobody has explained (you didn't bring them in, and you haven't said you know them)
    cnt, days, first_by, yours = {}, {}, {}, set()
    for m in msgs:
        for mm in SUGG_RX.finditer(m["x"]):
            w = mm.group(1)
            n = w.lower()
            if n in TERM_COMMON or TERM_SKIP.search(w) or len(n) < 4 or (n.count("-") and not re.search(r"[a-z]", w)):
                continue
            if m["r"] == "user":
                yours.add(n)
                continue
            if m["r"] not in ("agent", "sub"):
                continue
            cnt[n] = cnt.get(n, 0) + 1
            days.setdefault(n, set()).add(int(m["e"] // 86400))
            first_by.setdefault(n, (w, m))
    sugg = []
    for n, c in cnt.items():
        if n in names or n in known or n in yours or c < 4 or len(days[n]) < 2:
            continue
        w, m = first_by[n]
        sugg.append({"term": w, "n": n, "id": term_id(gkey, n), "count": c, "days": len(days[n]),
                     "first": {"who": term_who(m.get("w"), m["r"]), "t": m["e"]}})
    sugg.sort(key=lambda x: -x["count"] * len(days[x["n"]]) ** .5)
    vis = [r for r in out if not r["hidden"]]
    rep = {"terms": out[:90], "sugg": sugg[:10], "setup": st.get("setup"), "has_list": any(e["src"] == "list" for e in ents),
           "counts": {"total": len(vis), "waiting": sum(1 for r in vis if r["waiting"]), "pending": sum(1 for r in vis if r["pending"]),
                      "changed": sum(1 for r in vis if r["changed"] and NOW - (r["t"] or 0) < 14 * 86400),
                      "hints": sum(1 for r in vis if r["shift"] or r["mainsub"] or r["differ"]), "hidden": len(out) - len(vis)},
           "new24": [r["term"] for r in vis if r["first_def"] and NOW - (r["first_def"]["t"] or 0) < 86400][:8],
           "changed24": [r["term"] for r in vis if r["changed"] and NOW - (r["changed"]["t"] or 0) < 86400][:8]}
    if json.dumps(st, sort_keys=True) != before:
        terms_save(gkey, st)
    return rep


def term_client(r):
    """One word, as the chat's word card needs it (times already in words)."""
    q = lambda x: {"who": x["who"], "t": hm(x["t"]), "text": x["text"]} if x else None
    return {"id": r["id"], "n": r["n"], "term": r["term"], "meaning": r["meaning"], "by": r["by"], "t": hm(r["t"]), "src": r["src"],
            "contract": r["contract"], "pinned": r["pinned"], "waiting": r["waiting"], "asked": hm(r["asked"]) if r.get("asked") else "",
            "pending": dict(r["pending"], t=hm(r["pending"]["t"])) if r["pending"] else None,
            "changed": dict(r["changed"], t0=hm(r["changed"]["t0"]), t=hm(r["changed"]["t"])) if r["changed"] else None,
            "differ": [dict(d, t=hm(d["t"])) for d in r["differ"]] if r["differ"] else None,
            "shift": {"then": q(r["shift"]["then"]), "now": q(r["shift"]["now"])} if r["shift"] else None,
            "mainsub": {"main": q(r["mainsub"]["main"]), "sub": q(r["mainsub"]["sub"])} if r["mainsub"] else None,
            "first_use": dict(r["first_use"], t=hm(r["first_use"]["t"]), text=trunc(r["first_use"]["text"], 200)) if r["first_use"] else None,
            "first_def": {"who": r["first_def"]["who"], "t": hm(r["first_def"]["t"])} if r["first_def"] else None,
            "main": r["main"], "subs": r["subs"]}


def terms_json(s):
    rep = s.get("terms") or {}
    rows = [term_client(r) for r in rep.get("terms") or [] if not r.get("hidden")]
    return '<script type="application/json" id="mc-terms" data-key="%s">%s</script>' % (
        esc(s["key"]), json.dumps(rows, separators=(",", ":")).replace("</", "<\\/"))


def term_btn(label, op, r, confirm="", psig="", cls="btn2"):
    a = {"label": label, "op": op, "item": r["id"]}
    if confirm:
        a["confirm"] = confirm
    if psig:
        a["psig"] = psig
    return act_btn(a, cls)


def term_row_html(r, gkey):
    badges = []
    if r["pending"]:
        badges.append('<span class="tbad amb">new meaning · waiting for you</span>')
    if r["differ"]:
        badges.append('<span class="tbad amb">defined differently</span>')
    if r["changed"] and not r["pending"]:
        badges.append('<span class="tbad amb">changed %s</span>' % esc(hm(r["changed"]["t"])))
    if r["shift"] or r["mainsub"]:
        badges.append('<span class="tbad">%s</span>' % ("main and sub-agents differ?" if r["mainsub"] and not r["shift"] else "used differently lately?"))
    if r["contract"]:
        badges.append('<span class="tbad">in contract</span>')
    if r["waiting"]:
        badges.append('<span class="tbad">%s</span>' % (("asked " + esc(hm(r["asked"]))) if r.get("asked") else "no meaning yet"))
    lead = (r["pinned"] if r["pending"] else r["meaning"]) or ("Asked at %s; the answer shows here." % hm(r["asked"]) if r.get("asked") else "Waiting to be asked.")
    o = ['<details class="trow" id="%s"><summary><span class="tn">%s</span>%s<span class="tmean">%s</span></summary><div class="tbody">' % (
        esc(r["id"]), esc(r["term"]), "".join(badges), esc(trunc(lead, 150)))]
    if r["meaning"] and not r["pending"]:   # a pending one shows its pinned and new meaning below instead
        o.append('<div class="tline">%s</div>' % esc(r["meaning"]))
    if r["meaning"] and not r["pending"]:
        o.append('<div class="tmeta">%s · %s%s</div>' % (esc(r["by"]), esc(hm(r["t"])), " · answer to your question" if r["src"] == "answer" else ""))
    if r["pending"]:
        p = r["pending"]
        o.append('<div class="tq amb"><span class="tl">Pinned (the contract uses this word)</span>%s</div><div class="tq amb"><span class="tl">New meaning, %s, %s%s</span>%s</div>' % (
            esc(r["pinned"]), esc(p["by"]), esc(hm(p["t"])), (" · why: " + esc(p["why"])) if p.get("why") else "", esc(p["meaning"])))
    elif r["pinned"]:
        o.append('<div class="tmeta">Pinned for the contract: %s</div>' % esc(trunc(r["pinned"], 200)))
    if r["changed"] and not r["pending"]:
        c = r["changed"]
        o.append('<div class="tq amb"><span class="tl">Before, %s, %s</span>%s</div>' % (esc(c["by0"]), esc(hm(c["t0"])), esc(c["from"])))
    if r["differ"]:
        o.append('<div class="tq amb"><span class="tl">Defined differently</span>%s</div>' % "".join(
            '<div><b>%s</b> (%s): %s</div>' % (esc(d["by"]), esc(hm(d["t"])), esc(d["meaning"])) for d in r["differ"]))
    if r["shift"]:
        sh = r["shift"]
        o.append('<div class="tq"><span class="tl">Used differently lately? An automatic hint: it can also mean the topic moved.</span>'
                 '<div><i>%s, %s:</i> “%s”</div><div><i>%s, %s:</i> “%s”</div></div>' % (
                     esc(hm(sh["then"]["t"])), esc(sh["then"]["who"]), esc(sh["then"]["text"]), esc(hm(sh["now"]["t"])), esc(sh["now"]["who"]), esc(sh["now"]["text"])))
    if r["mainsub"]:
        ms = r["mainsub"]
        o.append('<div class="tq"><span class="tl">The main agent and a sub-agent use it in different settings (a hint)</span>'
                 '<div><i>main agent:</i> “%s”</div><div><i>%s:</i> “%s”</div></div>' % (
                     esc(ms["main"]["text"]), esc(ms["sub"]["who"]), esc(ms["sub"]["text"])))
    shown_m = {(r["changed"] or {}).get("from")} | {d["meaning"] for d in r["differ"] or []}
    older = [h for h in r["hist"][:-1] if h["meaning"] not in shown_m]
    if older:
        o.append('<details class="fold"><summary>Earlier meanings (%d)</summary>%s</details>' % (len(older), "".join(
            '<div class="tq"><span class="tl">%s, %s</span>%s</div>' % (esc(h["by"]), esc(hm(h["t"])), esc(h["meaning"])) for h in reversed(older))))
    who = []
    if r["first_use"]:
        who.append("First used by %s, %s" % (r["first_use"]["who"], hm(r["first_use"]["t"])))
    if r["first_def"]:
        who.append("first defined by %s, %s" % (r["first_def"]["who"], hm(r["first_def"]["t"])))
    uses = ["main agent: %d" % r["main"]] + ["%s: %d" % kv for kv in sorted(r["subs"].items())]
    o.append('<div class="tmeta">%s<br>Uses: %s</div>' % (esc(" · ".join(who) or "Not used in the recorded chat yet"), esc(" · ".join(uses))))
    if r["first_use"] and r["first_use"]["text"]:
        o.append('<div class="tmeta">First use: “%s”</div>' % esc(trunc(r["first_use"]["text"], 220)))
    b = []
    if r["pending"]:
        b.append(term_btn("Accept new meaning", "term_accept", r, "Pin the new meaning of “%s”? Contract rules that use it then mean the new thing." % r["term"], r["pending"]["sig"]))
        b.append(term_btn("Keep the pinned meaning", "term_keep", r, "Tell the agent to keep using “%s” in its pinned meaning?" % r["term"], r["pending"]["sig"]))
    b.append(term_btn("Ask agent" if r["waiting"] else "Ask again", "term_ask", r,
                      "Ask the agent what “%s” means? Other words waiting for a meaning go in the same message." % r["term"]))
    if r["pending"] or r["changed"] or r["differ"] or r["shift"] or r["mainsub"]:
        b.append(term_btn("Question this", "term_question", r, "Ask the agent whether “%s” still means the same thing? It gets the quotes shown here." % r["term"]))
    if r["shift"] or r["mainsub"]:
        b.append(term_btn("Same meaning", "term_same", r))
    b.append(term_btn("I know this", "term_hide", r, "Hide “%s” on every session’s Terms card and stop underlining it?" % r["term"]))
    o.append('<div class="cacts" data-ckey="%s">%s<div class="actmsg"></div></div></div></details>' % (esc(gkey), " ".join(b)))
    return "".join(o)


def terms_html(s):
    rep = s.get("terms")
    if rep is None:
        return ""
    gk, c = s["key"], rep.get("counts") or {}
    rows = [r for r in rep.get("terms") or [] if not r.get("hidden")]
    hidden = [r for r in rep.get("terms") or [] if r.get("hidden")]
    bits = ["%d word%s" % (c.get("total", 0), "" if c.get("total") == 1 else "s") if c.get("total") else "no words yet"]
    for k, w in (("pending", "waiting for you"), ("changed", "changed"), ("waiting", "without a meaning")):
        if c.get(k):
            bits.append("%d %s" % (c[k], w))
    o = ['<section class="card tcard" id="terms"%s><h2>Terms <span class="h2n">%s</span></h2>' % (sec("g", "terms"), esc(" · ".join(bits))),
         '<div class="ttip">%s</div>' % ("Underlined words in the chat open their meaning. Select any other word or phrase to ask what it means."
                                         if rows else "Select any word or phrase in the agent\u2019s messages to ask what it means.")]
    if not rep.get("has_list"):
        if rep.get("setup"):
            o.append('<div class="mrow mut">Asked for a word list at %s; its words show here as it adds them.</div>' % esc(hm(rep["setup"])))
        else:
            o.append('<div class="small mut" style="margin:-2px 0 8px">Agents rarely explain their own words. Ask this one to keep a word list '
                     '(.mc/glossary.jsonl) and to define new words the first time it uses them; it also pins that rule in AGENTS.md.</div>')
        o.append('<div class="cacts" data-ckey="%s" style="margin:0 0 10px">%s<span class="actmsg"></span></div>' % (esc(gk), act_btn(
            {"label": "Ask again for a word list" if rep.get("setup") else "Ask the agent to keep a word list", "op": "term_setup",
             "confirm": "Send the agent a short request: keep .mc/glossary.jsonl, define new words on first use, pin that in AGENTS.md, and add the words it already uses?"})))
    wait = [r for r in rows if r["waiting"]]
    if wait:
        o.append('<div class="mrow"><b>Waiting for a meaning:</b> %s <span class="cacts" data-ckey="%s">%s<span class="actmsg"></span></span></div>' % (
            esc(", ".join(r["term"] for r in wait[:12]) + (" and %d more" % (len(wait) - 12) if len(wait) > 12 else "")), esc(gk),
            act_btn({"label": "Ask agent about %s" % ("it" if len(wait) == 1 else "these %d" % len(wait)), "op": "term_ask",
                     "confirm": "Ask the agent what %s mean%s? Answers show here." % ("this word" if len(wait) == 1 else "these %d words" % len(wait), "s" if len(wait) == 1 else "")})))
    shown = [r for r in rows if not r["waiting"]]
    if shown:
        if len(shown) > 8:
            o.append('<input class="tfind" type="search" placeholder="Find a word" aria-label="Find a word">')
        o.append('<div class="tlist">%s</div>' % "".join(term_row_html(r, gk) for r in shown[:8]))
        if len(shown) > 8:
            o.append('<details class="fold tmore"><summary>%d more words</summary><div class="tlist">%s</div></details>' % (
                len(shown) - 8, "".join(term_row_html(r, gk) for r in shown[8:])))
    if rep.get("sugg"):
        o.append('<details class="fold"><summary>Used often, never explained (%d)</summary><div class="small mut" style="margin:4px 0 6px">'
                 'Words the agents keep using that nobody has defined. Ask adds them to the waiting list.</div>%s</details>' % (
                     len(rep["sugg"]), "".join(
                         '<span class="tsug cacts" data-ckey="%s"><span>%s <span class="faint">×%d</span></span>%s%s<span class="actmsg"></span></span>' % (
                             esc(gk), esc(x["term"]), x["count"], act_btn({"label": "Ask", "op": "term_add", "item": x["id"]}, "btn2 tsm"),
                             act_btn({"label": "I know it", "op": "term_hide", "text": x["term"]}, "btn2 tsm")) for x in rep["sugg"])))
    if hidden:
        o.append('<details class="fold"><summary>Hidden (%d)</summary>%s</details>' % (len(hidden), "".join(
            '<span class="tsug cacts" data-ckey="%s"><span>%s</span>%s<span class="actmsg"></span></span>' % (
                esc(gk), esc(r["term"]), act_btn({"label": "Bring back", "op": "term_unhide", "item": r["id"]}, "btn2 tsm")) for r in hidden[:40])))
    o.append(terms_json(s))
    o.append("</section>")
    return "".join(o)


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
        meta = s.get("meta", {}) or {}
        # Newer Codex sub-agents (explorer, implementer, ...) are forks that keep the main session's id: tell them apart by file
        fork = bool(meta.get("agent_role")) or "subagent" in json.dumps(meta.get("source") or "").lower()
        sid = s.get("path") if fork else (meta.get("id") or meta.get("sessionId") or s.get("path"))
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
    codex_goal = (goal or {}).get("text") or ""
    for iid, a in item_answers(sessions, ndoc).items():   # "needs your decision" with a setting and value: a request
        if a["status"] == "need_decision" and a.get("param") and a.get("value") and ndoc is not None:
            ndoc.setdefault("requests", []).append({"id": iid, "param": a["param"], "value": a["value"], "why": a.get("note") or ""})
    cst, cnotes = contract_upkeep(gkey, goal_text, codex_goal, ndoc, shared)
    if cnotes:
        contract = parse_contract()
    went = CT.watch_entries(contract["raw"])
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
    # "Reviewing approval request" is Codex's own reviewer at work (the agent is busy), not a question for you
    rv = re.search(r"Reviewing approval request \((?:(\d+)h )?(\d+)m", "\n".join(tail[-6:]))
    if rv and int(rv.group(1) or 0) * 60 + int(rv.group(2)) >= 15:
        flags.add("pane:review-slow", "amber", "goal", "Codex's approval reviewer has been checking one request for %s"
                  % ("%sh %sm" % (rv.group(1), rv.group(2)) if rv.group(1) else "%s min" % rv.group(2)),
                  "The agent waits until the reviewer decides. It isn't asking you anything.",
                  "If it stays stuck, press Esc in the session (mctl peek %s to look first)." % gkey)
    m = QN.find(tail) if tail else None
    if m:
        m["key"] = gkey
        flags.add("pane:approval", "red", "goal", "The agent is asking you something", trunc(m["text"], 220),
                  ("Pick an answer below. It is typed into the session for you." if m["options"] else
                   "Type your answer in the message box and use Send anyway.") if not tm.get("notmux") else
                  "Answer it in the agent's terminal (mctl peek %s to look first)." % gkey, q=None if tm.get("notmux") else m)
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
    my_dirs = (shared.get("goal_dirs") or {}).get(gkey) or []
    other_dirs = [c for k, v in (shared.get("goal_dirs") or {}).items() if k != gkey for c in v]

    def elsewhere(wd):   # a job run from another session's folder (and not from this one's): flagged on that session's page
        wd = str(wd or "").rstrip("/")
        inside = lambda dirs: any(wd == c or wd.startswith(c + "/") for c in dirs)
        return bool(wd) and not inside(my_dirs) and inside(other_dirs)
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
                if el < 300 and not elsewhere(a.get("WorkDir")):
                    stem = re.sub(r"[\d_\-.]+$", "", a.get("JobName", "")) or a.get("JobName", "")
                    quick_fails.setdefault((c["name"], stem), []).append(a)
            else:
                idle_part = max(0.0, ch - used)
                idle += idle_part
                eff = used / ch if ch else 1
                if eff < CFG["low_eff"] and el > 600 and ncpu > 1 and stt == "COMPLETED" and not elsewhere(a.get("WorkDir")):
                    low_eff_jobs.append((c["name"], a, eff))
        tot_alloc += alloc
        tot_idle += idle
        tot_failed += failed
        tot_lost += lost
        long_pd, held, never = [], [], []
        for j in c["jobs"]:
            if j.get("State") == "PENDING" and not elsewhere(j.get("WorkDir")):
                sub = iso_epoch(j.get("Submit"))
                why = str(j.get("Reason") or "").strip("() ")
                if why == "DependencyNeverSatisfied":
                    never.append((sub or NOW, j))
                elif why == "Dependency":
                    continue   # waiting for another job, as planned
                elif sub and NOW - sub > CFG["pending_h"] * 3600:
                    (held if why == "JobHeldUser" else long_pd).append((sub, j))
        if never:
            j = sorted(never, key=lambda x: x[0])[0][1]
            flags.add("never:%s:%s" % (c["name"], j.get("JobID")), "amber", "compute",
                      "%d job(s) on %s can never start: the job they wait for failed" % (len(never), c["name"]),
                      "e.g. %s (%s)" % (j.get("JobID"), trunc(j.get("Name"), 40)),
                      "Cancel them, or resubmit without the dependency.")
        if long_pd:   # keyed by the oldest one, so the item (and your OK) stays the same until it starts
            j = sorted(long_pd, key=lambda x: x[0])[0][1]
            flags.add("pd:%s:%s" % (c["name"], j.get("JobID")), "amber", "compute",
                      "%d job(s) pending over %g h on %s" % (len(long_pd), CFG["pending_h"], c["name"]),
                      "e.g. %s (%s), reason %s" % (j.get("JobID"), trunc(j.get("Name"), 40), j.get("Reason")),
                      "Consider moving them to a less busy partition or cluster.")
        if held:   # put on hold on purpose (scontrol hold): worth knowing, not a queue problem
            j = sorted(held, key=lambda x: x[0])[0][1]
            flags.add("held:%s:%s" % (c["name"], j.get("JobID")), "info", "compute",
                      "%d job(s) on hold on %s" % (len(held), c["name"]),
                      "e.g. %s (%s), submitted %s; it starts after scontrol release" % (
                          j.get("JobID"), trunc(j.get("Name"), 40), ago(sorted(held, key=lambda x: x[0])[0][0])), "")
        seen = {}
        for j in c["jobs"]:
            if j.get("State") in ("RUNNING", "PENDING") and not elsewhere(j.get("WorkDir")):
                k = (j.get("Name"), j.get("WorkDir"))
                seen.setdefault(k, []).append(j.get("JobID"))
        for (nm, wd), ids in seen.items():
            # tasks of one array job (123_0, 123_1) share a name and folder on purpose
            if len(ids) > 1 and nm and len({str(x).split("_")[0] for x in ids}) > 1:
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
        flags.add("loweff:%s:%s" % (cl, a.get("JobID")), "amber", "compute",
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
        if not vals and not (went.get(k) or {}).get("op"):
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
        we = went.get(k) or {}
        row["auto"], row["tag"] = bool(we.get("auto")), we.get("tag") or ""
        if row["bad"]:
            badv = [x for x in row["values"] if x["bad"]]
            acts = [{"label": "Allow %s" % badv[0]["v"], "op": "allow", "name": k, "value": badv[0]["v"],
                     "confirm": "Allow %s = %s? The rule is widened and an amendment is recorded." % (k, badv[0]["v"])}]
            if row["auto"]:
                acts.append({"label": "Keep the rule", "op": "keep", "name": k,
                             "confirm": "Keep %s %s as your own rule? Breaks then show red." % (k, row["rule"])})
            flags.add("param:%s" % k, "amber" if row["auto"] else "red", "integrity",
                      "%s breaks the contract in %d recent input file(s)" % (k, sum(x["n"] for x in badv)),
                      "Rule %s%s; found %s%s" % (row["rule"], (" (%s)" % row["tag"]) if row["auto"] else "",
                                                   ", ".join("%s \u00d7%d" % (x["v"], x["n"]) for x in badv[:3]),
                                                   (" (e.g. %s)" % trunc(badv[0]["ex"], 120)) if badv[0].get("ex") else ""),
                      ("An automatic rule: keep it to make breaks red, or allow the value." if row["auto"] else
                       "Ask the agent to fix it, or allow the value."), acts=acts)
        elif len(row["values"]) > 1 and not rule.get("op") and k not in VARIES:
            minority = [x for x in row["values"][1:] if not authorized(k, x["v"], perm_texts)]
            if minority:
                flags.add("param-mixed:%s" % k, "amber", "integrity",
                          "%s has %d different values in recent inputs" % (k, len(row["values"])),
                          ", ".join("%s \u00d7%d" % (x["v"], x["n"]) for x in row["values"][:4]),
                          "Pick what's valid: one value, any of these, a bound, or your own rule.",
                          acts=lock_acts(k, [x["v"] for x in row["values"]]))
    changes = []
    pending = set()   # values the agent has asked you for (waiting under Needs you), so the edits aren't a second alarm
    for pr in CT.proposals(contract["raw"]):
        mm = re.match(r"^(?:allow|set)\s+([A-Z_][A-Z0-9_]*)\s+(?:(?:abs<=|<=|>=|==|<|>|in)\s+)?(.+)$", pr["action"], re.I)
        if mm:
            for v in mm.group(2).split("|"):
                pending.add((mm.group(1).upper(), norm_pv(v), pr["id"]))
    docs_rx = re.compile(r"\.(md|markdown|txt|rst|log|tex|html?|org|ipynb|csv|out)$", re.I)
    for s in sessions:
        for p in s.get("patches", []):
            e = iso_epoch(p.get("ts"))
            if e and NOW - e > CFG["hours"] * 3600:
                continue
            olds, news = {}, {}
            for sign, fpath, body, keep in p.get("lines", []):
                if docs_rx.search(fpath or ""):   # notes and docs mention settings; they don't set them
                    continue
                for k in watch:
                    mm = re.search(r"(?<![A-Za-z0-9_])%s\s*[=:]\s*([^\s,;#!]+)" % re.escape(k), body, re.I)
                    if mm:
                        (olds if sign == "-" else news).setdefault((k, fpath), mm.group(1))
            for (k, fpath), nv in news.items():
                ov = olds.get((k, fpath))
                if ov is not None and pv_equal(ov, nv):
                    continue
                if ov is None and not rule_violated(watch[k], nv):
                    continue   # a new line that keeps to the contract
                bad = rule_violated(watch[k], nv) and not authorized(k, norm_pv(nv), perm_texts)
                asked = next((pid for (pk, pv, pid) in pending if pk == k and pv_equal(pv, nv)), None)
                changes.append({"ts": p.get("ts"), "who": s.get("label"), "param": k, "old": ov, "new": nv, "file": fpath,
                                "bad": bad and not asked, "asked": asked})
    for ch in changes[-12:]:
        k = ch["param"]
        ok = authorized(k, norm_pv(ch["new"]), perm_texts)
        if ch.get("asked"):
            flags.add("chg:%s:%s:%s" % (k, ch["file"], norm_pv(ch["new"])), "info", "integrity",
                      "%s set to %s in %s, as the agent asked in %s" % (k, norm_pv(ch["new"]), trunc(ch["file"], 60), ch["asked"]),
                      "Waiting for your decision on %s under Needs you." % ch["asked"])
            continue
        if ch["bad"]:
            sev = "red"
        elif ch["old"] is None:
            continue
        else:
            sev = "info" if ok else "amber"
        rule = watch.get(k, {})
        flags.add("chg:%s:%s:%s" % (k, ch["file"], norm_pv(ch["new"])), sev, "integrity",
                  "%s changed %s \u2192 %s by %s" % (k, ch["old"] or "(new)", ch["new"], ch["who"]),
                  "%s at %s%s" % (trunc(ch["file"], 90), hm(iso_epoch(ch["ts"])),
                                   (" \u00b7 breaks %s %s%s" % (k, ("%s %s" % (rule.get("op"), rule.get("val"))), " from your goal" if rule.get("src") == "goal text" else " in the contract")) if ch["bad"] else
                                   (" \u00b7 you allowed %s = %s in an instruction" % (k, ch["new"]) if ok else "")),
                  "" if sev == "info" else "Ask the agent about it, allow the value, or dismiss it.",
                  acts=[{"label": "Allow %s" % norm_pv(ch["new"]), "op": "allow", "name": k, "value": norm_pv(ch["new"]),
                         "confirm": "Allow %s = %s? The rule is widened and an amendment is recorded." % (k, norm_pv(ch["new"]))}] if ch["bad"] else None)
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
    done_claims = [dict(d, who=s.get("label"), epoch=iso_epoch(d.get("ts"))) for s in (fam or sessions) for d in s.get("done_claims", [])
                   if not ANSWER_RX.match(d.get("text") or "") and is_done_claim(d.get("text"))]   # a plain "it's met", not an item answer
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
    contract_flags(flags, contract, cst, res, {"alloc_ch": tot_alloc}, node, gnode, codex_goal)
    storage = storage_update(gkey, (ndoc or {}).get("storage"), flags)

    # ---- instructions you typed
    instr = []
    promoted = " ".join(a["text"] for a in contract["amends"])
    for s in sessions:
        meta = s.get("meta") or {}
        robot = bool(meta.get("agent_role")) or any(w in json.dumps(meta.get("source") or "").lower() + str(meta.get("thread_source") or "")
                                                   for w in ("guardian", "subagent", "review"))
        if robot:   # its "user" messages come from the main agent or Codex's reviewer, not from you
            continue
        typed = [u for u in s.get("user", []) if u.get("typed")] or (s.get("user", []) if s.get("label") in (None, "main") else [])
        for u in typed:
            if u["text"].lstrip().startswith("[mission control]"):   # mission control's own messages (asks, contract notes)
                continue
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
    try:
        memory = memory_report(gkey, flags, conv, main, fam, contract, instr, goal_text, changes, plan_done, len(plan or []))
    except Exception as e:  # noqa: a bad record must not take the page down
        sys.stderr.write("memory %s: %s\n" % (gkey, e))
        memory = None
    checked = [c["e"] for c in (memory or {}).get("comps") or [] if c.get("has_text") or c.get("enc")]   # on the Memory card
    for f in list(flags.items):
        if f["key"].startswith("goal:compaction"):
            if any(re.search(r"compacted at (.+)$", f["title"]) and hm(e) in f["title"] for e in checked):
                flags.items.remove(f)   # checked on the Memory card instead
            elif not f.get("acts"):
                f["acts"] = [{"label": "Check what it remembers", "op": "probe",
                              "confirm": "Ask the agent to list its goal, rules, limits and next steps from memory? Its answer is scored on the Memory card."}]
    try:
        terms = terms_report(gkey, flags, conv, fam or ([main] if main else []), ndoc, rd(CONTRACT, "") or "")
    except Exception as e:  # noqa: same as memory
        sys.stderr.write("terms %s: %s\n" % (gkey, e))
        terms = None
    items_decorate(flags, gkey, sessions, ndoc)

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
        "storage": storage,
        "ccard": {"path": os.path.relpath(CONTRACT, MC_DIR), "codes": cst.get("codes") or {}, "specs": sorted((cst.get("specs_seen") or {}).items()),
                  "compute": {k: v for k, v in CT.parse_compute(contract["raw"]).items() if k != "entries"},
                  "criteria": [t for _, t in CT.criteria_texts(contract["raw"])], "log": (cst.get("log") or [])[-6:][::-1],
                  "rules": {n: {"op": e["op"], "val": e["val"], "auto": e["auto"], "tag": e["tag"]} for n, e in CT.watch_entries(contract["raw"]).items()},
                  "proposals": [{k: p[k] for k in ("id", "text", "action")} for p in CT.proposals(contract["raw"])],
                  "codex_goal": codex_goal},
        "compute": {"alloc_ch": round(tot_alloc, 1), "idle_ch": round(tot_idle, 1), "failed_ch": round(tot_failed, 1), "lost_ch": round(tot_lost, 1),
                    "waste_pct": None if waste_pct is None else round(waste_pct, 1), "gpu_h": round(gpu_h, 1),
                    "running_cores": running_cores},
        "params": {"table": ptable, "scan": {k: params.get(k) for k in ("files", "dirs", "roots", "truncated", "note", "cached", "t")}},
        "changes": changes[-20:], "instructions": instr, "timeline": tl, "criteria": crit_rows, "memory": memory, "terms": terms,
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


def contract_card_html(s):
    """The contract inside the Scientific integrity card: what it protects, where the rules came from, and buttons to change them."""
    cc, k = s.get("ccard") or {}, s["key"]
    rules = cc.get("rules") or {}
    o = ['<div class="ccard" data-ckey="%s">' % esc(k)]
    meta = []
    if cc.get("codes"):
        meta.append("codes in use: " + ", ".join("%s (%d file%s)" % (c, n, "s" if n != 1 else "") for c, n in sorted(cc["codes"].items(), key=lambda kv: -kv[1])))
    if cc.get("specs"):
        meta.append("rules read from " + ", ".join("%s (%s)" % (n, hm(t)) for n, t in cc["specs"]))
    o.append('<div class="rnote" style="margin:8px 0 0" title="Automatic changes only add or tighten rules; anything looser waits for your OK.">'
             '<span class="mono">%s</span>%s</div>' % (esc(cc.get("path") or ""), (" \u00b7 " + esc(" \u00b7 ".join(meta))) if meta else ""))
    cp = cc.get("compute") or {}
    bits = []
    if cp.get("allowed"):
        bits.append("allowed: " + ", ".join(cp["allowed"]))
    if cp.get("forbid"):
        bits.append("not allowed: " + ", ".join(cp["forbid"]))
    if cp.get("budget"):
        bits.append("budget %s %s" % ("{:,.0f}".format(cp["budget"][0]), cp["budget"][1]))
    for h, pct in sorted((cp.get("cpu_cap") or {}).items()):
        bits.append("%s CPU \u2264 %g%%" % (h, pct))
    if bits:
        o.append('<div class="small" style="margin-top:6px"><b>Compute</b> \u00b7 %s</div>' % esc(" \u00b7 ".join(bits)))
    pt = s["params"]["table"]
    if pt:
        o.append('<div class="tscroll"><table style="margin-top:8px"><tr><th>Setting</th><th>Rule</th><th title="Values in input files changed recently">Recent values</th><th></th></tr>')
        for row in pt:
            vals = ", ".join(('<span class="%s" title="%s">%s \u00d7%d</span>' % ("bad-t" if v["bad"] else "", esc(v["ex"]), esc(v["v"]), v["n"])) for v in row["values"][:6]) \
                or '<span class="mut">none in recent files</span>'
            r = rules.get(row["name"]) or {}
            acts = []
            if r.get("op"):
                if r.get("auto"):
                    acts.append({"label": "Keep", "op": "keep", "name": row["name"], "confirm": "Keep %s %s %s as your own rule?" % (row["name"], r["op"], r["val"])})
                acts.append({"label": "Unlock", "op": "unlock", "name": row["name"], "danger": True,
                             "confirm": "Unlock %s (now %s %s)? Changes are still shown, but no value breaks the contract." % (row["name"], r["op"], r["val"])})
            elif row["values"]:
                acts = lock_acts(row["name"], [x["v"] for x in row["values"]], cls_form=False)
            src = ('<div class="small mut" title="%s">%s</div>' % (esc(r["tag"]), esc(r["tag"].split(":")[0]))) if r.get("auto") and r.get("tag") else ""
            menu = ('<details class="rowmenu"><summary title="Change %s">\u22ef</summary><div class="cacts" data-ckey="%s">%s<div class="actmsg"></div></div></details>' % (
                esc(row["name"]), esc(k), "".join(act_btn(a, "btn2 sm") for a in acts))) if acts else ""
            o.append('<tr><td class=mono>%s</td><td class=mut>%s%s</td><td>%s</td><td class="mcell">%s</td></tr>' % (esc(row["name"]), esc(row["rule"]), src, vals, menu))
        o.append("</table></div>")
    o.append('<div class="cform" data-ckey="%s"><span class="small mut">Add a rule</span>'
             '<input name="name" placeholder="ENCUT" maxlength="40" autocapitalize="characters" list="cnames-%s">'
             '<select name="rule_op"><option>==</option><option>&lt;=</option><option>&gt;=</option><option value="in">in (A|B)</option></select>'
             '<input name="val" placeholder="520" maxlength="80"><button type="button" class="btn2 sm" data-cop="set" data-cconfirm="Add this rule?">Add</button>'
             '<datalist id="cnames-%s">%s</datalist><div class="actmsg"></div></div>' % (
                 esc(k), esc(k), esc(k), "".join('<option value="%s">' % esc(n) for n in CT.SCAN_NAMES if n != "POT_*")))
    crit = cc.get("criteria") or []
    o.append('<div class="small" style="margin-top:10px"><b>Acceptance criteria</b></div>')
    if crit:
        o.append('<ul class="clist">%s</ul>' % "".join(
            '<li>%s <span class="cacts" data-ckey="%s">%s</span></li>' % (esc(t), esc(k), act_btn(
                {"label": "Remove", "op": "uncriterion", "text": t, "danger": True, "confirm": "Remove this criterion?\n\n" + t}, "btn2 sm")) for t in crit))
    else:
        o.append('<div class="small mut">None yet. They let the checks tell finished from unfinished work.</div>')
    o.append('<div class="cform" data-ckey="%s"><input name="text" class="wide" placeholder="Force error on the test set <= 30 meV/A" maxlength="300">'
             '<button type="button" class="btn2 sm" data-cop="criterion" data-cconfirm="Add this criterion?">Add criterion</button><div class="actmsg"></div></div>' % esc(k))
    if cc.get("log"):
        o.append('<details class="clog"><summary class="small mut">Recent contract changes</summary><ul class="clist small">%s</ul></details>' % "".join(
            "<li><span class=mut>%s</span> %s</li>" % (esc(hm(x.get("t"))), esc(x.get("x"))) for x in cc["log"]))
    if not (s.get("tmux") or {}).get("notmux"):
        o.append('<div class="cacts" data-ckey="%s" style="margin-top:10px" title="Its requests then show up here with Approve and Reject">%s'
                 '<span class="small mut">sends the agent its protected settings and how to ask first</span><div class="actmsg"></div></div>' % (
                     esc(k), act_btn({"label": "Tell the agent how to ask", "op": "tell",
                                      "confirm": "Send the agent the contract's protected settings and how to ask before changing one?"}, "btn2 sm")))
    o.append("</div>")
    return "".join(o)


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
    yours = {m["x"].strip() for m in msgs if m["r"] == "user"}
    msgs = [m for m in msgs if not (m["r"] == "compact" and m["x"].strip() in yours)]
    cmp_at = [m["e"] for m in msgs if m["r"] == "compact"]
    if cmp_at:   # the plain "Context compacted" line, where the summary itself is now kept
        msgs = [m for m in msgs if not (m["r"] == "event" and m["x"].startswith("Context compacted") and any(abs(m["e"] - e) < 120 for e in cmp_at))]
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
var ROLES=['agent','sub','user','task','event','review'],P=window.MCP;
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
function on(r){var el=document.getElementById('f_'+(r==='compact'?'event':r));return !el||el.checked;}
function idOf(m){return m.e+'|'+m.r+'|'+m.x.length+'|'+m.x.slice(0,60);}
function atBottom(){return window.innerHeight+window.scrollY>=document.body.scrollHeight-160;}
function toLatest(){if(O.value==='new')window.scrollTo(0,0);else window.scrollTo(0,document.body.scrollHeight);}
function render(){var q=(Q.value||'').toLowerCase();
 var items=D.filter(function(m){return on(m.r)&&(!q||m.x.toLowerCase().indexOf(q)>=0);}).concat(q||!P?[]:P.list().map(function(p){return {e:p.e,r:'user',x:p.x,pend:p};}));
 if(O.value==='new')items=items.slice().reverse();
 var h=[],lastDay='',pk=null,pe=0;
 items.forEach(function(m){var d=day(m.e);if(d!==lastDay){h.push('<div class="day"><span>'+d+'</span></div>');lastDay=d;pk=null;}
  if(m.r==='event'){h.push('<div class="evt"><span>'+inl(m.x)+' · '+tm(m.e)+'</span></div>');pk=null;return;}
  if(m.pend){h.push(P.row(m.pend));pk=null;return;}
  if(m.r==='compact'){h.push('<details class="cmp"><summary>Context compacted · '+tm(m.e)+' · the agent carries on from this summary</summary><div class="mb">'+md(m.x)+'</div></details>');pk=null;return;}
  var k=m.r+'|'+(m.w||''),cont=k===pk&&Math.abs(m.e-pe)<600;pk=k;pe=m.e;
  var long=m.x.length>2200||m.x.split('\n').length>26;
  h.push('<div class="row r-'+m.r+(cont?' cont':'')+'">'+(m.r==='user'?'':'<div class="av">'+av(m)+'</div>')+
   '<div class="bub">'+(cont?'':'<div class="mh"><span class="who">'+who(m)+'</span><span class="tm">'+tm(m.e)+'</span>'+(m.md?'<span class="mdl">'+esc(m.md)+'</span>':'')+'</div>')+
   '<div class="mb'+(long?' clip':'')+'" title="'+tm(m.e)+'">'+md(m.x)+'</div>'+(long?'<button class="more">Show all</button>':'')+'</div></div>');});
 L.innerHTML=h.join('')||'<div class="empty">No messages match.</div>';
 if(window.mcTermMark)window.mcTermMark(L);
 document.getElementById('n').textContent=D.length.toLocaleString()+' messages';}
L.addEventListener('click',function(ev){var b=ev.target.closest('.more');if(!b)return;var mb=b.previousElementSibling;var c=mb.classList.toggle('clip');b.textContent=c?'Show all':'Show less';});
var tmr;Q.addEventListener('input',function(){clearTimeout(tmr);tmr=setTimeout(render,200);});
try{var so=localStorage.getItem('mc.order');if(so)O.value=so;}catch(e){}
O.addEventListener('change',function(){try{localStorage.setItem('mc.order',O.value);}catch(e){}render();toLatest();});
ROLES.forEach(function(r){var el=document.getElementById('f_'+r);if(el)el.addEventListener('change',function(){el.parentNode.classList.toggle('off',!el.checked);render();});});
document.getElementById('jump').addEventListener('click',toLatest);
function mine(){return D.filter(function(m){return m.r==='user';});}
var np=P?P.list().length:0;
if(P)P.on(function(){var n=P.list().length,grew=n>np;np=n;render();if(grew||atBottom())toLatest();});
D.sort(function(a,b){return a.e-b.e;});if(P)P.seen(mine());render();toLatest();
// opened with mctl (http): pick up new messages every minute without reloading the page
if(/^https?:/.test(location.protocol)){var pad=function(n){return (n<10?'0':'')+n;};
 window.mcConvPoll=function(){var d=new Date(),f='conv/'+encodeURIComponent(KEY)+'/'+d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+'.js';
  fetch(f,{cache:'no-store'}).then(function(r){return r.ok?r.text():'';}).then(function(t){var i=t.indexOf('MC_CONV,'),j=t.lastIndexOf(');');if(i<0||j<0)return;
   var arr=JSON.parse(t.slice(i+8,j)),seen={},add=0,bottom=atBottom();D.forEach(function(m){seen[idOf(m)]=1;});
   arr.forEach(function(m){if(!seen[idOf(m)]){D.push(m);add++;}});
   if(add){D.sort(function(a,b){return a.e-b.e;});if(P)P.seen(mine());render();if(bottom)toLatest();}}).catch(function(){});};
 setInterval(window.mcConvPoll,30000);document.addEventListener('visibilitychange',function(){if(!document.hidden)window.mcConvPoll();});}
})();
"""

CONV_CSS = """
.hback{color:inherit;text-decoration:none}.hback:hover{text-decoration:underline;text-underline-offset:4px}
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
            ("memory", "Memory"), ("terms", "Terms"), ("timeline", "What happened"), ("instructions", "Your instructions"), ("screen", "Live screen"), ("slurm", "Slurm jobs"),
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
            # On a conversation page the other pills stay in conversation view, and this session's own pill goes back to its session page.
            back = conv and n["key"] == cur
            href = pre + n["key"] + (".conversation.html" if conv and n.get("conv") and not back else ".html")
            tip = "Back to the %s session page" % n["name"] if back else "%s on %s" % (n["name"], h)
            dot = "gray" if n.get("dim") else {"red": "red", "amber": "amber"}.get(n.get("sev"), "")
            parts.append('<a class="np%s%s" style="%s" href="%s" title="%s"><span class="dot %s"></span>%s</a>' % (
                " cur" if n["key"] == cur else "", " dim" if n.get("dim") else "", hue(n["key"]), esc(href), esc(tip), dot,
                esc(trunc(n["name"], 30))))
    newb = ('<button class="pill newb" id="mc-new" type="button" title="Start a new agent session in tmux">+ New session</button>'
            if LAUNCH.get("hosts") else "")
    return ('<div class="appbar"><div class="in"><a class="brand" href="%s"><i></i><span>Mission control</span></a><nav class="sw">%s</nav>%s%s</div></div>%s' % (
        home, "".join(parts) or '<span class="small mut">no agent sessions found</span>', newb,
        fresh_pill(collected) if collected is not None else "", launch_dialog() if newb else ""))


def reds_json():
    return '<script type="application/json" id="mc-reds">%s</script>' % json.dumps(REDS).replace("</", "<\\/")


def attach_cmd(s):
    """The command that opens this session's terminal from your WSL, the way mission control reaches the node."""
    node, target = s.get("node_name") or "", (s.get("tmux") or {}).get("target") or s["session"]
    if node == LOCAL_NAME:
        return "tmux attach -t %s" % target
    route = os.environ.get("ROUTE_" + re.sub(r"[^A-Za-z0-9_]", "_", node)) or (
        os.environ.get("SAIPH_ROUTE") if node == os.environ.get("SAIPH_HOST") else "") or ("nested" if CFG["rf"] else "direct")
    if node in LOGIN_HOSTS:
        host = CFG["rf"] if node.startswith("rockfish") else CFG["sj"]
        return "ssh -t %s tmux attach -t %s" % (host or node, target)
    if route == "direct" or not CFG["rf"]:
        return "ssh -t %s tmux attach -t %s" % (node, target)
    return "ssh -t %s ssh -t %s tmux attach -t %s" % (CFG["rf"], node, target)


def session_actions(s, g):
    if g.get("notmux"):
        return ""
    ended = bool(s.get("ended") or s.get("unreachable"))
    return ('<div class="acts" id="acts" data-key="%s" data-name="%s">'
            '<button type="button" class="btn2" data-act="esc" title="Press Esc in the agent\u2019s terminal: it stops what it is doing and waits for you"%s>Interrupt</button>'
            '<button type="button" class="btn2" data-act="copy" data-cmd="%s" title="%s">Copy terminal command</button>'
            '<button type="button" class="btn2 dng" data-act="end" title="Close this tmux session (stops the agent; its conversation stays in its logs)"%s>End session\u2026</button>'
            '<div class="actmsg" id="actmsg"></div></div>' % (
                esc(s["key"]), esc(s["session"]), " disabled" if ended else "", esc(attach_cmd(s)),
                esc("Copies: " + attach_cmd(s)), " disabled" if ended else ""))


def launch_dialog():
    return ('<dialog id="mc-launch" class="mcdlg"><form method="dialog" class="lf" onsubmit="return false">'
            '<div class="lh"><h2>New session</h2><button class="x" type="button" data-close aria-label="Close">\u00d7</button></div>'
            '<div class="lrow"><span class="ll">Agent</span><div class="seg" role="radiogroup">'
            '<label><input type="radio" name="agent" value="codex" checked><span>Codex</span></label>'
            '<label><input type="radio" name="agent" value="claude"><span>Claude</span></label></div></div>'
            '<div class="lusage" id="l-usage"></div>'
            '<div class="lrow" id="l-homerow"><label class="ll" for="l-home">Command</label><select id="l-home" title="Which Codex command (and so which account) starts the agent"></select></div>'
            '<div class="lrow"><label class="ll" for="l-model">Model</label><select id="l-model"></select>'
            '<label class="ll2" for="l-effort">Thinking</label><select id="l-effort"></select></div>'
            '<div class="lrow"><label class="ll" for="l-name">Name</label><input id="l-name" autocomplete="off" spellcheck="false" placeholder="e.g. phase3-hcp" maxlength="60">'
            '<span class="ll2" id="l-onhost"></span><select id="l-host"></select></div>'
            '<div class="lrow"><label class="ll" for="l-dir">Folder</label><input id="l-dir" autocomplete="off" spellcheck="false"></div>'
            '<div class="lnote">Created if it doesn\u2019t exist. Your first message can send the agent anywhere else.</div>'
            '<label class="ll" for="l-text">First message <span class="mut">(optional)</span></label>'
            '<textarea id="l-text" rows="5" placeholder="What should the agent do? Long instructions are saved in the folder as START_TASK.md (START_GOAL.md for a goal) and the agent is asked to read them."></textarea>'
            '<label class="lgoal" id="l-goalrow"><input type="checkbox" id="l-goal"> Make it a Codex goal (/goal): it keeps working toward it across turns</label>'
            '<div class="lbtns"><span class="lmsg" id="l-msg"></span><button type="button" class="btn2" id="l-check">Check</button>'
            '<button type="button" class="sendbtn" id="l-go">Start session</button></div>'
            '<div class="lreply" id="l-reply" hidden><input id="l-ans" autocomplete="off" placeholder="Your answer, as you would type it (e.g. 1 or y)" maxlength="200">'
            '<button type="button" class="btn2" id="l-ansgo">Send answer</button><button type="button" class="btn2" id="l-enter">Press Enter</button></div>'
            '<pre class="screen lscr" id="l-scr" hidden></pre></form>'
            '<script type="application/json" id="mc-launch-data">%s</script></dialog>' % json.dumps(LAUNCH).replace("</", "<\\/"))


LAUNCH = {}   # what the "New session" dialog offers (also written to state/launch.json for open pages)
REDS = []     # every red alert right now, for desktop notifications


def _windows(rl_items, t):
    out = []
    for lab0, w in rl_items:
        if not isinstance(w, dict):
            continue
        used = w.get("used_percent", w.get("used_percentage"))
        if used is None:
            continue
        wm = w.get("window_minutes") or 0
        lab = {"five_hour": "5 h", "seven_day": "week"}.get(lab0) or (
            "week" if wm >= 7 * 1440 - 60 else ("%d h" % round(wm / 60) if wm else lab0))
        rs = w.get("resets_at")
        if isinstance(rs, str):
            try:
                rs = dt.datetime.fromisoformat(rs.replace("Z", "+00:00")).timestamp()
            except ValueError:
                rs = None
        if not rs and w.get("resets_in_seconds"):
            rs = t + float(w["resets_in_seconds"])
        out.append({"label": lab, "used": round(float(used), 1), "resets": rs})
    return out


def usage_info(shared):
    """Remaining Codex usage (newest session log of each Codex home) and Claude usage (what Claude reported to the
    status line of a session started from the dashboard)."""
    docs = [nd.get("doc") or {} for nd in shared["nodes"].values()] + [(x.get("doc") or {}) for x in shared["logins"].values()]
    docs.append(shared.get("local") or {})
    codex, claude = {}, None
    for doc in docs:
        for s in doc.get("sessions") or []:
            rl, path, t = s.get("rate"), s.get("path") or "", s.get("mtime") or 0
            if not isinstance(rl, dict) or "/sessions/" not in path:
                continue
            home = path.split("/sessions/")[0]
            if home in codex and codex[home]["t"] >= t:
                continue
            codex[home] = {"home": home, "label": codex_label(home), "plan": rl.get("plan_type"), "t": t,
                           "windows": _windows([(k, rl.get(k)) for k in ("primary", "secondary")], t),
                           "reached": rl.get("rate_limit_reached_type")}
        cu = doc.get("claude_usage")
        if isinstance(cu, dict) and isinstance(cu.get("rate_limits"), dict) and (claude is None or (cu.get("t") or 0) > claude["t"]):
            claude = {"t": cu.get("t") or 0, "model": cu.get("model"),
                      "windows": _windows(sorted(cu["rate_limits"].items(), key=lambda kv: kv[0] != "five_hour"), cu.get("t") or 0)}
    return {"codex": sorted(codex.values(), key=lambda u: -u["t"]), "claude": claude}


def codex_label(home):
    b = os.path.basename(home.rstrip("/"))
    return "default" if b == ".codex" else (b[len(".codex-"):] if b.startswith(".codex-") else b)


def _host_launch(pr, root, dflt):
    """Launch options for one host, from its agent probe: Codex accounts (your codex_* commands) and models, Claude."""
    homes, base_models, byhome = [], [], {}
    for h in pr.get("codex_homes") or []:
        models = [{"slug": m["slug"], "name": m.get("name") or m["slug"], "levels": m.get("levels") or [], "default": m.get("default"),
                   "desc": m.get("desc") or ""} for m in h.get("models") or [] if m.get("slug") and m.get("visibility") != "hide"]
        byhome[h["dir"]] = {"models": models, "model": (h.get("config") or {}).get("model"),
                            "effort": (h.get("config") or {}).get("model_reasoning_effort")}
        if h["dir"] == dflt or not base_models:
            base_models = models or base_models
    # your own commands (codex_chat, codex_api, ...) each pick an account; plain `codex` uses ~/.codex
    home_dir = pr.get("home") or "~"
    cmds = [("codex", os.path.join(home_dir, ".codex"))]
    for w, wi in sorted((pr.get("wrappers") or {}).items()):
        hd = (wi or {}).get("home") or os.path.join(home_dir, ".codex-" + re.split(r"[_-]", w, maxsplit=1)[1])
        cmds.append((w, hd.replace("~", home_dir, 1) if hd.startswith("~") else hd))
    for w, hd in cmds:
        hi = byhome.get(hd) or {}
        homes.append({"cmd": w, "dir": hd, "label": w + (" (plain, uses ~/.codex)" if w == "codex" else ""), "default": False,
                      "models": hi.get("models") or base_models, "model": hi.get("model"), "effort": hi.get("effort")})
    pick = next((h for h in homes if h["dir"] == dflt and h["cmd"] != "codex"), None) or next((h for h in homes if h["dir"] == dflt), None)
    if pick:
        pick["default"] = True
    homes.sort(key=lambda h: (not h["default"], h["cmd"] != "codex", h["cmd"]))
    chelp = (pr.get("claude") or {}).get("help") or ""
    mm = re.search(r"--effort <level>[\s\S]{0,200}?\(([a-z, ]+)\)", chelp)
    clevels = [x.strip() for x in mm.group(1).split(",")] if mm else []
    cset = pr.get("claude_settings") or {}
    cmodels = [{"slug": a, "name": n} for a, n in (("fable", "Fable (latest)"), ("opus", "Opus (latest)"), ("sonnet", "Sonnet (latest)"),
                                                   ("haiku", "Haiku (latest)"))]
    return {"root": root, "probed": pr.get("ts"),
            "codex": {"available": bool((pr.get("codex") or {}).get("path")), "version": (pr.get("codex") or {}).get("version"),
                      "homes": homes, "goals": True},
            "claude": {"available": bool((pr.get("claude") or {}).get("path")), "version": (pr.get("claude") or {}).get("version"),
                       "models": cmodels, "levels": clevels, "model": cset.get("model") or "", "effort": cset.get("effortLevel") or ""}}


def launch_info(shared, snaps):
    hosts = [n for n in CFG["nodes"].split() if n]
    docs = dict((n, nd.get("doc") or {}) for n, nd in shared["nodes"].items())
    for n, d in docs.items():
        if n not in hosts and d.get("ok"):
            hosts.append(n)
    hosts = [h for h in hosts if not any(w in h.lower() for w in NEVER)]
    host = os.environ.get("SAIPH_HOST") or (hosts[0] if hosts else "")
    usage = usage_info(shared)
    per = {}
    for h in hosts:
        pr = docs.get(h, {}).get("probe") or {}
        roots = {}
        for x in snaps:   # the folder your sessions on that host work in (.../WORK/...)
            c = (x.get("goal") or {}).get("cwd") or ""
            if x.get("node_name", x.get("node")) == h and "/WORK/" in c:
                r = c[:c.index("/WORK/") + 5]
                roots[r] = roots.get(r, 0) + 1
        root = (os.environ.get("LAUNCH_ROOT") if h == host else "") or (max(roots, key=roots.get) if roots else (pr.get("home") or "~"))
        # the home plain `codex` uses: CODEX_HOME if your shell sets it, otherwise the one your running sessions write to
        dflt = pr.get("codex_home_env") or (next((u["home"] for u in usage["codex"]), "") if h == host else "") \
            or os.path.join(pr.get("home") or "~", ".codex")
        per[h] = _host_launch(pr, root, dflt)
    info = dict(per.get(host) or _host_launch({}, os.environ.get("LAUNCH_ROOT") or "~", "~/.codex"))
    info.update({"hosts": hosts, "host": host, "usage": usage, "per": per})
    return info


def usage_html(u):
    """One line per agent: how much of each usage window is left and when it resets."""
    def wins(ws):
        out = []
        for w in ws:
            left = max(0, 100 - w["used"])
            cls = "bad-t" if left < 10 else ("warn-t" if left < 25 else "ok-t")
            rs = (" · resets %s" % time.strftime("%a %-I:%M %p", time.localtime(w["resets"]))) if w.get("resets") else ""
            out.append('<b class="%s">%d%%</b> left <span class="mut">(%s)%s</span>' % (cls, left, esc(w["label"]), rs))
        return "; ".join(out)
    parts = []
    for c in (u or {}).get("codex") or []:
        if c["windows"]:
            parts.append('<span class="ug"><span class="kind codex">Codex%s</span> %s%s</span>' % (
                (" " + esc(c["label"])) if c["label"] not in ("default", "chat") or len(u["codex"]) > 1 else "",
                wins(c["windows"]), (" · <span class='bad-t'>limit reached</span>" if c.get("reached") else "")))
    cl = (u or {}).get("claude")
    if cl and cl["windows"]:
        parts.append('<span class="ug"><span class="kind claude">Claude</span> %s <span class="mut">(as of %s)</span></span>' % (wins(cl["windows"]), esc(ago(cl["t"]))))
    else:
        parts.append('<span class="ug" title="Claude\u2019s usage shows once a Claude session started from here replies"><span class="kind claude">Claude</span> '
                     '<span class="mut">no reading yet</span></span>')
    return '<div class="usage">%s</div>' % "".join(parts)


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
            '<header><div><h1><a class="hback" href="%s.html" title="Back to the session page">%s</a></h1><p class="sub">%s on %s \u00b7 <span id="n"></span> \u00b7 new messages appear by themselves when opened with mctl</p></div>'
            '<a class="np" href="%s.html">\u2190 Session page</a></header>'
            '<div class="ctl"><input id="q" type="search" placeholder="Search the conversation">%s'
            '<select id="order"><option value="chat">Chat order</option><option value="new">Newest first</option></select><button id="jump">Latest \u2193</button></div>'
            '<div id="list" class="chat"></div><div class="composer">%s</div></main>%s<script>%s</script>%s</body></html>') % (
        HEAD_META, esc(gname), CSS, CONV_CSS, esc(gkey), hue(gkey), appbar(nav, gkey, "goals", True, snap.get("collected")), esc(gkey), esc(gname),
        esc((snap["goal"].get("agent_kind") or "agent").capitalize()), esc(LOGIN_HOSTS.get(snap.get("node_name"), snap.get("node_name") or "?")),
        esc(gkey), chips, send_box(snap, compact=True), scripts + PEND_JS + terms_json(dict(snap, key=gkey)), CONV_JS, SEND_JS + REFRESH_JS + TOOLS_JS + TERMS_JS)
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
    try:
        tidy_contracts(goals)
    except Exception as e:
        sys.stderr.write("tidy contracts: %s\n" % e)
    for d in (os.path.join(STATE, "goals"), os.path.join(MC_DIR, "goals")):
        os.makedirs(d, exist_ok=True)
    if not goals:  # no agent sessions anywhere: still compute shared resources for the overview
        n0 = (CFG["nodes"].split() or ["node"])[0]
        goals = [{"name": "(no goals)", "node": n0, "key": "_none", "tm": {}, "doc": None, "pseudo": True}]
    snaps = []
    shared["goal_dirs"] = {}   # each session's working folders: jobs inside another session's folder are its business
    for g in goals:
        try:
            shared["goal_dirs"][g["key"]] = [c.rstrip("/") for c in goal_doc(g).get("goal_cwds") or [] if c and c != "/"]
        except Exception:  # noqa
            pass
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
    global LAUNCH, REDS
    try:
        LAUNCH = launch_info(shared, real)
    except Exception as e:  # the dialog is optional; never let it break the dashboard
        sys.stderr.write("launch options: %s\n" % e)
        LAUNCH = {}
    try:
        wjson(os.path.join(STATE, "launch.json"), LAUNCH)
    except OSError:
        pass
    REDS = [{"k": x["key"] + ":" + f["key"], "t": "%s: %s" % (x["session"], f["title"])} for x in real for f in x["flags"] if f["sev"] == "red"]
    REDS += [{"k": f["key"], "t": f["title"]} for f in shared["conn_flags"] if f.get("sev") == "red"]
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
    gone = {f["key"] for x in snaps for f in x["flags"] if f.get("acked")}   # dismissed on a session page: not repeated here
    for x in snaps:
        for f in x["flags"]:
            if f["area"] == "compute" and f["key"] not in seen and f["key"] not in gone:
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
                      "plan_now": x["plan"]["now"], "crit_met": sum(1 for r in crit if r["status"] == "met"), "crit_total": len(crit), "terms_new": ((x.get("terms") or {}).get("new24") or []), "terms_changed": ((x.get("terms") or {}).get("changed24") or []),
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
    o.append('<div class="st %s">%s%s</div>' % (scls, esc(stxt), ('<span class="ago"> \u00b7 active %s</span>' % esc(ago(g["last"]))) if g.get("last") and not gray else ""))
    if g.get("title") and g["title"] != g["name"]:
        o.append('<div class="obj" title="%s">%s</div>' % (esc(trunc(g["title"], 600)), esc(trunc(g["title"], 160))))
    w, we = g.get("latest_words"), g.get("latest_epoch")
    if w and len(w) < 200 and g.get("latest_update"):
        w, we = g["latest_update"], g.get("latest_update_epoch") or we
    if w:
        o.append('<div class="words"><div class="wt"><span class="mut">%s \u00b7</span> %s</div></div>' % (esc(ago(we)), esc(trunc(re.sub(r"[*`#]", "", w).replace("\n", " "), 420))))
    meta = []
    if g.get("context_left") is not None:
        meta.append('<b class="%s">%d%%</b> context free' % ("warn-t" if g["context_left"] <= 15 else "", g["context_left"]))
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
        meta.append(esc(" ".join(x for x in (g["model"], g.get("effort")) if x)))
    cnt = "".join((('<span class="cnt red">%d</span>' % g["reds"]) if g["reds"] else "", ('<span class="cnt amber">%d</span>' % g["ambers"]) if g["ambers"] else ""))
    o.append('<div class="meta"><span class="facts">%s</span>%s</div>' % (" \u00b7 ".join(meta), cnt))
    if g.get("top") and not gray:
        o.append('<div class="topf %s" title="%s">%s</div>' % ("red" if g["reds"] else "amber" if g["ambers"] else "", esc(g["top"]), esc(trunc(g["top"], 140))))
    o.append("</a>")
    return "".join(o)


def render_overview(ov):
    reds = [f for f in ov["flags"] if f["sev"] == "red"]
    goals = ov["goals"]
    o = ['<!doctype html><html lang="en"><head>%s<title>%smission control</title><style>%s</style>%s</head><body class="ov">' % (
             HEAD_META, ("(%d) " % len(reds)) if reds else "", CSS, hide_css())]
    o.append(appbar(ov.get("nav") or [], None, "root", False, ov.get("collected")))
    o.append("<main>" + reds_json())
    needs = [f for f in ov["flags"] if f.get("needs_you")]
    hosts_with = []
    for g in goals:
        if g["node"] not in hosts_with:
            hosts_with.append(g["node"])
    live = [g for g in goals if not g["unreachable"] and not g["ended"]]
    chips = "".join('<span class="hc" title="%s"><span class="dot %s"></span>%s</span>' % (
        esc(h.get("detail") or ""), "" if h["ok"] else "red", esc(h["name"])) for h in ov["hosts"])
    o.append('<header class="ovh"><div><p class="ovsum"><b>%d agent session%s</b> running on %s%s</p>%s</div><div class="chips">%s'
             '<button type="button" class="pill bell" id="mc-bell" title="Desktop alerts for new red items while a mission control page is open">'
             'Alerts off</button></div></header>' % (
        len(live), "" if len(live) == 1 else "s", esc(", ".join(LOGIN_HOSTS.get(h, h) for h in hosts_with)) or "no hosts",
        (" \u00b7 <b class='bad-t'>%d need%s you</b>" % (len(needs), "s" if len(needs) == 1 else "")) if needs else "",
        usage_html(LAUNCH.get("usage")) if LAUNCH else "", chips))
    o.append('<div id="hidden-bar" class="hbar" style="display:none"></div>')
    if needs:
        o.append('<section class="card"%s><h2>Needs you</h2>' % sec("o", "needs"))
        for i, f in enumerate(needs[:20]):
            if i == 4:
                o.append("<details style='border:none;padding:0;margin:4px 0 0;box-shadow:none'><summary class='small'>%d more</summary>" % (len(needs[:20]) - 4))
            h = flag_html(f, bar=False)
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
        o.append('<section class="card"%s><h2>Watchdog</h2>%s</section>' % (sec("o", "watchdog"), "".join(flag_html(f, bar=False) for f in comp)))
    if ov["others"]:
        o.append("<details" + sec("o", "others") + "><summary>Other agent logs seen recently (not running now): %d</summary><table><tr><th>Host</th><th>Kind</th><th>Last active</th><th>Calls</th><th>Folder</th></tr>" % len(ov["others"]))
        for x in ov["others"]:
            o.append("<tr><td>%s</td><td>%s</td><td class=mut>%s</td><td>%s</td><td class='mut small mono'>%s</td></tr>" % (
                esc(x["host"]), esc(x["kind"]), esc(ago(x["last"])), esc(x["calls"]), esc(trunc(x["cwd"], 60))))
        o.append("</table></details>")
    hs = " \u00b7 ".join('%s <span style="color:var(--%s)">%s</span>' % (esc(h["name"]), "grn" if h["ok"] else "red", "ok" if h["ok"] else "down") for h in ov["hosts"])
    o.append('<footer><div>%s</div><div>Collected %s, every %d min. Sessions are found by themselves (tmux or not) on the watched nodes, the login nodes and this PC.</div>'
             '<div>Mission control types into a session only when you press a button%s. Press a block\u2019s \u00d7 to hide it here; <span class="mono">mctl hide NAME</span> hides it everywhere.%s</div></footer></main>%s</body></html>' % (
                 hs, esc(time.strftime("%Y-%m-%d %H:%M", time.localtime(ov["collected"]))) if ov.get("collected") else "never", CFG["interval"], never_note(), hidden_note(), HIDE_JS + REFRESH_JS + TOOLS_JS))
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
            if g.get("terms_new") or g.get("terms_changed"):   # the agents' words: new ones, and ones whose meaning moved
                lines.append("  words: %s%s" % (("new " + ", ".join(g["terms_new"][:6])) if g.get("terms_new") else "",
                                                ("%schanged %s" % ("; " if g.get("terms_new") else "", ", ".join(g["terms_changed"][:6]))) if g.get("terms_changed") else ""))
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
@media (prefers-color-scheme:dark){:root{--bg:#0f1219;--card:#171b26;--fg:#eceef5;--mut:#a3a8bd;--faint:#6d7387;--line:rgba(175,185,220,.13);--bar:#272c3a;--shadow:none;
--violet:#b39dff;--sky:#62bfff;--teal:#2fdcc0;--lime:#a6e35c;--amber:#ffc25a;--orange:#ff9a5c;--rose:#ff6f92;--pink:#f78ad6;--gold:#ffd54a;--slate:#94a0d4;
--redbg:#3a1a2a;--ambbg:#33291c;--grnbg:#1b2c22;--blubg:#16263f;--purbg:#271f4f;--tealbg:#0f2e31;
--c-you:#2d6ae0;--t-agent:#27174f;--t-you:#18233a;--t-sub:#0b3836;--c-task:#35b09f;--t-task:#11303a;--t-event:#2d2a2c;--t-review:#1c202b;
--chatbg:#0a0c11;--hu0:#b197fc;--hu1:#74b0ff;--hu2:#3bd9f0;--hu3:#38d9a9;--hu4:#a9e34b;--hu5:#ffd43b;--hu6:#ff9f5a;--hu7:#f783c4}}
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
.row.pend .bub{background:var(--t-you);color:var(--mut);border:1px dashed color-mix(in srgb,var(--c-you) 60%,transparent);box-shadow:none}.row.pend .bub .who,.row.pend .bub .tm{color:var(--mut)}.pw{white-space:pre-wrap}
.pst{display:flex;align-items:center;gap:6px;margin-top:6px;font-size:11.5px;color:var(--mut)}.pend.warn .pst{color:var(--amb)}.pend.bad .pst{color:var(--red)}
.pend.warn .bub{border-color:color-mix(in srgb,var(--amb) 60%,transparent)}.pend.bad .bub{border-color:color-mix(in srgb,var(--red) 60%,transparent)}
.pdot{width:7px;height:7px;border-radius:50%;background:var(--c-you);animation:mcpulse .9s ease-in-out infinite alternate}@keyframes mcpulse{from{opacity:.25}to{opacity:1}}@media (prefers-reduced-motion:reduce){.pdot{animation:none}}
.pendrows .row{margin:10px 0}.pendrows:empty{display:none}
.tw{text-decoration:underline dotted;text-decoration-thickness:1.5px;text-underline-offset:3px;text-decoration-color:color-mix(in srgb,currentColor 50%,transparent);cursor:help;border-radius:3px}
.tw:hover,.tw:focus-visible{background:color-mix(in srgb,var(--c-you) 16%,transparent);outline:none}.tw.warn{text-decoration-color:var(--amb)}.tw.wait{text-decoration-style:dashed}
.tpop{position:absolute;z-index:70;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.28);padding:12px 14px 10px;font-size:13.5px;line-height:1.45;text-align:left}
.tpop.sheet{position:fixed;left:8px;right:8px;bottom:8px;max-height:68vh;overflow:auto}
.tph{display:flex;align-items:center;gap:8px;margin-bottom:6px}.tph b{font-size:15.5px}.tpx{margin-left:auto;border:0;background:none;color:var(--mut);font-size:20px;line-height:1;cursor:pointer;padding:2px 4px}
.tmn{margin:2px 0 4px}.tmeta{font-size:12px;color:var(--mut);margin-top:6px}.tl{display:block;font-size:11.5px;color:var(--mut);margin-bottom:2px}
.tq{margin:8px 0;padding:1px 0 1px 10px;border-left:2px solid var(--line)}.tq.amb{border-left-color:var(--amb)}.tq div{margin-top:3px}
.tpop .cacts{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}.tpop .actmsg{flex-basis:100%}.tpl{display:inline-block;margin-top:8px;font-size:12px}
.tchip{position:absolute;z-index:71;background:var(--card);border:1px solid var(--line);border-radius:10px;box-shadow:0 6px 20px rgba(0,0,0,.25);padding:6px}
.tchip .cacts{display:flex;flex-wrap:wrap;gap:6px;align-items:center;max-width:min(420px,calc(100vw - 32px))}.tchip .actmsg{flex-basis:100%;font-size:12px}
.tbad{display:inline-block;font-size:11px;line-height:1.5;border:1px solid var(--line);border-radius:999px;padding:0 7px;margin-left:6px;color:var(--mut);white-space:nowrap;vertical-align:1px}
.tbad.amb{border-color:color-mix(in srgb,var(--amb) 60%,transparent);color:var(--amb)}
details.trow{background:none;border:0;border-top:1px solid var(--line);border-radius:0;box-shadow:none;margin:0;padding:9px 0}.tlist{margin-top:4px}.tlist>.trow:first-child{border-top:0}.trow>summary{list-style:none;cursor:pointer}.trow>summary::-webkit-details-marker{display:none}
.trow .tn{font-weight:650}.tmean{display:block;color:var(--mut);font-size:13px;margin-top:2px}.trow[open] .tmean{display:none}.trow[open] .tline,.trow .tbody{font-size:13.5px}
.tbody{padding:4px 0 2px}.tline{margin:2px 0}.tbody .cacts{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}.tbody .actmsg{flex-basis:100%}
.tfind{width:100%;max-width:320px;margin:2px 0 6px;padding:6px 10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg);font:inherit;font-size:13px}
.cacts.tsug{display:inline-flex;width:auto;max-width:100%;margin-top:3px;align-items:center;gap:4px;flex-wrap:wrap;border:1px solid var(--line);border-radius:999px;padding:2px 3px 2px 10px;margin:3px 4px 3px 0;font-size:13px}
.tsug .actmsg{font-size:11.5px}.ttip{font-size:12px;color:var(--faint);margin:-2px 0 8px}.btn2.tsm{padding:1px 8px;font-size:12px}
.evt{display:flex;align-items:center;gap:10px;margin:16px 0;font-size:12px}.evt:before,.evt:after{content:"";flex:1;height:1px;background:var(--line)}
.evt span{padding:3px 12px;border-radius:99px;background:var(--t-event);color:var(--amb)}
details.cmp{margin:16px auto;max-width:760px;border:1px dashed color-mix(in srgb,var(--amb) 45%,transparent);border-radius:12px;padding:6px 12px;background:none;box-shadow:none}
details.cmp>summary{font-size:12px;color:var(--amb);font-weight:600;cursor:pointer}details.cmp .mb{font-size:13px;margin-top:8px;max-height:60vh;overflow:auto}
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
html{background:var(--bg);color-scheme:light dark}main{overflow-wrap:break-word}header>div{min-width:0;flex:1}h1,.sub,.objective,.xp{overflow-wrap:anywhere}
@media (max-width:640px){.appbar,.ctl{-webkit-backdrop-filter:none!important;backdrop-filter:none!important}.appbar{background:var(--card)}.ctl{background:var(--bg)}
main table{display:block;max-width:100%;overflow-x:auto}}
button.fresh{display:inline-flex;align-items:center;gap:7px;flex:none;font:inherit;font-size:12px;border:1px solid transparent;cursor:pointer}
button.fresh svg{opacity:.7;flex:none}button.fresh:hover{border-color:currentColor}button.fresh:hover svg{opacity:1}
button.fresh:focus-visible{outline:2px solid currentColor;outline-offset:2px}
.fresh.spin svg{opacity:1;animation:mcspin .9s linear infinite}@keyframes mcspin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.fresh.spin svg{animation-duration:2.5s}}
.newb{flex:none;cursor:pointer;font:inherit;font-size:12px;font-weight:600;border:1px solid color-mix(in srgb,var(--lime) 45%,transparent);
background:color-mix(in srgb,var(--lime) 14%,transparent);color:color-mix(in srgb,var(--lime) 80%,var(--fg));margin-right:2px}
.newb:hover{border-color:var(--lime)}
.mcdlg{border:1px solid var(--line);border-radius:16px;background:var(--card);color:var(--fg);padding:0;width:min(640px,94vw);max-height:92vh}
.mcdlg::backdrop{background:rgba(5,7,12,.62)}
.lf{display:flex;flex-direction:column;gap:9px;padding:16px 18px 18px}.lh{display:flex;align-items:center;justify-content:space-between}
.lh h2{margin:0;font-size:17px}.lh .x{border:none;background:none;color:var(--mut);font-size:22px;cursor:pointer;line-height:1}
.lrow{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.lrow[hidden],.lgoal[hidden]{display:none}
.ll{min-width:62px;font-size:12.5px;color:var(--mut);font-weight:600}.ll2{font-size:12.5px;color:var(--mut);margin-left:6px}
.lf select,.lf input[type=text],.lf input:not([type]),.lf textarea{font:inherit;font-size:13.5px;color:var(--fg);background:var(--bg);
border:1px solid var(--line);border-radius:8px;padding:6px 8px}
.lf input:not([type]){flex:1;min-width:140px}#l-dir{flex:1;font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
.lf textarea{width:100%;resize:vertical;min-height:90px;line-height:1.5}.lf select:focus,.lf input:focus,.lf textarea:focus{outline:2px solid var(--h);outline-offset:1px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:999px;overflow:hidden}.seg label{cursor:pointer}
.seg input{position:absolute;opacity:0;pointer-events:none}.seg span{display:inline-block;padding:5px 16px;font-size:13px;color:var(--mut)}
.seg input:checked+span{background:color-mix(in srgb,var(--violet) 28%,transparent);color:var(--fg);font-weight:600}
.seg input:disabled+span{opacity:.4;cursor:not-allowed}.seg input:focus-visible+span{outline:2px solid var(--h)}
.lusage{font-size:12.5px;color:var(--mut);background:var(--bg);border-radius:8px;padding:7px 10px;line-height:1.6}
.lnote{font-size:12px;color:var(--faint);margin:-4px 0 2px 70px}.lgoal{font-size:12.5px;color:var(--mut);display:flex;gap:7px;align-items:center}
.lbtns{display:flex;gap:8px;align-items:center;justify-content:flex-end;flex-wrap:wrap;margin-top:4px}
.lmsg{flex:1;font-size:12.5px;color:var(--mut);min-width:160px}.lmsg.ok{color:var(--grn)}.lmsg.bad{color:var(--red)}
.btn2{font:inherit;font-size:12.5px;padding:6px 12px;border-radius:999px;border:1px solid var(--line);background:none;color:var(--fg);cursor:pointer}
.btn2:hover{border-color:var(--h)}.btn2:disabled{opacity:.45;cursor:not-allowed}.btn2.dng{color:var(--rose)}
.lscr{max-height:260px;margin:0}.lreply{display:flex;gap:8px;flex-wrap:wrap}.lreply[hidden]{display:none}.lreply input{flex:1;min-width:180px}
@media (max-width:640px){.mcdlg{width:100vw;max-width:100vw;max-height:100vh;height:100%;border-radius:0;margin:0}.lnote{margin-left:0}}
.usage{display:flex;flex-direction:column;gap:3px;margin-top:6px;font-size:12.5px;color:var(--mut)}.ug .kind{margin-right:6px}
.ok-t{color:var(--grn)}.warn-t{color:var(--amb)}
.acts{display:flex;gap:6px;flex-wrap:wrap;align-items:center;justify-content:flex-end;max-width:420px}
.actmsg{flex-basis:100%;text-align:right;font-size:12px;color:var(--mut);overflow-wrap:anywhere}.actmsg.ok{color:var(--grn)}.actmsg.bad{color:var(--red)}
@media (max-width:640px){.acts{justify-content:flex-start}.actmsg{text-align:left}}
.qa{margin-top:10px}.qa .qctx{font-size:12px;color:var(--mut);overflow-wrap:anywhere;margin:2px 0}
.qbtns{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}.qbtns .btn2{text-align:left;white-space:normal;max-width:100%;border-radius:12px}
.qa .actmsg{text-align:left;margin-top:6px}.qa .sendscr{margin-top:8px}
.cacts{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:8px}.cacts.tight{margin:0;min-width:120px}
.cacts .cform{margin:0;display:inline-flex;gap:6px;flex-wrap:wrap}
details.stor,details.clog{background:none;border:0;box-shadow:none;padding:0;margin:2px 0 0}details.stor>summary,details.clog>summary{font-weight:400;font-size:12.5px}
details.stor[open]{padding-bottom:6px}.cacts .cform input[name=val]{width:100px}
.cacts .actmsg,.cform .actmsg{flex-basis:100%;text-align:left}.btn2.sm{padding:3px 10px;font-size:12px}
.cform{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:8px}.cform input,.cform select{background:var(--chatbg,#0a0c11);color:inherit;
border:1px solid var(--line,#2a3142);border-radius:8px;padding:4px 8px;font:inherit;font-size:13px;min-width:0}.cform input[name=name]{width:110px}
.cform input[name=val]{width:110px}.cform input.wide{flex:1 1 240px}.clist{margin:6px 0 0 18px;padding:0}.clist li{margin:3px 0}
.clist .cacts{display:inline-flex;margin:0 0 0 6px}.tscroll{overflow-x:auto}.clog{margin-top:8px}
.bell{cursor:pointer;font:inherit;font-size:12px;border:1px solid var(--line);background:none;color:var(--mut)}.bell.on{color:var(--grn);border-color:var(--grn)}
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

/* cleanup pass: fewer boxes and words; facts small and quiet; colour only where it means something */
h2{font-size:15px}.h2n{font-weight:400;font-size:12px;color:var(--faint);margin-left:6px}
header{margin:4px 0 12px}.sub{font-size:12.5px}.sub2{color:var(--faint);font-size:12px;margin:2px 0 0;overflow-wrap:anywhere}
.objective{margin-top:8px}.objective b{font-weight:600;color:var(--fg);margin-right:4px}
.ovh{gap:10px}.ovsum{margin:2px 0 6px;font-size:14px;color:var(--mut)}.ovsum b{color:var(--fg);font-weight:600}
.usage{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:12.5px}.ug{display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap}
.ovh .chips{gap:5px}.hc,.bell{font-size:11.5px;padding:2px 9px}
.quiet{background:none;border:none;padding:0 2px;margin:0 0 14px;font-size:13px}
.cards{gap:8px}.m{padding:9px 11px;border-radius:10px;box-shadow:none;background:color-mix(in srgb,var(--s) 6%,var(--card));border-color:var(--line)}
.m .l{font-size:11.5px;display:flex;align-items:center;gap:6px}.m .l:before{content:"";flex:none;width:6px;height:6px;border-radius:50%;background:var(--s)}
.m .v{font-size:17px;margin:1px 0}.m .s{font-size:11.5px}
@media (max-width:640px){.cards{grid-template-columns:repeat(3,minmax(0,1fr))}.m{padding:8px 9px}.m .v{font-size:15px}.m .l{font-size:11px}}
@media (max-width:360px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
.flag{padding:8px 11px;margin:6px 0;border-radius:9px;border-left-width:3px}.flag .t{font-size:13.5px}
.flag .d{font-size:12.5px;opacity:.85}.flag .a{font-size:12px;margin-top:3px}
.scard{padding:11px 13px}.scard .st{font-size:13px;margin:3px 0 1px}.st .ago{color:var(--mut);font-weight:400}
.scard .words{background:color-mix(in srgb,var(--t-agent) 65%,transparent);border-left-width:2px;padding:7px 10px;margin:7px 0}
.scard .meta{justify-content:space-between;align-items:center;flex-wrap:nowrap;gap:8px}.scard .facts{min-width:0}
.cnt{display:inline-flex;align-items:center;justify-content:center;min-width:20px;height:20px;padding:0 6px;border-radius:99px;font-size:11.5px;font-weight:700;margin-left:4px}
.cnt.amber{background:var(--ambbg);color:var(--amb)}.cnt.red{background:var(--redbg);color:var(--red)}
.topf{font-size:12px;color:var(--mut);margin-top:5px;padding-left:12px;position:relative;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.topf:before{content:"";position:absolute;left:0;top:.55em;width:6px;height:6px;border-radius:50%;background:var(--faint)}
.topf.amber:before{background:var(--amb)}.topf.red{color:var(--red)}.topf.red:before{background:var(--red)}
.hide-x{top:8px;right:8px;width:18px;height:18px;line-height:16px;font-size:12px;border:none;background:none;box-shadow:none}
@media (hover:none){.hide-x{opacity:.35}}
.rnote{font-size:11.5px;color:var(--faint);margin:-4px 0 6px 102px}
.gpus{display:flex;gap:4px;align-items:flex-end;max-width:380px}.gpu{flex:1;min-width:0;display:flex;flex-direction:column;align-items:center;font-size:10px;color:var(--mut);gap:2px;line-height:1}
.gb{width:100%;max-width:22px;height:20px;background:var(--bar);border-radius:4px;display:flex;align-items:flex-end;overflow:hidden}
.gb i{display:block;width:100%;background:linear-gradient(0deg,var(--teal),var(--lime) 60%,var(--amber));border-radius:3px}.gb i.w{background:var(--amber)}
.tl .e{padding:5px 0;gap:8px}.tl .tm{width:auto;min-width:42px;white-space:nowrap}.tl .w{width:62px}.tl .pill{font-size:11px;padding:1px 8px}
.tl .tx{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;cursor:pointer}.tl .tx.open{display:block}
details.fold{background:none;border:none;box-shadow:none;padding:0;margin:6px 0 0}details.fold>summary{font-weight:500;font-size:12.5px;color:var(--mut)}
td.mcell{width:1%;white-space:nowrap;text-align:right}
details.rowmenu{background:none;border:none;box-shadow:none;padding:0;margin:0;display:inline-block;text-align:left}
details.rowmenu>summary{list-style:none;font-weight:700;color:var(--mut);padding:0 9px;border-radius:8px;border:1px solid var(--line);font-size:14px;line-height:22px}
details.rowmenu>summary::-webkit-details-marker{display:none}details.rowmenu[open]>summary{color:var(--fg);border-color:var(--fg)}
details.rowmenu .cacts{flex-direction:column;align-items:stretch;gap:4px;margin-top:6px}
footer div{margin:2px 0}
.ibar{margin-top:6px;font-size:12px}.mrow{font-size:13px;margin:4px 0}.mrow>b{font-weight:600;margin-right:6px}
table.mtab td{vertical-align:top;padding-top:6px;padding-bottom:6px}table.mtab td:first-child{white-space:nowrap}.pchg{font-family:var(--mono,ui-monospace,monospace);font-size:13px;font-weight:600;margin:3px 0 4px;color:var(--fg)}
.pmeta{font-size:12.5px;color:var(--mut);margin:3px 0;line-height:1.45}.pmeta>b{font-weight:600;color:var(--fg);margin-right:6px}.pmeta.faint{color:var(--faint);font-size:11.5px}
ol.plist{margin:4px 0 6px;padding-left:22px;font-size:13px}ol.plist li{margin:5px 0;line-height:1.45}ol.plist .pbt{white-space:nowrap;margin-left:10px}
ol.plist .pbt .lnk{margin-left:8px}ol.plist .pbt .lnk.dng{color:var(--rose)}
details.dd{display:inline;border:none;padding:0;margin:0;background:none;box-shadow:none}details.dd>summary{display:inline;list-style:none;cursor:pointer;font-weight:inherit;font-size:inherit;color:inherit;border:none;padding:0;margin:0;background:none;box-shadow:none}
details.dd>summary::-webkit-details-marker{display:none}details.dd .full{display:none}details.dd[open] .full{display:inline}details.dd[open] .lead{display:none}
details.dd .mtog::before{content:"more";color:var(--sky);font-weight:600;font-size:12px}details.dd[open] .mtog::before{content:"less"}.ibar .clr{color:var(--faint);font-size:11.5px;margin-top:2px}
.ibtn{display:flex;flex-wrap:wrap;align-items:center;gap:2px 12px;margin-top:3px}
.lnk{background:none;border:none;padding:2px 0;font:inherit;font-size:12px;font-weight:600;color:var(--sky);cursor:pointer}
.lnk:hover{text-decoration:underline}.lnk:disabled{opacity:.5}.iid{font-family:ui-monospace,monospace;font-size:11px;color:var(--faint)}
.ans{margin:3px 0 2px;padding:5px 8px;border-radius:8px;background:color-mix(in srgb,var(--violet) 12%,transparent);color:var(--fg)}
.ans.mut{background:none;padding:0;color:var(--mut)}.as-fixed{color:var(--grn)}.as-intended,.as-explained{color:var(--sky)}
.as-need_decision{color:var(--amb)}.as-wontfix{color:var(--red)}.as-working{color:var(--mut)}
.flag.ok{--fc:var(--slate);opacity:.75}.card .flag.ok{background:none;border:none;border-bottom:1px solid var(--line);border-radius:0;padding:9px 0 9px 16px;margin:0;position:relative}
.card .flag.ok:before{content:"\u2713";position:absolute;left:0;top:8px;font-size:11px;color:var(--grn)}.flag.ok .t{color:var(--mut)}
.ibar .actmsg{text-align:left}h2 .askall{display:inline-flex;margin:0 0 0 8px;vertical-align:middle}h2 .askall .lnk{font-size:12px}

.card .flag.amber,.card .flag.info{background:none;border:none;border-radius:0;border-bottom:1px solid var(--line);padding:9px 0 9px 16px;margin:0;position:relative}
.card .flag.amber:before,.card .flag.info:before{content:"";position:absolute;left:2px;top:16px;width:7px;height:7px;border-radius:50%;background:var(--fc)}
.card h2+.flag.amber,.card h2+.flag.info{padding-top:2px}.card h2+.flag.amber:before,.card h2+.flag.info:before{top:9px}
.card .flag.info .t{color:var(--fg)}.card .flag.amber:last-of-type,.card .flag.info:last-of-type{border-bottom:none}
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


def question_html(q):
    """Answer buttons for a question on the agent's screen (each one types that answer into the session)."""
    if not q or not q.get("options"):
        return ""
    ctx = "".join('<div class="qctx mono">%s</div>' % esc(c) for c in q.get("context") or [])
    return ('<div class="qa" data-key="%s" data-sig="%s">%s<div class="qbtns">%s</div><div class="actmsg"></div></div>' % (
        esc(q.get("key", "")), esc(q["sig"]), ctx, "".join(
            '<button type="button" class="btn2%s" data-ans="%s">%s</button>' % (
                " dng" if str(o["label"]).lower().startswith("no") else "", esc(str(o["n"])),
                esc(("%s. %s" % (o["n"], o["label"])) if q["kind"] == "menu" else o["label"]))
            for o in q["options"])))


def act_btn(a, cls="btn2"):
    """A button that changes the contract (see cmd_contract_edit); its fields travel as data-c* attributes."""
    attrs = "".join(' data-c%s="%s"' % (k.replace("_", ""), esc(str(v))) for k, v in a.items() if k not in ("label", "danger", "key"))
    return '<button type="button" class="%s%s"%s>%s</button>' % (cls, " dng" if a.get("danger") else "", attrs, esc(a["label"]))


def lock_acts(name, values, cls_form=True):
    """Ways to lock a setting seen with these values: one of them, any of them, a bound (for settings where more is more
    accurate, e.g. ENCUT at least the smallest), or a rule of your own (the small form)."""
    vals = [str(v) for v in values][:5]
    acts = [{"label": "Only %s" % v, "op": "set", "name": name, "rule_op": "==", "val": v,
             "confirm": "Lock %s to %s? Other values then break the contract." % (name, v)} for v in vals]
    if len(vals) > 1:
        acts.append({"label": "Any of these %d" % len(vals), "op": "set", "name": name, "rule_op": "in", "val": "|".join(vals),
                     "confirm": "Allow %s to be %s, and nothing else?" % (name, " or ".join(vals))})
    nums = [num(v) for v in vals]
    op = IMPLICIT_OPS.get(name)
    if op and nums and all(x is not None for x in nums):
        if op == ">=":
            b, word = min(nums), "At least"
        elif op == "abs<=":
            b, word = max(abs(x) for x in nums), "At most |x|"
        else:
            b, word = max(nums), "At most"
        acts.append({"label": "%s %g" % (word, b), "op": "set", "name": name, "rule_op": op, "val": "%g" % b,
                     "confirm": "Lock %s to %s %g (the more accurate side stays allowed)?" % (name, op, b)})
    if cls_form:
        acts.append({"form": True, "name": name})
    return acts


def acts_html(acts, gkey):
    if not acts:
        return ""
    btns = []
    for a in acts:
        if a.get("form"):   # a rule of your own for this setting
            btns.append('<span class="cform"><input type="hidden" name="name" value="%s"><select name="rule_op"><option>==</option>'
                        '<option>&gt;=</option><option>&lt;=</option><option value="in">in (A|B)</option></select>'
                        '<input name="val" placeholder="other value" maxlength="80"><button type="button" class="btn2" data-cop="set">Set</button></span>'
                        % esc(a["name"]))
        else:
            btns.append(act_btn(a))
    return '<div class="cacts" data-ckey="%s">%s<div class="actmsg"></div></div>' % (esc(gkey), "".join(btns))


def flag_html(f, bar=True):
    return ('<div class="flag %s"><div class="t">%s</div>%s%s%s%s%s</div>' % (
        f["sev"], esc(f["title"]), prop_html(f["prop"], f.get("gkey") or "") if f.get("prop") else
        ('<div class="d">%s</div>' % clip_html(f["detail"], 300)) if f["detail"] else "",
        ('<div class="a">%s</div>' % esc(f["action"])) if f["action"] and not f.get("acked") else "", question_html(f.get("q")),
        acts_html(f.get("acts"), f.get("gkey") or "") if not f.get("acked") else "", item_bar(f) if bar else ""))


KIND_PILL = {"submit": ("submit", "ok"), "cancel": ("cancel", "warn"), "alloc": ("srun", "ok"), "delete": ("delete", "bad"),
             "edit": ("edit", "neu"), "git": ("git", "neu"), "agent": ("agent", "dec"), "goal": ("goal", "dec"),
             "ssh": ("ssh", "neu"), "plan": ("plan", "dec"), "error": ("error", "bad"), "event": ("event", "warn"),
             "you": ("you", "you"), "decision": ("decision", "dec"), "other": ("cmd", "neu")}


def resources_html(resources, nodes, c, hist):
    o = ['<h2>Resources <span class="h2n">from the scheduler</span></h2>']
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
        gps = node.get("gpus", [])
        if gps:   # all GPUs on one row: a small bar each, its use underneath (memory in the tooltip)
            cells = "".join('<span class="gpu" title="GPU %s: %d%% busy, %.0f GB in use"><span class="gb"><i style="height:%d%%" class="%s"></i></span>%d</span>' % (
                esc(gp.get("i")), gp.get("util", 0), gp.get("mem_used", 0) / 1024, max(4, min(100, gp.get("util", 0))),
                "w" if gp.get("util", 0) < 10 and gp.get("mem_used", 0) > 1024 else "", gp.get("util", 0)) for gp in gps)
            avg = sum(gp.get("util", 0) for gp in gps) / len(gps)
            o.append('<div class="row"><span class="n">%s GPUs</span><span class="x gpus">%s</span><span class="r">%.0f%% avg \u00b7 %.0f GB</span></div>' % (
                esc(nname), cells, avg, sum(gp.get("mem_used", 0) for gp in gps) / 1024))
    for r in resources:
        if not r["ok"]:
            o.append('<div class="row"><span class="n">%s</span><span class="x bad-t">unreachable</span><span class="r small">%s</span></div>' % (esc(r["name"]), esc(trunc(r.get("error"), 50))))
            continue
        eff = 1 - (r["idle_ch"] + r["failed_ch"]) / r["alloc_ch"] if r["alloc_ch"] else None
        o.append('<div class="row"><span class="n">%s</span><span class="x">%s</span><span class="r">%dR %dPD \u00b7 %d cores</span></div>' % (
            esc(r["name"]), bar(eff if eff is not None else 0, "eff" if (eff or 1) > .7 else "w") if eff is not None else '<span class="small mut">no finished jobs in 48 h</span>',
            r["running"], r["pending"], r["cores"]))
        if r["alloc_ch"]:
            o.append('<div class="rnote" title="The bar is the share of the last 48 h of core-hours used well">48 h: %.0f core-h \u00b7 %.0f idle \u00b7 %.0f failed</div>' % (
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
 el.className='pill fresh '+cls+(el.classList.contains('spin')?' spin':'');
 if(!(+el.getAttribute('data-hold')>Date.now())){(el.querySelector('.ft')||el).textContent=txt;if(!el.classList.contains('spin'))el.title=tip;}});}
window.mcFreshTick=tick;tick();setInterval(tick,15000);
})();
</script>"""


HEAD_META = ('<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
             '<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#0f1219"><meta name="theme-color" media="(prefers-color-scheme: light)" content="#f1f3fd"><meta name="apple-mobile-web-app-capable" content="yes">'
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
function spin(on,msg){if(!btn)return;btn.classList.toggle('spin',!!on);if(msg)say(msg,on?0:4000);else if(!on){btn.removeAttribute('data-hold');if(window.mcFreshTick)window.mcFreshTick();}}
function say(t,ms){var f=btn&&btn.querySelector('.ft');if(!f)return;f.textContent=t;btn.setAttribute('data-hold',ms?Date.now()+ms:9e15);
 if(ms)setTimeout(function(){btn.removeAttribute('data-hold');if(window.mcFreshTick)window.mcFreshTick();},ms+50);}
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
 applyX();if(window.mcHideScan)window.mcHideScan();if(window.mcNotify)window.mcNotify();if(window.mcPendPaint)window.mcPendPaint();if(window.mcTermsInit)window.mcTermsInit();window.scrollTo(0,y);
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
 if(btn.classList.contains('spin'))return;
 spin(true,'Checking\u2026');
 stamp().then(function(j){return pull().then(function(ok){if(ok)mt=j.mtime;return j;});}).then(function(j){
  var age=Date.now()/1000-(j.collected||0);
  if(age<240){spin(false,'Fresh \u00b7 again in '+Math.max(1,Math.ceil((240-age)/60))+' min');return;}   // at most every 4 min
  return fetch('/api/collect',{method:'POST',headers:{'Content-Type':'application/json','X-MC-Token':S.token},body:'{}'}).then(function(r){return r.json();}).then(function(c){
   if(c.message)btn.title=c.message;if(!c.started){spin(false,c.short||'Not now');return;}
   say('Collecting fresh data\u2026',0);
   var n=0,iv=setInterval(function(){n++;stamp().then(function(s){
    if(s.mtime>mt){clearInterval(iv);var m=s.mtime;pull().then(function(ok){if(ok)mt=m;spin(false);btn.title='Refresh now';});}
    else if(n>40){clearInterval(iv);spin(false);}}).catch(function(){});},5000);});
 }).catch(function(){spin(false);});});
})();
</script>"""

PEND_JS = r"""<script>
// Your messages from the send box, shown greyed in the chat until the agent's own log records them.
// States: sending, sent (typed into the agent's terminal), question (not sent: a prompt on its screen), failed.
// Sent ones are kept in this browser (localStorage, per session) so they survive page refreshes and the session/conversation switch.
window.MCP=(function(){
var items=[],subs=[],K='';
function key(){if(!K){var b=document.getElementById('sendbox');K=(b&&b.getAttribute('data-key'))||document.body.getAttribute('data-key')||'';}return K;}
function lk(){return 'mc-pend:'+key();}
function now(){return Date.now()/1000;}
function load(){try{items=(JSON.parse(localStorage.getItem(lk())||'[]')||[]).filter(function(p){return p&&p.x&&p.st==='sent'&&now()-p.e<43200;});}catch(e){items=[];}}
function save(){try{var a=items.filter(function(p){return p.st==='sent';}).slice(-20);if(a.length)localStorage.setItem(lk(),JSON.stringify(a));else localStorage.removeItem(lk());}catch(e){}}
function norm(t){return String(t||'').toLowerCase().replace(/[^a-z0-9]+/g,'');}
function same(a,b){a=norm(a);b=norm(b);var n=Math.min(a.length,b.length,160);return n>0&&a.slice(0,n)===b.slice(0,n);}
function esc(t){return String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function hm(e){return new Date(e*1000).toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'});}
function fire(){save();subs.forEach(function(f){try{f();}catch(e){}});}
// drops the ones the agent's log now shows (messages reach the terminal in order, so a later one recorded means earlier ones were too)
function seen(rec){var last=0,n0=items.length;rec.forEach(function(m){if(m.e>last)last=m.e;});
 items=items.filter(function(p){if(p.st!=='sent')return true;
  if(rec.some(function(m){return m.e>=p.e-300&&same(m.x,p.x);}))return false;return !(last&&p.e<last-120);});
 if(items.length!==n0)fire();}
var ST={sending:['','<span class="pdot"></span>Sending…'],sent:['','✓ Sent '],question:['warn','Not sent yet · the agent is asking something (see below)'],
 failed:['bad','Not sent · your text is still in the box']};
function row(p){var st=ST[p.st]||ST.sent,x=p.x.length>1500?p.x.slice(0,1500)+'…':p.x;
 return '<div class="row r-user pend '+st[0]+'"><div class="bub"><div class="mh"><span class="who">You</span><span class="tm">'+hm(p.e)+'</span></div>'+
  '<div class="mb pw">'+esc(x)+'</div><div class="pst">'+st[1]+(p.st==='sent'?hm(p.t||p.e)+' · in the agent’s terminal, not yet in its log':'')+'</div></div></div>';}
function paint(){   // the session page: under the latest words, just above the send box
 var box=document.getElementById('sendbox');if(!box||document.getElementById('list'))return;
 var j=document.getElementById('mc-lastyou');if(j){try{var o=JSON.parse(j.textContent);if(o&&o.e){var n0=items.length;seen([o]);if(items.length!==n0)return;}}catch(e){}}
 var h=box.parentNode.querySelector('.pendrows');
 if(!h){h=document.createElement('div');h.className='chat pendrows';box.parentNode.insertBefore(h,box);}
 h.innerHTML=items.map(row).join('');}
load();subs.push(paint);
window.mcPendPaint=paint;
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',paint);else paint();
return {list:function(){return items.slice();},on:function(f){subs.push(f);},row:row,seen:seen,
 start:function(x){items=items.filter(function(p){return p.st==='sending'||p.st==='sent';});var p={id:'p'+Date.now(),x:x,e:now(),st:'sending'};items.push(p);fire();return p.id;},
 set:function(id,st){items.forEach(function(p){if(p.id===id){p.st=st;if(st==='sent')p.t=now();}});fire();}};
})();
</script>"""

TERMS_JS = r"""<script>
// Terms: underline the agents' words in the chat; a tap opens what each means (who used and defined it first, older
// meanings, hints that its use moved). Selecting any other word or phrase offers to ask the agent what it means.
(function(){
var T=[],byId={},byN={},rx=null,rt=null,KEY='',pop=null,chip=null,lastDown=0;
function esc(t){return String(t==null?'':t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function load(){var J=document.getElementById('mc-terms');T=[];byId={};byN={};rx=rt=null;if(!J)return;KEY=J.getAttribute('data-key')||'';
 try{T=JSON.parse(J.textContent)||[];}catch(e){T=[];}
 var parts=[];T.forEach(function(t){byId[t.id]=t;byN[t.n]=t;parts.push(t.n.split(/[\s-]+/).map(function(w){return w.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');}).join('[\\s-]?'));});
 parts.sort(function(a,b){return b.length-a.length;});
 if(parts.length){var src='(^|[^\\w-])('+parts.join('|')+')(?![\\w-])';rx=new RegExp(src,'gi');rt=new RegExp(src,'i');}}
function find(s){var k=String(s).toLowerCase().replace(/\s+/g,' ').trim();if(byN[k])return byN[k];var k2=k.replace(/[\s-]+/g,'');
 for(var n in byN)if(n.replace(/[\s-]+/g,'')===k2)return byN[n];return null;}
function mark(root){if(!rx||!root)return;var n0=0;
 var w=document.createTreeWalker(root,NodeFilter.SHOW_TEXT,{acceptNode:function(nd){var p=nd.parentNode;
  if(!p||!p.closest('.mb')||p.closest('pre,a,button,.tw,.tpop,.tcard,textarea'))return NodeFilter.FILTER_REJECT;
  return rt.test(nd.nodeValue)?NodeFilter.FILTER_ACCEPT:NodeFilter.FILTER_SKIP;}}),list=[],nd;
 while((nd=w.nextNode())&&list.length<3000)list.push(nd);
 list.forEach(function(nd){var s=nd.nodeValue,frag=document.createDocumentFragment(),last=0,m;rx.lastIndex=0;
  while((m=rx.exec(s))){var st=m.index+m[1].length,t=find(m[2]);if(!t)continue;
   if(st>last)frag.appendChild(document.createTextNode(s.slice(last,st)));
   var sp=document.createElement('span');sp.className='tw'+(t.pending||t.differ?' warn':t.waiting?' wait':'');sp.setAttribute('data-t',t.id);
   sp.setAttribute('tabindex','0');sp.setAttribute('role','button');sp.textContent=m[2];frag.appendChild(sp);last=st+m[2].length;n0++;}
  if(last){if(last<s.length)frag.appendChild(document.createTextNode(s.slice(last)));nd.parentNode.replaceChild(frag,nd);}});}
function btn(label,op,t,o){o=o||{};return '<button type="button" class="btn2" data-cop="'+op+'"'+(t?' data-citem="'+t.id+'"':'')+(o.text?' data-ctext="'+esc(o.text)+'"':'')+
 (o.psig?' data-cpsig="'+esc(o.psig)+'"':'')+(o.confirm?' data-cconfirm="'+esc(o.confirm)+'"':'')+'>'+esc(label)+'</button>';}
function q(label,x){return x?'<div class="tq"><span class="tl">'+esc(label)+'</span>'+esc(x)+'</div>':'';}
function card(t){var h='<div class="tph"><b>'+esc(t.term)+'</b>'+(t.contract?'<span class="tbad">in contract</span>':'')+
  '<button type="button" class="tpx" aria-label="Close">×</button></div>';
 if(t.pending)h+=q('Pinned (the contract uses this word)',t.pinned)+'<div class="tq amb"><span class="tl">New meaning, '+esc(t.pending.by)+', '+esc(t.pending.t)+'</span>'+esc(t.pending.meaning)+'</div>';
 else if(t.meaning)h+='<div class="tmn">'+esc(t.meaning)+'</div><div class="tmeta">'+esc(t.by)+' · '+esc(t.t)+(t.src==='answer'?' · answer to your question':'')+'</div>';
 else h+='<div class="tmn mut">No meaning yet'+(t.asked?' · asked at '+esc(t.asked):'')+'.</div>';
 if(t.changed&&!t.pending)h+='<div class="tq amb"><span class="tl">Before, '+esc(t.changed.by0)+', '+esc(t.changed.t0)+'</span>'+esc(t.changed.from)+'</div>';
 if(t.differ)h+='<div class="tq amb"><span class="tl">Defined differently</span>'+t.differ.map(function(d){return '<div><b>'+esc(d.by)+':</b> '+esc(d.meaning)+'</div>';}).join('')+'</div>';
 if(t.shift)h+='<div class="tq"><span class="tl">Used differently lately? (a hint; the topic may just have moved)</span><div><i>'+esc(t.shift.then.t)+':</i> “'+esc(t.shift.then.text)+'”</div><div><i>'+esc(t.shift.now.t)+':</i> “'+esc(t.shift.now.text)+'”</div></div>';
 if(t.mainsub)h+='<div class="tq"><span class="tl">Main agent and a sub-agent use it differently? (a hint)</span><div><i>main agent:</i> “'+esc(t.mainsub.main.text)+'”</div><div><i>'+esc(t.mainsub.sub.who)+':</i> “'+esc(t.mainsub.sub.text)+'”</div></div>';
 var who=[];if(t.first_use)who.push('First used by '+t.first_use.who+', '+t.first_use.t);if(t.first_def)who.push('first defined by '+t.first_def.who+', '+t.first_def.t);
 var uses=['main agent: '+t.main];for(var k in t.subs)uses.push(k+': '+t.subs[k]);
 h+='<div class="tmeta">'+esc(who.join(' · '))+(who.length?'<br>':'')+'Uses: '+esc(uses.join(' · '))+'</div>';
 var b=[];
 if(t.pending){b.push(btn('Accept new meaning','term_accept',t,{psig:t.pending.sig,confirm:'Pin the new meaning of “'+t.term+'”? Contract rules that use it then mean the new thing.'}));
  b.push(btn('Keep the pinned meaning','term_keep',t,{psig:t.pending.sig,confirm:'Tell the agent to keep using “'+t.term+'” in its pinned meaning?'}));}
 b.push(btn(t.waiting?'Ask agent':'Ask again','term_ask',t,{confirm:'Ask the agent what “'+t.term+'” means? Other words waiting for a meaning go in the same message.'}));
 if(t.pending||t.changed||t.differ||t.shift||t.mainsub)b.push(btn('Question this','term_question',t,{confirm:'Ask the agent whether “'+t.term+'” still means the same thing? It gets the quotes shown here.'}));
 if(t.shift||t.mainsub)b.push(btn('Same meaning','term_same',t));
 b.push(btn('I know this','term_hide',t,{confirm:'Hide “'+t.term+'” on every session’s Terms card and stop underlining it?'}));
 h+='<div class="cacts" data-ckey="'+esc(KEY)+'">'+b.join('')+'<div class="actmsg"></div></div>';
 if(!document.getElementById('terms'))h+='<a class="tpl" href="'+encodeURIComponent(KEY)+'.html#'+t.id+'">All terms for this session</a>';
 return h;}
function place(el,box){var r=el.getBoundingClientRect(),vw=document.documentElement.clientWidth;
 if(vw<640){box.classList.add('sheet');return;}
 var w=Math.min(400,vw-16);box.style.width=w+'px';box.style.left=Math.max(8,Math.min(r.left,vw-w-8))+window.scrollX+'px';
 var ph=box.offsetHeight,below=r.bottom+6;box.style.top=(below+ph>window.innerHeight-8&&r.top>ph+14?r.top-ph-6:below)+window.scrollY+'px';}
function close(){if(pop){pop.remove();pop=null;}}
function open(el,t){close();hideChip();pop=document.createElement('div');pop.className='tpop';pop.setAttribute('role','dialog');pop.innerHTML=card(t);
 document.body.appendChild(pop);place(el,pop);var f=pop.querySelector('button.btn2');if(f&&!('ontouchstart' in window))f.focus({preventScroll:true});}
function hideChip(){if(chip){chip.remove();chip=null;}}
function sel(){var s=window.getSelection&&window.getSelection();if(!s||s.isCollapsed||!s.rangeCount)return null;
 var a=s.anchorNode&&(s.anchorNode.nodeType===1?s.anchorNode:s.anchorNode.parentNode);if(!a||!a.closest||!a.closest('.mb')||a.closest('.tcard,.tpop'))return null;
 var t=s.toString().replace(/\s+/g,' ').trim().replace(/^[^\w]+|[^\w]+$/g,'');if(t.length<2||t.length>60)return null;
 return {t:t,r:s.getRangeAt(0).getBoundingClientRect()};}
function showChip(){if(!KEY&&!document.getElementById('sendbox'))return;var x=sel();if(!x){if(chip&&Date.now()-lastDown>1500&&!chip.getAttribute('data-busy'))hideChip();return;}
 hideChip();var k=find(x.t);chip=document.createElement('div');chip.className='tchip';
 if(k)chip.innerHTML='<button type="button" class="btn2 topen">What “'+esc(k.term)+'” means</button>';
 else chip.innerHTML='<div class="cacts" data-ckey="'+esc(KEY||(document.getElementById('sendbox')||{}).getAttribute('data-key'))+'">'+
  btn('Ask what “'+(x.t.length>28?x.t.slice(0,26)+'…':x.t)+'” means','term_ask',null,{text:x.t,confirm:'Ask the agent what “'+x.t+'” means? Other words waiting for a meaning go in the same message.'})+
  btn('Save for later','term_add',null,{text:x.t})+'<div class="actmsg"></div></div>';
 document.body.appendChild(chip);var vw=document.documentElement.clientWidth,cw=chip.offsetWidth,ch=chip.offsetHeight;
 chip.style.left=Math.max(8,Math.min(x.r.left+x.r.width/2-cw/2,vw-cw-8))+window.scrollX+'px';
 chip.style.top=(x.r.top>ch+60?x.r.top-ch-8:x.r.bottom+8)+window.scrollY+'px';
 var o=chip.querySelector('.topen');if(o)o.onclick=function(){var el=document.createElement('span');document.body.appendChild(el);el.style.cssText='position:absolute;left:'+(x.r.left+window.scrollX)+'px;top:'+(x.r.top+window.scrollY)+'px;width:1px;height:'+x.r.height+'px';open(el,k);el.remove();};
 [].forEach.call(chip.querySelectorAll('button[data-cop]'),function(b){b.addEventListener('click',function(){chip.setAttribute('data-busy','1');setTimeout(function(){if(chip)chip.removeAttribute('data-busy');},9000);});});}
var stt;
document.addEventListener('pointerdown',function(e){if(e.target.closest&&e.target.closest('.tchip,.tpop'))lastDown=Date.now();},true);
document.addEventListener('mouseup',function(e){if(e.target.closest&&e.target.closest('.tchip,.tpop'))return;setTimeout(showChip,20);});
document.addEventListener('selectionchange',function(){clearTimeout(stt);stt=setTimeout(showChip,500);});
document.addEventListener('click',function(e){var tw=e.target.closest&&e.target.closest('.tw');
 if(tw){var t=byId[tw.getAttribute('data-t')];if(t&&!(window.getSelection&&String(window.getSelection())).trim()){open(tw,t);}return;}
 if(pop&&(e.target.closest('.tpx')||!e.target.closest('.tpop')))close();});
document.addEventListener('keydown',function(e){if(e.key==='Escape'){close();hideChip();}
 var a=document.activeElement;if((e.key==='Enter'||e.key===' ')&&a&&a.classList&&a.classList.contains('tw')){e.preventDefault();var t=byId[a.getAttribute('data-t')];if(t)open(a,t);}});
document.addEventListener('input',function(e){if(!e.target.classList||!e.target.classList.contains('tfind'))return;var v=e.target.value.toLowerCase().trim(),c=e.target.closest('.tcard');
 [].forEach.call(c.querySelectorAll('.trow'),function(r){r.style.display=!v||r.textContent.toLowerCase().indexOf(v)>=0?'':'none';});var m=c.querySelector('.tmore');if(m&&v)m.open=true;});
function init(){load();mark(document.getElementById('list'));[].forEach.call(document.querySelectorAll('[data-sec="g:latest"]'),mark);
 var h=location.hash.slice(1);if(/^T[0-9a-f]{5}$/.test(h)){var d=document.getElementById(h);if(d){var mo=d.closest('.tmore');if(mo)mo.open=true;d.open=true;d.scrollIntoView({block:'center'});}}}
window.mcTermMark=mark;window.mcTermsInit=init;
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
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
 var P=window.MCP,pid=P?P.start(t):null,mark=function(st){if(P&&pid)P.set(pid,st);};
 fetch('/api/send',{method:'POST',headers:{'Content-Type':'application/json','X-MC-Token':S.token},body:JSON.stringify({key:box.getAttribute('data-key'),text:t,force:!!force})})
 .then(function(r){return r.json();}).then(function(d){btn.disabled=false;window.MC_BUSY=false;
  var when=new Date().toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'}),scr=d.screen?'<pre class="sendscr">'+esc(d.screen)+'</pre>':'';
  if(d.ok){var sent=ta.value;ta.value='';ta.style.height='';
   var via=d.route?' \u00b7 '+esc(d.route):'';
   if(box.getAttribute('data-compact')==='1')res.innerHTML='<div class="small sentok">\u2713 Sent at '+when+via+'</div>'+(scr?'<details class="scr"><summary class="small">The agent\u2019s screen a few seconds later</summary>'+scr+'</details>':'');
   else res.innerHTML='<div class="small sentok">\u2713 Sent at '+when+via+'. The agent\u2019s screen a few seconds later:</div>'+scr;
   var bk=box.getAttribute('data-key');if(bk&&sent.trim()){var bd=document.createElement('div');bd.className='cacts';bd.setAttribute('data-ckey',bk);
    var bb=document.createElement('button');bb.type='button';bb.className='btn2 sm';bb.textContent='Make this binding';bb.setAttribute('data-cop','amend');
    bb.setAttribute('data-ctext',sent.trim().slice(0,1200));bb.setAttribute('data-cconfirm','Add this message to the contract as an amendment, so later checks hold the agent to it?');
    var bm=document.createElement('div');bm.className='actmsg';bd.appendChild(bb);bd.appendChild(bm);res.insertBefore(bd,res.firstChild.nextSibling);}
   mark('sent');}
  else if(d.code===3){mark('question');var q=d.question,qa='';
   if(q&&q.options&&q.options.length){qa='<div class="qa" data-key="'+esc(box.getAttribute('data-key'))+'" data-sig="'+esc(q.sig)+'">'+(q.context||[]).map(function(c){return '<div class="qctx mono">'+esc(c)+'</div>';}).join('')+'<div class="qbtns">'+
    q.options.map(function(o){return '<button type="button" class="btn2'+(/^no/i.test(o.label)?' dng':'')+'" data-ans="'+esc(o.n)+'">'+esc(q.kind==='menu'?o.n+'. '+o.label:o.label)+'</button>';}).join('')+'</div><div class="actmsg"></div></div>';}
   res.innerHTML='<div class="flag amber"><div class="t">Not sent yet</div><div class="d">'+esc(d.message)+(q&&q.text?'<br><b>'+esc(q.text)+'</b>':'')+'</div>'+qa+'<div class="a"><button type="button" class="more" id="sendforce">'+(q&&q.kind==='text'?'Send my message as the answer':'Send anyway')+'</button></div></div>'+scr;
   document.getElementById('sendforce').onclick=function(){send(true);};}
  else{mark('failed');res.innerHTML='<div class="flag red"><div class="t">Not sent</div><div class="d">'+esc(d.message||'failed')+'</div></div>'+scr;}})
 .catch(function(e){btn.disabled=false;window.MC_BUSY=false;mark('failed');res.innerHTML='<div class="flag red"><div class="t">Not sent</div><div class="d">The local mission control server didn\u2019t answer. Type mctl in WSL to restart it.</div></div>';});}
btn.addEventListener('click',function(){send(false);});
ta.addEventListener('input',function(){ta.style.height='auto';ta.style.height=Math.min(ta.scrollHeight,220)+'px';});
ta.addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();send(false);}});
})();
</script>"""

TOOLS_JS = r"""<script>
(function(){
// New-session dialog, session actions (interrupt / copy terminal command / end) and desktop alerts.
var S=window.MC_SEND||null,served=!!(S&&/^https?:/.test(location.protocol));
function post(u,b){return fetch(u,{method:'POST',headers:{'Content-Type':'application/json','X-MC-Token':S.token},body:JSON.stringify(b)}).then(function(r){return r.json();})
 .then(function(j){if(j&&j.route&&j.message)j.message+=' ('+j.route+')';return j;});}
function esc(t){return String(t==null?'':t).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function rs(t){return t?' · resets '+new Date(t*1000).toLocaleString([],{weekday:'short',hour:'numeric',minute:'2-digit'}):'';}
function wins(ws){return (ws||[]).map(function(w){var l=Math.max(0,Math.round(100-w.used)),c=l<10?'bad-t':(l<25?'warn-t':'ok-t');
 return '<b class="'+c+'">'+l+'%</b> of the '+esc(w.label)+' left'+rs(w.resets);}).join('; ');}
function ago(t){var s=Date.now()/1000-t;return s<90?'just now':s<5400?Math.round(s/60)+' min ago':s<172800?Math.round(s/3600)+' h ago':Math.round(s/86400)+' d ago';}
var dlg=document.getElementById('mc-launch'),nb=document.getElementById('mc-new');
if(dlg&&nb){
 var L={},pref={},dirTouched=false,$=function(id){return document.getElementById(id);};
 try{L=JSON.parse($('mc-launch-data').textContent)||{};}catch(_){}
 try{pref=JSON.parse(localStorage.getItem('mc-launch')||'{}')||{};}catch(_){}
 var agent=function(){var r=dlg.querySelector('input[name=agent]:checked');return r?r.value:'codex';};
 var opt=function(sel,items,val){sel.innerHTML=items.map(function(i){return '<option value="'+esc(i[0])+'">'+esc(i[1])+'</option>';}).join('');
  if(items.some(function(i){return i[0]===val;}))sel.value=val;};
 var homes=function(){return (L.codex||{}).homes||[];};
 var home=function(){return homes().filter(function(h){return h.cmd===$('l-home').value;})[0]||homes()[0]||{models:[]};};
 var models=function(){return agent()==='codex'?(home().models||[]):((L.claude||{}).models||[]);};
 var usage=function(){var u=L.usage||{},h;
  if(agent()==='codex'){var c=(u.codex||[]).filter(function(x){return x.home===home().dir;})[0]||(u.codex||[])[0];
   h=c&&c.windows&&c.windows.length?'<span class="kind codex">Codex'+(c.plan?' '+esc(c.plan):'')+'</span> '+wins(c.windows)+(c.reached?' · <b class="bad-t">limit reached</b>':''):'Codex usage hasn’t been reported yet.';}
  else{var cl=u.claude;h=cl&&cl.windows&&cl.windows.length?'<span class="kind claude">Claude</span> '+wins(cl.windows)+' <span class="mut">(as of '+ago(cl.t)+')</span>':
   'Claude’s remaining usage shows here once a Claude session started from here has replied.';}
  $('l-usage').innerHTML=h;};
 var fillLevels=function(keep){var a=agent(),m=models().filter(function(x){return x.slug===$('l-model').value;})[0],cur=keep!=null?keep:$('l-effort').value;
  var lv=a==='codex'?((m&&m.levels)||[]):((L.claude||{}).levels||[]);
  var d=a==='codex'?(($('l-model').value===(home().model||'')||!$('l-model').value)&&home().effort?home().effort:(m&&m.default)||''):((L.claude||{}).effort||'');
  opt($('l-effort'),[['','Default'+(d?' ('+d+')':'')]].concat(lv.map(function(l){return [l,l];})),cur);};
 var fillModels=function(keep){var a=agent(),cur=keep!=null?keep:$('l-model').value;
  var items=(a==='claude'?[['','Your default'+((L.claude||{}).model?' ('+L.claude.model+')':'')]]:[]).concat(models().map(function(m){return [m.slug,m.name];}));
  var d=a==='codex'?(home().model||''):'';
  opt($('l-model'),items,items.some(function(i){return i[0]===cur;})?cur:d);fillLevels();};
 var sync=function(){var a=agent();$('l-homerow').hidden=a!=='codex'||homes().length<2;$('l-goalrow').hidden=a!=='codex';fillModels('');usage();};
 var dirDefault=function(){if(!dirTouched)$('l-dir').value=String(L.root||'~').replace(/\/$/,'')+'/'+$('l-name').value.trim();};
 var base=L,onHost=function(h){L=Object.assign({},base,(base.per||{})[h]||{});L.host=h;};
 var init=function(first){
  if(first){var hs=base.hosts||[];onHost(hs.indexOf(pref.host)>=0?pref.host:base.host);opt($('l-host'),hs.map(function(h){return [h,h];}),L.host);
   $('l-host').hidden=hs.length<2;$('l-onhost').textContent=hs.length<2?'on '+(L.host||''):'on';}
  opt($('l-home'),homes().map(function(h){return [h.cmd,h.label+(h.default?' (used by your running sessions)':'')];}),pref.cmd||((homes()[0]||{}).cmd));
  [['codex',(L.codex||{}).available],['claude',(L.claude||{}).available]].forEach(function(p){var r=dlg.querySelector('input[name=agent][value='+p[0]+']');
   if(r){r.disabled=!p[1];r.parentNode.title=p[1]?'':'Not found on '+(L.host||'the node');}});
  var want=pref.agent&&!(dlg.querySelector('input[name=agent][value='+pref.agent+']')||{}).disabled?pref.agent:'codex';
  (dlg.querySelector('input[name=agent][value='+want+']')||{}).checked=true;
  sync();if(pref.agent===agent()){fillModels(pref.model||'');fillLevels(pref.effort||'');}dirDefault();};
 init(true);
 dlg.addEventListener('change',function(e){var t=e.target;if(t.id==='l-host'){onHost(t.value);if(!dirTouched)dirDefault();init(false);}else if(t.name==='agent')sync();else if(t.id==='l-home'){fillModels();usage();}else if(t.id==='l-model')fillLevels('');});
 $('l-name').addEventListener('input',dirDefault);$('l-dir').addEventListener('input',function(){dirTouched=true;});
 var msg=function(t,c){var m=$('l-msg');m.textContent=t;m.className='lmsg '+(c||'');};
 nb.addEventListener('click',function(){
  if(served)fetch('/api/launch-info',{cache:'no-store'}).then(function(r){return r.json();}).then(function(j){if(j&&j.hosts){var a=agent(),m=$('l-model').value,ef=$('l-effort').value;pref.host=$('l-host').value||pref.host;base=j;
   init(true);(dlg.querySelector('input[name=agent][value='+a+']')||{}).checked=true;sync();fillModels(m);fillLevels(ef);}}).catch(function(){});
  msg(served?'':'Open the dashboard with mctl (http://localhost:8765) or on your phone to start sessions.',served?'':'bad');
  if(dlg.showModal)dlg.showModal();else dlg.setAttribute('open','');setTimeout(function(){$('l-name').focus();},60);});
 dlg.querySelector('[data-close]').addEventListener('click',function(){dlg.close();});
 var req=function(check){var name=$('l-name').value.trim();
  if(!/^[A-Za-z0-9][A-Za-z0-9_-]{0,59}$/.test(name)){msg('Give it a name: letters, digits, - and _ (no spaces).','bad');$('l-name').focus();return null;}
  return {check:check,host:$('l-host').value||L.host,name:name,dir:$('l-dir').value.trim(),agent:agent(),model:$('l-model').value,effort:$('l-effort').value,
   cmd:agent()==='codex'?(home().cmd||''):'',goal:agent()==='codex'&&$('l-goal').checked,text:$('l-text').value};};
 var busy=function(on){$('l-go').disabled=on;$('l-check').disabled=on;window.MC_BUSY=on;};
 var fail=function(){busy(false);msg('Couldn’t reach the mission control server.','bad');};
 $('l-check').addEventListener('click',function(){if(!served)return;var r=req(true);if(!r)return;busy(true);msg('Checking on '+r.host+'…','');
  post('/api/launch',r).then(function(j){busy(false);msg(j.message||'',j.ok?'ok':'bad');}).catch(fail);});
 $('l-go').addEventListener('click',function(){if(!served)return;var r=req(false);if(!r)return;busy(true);$('l-scr').hidden=true;
  msg('Starting '+r.name+' on '+r.host+'… this takes up to a minute.','');
  try{localStorage.setItem('mc-launch',JSON.stringify({agent:r.agent,cmd:home().cmd,model:r.model,effort:r.effort,host:r.host}));}catch(_){}
  post('/api/launch',r).then(function(j){show(j,{host:r.host,name:r.name,line:j.line||''});
   if(j.ok){$('l-text').value='';$('l-name').value='';$('l-goal').checked=false;dirTouched=false;dirDefault();}}).catch(fail);});
 var last=null;
 var show=function(j,ctx){busy(false);msg(j.message||'',j.ok?'ok':'bad');
  if(j.screen){$('l-scr').textContent=j.screen;$('l-scr').hidden=false;}
  last=ctx;$('l-reply').hidden=!(j.state&&!j.ok&&(j.code===8||j.code===9));if(!$('l-reply').hidden)setTimeout(function(){$('l-ans').focus();},50);};
 var answer=function(a){if(!last||!served)return;busy(true);msg('Answering\u2026 then waiting for the agent (up to a minute).','');
  post('/api/launch',{continue:true,host:last.host,name:last.name,answer:a,line:last.line}).then(function(j){
   if(j.ok)last.line='';show(j,last);$('l-ans').value='';}).catch(fail);};
 $('l-ansgo').addEventListener('click',function(){var a=$('l-ans').value;if(a.trim())answer(a);});
 $('l-ans').addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();var a=$('l-ans').value;if(a.trim())answer(a);}});
 $('l-enter').addEventListener('click',function(){answer('<enter>');});
}
// session actions (delegated: the page content is swapped in place on updates)
document.addEventListener('click',function(e){var b=e.target.closest&&e.target.closest('#acts button[data-act]');if(!b)return;
 var acts=document.getElementById('acts'),a=b.getAttribute('data-act'),key=acts.getAttribute('data-key'),name=acts.getAttribute('data-name');
 var say=function(t,c){var o=document.getElementById('actmsg');if(o){o.className='actmsg '+(c||'');o.textContent=t;}};
 if(a==='copy'){var c=b.getAttribute('data-cmd');var ok=function(){say('Copied. Paste it in WSL: '+c,'ok');};
  if(navigator.clipboard&&window.isSecureContext)navigator.clipboard.writeText(c).then(ok,function(){say(c,'');});else say(c,'');return;}
 if(!served){say('Open the dashboard with mctl (http://localhost:8765) to do this.','bad');return;}
 if(a==='esc'){if(!confirm('Press Esc in '+name+'?\n\nThe agent stops what it is doing and waits for you. Nothing is lost.'))return;say('Pressing Esc…','');
  post('/api/key',{key:key}).then(function(j){say(j.message||'',j.ok?'ok':'bad');}).catch(function(){say('Couldn’t reach the mission control server.','bad');});return;}
 if(a==='end'){var t=prompt('End '+name+'?\n\nThis closes its tmux session and stops the agent. Its conversation stays in its logs (codex resume / claude --resume).\n\nType the session name to confirm:');
  if(t===null)return;if(t.trim()!==name){say('Not ended: the name didn’t match.','bad');return;}say('Ending '+name+'…','');
  post('/api/end',{key:key,confirm:t.trim()}).then(function(j){say(j.message||'',j.ok?'ok':'bad');}).catch(function(){say('Couldn’t reach the mission control server.','bad');});}});
// Dismiss / Bring back take effect on the page at once (the next update renders the same): the item moves into its
// section's "Dismissed (N)" fold, or back out of it
function dismFold(sec,make){var d=null,kids=sec.children;for(var i=0;i<kids.length;i++)if(kids[i].matches&&kids[i].matches('details.dism'))d=kids[i];
 if(!d&&make){d=document.createElement('details');d.className='fold dism';d.innerHTML='<summary>Dismissed (0)</summary>';
  var fl=[].filter.call(kids,function(c){return c.classList&&c.classList.contains('flag');}),em=[].filter.call(kids,function(c){return c.classList&&c.classList.contains('empty');});
  var after=fl.length?fl[fl.length-1]:(em.length?em[em.length-1]:sec.querySelector('h2'));sec.insertBefore(d,after?after.nextSibling:sec.firstChild);}
 return d;}
function dismCount(sec,d){if(d){var n=d.querySelectorAll(':scope > .flag').length;if(!n)d.remove();else d.querySelector('summary').textContent='Dismissed ('+n+')';}
 var live=[].filter.call(sec.children,function(c){return c.classList&&c.classList.contains('flag');}).length,em=sec.querySelector(':scope > .empty.nothing');
 if(!live&&d&&d.parentNode&&!em){em=document.createElement('div');em.className='empty nothing';em.textContent='Nothing open.';sec.insertBefore(em,d);}
 else if(live&&em)em.remove();}
function moveItem(fl,dismiss,b){var sec=fl.closest('section');if(!sec)return;var bar=fl.querySelector('.ibar');
 if(dismiss){fl.classList.add('ok');b.textContent='Bring back';b.setAttribute('data-cop','unack');
  if(bar&&!bar.querySelector('.ackline')){var l=document.createElement('div');l.className='ans mut ackline';l.textContent='Dismissed by you just now';
   var clr=bar.querySelector('.clr');bar.insertBefore(l,clr||bar.firstChild);}
  var note=document.createElement('div');note.className='small mut mvnote';note.textContent='Dismissed: it is under “Dismissed” at the end of this section.';
  fl.parentNode.insertBefore(note,fl);setTimeout(function(){note.remove();},6000);
  var d=dismFold(sec,true);d.appendChild(fl);dismCount(sec,d);}
 else{fl.classList.remove('ok');b.textContent='Dismiss';b.setAttribute('data-cop','ack');var al=bar&&bar.querySelector('.ackline');if(al)al.remove();
  var d2=dismFold(sec,false);sec.insertBefore(fl,d2||null);dismCount(sec,d2);fl.scrollIntoView({block:'nearest'});}}
// change the contract: buttons and small forms carry the change in data-c* attributes (lib/contract.py does the work)
document.addEventListener('click',function(e){var b=e.target.closest&&e.target.closest('button[data-cop]');if(!b)return;
 var box=b.closest('[data-ckey]');if(!box)return;var o=box.querySelector('.actmsg')||(box.parentNode&&box.parentNode.querySelector('.actmsg'));
 var say=function(t,c){if(o){o.className='actmsg '+(c||'');o.textContent=t;}else if(c==='bad')alert(t);};
 if(!served){say('Open the dashboard with mctl (http://localhost:8765) to change the contract from here.','bad');return;}
 var body={key:box.getAttribute('data-ckey'),op:b.getAttribute('data-cop')},map={cname:'name',cvalue:'value',cruleop:'rule_op',cval:'val',cpid:'pid',cuid:'uid',ctext:'text',citems:'items',citem:'item',cpsig:'psig'};
 for(var a in map){var v=b.getAttribute('data-'+a);if(v!==null)body[map[a]]=v;}
 var form=b.closest('.cform');if(form){var ins=form.querySelectorAll('input,select');for(var i=0;i<ins.length;i++)body[ins[i].name]=ins[i].value;
  if(body.op==='set'&&(!body.name||!body.val)){say('Fill in the name and the value.','bad');return;}
  if(body.op==='criterion'&&!(body.text||'').trim()){say('Write the criterion first.','bad');return;}}
 var q=b.getAttribute('data-cconfirm');if(form&&body.op==='set')q='Add the rule '+body.name.toUpperCase()+' '+body.rule_op+' '+body.val+'?';
 if(['set','allow','unlock','unwatch','criterion','uncriterion','amend','bind'].indexOf(body.op)>=0)q=(q||'Change the contract?')+'\n\nThe agent gets a short message about the change.';
 if(body.op==='ask'){var n=(body.items||'').split(',').length;var t=prompt('Ask the agent about '+(n>1?'these '+n+' items':'this item')+'.\nYour question (leave empty for: is this intended? fix it if not):','');if(t===null)return;body.text=t;q='';}
 else if(body.op==='ack'){var t2=prompt('Dismiss this item? It stays hidden until it changes (7 days at most).\nWhy is it a non-issue? (optional, kept with it)','');if(t2===null)return;body.text=t2;q='';}
 if(q&&!confirm(q))return;b.disabled=true;window.MC_BUSY=true;say(['ask','probe','remind','pin','term_ask','term_question','term_setup','term_keep'].indexOf(body.op)>=0?'Sending\u2026':'Saving\u2026','');
 post('/api/contract',body).then(function(j){window.MC_BUSY=false;b.disabled=false;say(j.message||'',j.ok?'ok':'bad');
  if(j.ok&&(body.op==='ack'||body.op==='unack')){var fl=b.closest('.flag');if(fl)moveItem(fl,body.op==='ack',b);}
  if(j.ok&&form){var ins2=form.querySelectorAll('input');for(var i=0;i<ins2.length;i++)if(ins2[i].type!=='hidden')ins2[i].value='';}})
 .catch(function(){window.MC_BUSY=false;b.disabled=false;say('Couldn\u2019t reach the mission control server.','bad');});});
// answer a question on the agent's screen: the button types that answer into the session (after you confirm)
document.addEventListener('click',function(e){var b=e.target.closest&&e.target.closest('.qa button[data-ans]');if(!b)return;
 var qa=b.closest('.qa'),o=qa.querySelector('.actmsg'),say=function(t,c){if(o){o.className='actmsg '+(c||'');o.textContent=t;}};
 if(!served){say('Open the dashboard with mctl (http://localhost:8765) to answer from here.','bad');return;}
 if(!confirm('Answer "'+b.textContent+'"?\n\nThis is typed into the agent\u2019s session now.'))return;
 var all=qa.querySelectorAll('button');for(var i=0;i<all.length;i++)all[i].disabled=true;window.MC_BUSY=true;say('Answering\u2026','');
 post('/api/answer',{key:qa.getAttribute('data-key'),choice:b.getAttribute('data-ans'),sig:qa.getAttribute('data-sig')}).then(function(j){window.MC_BUSY=false;
  say(j.message||'',j.ok?'ok':'bad');if(!j.ok)for(var i=0;i<all.length;i++)all[i].disabled=false;
  if(j.screen){var p=document.createElement('pre');p.className='sendscr';p.textContent=j.screen;qa.appendChild(p);}})
 .catch(function(){window.MC_BUSY=false;say('Couldn\u2019t reach the mission control server.','bad');for(var i=0;i<all.length;i++)all[i].disabled=false;});});
// desktop alerts for new red items (works while any mission control page is open in a tab)
var bell=document.getElementById('mc-bell');
var on=function(){try{return localStorage.getItem('mc-alerts')==='1'&&!!window.Notification&&Notification.permission==='granted';}catch(_){return false;}};
var reds=function(){try{return JSON.parse((document.getElementById('mc-reds')||{}).textContent||'[]');}catch(_){return [];}};
var mark=function(l){try{localStorage.setItem('mc-reds-seen',JSON.stringify(l.map(function(r){return r.k;})));}catch(_){}};
var paint=function(){if(bell){bell.textContent='Alerts '+(on()?'on':'off');bell.classList.toggle('on',on());}};
if(bell){bell.addEventListener('click',function(){
 if(!window.Notification||!window.isSecureContext){bell.title='Desktop alerts need the dashboard opened with mctl (http://localhost:8765).';bell.textContent='Alerts: open with mctl';return;}
 if(on()){localStorage.setItem('mc-alerts','0');paint();return;}
 Notification.requestPermission().then(function(p){if(p==='granted'){localStorage.setItem('mc-alerts','1');mark(reds());
  try{new Notification('Mission control',{body:'Desktop alerts are on: you’ll hear about new red items while a dashboard tab is open.'});}catch(_){}}paint();});});paint();}
window.mcNotify=function(){if(!on()||!document.getElementById('mc-reds'))return;var cur=reds(),old=[];try{old=JSON.parse(localStorage.getItem('mc-reds-seen')||'[]');}catch(_){}
 var fresh=cur.filter(function(r){return old.indexOf(r.k)<0;});mark(cur);
 fresh.slice(0,3).forEach(function(r){try{var n=new Notification('Mission control needs you',{body:r.t,tag:r.k});n.onclick=function(){window.focus();};}catch(_){}});};
window.mcNotify();
})();
</script>"""

# Pages carry a fingerprint of their code, so an open page reloads fully (rather than swapping content) after an update.
BUILD = hashlib.md5((CSS + CONV_CSS + CONV_JS + HIDE_JS + FRESH_JS + REFRESH_JS + PEND_JS + SEND_JS + TOOLS_JS + TERMS_JS).encode("utf-8")).hexdigest()[:12]
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
    o.append("<main>" + reds_json())
    stxt, scls = session_state(g, [f["key"] for f in s["flags"]], s.get("unreachable"), s.get("ended"), s.get("node_name"), s.get("last_seen"))
    where = LOGIN_HOSTS.get(s.get("node_name"), s.get("node_name") or "?")
    meta = ["%s \u00b7 %s" % (esc(where), "no tmux (pid %s)" % esc((s.get("tmux") or {}).get("pid") or "?") if g.get("notmux") else "tmux " + esc(s["session"]))]
    if g.get("model"):
        meta.append('<span title="Main agent\u2019s model and reasoning level">%s</span>' % esc(" ".join(x for x in (g["model"], g.get("effort")) if x)))
    more = []
    subs, revs = {}, {}
    for x in s.get("sessions") or []:
        if x.get("role") == "goal" and x.get("label") not in (None, "main") and x.get("model"):
            k = " \u00b7 ".join(v for v in (x["model"], x.get("effort")) if v)
            grp = revs if "review" in x["model"] else subs   # Codex's approval reviewer runs as its own session
            grp[k] = grp.get(k, 0) + 1
    for grp, name, tip in ((subs, "Sub-agents", "Sub-agents\u2019 models and reasoning levels"),
                           (revs, "Reviewer", "Codex\u2019s automatic approval reviewer")):
        if grp:
            more.append('<span title="%s">%s %s</span>' % (tip, name, esc(", ".join(
                "%s%s" % (k.replace(" \u00b7 ", " "), " \u00d7%d" % n if n > 1 else "") for k, n in sorted(grp.items(), key=lambda kv: -kv[1])))))
    if g.get("cwd"):  # shortened; click (or tap) for the whole path, selectable for copying
        more.append('<span class="mono xp" id="x-cwd" data-x title="Click to show the full folder"><span class="xs">%s</span><span class="xf">%s</span></span>'
                    % (esc(trunc(g["cwd"], 70)), esc(g["cwd"])))
    goal_full = ((s.get("goal") or {}).get("text") or s.get("title") or "").strip()
    o.append('<header><div style="min-width:0"><div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap"><h1>%s</h1>%s<span class="pill %s">%s</span></div>'
             '<p class="sub">%s</p>%s%s</div>%s</header>' % (
                 esc(s["session"]), kind_badge(g.get("agent_kind")), scls, esc(stxt), " \u00b7 ".join(meta),
                 "".join('<p class="sub2">%s</p>' % m for m in more),
                 ('<p class="objective" id="x-goal" data-x title="Click to show the whole goal"><b>Goal</b> %s</p>' % esc(trunc(goal_full, 20000)))
                 if goal_full and goal_full != s["session"] else "",
                 session_actions(s, g)))
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
    ly = cv.get("last_you") or {}
    if ly.get("epoch"):
        o.append('<script type="application/json" id="mc-lastyou">%s</script>' % json.dumps({"e": ly["epoch"], "x": (ly.get("text") or "")[:400]}).replace("</", "<\\/"))
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
    short = {"Last agent activity": "Activity", "Slurm jobs now": "Slurm now", "Idle or failed compute, 48 h": "Idle 48 h", "Acceptance criteria": "Criteria"}
    for l, v, sub in cards:
        o.append('<div class="m" style="--s:var(--%s)" title="%s"><div class="l">%s</div><div class="v">%s</div><div class="s">%s</div></div>'
                 % (tile_hue.get(l, "slate"), esc(l), esc(short.get(l, l)), v, sub))
    o.append("</section>")

    # needs you
    needs = [f for f in s["flags"] if f.get("needs_you")]
    in_needs = lambda f: f.get("needs_you") or (needs and (f.get("acked") or {}).get("needs"))   # dismissed ones stay in its fold
    needs = [f for f in s["flags"] if in_needs(f)]
    if needs:
        o.append('<section class="card"%s><h2>Needs you %s</h2>' % (sec("g", "needs"), ask_all_btn(needs, s["key"], "Ask the agent about all")))
        o.append(flag_list_html(needs))
        o.append("</section>")
    else:
        o.append('<div class="quiet"%s><span class="dot"></span>Nothing needs you right now%s</div>' % (
            sec("g", "needs"), (' <span class="mut">\u00b7 %d amber item%s below</span>' % (len(ambers), "" if len(ambers) == 1 else "s")) if ambers else ""))

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
    if p["steps"]:
        o.append('<section class="grid2"><div class="card"%s><h2>Plan</h2>' % sec("g", "plan"))
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
        o.append('</div><div class="card"%s>' % sec("g", "resources"))
    else:
        o.append('<section><div class="card"%s>' % sec("g", "resources"))
    o.append(resources_html(s["resources"], {s.get("node_name") or "node": s.get("node") or {}}, c, s.get("history") or []))
    o.append(storage_html(s.get("storage")))
    o.append("</div></section>")
    o.append('<div style="height:14px"></div>' if p["steps"] else "")

    # integrity
    integ = [f for f in s["flags"] if f["area"] == "integrity" and not in_needs(f)]
    o.append('<section class="card"%s><h2>Scientific integrity %s</h2>' % (sec("g", "integrity"), ask_all_btn(integ, s["key"], "Ask about these")))
    if integ:
        o.append(flag_list_html(integ))
    elif not any(f["area"] == "integrity" for f in s["flags"]):
        o.append('<div class="empty">No contract breaks, unexplained parameter changes or shortcut wording found.</div>')
    o.append(contract_card_html(s))
    sc = s["params"]["scan"]
    o.append('<div class="rnote" style="margin:10px 0 0">Scanned %s input file%s changed in the last %g h under <span class="mono">%s</span>%s.</div>' % (
        sc.get("files") or 0, "" if sc.get("files") == 1 else "s", CFG["scan_hours"], esc(", ".join(sc.get("roots") or []) or "the goal folder"),
        " (cut short)" if sc.get("truncated") else ""))
    o.append("</section>")
    o.append(memory_html(s))
    o.append(terms_html(s))

    # other flags (goal / compute amber+info not shown above)
    other = [f for f in s["flags"] if f["area"] in ("goal", "compute", "connection") and not in_needs(f)]
    if other:
        o.append('<section class="card"%s><h2>Watchdog %s</h2>' % (sec("g", "watchdog"), ask_all_btn(other, s["key"], "Ask about these")))
        o.append(flag_list_html(other))
        o.append("</section>")

    # timeline
    o.append('<section class="card"' + sec("g", "timeline") + '><h2>What happened <span class="h2n">newest first</span></h2><div class="tl">')
    if s["timeline"]:
        for i, t in enumerate(s["timeline"]):
            if i == 12:
                o.append('<details class="fold"><summary>%d older</summary>' % (len(s["timeline"]) - 12))
            lab, cls = KIND_PILL.get(t.get("kind"), (t.get("kind") or "cmd", "neu"))
            extra = ""
            if t.get("exit") not in (None, 0):
                extra += ' <span class="pill bad">exit %s</span>' % esc(t["exit"])
            if t.get("jobid"):
                extra += ' <span class="pill ok">job %s</span>' % esc(t["jobid"])
            if t.get("watch"):
                extra += ' <span class="pill warn">%s</span>' % esc(", ".join(t["watch"][:3]))
            o.append('<div class="e"><span class="tm">%s</span><span class="w"><span class="pill %s">%s</span></span><span class="tx" onclick="this.classList.toggle(\'open\')">'
                     '<span class="small mut">%s</span> %s%s</span></div>' % (
                         esc(hm(t.get("epoch"))), cls, esc(lab), esc(t.get("who") if t.get("who") not in ("you", "decision") else ""),
                         esc(trunc(t.get("text"), 1200)), extra))
        if len(s["timeline"]) > 12:
            o.append("</details>")
    else:
        o.append('<div class="empty">No notable agent actions in the window.</div>')
    o.append("</div></section>")

    # instructions
    o.append('<section class="card"' + sec("g", "instructions") + '><h2>Your instructions to the agents</h2><div class="small mut" style="margin-bottom:6px">Captured from the session logs. Make binding adds one to the contract as an amendment, so later checks hold the agents to it.</div>')
    if s["instructions"]:
        o.append("<table><tr><th>ID</th><th>When</th><th>To</th><th>Instruction</th></tr>")
        for u in s["instructions"][:25]:
            o.append('<tr><td class=mono>%s%s</td><td class=mut style="white-space:nowrap">%s</td><td class=mut>%s</td><td>%s%s</td></tr>' % (
                esc(u["id"]), ' <span class="pill ok">in contract</span>' if u["promoted"] else "", esc(hm(iso_epoch(u.get("ts")))),
                esc(u.get("who")), esc(trunc(u["text"], 400)), "" if u["promoted"] else '<div class="cacts" data-ckey="%s">%s<div class="actmsg"></div></div>' % (
                    esc(s["key"]), act_btn({"label": "Make binding", "op": "bind", "uid": u["id"],
                                            "confirm": "Add this instruction to the contract as an amendment?\n\n" + trunc(u["text"], 300)}, "btn2 sm"))))
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
    o.append('<footer><div>%s</div><div>Collected %s, page built %s; new data every %d min.</div>'
             '<div>Mission control types into a session only when you press a button%s. Press a block\u2019s \u00d7 to hide it here; <span class="mono">mctl hide NAME</span> hides it everywhere.%s</div></footer>' % (
                 hs, esc(time.strftime("%Y-%m-%d %H:%M", time.localtime(s["collected"]))) if s.get("collected") else "never",
                 esc(time.strftime("%H:%M", time.localtime(s["generated"]))), CFG["interval"], never_note(), hidden_note()))
    o.append("</main>%s%s%s%s%s%s</body></html>" % (HIDE_JS, PEND_JS, SEND_JS, REFRESH_JS, TOOLS_JS, TERMS_JS))
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
        argv = (rd("/proc/%s/cmdline" % d, "") or "").split("\0")
        own, skip = [], False   # the master's own command, without the jump host it travels through (ProxyCommand/-J)
        for i, x in enumerate(argv):
            if skip:
                skip = False
                continue
            if x == "-J" or (x == "-o" and i + 1 < len(argv) and re.match(r"(?i)\s*proxy(command|jump)\b", argv[i + 1])):
                skip = True
                continue
            if re.match(r"(?i)(-o)?\s*proxy(command|jump)\b", x):
                continue
            own.append(x)
        out.append((path, int(d), _proc_peer_ips(int(d)), " ".join(own).strip()))
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
        res.append((path, pid, ips, (cmd + (" ||via|| " + extra if extra else "")).strip()))
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
        g_own = "\n".join(ln for ln in g.splitlines() if not re.match(r"(?i)proxy(command|jump)\s", ln))   # a jump host isn't the host
        if excl_rx.search(h) or never_rx.search(g_own) or h.lower() in excl_words:
            hn_x = next((ln.split(None, 1)[1].strip() for ln in g.splitlines() if ln.lower().startswith("hostname ")), h)
            try:
                for ai in so.getaddrinfo(hn_x, None):
                    excl_ips.add(ai[4][0])
            except Exception:
                pass
            excl_words.append(h.lower())
            excl_rx = word_rx(excl_words)
    # Judged on the connection itself: a connection that only travels through a never-contact host (its ProxyCommand or
    # -J) is to another host and is allowed; the never-contact host's own connection is never used.
    blocked = {path for path, pid, ips, cmd in found if (set(ips) & excl_ips) or excl_rx.search(os.path.basename(path))
               or excl_rx.search(cmd.split(" ||via|| ")[0]) or never_rx.search(os.path.basename(path))}
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


# ---------------------------------------------------------------- relay between your PCs over Tailscale
# A request for another PC goes straight to that PC's mission control server over your tailnet (seconds). If it can't
# get there (a VPN in the way, Tailscale off, the PC asleep), mctl falls back to the shared folder (about a minute).
# Each request is signed with a key kept in the shared folder, and the receiving server also requires your own
# Tailscale login, a fresh timestamp and an id it hasn't seen.
def relay_key():
    p = os.path.join(STATE, "relay.key")
    try:
        with open(p) as f:
            k = f.read().strip()
        if len(k) >= 32:
            return k
    except OSError:
        pass
    import secrets
    os.makedirs(STATE, exist_ok=True)
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    with open(p) as f:
        return f.read().strip()


def relay_sig(key, body):
    import hmac
    return hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


def cmd_relay_send(pc, reqfile):
    """Hand one request to another PC over Tailscale. Exit 0: done (result JSON printed). Exit 3: couldn't reach it,
    nothing ran, use the shared folder. Exit 4: it was delivered but no answer came back (don't send it again)."""
    import http.client
    import socket
    import ssl
    info = rjson(os.path.join(STATE, "ts", pc + ".json"), None) or {}
    name, ip = str(info.get("name") or ""), str(info.get("ip") or "")
    if not name or not ip or time.time() - float(info.get("t") or 0) > 3 * 86400 or os.environ.get("RELAY_TAILSCALE", "1") == "0":
        return 3
    body = open(reqfile, "rb").read()
    try:
        ctx = ssl.create_default_context()
        sock = socket.create_connection((ip, 443), timeout=3)
        tls = ctx.wrap_socket(sock, server_hostname=name)
    except (OSError, ssl.SSLError):
        return 3
    try:
        tls.settimeout(175)
        conn = http.client.HTTPSConnection(name, 443, context=ctx, timeout=175)
        conn.sock = tls
        conn.request("POST", "/api/relay", body=body, headers={"Content-Type": "application/json", "Host": name,
                                                                 "X-MC-Sig": relay_sig(relay_key(), body)})
    except (OSError, http.client.HTTPException, ssl.SSLError):
        return 3
    try:
        r = conn.getresponse()
        data = r.read()
    except (OSError, http.client.HTTPException, ssl.SSLError):
        return 4
    if r.status in (403, 404, 405, 502, 503):   # not set up there, refused before running: the shared folder is next
        return 3
    try:
        res = json.loads(data.decode("utf-8"))
    except ValueError:
        return 4
    print(json.dumps(res))
    return 0


def ts_publish(name, ip):
    """Each PC's server notes its Tailscale name and address here, so the others can reach it."""
    me = (os.environ.get("MC_HOST") or LOCAL_NAME).lower()
    p = os.path.join(STATE, "ts", me + ".json")
    cur = rjson(p, {}) or {}
    if cur.get("name") == name and cur.get("ip") == ip and time.time() - float(cur.get("t") or 0) < 6 * 3600:
        return
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w") as f:
        json.dump({"name": name, "ip": ip, "t": time.time()}, f)
    os.replace(p + ".tmp", p)


def cmd_relay_run(path, outdir):
    """Answer one request handed over from another of your PCs through the shared folder (see relay in mctl): run its
    script here, or on the node it names through this PC's connections, and write the result for the asker."""
    import subprocess
    req = rjson(path, None)
    rid = str((req or {}).get("id") or "")
    if not isinstance(req, dict) or not re.match(r"^[0-9]{6,12}-[0-9]{1,12}$", rid):
        print("relay: ignored %s (not a request)" % os.path.basename(path))
        return
    node = str(req.get("node") or "")
    res = {"id": rid, "rc": 2, "out": "", "err": ""}
    try:
        age = time.time() - float(req.get("time") or 0)
    except (TypeError, ValueError):
        age = 1e9
    if age > 180:   # it waited too long in the shared folder: the asker has given up, and the moment has passed
        res["err"] = "The request reached %s %d s late, so it was not run.\n" % (LOCAL_NAME, age)
    elif node and not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$", node):
        res["err"] = "relay: bad node name\n"
    else:
        try:
            script = base64.b64decode(str(req.get("stdin") or ""))
            cmd = ["bash", os.path.join(MC_DIR, "mctl"), "run-on", node] if node else ["bash", "-s"]
            p = subprocess.run(cmd, input=script, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=170)
            res.update(rc=p.returncode, out=p.stdout.decode("utf-8", "replace"), err=p.stderr.decode("utf-8", "replace"))
        except subprocess.TimeoutExpired:
            res.update(rc=124, err="timed out on %s\n" % LOCAL_NAME)
        except (ValueError, OSError) as e:
            res.update(rc=1, err="relay: %s\n" % e)
    os.makedirs(outdir, exist_ok=True)
    tmp = os.path.join(outdir, ".%s.tmp" % rid)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(res, f)
    os.replace(tmp, os.path.join(outdir, rid + ".json"))
    print("%s relay %s from %s%s: rc %s" % (time.strftime("%F %T"), rid, req.get("from"), (" for " + node) if node else "", res["rc"]))


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
    elif a[0] == "contract-edit":
        sys.exit(cmd_contract_edit(a[1], json.loads(sys.stdin.read() or "{}")))
    elif a[0] == "relay-send":
        sys.exit(cmd_relay_send(a[1], a[2]))
    elif a[0] == "ts-publish":
        ts_publish(a[1], a[2])
    elif a[0] == "relay-key":
        relay_key()
    elif a[0] == "relay-run":
        cmd_relay_run(a[1], a[2])
    elif a[0] == "digest-config":
        cmd_digest_config("--local" in a, a[a.index("--login") + 1] if "--login" in a and a.index("--login") + 1 < len(a) else None,
                          "--probe" in a)
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

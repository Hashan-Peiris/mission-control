"""Keeps each goal's contract (contract/<goal>.md) in step with the goal by itself.

One rule holds everywhere: automatic changes may only add or tighten. A rule that would loosen (a wider value range,
a bigger budget, one more cluster) waits under "Waiting for your OK" until you approve it on the dashboard.

Sources, in order of trust: what you wrote (no tag) > the goal's own instructions (START_GOAL.md and the like, the
Codex goal text) > what the run files agree on. Lines written automatically carry a short "# ..." tag saying where
they came from; remove the tag (or press Keep) to make a line yours.
"""
import hashlib
import json
import os
import re
import time

# Settings each code's input files are scanned for (the node digest reads them; see remote_digest.param_scan).
CODE_PARAMS = {
    "VASP": ["ENCUT", "EDIFF", "EDIFFG", "PREC", "GGA", "METAGGA", "ISMEAR", "SIGMA", "KSPACING", "KPOINTS", "ISPIN",
             "LDAU", "IVDW", "ALGO", "LASPH", "ADDGRID", "LREAL", "POT_SET"],
    "DeePMD": ["DP_DESCRIPTOR", "DP_RCUT", "DP_RCUT_SMTH", "DP_SEL", "DP_NEURON", "DP_FIT_NEURON", "DP_TYPE_MAP",
               "DP_START_LR", "DP_STOP_LR", "DP_NUMB_STEPS", "DP_START_PREF_E", "DP_START_PREF_F", "DP_LIMIT_PREF_F"],
    "LAMMPS": ["LMP_PAIR_STYLE", "LMP_UNITS", "LMP_TIMESTEP"],
}
SCAN_NAMES = sorted({n for v in CODE_PARAMS.values() for n in v}) + ["POT_*"]   # POT_<element>: one pseudopotential each
ALIASES = {"DP_RCUT": ["rcut"], "DP_RCUT_SMTH": ["rcut_smth"], "DP_NUMB_STEPS": ["numb_steps"], "DP_START_LR": ["start_lr"],
           "DP_STOP_LR": ["stop_lr"], "DP_TYPE_MAP": ["type_map"], "DP_SEL": ["sel"], "LMP_PAIR_STYLE": ["pair_style"],
           "LMP_TIMESTEP": ["timestep"]}
AUTO_RX = re.compile(r"\b(learned|from START_|from GOAL|from the goal|from the Codex goal|inferred|auto)\b", re.I)
NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eEdD][-+]?\d+)?"
LEARN_MIN_FILES = 5        # unanimous input files needed before a value is learned
LEARN_MIN_AGE = 20 * 60    # ... seen that way for at least this long (two collections or more)
STOPWORDS = {"the", "a", "an", "and", "or", "not", "must", "should", "be", "is", "this", "that", "value", "values", "same",
             "fixed", "unchanged", "kept", "keep", "set", "default", "any", "all", "used", "use", "from", "in", "on", "for"}


def now_s():
    return time.strftime("%Y-%m-%d %H:%M")


def today():
    return time.strftime("%Y-%m-%d")


def norm_val(v):
    v = str(v).strip().strip("'\"`.,;")
    try:
        return "%g" % float(v.lower().replace("d", "e"))
    except ValueError:
        return v.upper()[:60]


def sig(*parts):
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- sections of the contract file
def _split(raw):
    lines = raw.split("\n")
    secs = [(m.group(1).strip().lower(), i) for i, l in enumerate(lines) for m in [re.match(r"^##\s+(.*)", l)] if m]
    return lines, secs


def _range(secs, name, n):
    for j, (s, i) in enumerate(secs):
        if s == name:
            return i + 1, (secs[j + 1][1] if j + 1 < len(secs) else n)
    return None


def ensure_section(raw, title, before=None):
    lines, secs = _split(raw)
    if _range(secs, title.lower(), len(lines)):
        return raw
    block = ["## " + title, ""]
    at = next((i for s, i in secs if s == (before or "").lower()), None) if before else None
    if at is not None:
        lines[at:at] = block
        return "\n".join(lines)
    return raw.rstrip("\n") + "\n\n" + "\n".join(block) + "\n"


def section_lines(raw, title):
    lines, secs = _split(raw)
    r = _range(secs, title.lower(), len(lines))
    return [(i, lines[i]) for i in range(*r)] if r else []


def _insert_in(raw, title, new_line, before=None):
    raw = ensure_section(raw, title, before)
    lines, secs = _split(raw)
    a, b = _range(secs, title.lower(), len(lines))
    k = b
    while k > a and not lines[k - 1].strip():
        k -= 1
    lines.insert(k, new_line)
    return "\n".join(lines)


def _set_line(raw, i, new_line):
    lines = raw.split("\n")
    if new_line is None:
        del lines[i]
    else:
        lines[i] = new_line
    return "\n".join(lines)


def _body_tag(line):
    parts = re.split(r"\s+#\s?", line, maxsplit=1)
    return parts[0], (parts[1].strip() if len(parts) > 1 else "")


# ---------------------------------------------------------------- watched parameters
def watch_entries(raw):
    out = {}
    for i, line in section_lines(raw, "Watched parameters"):
        body, tag = _body_tag(line)
        m = re.match(r"^\s*-\s*`?([A-Za-z_][A-Za-z0-9_]*)`?\s*(abs<=|<=|>=|==|<|>|in\b)?\s*(.*?)\s*$", body)
        if m:
            out[m.group(1).upper()] = {"i": i, "name": m.group(1).upper(), "op": m.group(2) or "", "val": (m.group(3) or "").strip() if m.group(2) else "",
                                       "tag": tag, "auto": bool(tag and AUTO_RX.search(tag))}
    return out


def fmt_watch(name, op="", val="", tag=""):
    return "- %s%s%s" % (name, (" %s %s" % (op, val)) if op else "", ("   # " + tag) if tag else "")


def set_watch(raw, name, op="", val="", tag=""):
    e = watch_entries(raw).get(name)
    line = fmt_watch(name, op, val, tag)
    if e:
        return _set_line(raw, e["i"], line)
    return _insert_in(raw, "Watched parameters", line, before="Acceptance criteria")


def remove_watch(raw, name):
    e = watch_entries(raw).get(name)
    return _set_line(raw, e["i"], None) if e else raw


def tighter(old_op, old_val, new_op, new_val):
    """True when the new rule allows no more than the old one (so it may be applied without asking)."""
    if not old_op:
        return True

    def n(x):
        try:
            return float(str(x).lower().replace("d", "e"))
        except ValueError:
            return None
    ov, nv = n(old_val), n(new_val)
    oset = {x.strip().upper() for x in str(old_val).split("|")} if old_op == "in" else None
    if new_op == "==":
        if old_op == "==":
            return norm_val(old_val) == norm_val(new_val)
        if old_op == "in":
            return norm_val(new_val) in {norm_val(x) for x in oset}
        if nv is None or ov is None:
            return False
        return {"<=": nv <= ov, "<": nv < ov, ">=": nv >= ov, ">": nv > ov, "abs<=": abs(nv) <= abs(ov)}.get(old_op, False)
    if new_op == "in":
        nset = {norm_val(x) for x in str(new_val).split("|")}
        if old_op == "in":
            return nset <= {norm_val(x) for x in oset}
        return old_op == "==" and nset == {norm_val(old_val)}
    if nv is None or ov is None or new_op != old_op:
        return False
    return {"<=": nv <= ov, "<": nv <= ov, ">=": nv >= ov, ">": nv >= ov, "abs<=": abs(nv) <= abs(ov)}.get(new_op, False)


def widen(op, val, value):
    """The smallest change to a rule that also allows `value` (used when you approve a value the rule blocks)."""
    v = norm_val(value)
    if op == "==":
        return ("in", "%s|%s" % (val, v)) if norm_val(val) != v else (op, val)
    if op == "in":
        vals = [x.strip() for x in val.split("|") if x.strip()]
        return ("in", "|".join(vals + ([v] if v not in {norm_val(x) for x in vals} else [])))
    try:
        x, ref = float(v), float(str(val).lower().replace("d", "e"))
    except ValueError:
        return ("in", "%s|%s" % (val, v))
    if op in ("<=", "<"):
        return (op, val) if x <= ref else ("<=", v)
    if op == "abs<=":
        return (op, val) if abs(x) <= abs(ref) else (op, v)
    if op in (">=", ">"):
        return (op, val) if x >= ref else (">=", v)
    return ("", "")


# ---------------------------------------------------------------- acceptance criteria and amendments
def criteria_texts(raw):
    out = []
    for i, line in section_lines(raw, "Acceptance criteria"):
        m = re.match(r"^\s*-\s+(.*\S)", line)
        if m:
            out.append((i, _body_tag(m.group(1))[0].strip()))
    return out


def crit_key(text):
    return re.sub(r"\W+", " ", text).strip().lower()


def add_criterion(raw, text, tag=""):
    key = crit_key(text)
    if any(crit_key(t) == key for _, t in criteria_texts(raw)):
        return raw
    return _insert_in(raw, "Acceptance criteria", "- %s%s" % (text.replace("\n", " ").strip(), ("   # " + tag) if tag else ""), before="Compute")


def remove_criterion(raw, text):
    for i, t in criteria_texts(raw):
        if t == text.strip():
            return _set_line(raw, i, None)
    return raw


def next_amend_id(raw):
    ids = [int(m.group(1)) for _, l in section_lines(raw, "Amendments") for m in [re.search(r"\|\s*A(\d+)\s*\|", l)] if m]
    return "A%d" % (max(ids) + 1 if ids else 1)


def add_amendment(raw, text):
    aid = next_amend_id(raw)
    return _insert_in(raw, "Amendments", "- %s | %s | %s" % (now_s(), aid, text.replace("\n", " ").strip())), aid


# ---------------------------------------------------------------- compute
CKEYS = ("allowed", "not allowed", "budget", "cpu cap", "watched by mission control", "never contacted")


def compute_entries(raw):
    out, legacy = {}, False
    for i, line in section_lines(raw, "Compute"):
        body, tag = _body_tag(line)
        m = re.match(r"^\s*-\s*([A-Za-z ]+?)\s*:\s*(.*?)\s*$", body)
        if not m:
            continue
        k = m.group(1).strip().lower()
        if k == "note" and "never polled" in m.group(2):
            legacy = True
        out[k] = {"i": i, "val": m.group(2), "tag": tag, "auto": bool(tag and AUTO_RX.search(tag))}
    if legacy:   # the old one-time note written when the contract was created: mission control's own, so replaceable
        for k in ("allowed", "note"):
            if k in out:
                out[k]["auto"] = True
    return out


def set_compute(raw, key, val, tag=""):
    e = compute_entries(raw).get(key)
    line = "- %s: %s%s" % (key, val, ("   # " + tag) if tag else "")
    if e:
        return _set_line(raw, e["i"], line)
    return _insert_in(raw, "Compute", line, before="Amendments")


def drop_compute(raw, key):
    e = compute_entries(raw).get(key)
    return _set_line(raw, e["i"], None) if e else raw


def parse_compute(raw):
    e = compute_entries(raw)

    def names(k):
        v = (e.get(k) or {}).get("val") or ""
        return [x.strip() for x in re.split(r"[,;]", re.sub(r"\(.*?\)", "", v)) if x.strip()] if v else []
    budget = None
    m = re.match(r"\s*(" + NUM + r")\s*(\S.*)?$", ((e.get("budget") or {}).get("val") or "").replace(",", ""))
    if m:
        budget = (float(m.group(1)), (m.group(2) or "SU").strip())
    caps = {}
    for part in re.split(r"[,;]", (e.get("cpu cap") or {}).get("val") or ""):
        mm = re.match(r"\s*([\w.-]+)\s*<=?\s*(\d+(?:\.\d+)?)\s*%", part)
        if mm:
            caps[mm.group(1).lower()] = float(mm.group(2))
    return {"allowed": names("allowed") if "allowed" in e else None, "forbid": names("not allowed"), "budget": budget, "cpu_cap": caps,
            "entries": e}


# ---------------------------------------------------------------- reading the goal's own instructions
def spec_settings(text, names, implicit):
    """{NAME: (op, value, snippet)} for settings the text states once and only one way."""
    found = {}
    for name in names:
        for word in [name] + ALIASES.get(name, []):
            rx = re.compile(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])\s*(==|=|:|<=|>=|≤|≥|<|>)?\s*(?:of|is|to|at|set to)?\s*(%s|[A-Za-z][A-Za-z0-9_.+-]*)"
                            % (re.escape(word), NUM), re.I)
            for m in rx.finditer(text):
                sym, v = m.group(1), m.group(2)
                isnum = re.match(NUM + "$", v) is not None
                if not isnum and (sym not in ("=", "==", ":") or v.lower() in STOPWORDS):
                    continue
                op = {"<=": "<=", "≤": "<=", ">=": ">=", "≥": ">=", "<": "<", ">": ">"}.get(sym or "") or (
                    implicit.get(name, "==") if isnum else "==")
                a = max(0, m.start() - 50)
                found.setdefault(name, []).append((op, norm_val(v), re.sub(r"\s+", " ", text[a:m.end() + 30]).strip()))
    out = {}
    for name, lst in found.items():
        if len({(o, v) for o, v, _ in lst}) == 1:   # several different values = a test plan, not a rule
            out[name] = lst[0]
    return out


def spec_compute(text, hosts):
    res = {"only": [], "forbid": [], "budget": {}, "cpu_cap": {}, "why": {}}
    for h in hosts:
        H = re.escape(h)
        m = re.search(r"\b(?:no|never|avoid|without|not use|not run on|not on|don'?t use|do not use)\s+(?:(?:any|direct|new|more)\s+)?"
                      r"(?:[\w-]+\s+(?:or|and|nor)\s+(?:(?:any|direct|new)\s+)?)?%s\b(\s+\w+)?" % H, text, re.I)
        if m:
            res["forbid"].append(h + (" (direct work)" if re.search(r"direct\s+%s" % H, m.group(0), re.I) else ""))
            res["why"]["forbid:" + h] = m.group(0)
        # "Rockfish Slurm only for new jobs" or "only on Rockfish" (exclusive); not "Skipjack only if needed" (conditional)
        m = re.search(r"\b%s\b(?:\s+Slurm)?\s+only\b(?!\s+(?:if|when|as|after|in case|where|while|until|with|once))"
                      r"|\b(?:only|exclusively)\s+(?:use\s+|on\s+|run\s+on\s+)?%s\b(?!\s+(?:if|when|as needed))" % (H, H), text, re.I)
        if m:
            res["only"].append(h)
            res["why"]["only:" + h] = m.group(0)
        m = re.search(r"\b%s\s+CPU(?:\s+usage|\s+load)?\s+(?:at or below|at most|below|under|<=|≤|no more than|max(?:imum)?(?: of)?)\s*(\d+(?:\.\d+)?)\s*%%"
                      % H, text, re.I)
        if m:
            res["cpu_cap"][h.lower()] = float(m.group(1))
            res["why"]["cap:" + h] = m.group(0)
    for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*([kKmM])?\s*(SUs?|service units|core[- ]?hours?|core[- ]?h|CPU[- ]?hours?|GPU[- ]?hours?|GPU[- ]?h)\b",
                         text):
        ctx = text[max(0, m.start() - 60): m.end() + 60]
        if not re.search(r"ceiling|\bcap\b|budget|limit|at most|up to|maximum|\bmax\b|no more than|within", ctx, re.I):
            continue
        amt = float(m.group(1).replace(",", "")) * {"k": 1e3, "m": 1e6}.get((m.group(2) or "").lower(), 1)
        unit = "GPU-h" if "gpu" in m.group(3).lower() else ("SU" if m.group(3).lower().startswith(("su", "service")) else "core-h")
        if unit not in res["budget"] or amt < res["budget"][unit][0]:
            res["budget"][unit] = (amt, re.sub(r"\s+", " ", ctx).strip())
    return res


# ---------------------------------------------------------------- state kept per goal (decisions, learning)
def load_state(state_dir, key):
    try:
        with open(os.path.join(state_dir, key + ".json")) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    for k, d in (("rejected", []), ("learn", {}), ("proposals", {}), ("seen_specs", {}), ("requests_done", [])):
        st.setdefault(k, d)
    return st


def save_state(state_dir, key, st):
    os.makedirs(state_dir, exist_ok=True)
    tmp = os.path.join(state_dir, ".%s.tmp" % key)
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, os.path.join(state_dir, key + ".json"))


# ---------------------------------------------------------------- proposals ("Waiting for your OK")
def proposals(raw):
    out = []
    for i, line in section_lines(raw, "Waiting for your OK"):
        m = re.match(r"^\s*-\s*(P\d+)\s*\|\s*([^|]*)\|\s*([^|]*)\|\s*(.*?)\s*$", line)
        if m:
            out.append({"i": i, "id": m.group(1), "when": m.group(2).strip(), "text": m.group(3).strip(), "action": m.group(4).strip()})
    return out


def add_proposal(raw, text, action):
    for p in proposals(raw):
        if p["action"] == action:
            return raw, p["id"]
    ids = [int(m.group(1)) for _, l in section_lines(raw, "Waiting for your OK") for m in [re.search(r"-\s*P(\d+)\s*\|", l)] if m]
    pid = "P%d" % (max(ids) + 1 if ids else 1)
    raw = ensure_section(raw, "Waiting for your OK", before="Amendments")
    if not section_lines(raw, "Waiting for your OK") or not any(l.startswith("Changes that would loosen") for _, l in section_lines(raw, "Waiting for your OK")):
        raw = _insert_in(raw, "Waiting for your OK", "Changes that would loosen a rule. Approve or reject them on the dashboard.")
    return _insert_in(raw, "Waiting for your OK", "- %s | %s | %s | %s" % (pid, now_s(), text.replace("|", "/"), action)), pid


def remove_proposal(raw, pid):
    for p in proposals(raw):
        if p["id"] == pid:
            raw = _set_line(raw, p["i"], None)
    if not proposals(raw):   # tidy the empty section away
        lines, secs = _split(raw)
        r = _range(secs, "waiting for your ok", len(lines))
        if r:
            del lines[r[0] - 1:r[1]]
            raw = "\n".join(lines)
    return raw


def apply_action(raw, action):
    """Do one change: set NAME OP VAL | allow NAME VALUE | unwatch NAME | allowed A, B | forbid X | budget N UNIT | cap HOST PCT
    | criterion TEXT. Returns (raw, plain-words summary)."""
    a = action.strip()
    m = re.match(r"^set\s+([A-Z_][A-Z0-9_]*)\s+(abs<=|<=|>=|==|<|>|in)\s+(.+)$", a, re.I)
    if m:
        return set_watch(raw, m.group(1).upper(), m.group(2), m.group(3).strip()), "%s %s %s" % (m.group(1).upper(), m.group(2), m.group(3).strip())
    m = re.match(r"^allow\s+([A-Z_][A-Z0-9_]*)\s+(.+)$", a, re.I)
    if m:
        name = m.group(1).upper()
        e = watch_entries(raw).get(name) or {"op": "", "val": ""}
        op, val = widen(e["op"], e["val"], m.group(2)) if e["op"] else ("", "")
        return set_watch(raw, name, op, val), "%s now allows %s%s" % (name, m.group(2).strip(), (" (%s %s)" % (op, val)) if op else "")
    m = re.match(r"^unwatch\s+([A-Z_][A-Z0-9_]*)$", a, re.I)
    if m:
        return remove_watch(raw, m.group(1).upper()), "stopped watching %s" % m.group(1).upper()
    m = re.match(r"^allowed\s+(.+)$", a, re.I)
    if m:
        return set_compute(raw, "allowed", m.group(1).strip()), "allowed clusters: %s" % m.group(1).strip()
    m = re.match(r"^forbid\s+(.+)$", a, re.I)
    if m:
        cur = parse_compute(raw)["forbid"]
        new = cur + [x.strip() for x in m.group(1).split(",") if x.strip() and x.strip() not in cur]
        return set_compute(raw, "not allowed", ", ".join(new)), "not allowed: %s" % ", ".join(new)
    m = re.match(r"^budget\s+(" + NUM + r")\s*(.*)$", a, re.I)
    if m:
        return set_compute(raw, "budget", "%g %s" % (float(m.group(1)), m.group(2).strip() or "SU")), "budget %g %s" % (float(m.group(1)), m.group(2).strip() or "SU")
    m = re.match(r"^cap\s+([\w.-]+)\s+(\d+(?:\.\d+)?)$", a, re.I)
    if m:
        caps = parse_compute(raw)["cpu_cap"]
        caps[m.group(1).lower()] = float(m.group(2))
        return set_compute(raw, "cpu cap", ", ".join("%s <= %g%%" % kv for kv in sorted(caps.items()))), "%s CPU cap %s%%" % (m.group(1), m.group(2))
    m = re.match(r"^criterion\s+(.+)$", a, re.I)
    if m:
        return add_criterion(raw, m.group(1).strip()), "criterion: %s" % m.group(1).strip()
    raise ValueError("unknown change: %s" % action)


# ---------------------------------------------------------------- the per-collection upkeep
def upkeep(raw, ctx, st):
    """Bring one contract up to date. ctx: goal_text, specs {name: {text, mtime}}, values {NAME: {value: count}}, codes,
    hosts (cluster/host names), watched (hosts mission control watches, with notes), never (never-contacted names),
    requests (agent requests), implicit (default ops), infer_criteria (function), now.
    Returns (raw, notes) where notes lists what changed, for the dashboard and the log."""
    notes = []
    now = ctx.get("now") or time.time()
    implicit = ctx.get("implicit") or {}

    def propose(text, action, why_sig):
        nonlocal raw
        s = sig(action, why_sig)
        if s in st["rejected"]:
            return
        raw, pid = add_proposal(raw, text, action)
        st["proposals"][pid] = {"sig": s, "action": action}

    # 0. The old one-time compute note written when the contract was created goes first (its "allowed" was a guess)
    cp = parse_compute(raw)
    if "note" in cp["entries"] and cp["entries"]["note"]["auto"]:
        raw = drop_compute(raw, "note")
        e = parse_compute(raw)["entries"].get("allowed")
        if e and not e["tag"]:
            raw = drop_compute(raw, "allowed")
        notes.append("replaced the old compute note")

    # 1. The goal text: kept in step with the Codex goal while it is the auto-filled copy
    gt = (ctx.get("goal_text") or "").strip()
    cg = (ctx.get("codex_goal") or "").strip()   # only the Codex goal itself is copied in, never a typed message
    if cg:
        lines = section_lines(raw, "Goal")
        body = "\n".join(l for _, l in lines).strip()
        auto = re.search(r"_\(auto-filled from the Codex goal on [^)]*\)_", body)
        cur = re.sub(r"\s*_\(auto-filled[^)]*\)_\s*", "", body).strip()
        if lines and (auto or not cur or cur.startswith("_(empty")) and cur != cg:
            ls, secs = _split(raw)
            a, b = _range(secs, "goal", len(ls))
            ls[a:b] = ["", cg, "", "_(auto-filled from the Codex goal on %s; kept in step with it)_" % today(), ""]
            raw = "\n".join(ls)
            notes.append("goal text updated from the Codex goal")

    # 2. The goal's own written instructions (START_GOAL.md ...) and the goal text: settings, limits, criteria.
    #    Lines you wrote are never changed; automatic lines may only tighten; anything looser waits for your OK.
    texts = [(n, s.get("text") or "") for n, s in sorted((ctx.get("specs") or {}).items())]
    if gt:
        texts.append(("the Codex goal" if gt == cg else "the goal text", gt))
    varies = set(ctx.get("varies") or ())
    skip = set(st.get("unlocked") or [])   # names you unlocked or stopped watching: never locked again automatically
    dropped = set(st.get("dropped_criteria") or [])
    names = sorted((set(watch_entries(raw)) | {n for n in SCAN_NAMES if n != "POT_*"}) - varies - skip)
    low = lambda xs: {x.split(" (")[0].strip().lower() for x in xs}  # noqa: E731
    for src, text in texts:
        tag = "from %s" % src
        for name, (op, val, snip) in spec_settings(text, names, implicit).items():
            e = watch_entries(raw).get(name)
            if not e or not e["op"]:
                raw = set_watch(raw, name, op, val, tag)
                notes.append("%s %s %s (%s)" % (name, op, val, tag))
            elif not e["auto"] or (e["op"] == op and norm_val(e["val"]) == val):
                continue
            elif tighter(e["op"], e["val"], op, val):
                raw = set_watch(raw, name, op, val, tag)
                notes.append("%s %s %s (%s)" % (name, op, val, tag))
            else:
                propose("%s says %s %s %s; the contract has %s %s" % (src, name, op, val, e["op"], e["val"]),
                        "set %s %s %s" % (name, op, val), snip)
        comp = spec_compute(text, ctx.get("hosts") or [])
        cp = parse_compute(raw)
        e = cp["entries"].get("not allowed")
        new = [h for h in comp["forbid"] if h.split(" (")[0].lower() not in low(cp["forbid"])]
        if new and (e is None or e["auto"]):
            raw = set_compute(raw, "not allowed", ", ".join(cp["forbid"] + new), tag)
            notes.append("not allowed: %s (%s)" % (", ".join(new), tag))
        cp = parse_compute(raw)
        e = cp["entries"].get("allowed")
        if comp["only"] and (e is None or e["auto"]):
            only = sorted(set(comp["only"]), key=str.lower)
            if e is None or low(only) < low(cp["allowed"] or []):
                raw = set_compute(raw, "allowed", ", ".join(only), tag)
                notes.append("allowed: %s (%s)" % (", ".join(only), tag))
            elif low(only) != low(cp["allowed"] or []):
                propose("%s allows %s; the contract allows %s" % (src, ", ".join(only), ", ".join(cp["allowed"] or [])),
                        "allowed %s" % ", ".join(only), comp["why"])
        cp = parse_compute(raw)
        e = cp["entries"].get("budget")
        for unit, (amt, why) in sorted(comp["budget"].items()):
            b = parse_compute(raw)["budget"]
            if e is None and b is None:
                raw = set_compute(raw, "budget", "%g %s" % (amt, unit), tag)
                notes.append("budget %g %s (%s)" % (amt, unit, tag))
                e = parse_compute(raw)["entries"].get("budget")
            elif e and e["auto"] and b and b[1].lower() == unit.lower() and amt != b[0]:
                if amt < b[0]:
                    raw = set_compute(raw, "budget", "%g %s" % (amt, unit), tag)
                    notes.append("budget %g %s (%s)" % (amt, unit, tag))
                else:
                    propose("%s sets the budget to %g %s; the contract has %g %s" % (src, amt, unit, b[0], b[1]),
                            "budget %g %s" % (amt, unit), why)
        cp = parse_compute(raw)
        e = cp["entries"].get("cpu cap")
        for h, pct in sorted(comp["cpu_cap"].items()):
            old = cp["cpu_cap"].get(h)
            if e is not None and not e["auto"]:
                break
            if old is None or pct < old:
                caps = dict(cp["cpu_cap"], **{h: pct})
                raw = set_compute(raw, "cpu cap", ", ".join("%s <= %g%%" % kv for kv in sorted(caps.items())), tag)
                cp = parse_compute(raw)
                e = cp["entries"].get("cpu cap")
                notes.append("%s CPU cap %g%% (%s)" % (h, pct, tag))
            elif pct > old:
                propose("%s caps %s CPU at %g%%; the contract has %g%%" % (src, h, pct, old), "cap %s %g" % (h, pct), comp["why"])
        for c in (ctx.get("infer_criteria") or (lambda t: []))(text):
            if crit_key(c["text"]) in dropped:
                continue
            before = raw
            raw = add_criterion(raw, c["text"], tag)
            if raw != before:
                notes.append("criterion: %s (%s)" % (c["text"], tag))

    # 3. What the run files agree on: a value every recent input file shares becomes a rule (after two collections)
    codes = ctx.get("codes") or {}
    values = ctx.get("values") or {}
    learnable = {n for c in codes for n in CODE_PARAMS.get(c, [])} | ({k for k in values if k.startswith("POT_")} if "VASP" in codes else set())
    for name in sorted(learnable - varies - skip):
        vals = values.get(name) or {}
        e = watch_entries(raw).get(name)
        if len(vals) != 1:
            st["learn"].pop(name, None)
            continue
        (v, n), = vals.items()
        if e and e["op"]:
            st["learn"].pop(name, None)
            continue
        if n < LEARN_MIN_FILES:
            continue
        rec = st["learn"].get(name)
        if not rec or rec.get("v") != v:
            st["learn"][name] = {"v": v, "first": now}
            continue
        if now - rec["first"] >= LEARN_MIN_AGE:
            raw = set_watch(raw, name, "==", v, "learned: all %d recent input files agree (%s)" % (n, today()))
            notes.append("%s == %s (learned from %d files)" % (name, v, n))
            st["learn"].pop(name, None)

    # 4. Compute facts mission control knows: which hosts it watches and which it never contacts (always current)
    for key, val in (("watched by mission control", ctx.get("watched") or ""), ("never contacted", ", ".join(ctx.get("never") or []))):
        e = parse_compute(raw)["entries"].get(key)
        if val and (not e or e["val"] != val):
            raw = set_compute(raw, key, val, "auto, kept current")
        elif not val and e:
            raw = drop_compute(raw, key)

    # 5. The agent asking to change a rule (.mc/requests.jsonl in its folder): shown as a proposal for you to decide
    for r in ctx.get("requests") or []:
        rid = r.get("id") or sig(r.get("param"), r.get("value"), r.get("why"))[:6]
        rs = "agent:%s:%s" % (rid, sig(r.get("param"), r.get("value"), r.get("rule")))
        if rs in st["requests_done"] or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(r.get("param") or "")):
            continue
        name = str(r["param"]).upper()
        rule = re.match(r"^\s*(abs<=|<=|>=|==|<|>|in)\s+(.+)$", str(r.get("rule") or ""))
        action = ("set %s %s %s" % (name, rule.group(1), rule.group(2).strip())) if rule else ("allow %s %s" % (name, str(r.get("value") or "").strip()))
        if not rule and not str(r.get("value") or "").strip():
            continue
        if any(p["action"] == action for p in proposals(raw)):
            continue
        raw, pid = add_proposal(raw, "The agent asks (%s): %s%s. Why: %s" % (rid, action.split(" ", 1)[1],
                                                                          (" for %s" % r["scope"][:120]) if r.get("scope") else "", (r.get("why") or "not given")[:300]), action)
        st["proposals"][pid] = {"sig": rs, "action": action, "agent": rid}
        st["requests_done"].append(rs)
        notes.append("agent request %s waiting for your OK" % rid)
    return raw, notes


def decide(raw, st, pid, approve, once=False):
    """Approve or reject a waiting change. With once=True the value is allowed for that request only: an amendment
    records it (so those files aren't flagged) but the rule itself stays. Returns (raw, summary, info)."""
    p = next((x for x in proposals(raw) if x["id"] == pid), None)
    if not p:
        raise ValueError("%s is no longer waiting (already decided?)" % pid)
    meta = st["proposals"].pop(pid, {}) or {}
    raw = remove_proposal(raw, pid)
    summary = ""
    if approve and once:
        what = p["action"].split(" ", 1)[1] if " " in p["action"] else p["action"]
        raw, aid = add_amendment(raw, "Approved %s%s for that request only (the rule stays): %s (%s)" % (
            pid, " from the agent (%s)" % meta["agent"] if meta.get("agent") else "", what, p["text"]))
        summary = "%s allowed for that request only, recorded as %s; the rule stays" % (what, aid)
    elif approve:
        raw, summary = apply_action(raw, p["action"])
        raw, aid = add_amendment(raw, "Approved %s%s: %s (%s)" % (pid, " from the agent (%s)" % meta["agent"] if meta.get("agent") else "", summary, p["text"]))
        summary = "%s, recorded as %s" % (summary, aid)
    else:
        st["rejected"].append(meta.get("sig") or sig(p["action"], p["text"]))
        summary = "Rejected %s: %s" % (pid, p["text"])
    return raw, summary, {"agent": meta.get("agent"), "text": p["text"], "action": p["action"]}


def make_firm(raw, name):
    """Keep an automatic rule as yours (drops its source tag, so nothing automatic changes it again)."""
    e = watch_entries(raw).get(name)
    if not e or not e["op"]:
        raise ValueError("%s has no rule to keep" % name)
    return set_watch(raw, name, e["op"], e["val"], "")


def protocol_text(raw, cwd):
    rules = ["%s %s %s" % (n, e["op"], e["val"]) for n, e in sorted(watch_entries(raw).items()) if e["op"]]
    cp = parse_compute(raw)
    limits = []
    if cp["allowed"]:
        limits.append("clusters: only %s" % ", ".join(cp["allowed"]))
    if cp["forbid"]:
        limits.append("not allowed: %s" % ", ".join(cp["forbid"]))
    if cp["budget"]:
        limits.append("budget %g %s" % cp["budget"])
    for h, p in sorted(cp["cpu_cap"].items()):
        limits.append("%s CPU at or below %g%%" % (h, p))
    where = os.path.join(cwd, ".mc", "requests.jsonl") if cwd else ".mc/requests.jsonl (in your working folder)"
    return ("[mission control] The contract for this goal protects these settings: %s.%s Before you change one of them or work outside "
            "these limits, ask first: append one JSON line to %s, for example "
            "{\"id\": \"R1\", \"param\": \"ENCUT\", \"value\": \"520\", \"why\": \"cutoff convergence check\", \"scope\": \"runs/conv\"} "
            "(or \"rule\": \"<= 1e-6\" instead of \"value\"). Carry on with other work meanwhile; the user approves it (for that request "
            "only, or as a new rule) or rejects it on the dashboard, and you get a message with the answer. When the user asks about "
            "dashboard items (IDs like I3fa21), answer each on its own line: \"I3fa21: fixed | intended | explained | working | "
            "need_decision - short reason\".") % (
        "; ".join(rules) or "none yet", (" Limits: %s." % "; ".join(limits)) if limits else "", where)

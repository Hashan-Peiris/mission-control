#!/usr/bin/env python3
# mission-control digest: read-only summary of agent sessions on this host.
# Python 3.6+, standard library only. Prints one JSON document to stdout.
# Usage: python3 - <base64-json-config>   (script on stdin, config as one argument)
import base64
import glob
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time

T0 = time.time()
CFG = {}
if len(sys.argv) > 1 and sys.argv[1]:
    try:
        CFG = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
    except Exception as e:  # noqa
        CFG = {"_cfg_error": str(e)}

SESSION = CFG.get("tmux_session", "")
HOURS = float(CFG.get("hours", 48))
WATCH = [w.strip().upper() for w in CFG.get("watch", []) if w.strip()]
PARAM_FILES = [p.strip() for p in CFG.get("param_files", ["INCAR", "KPOINTS"]) if p.strip()]
SCAN_ROOTS = [os.path.expanduser(r) for r in CFG.get("scan_roots", []) if r]
SCAN_HOURS = float(CFG.get("scan_hours", 72))
SCAN_EVERY_MIN = float(CFG.get("scan_every_min", 30))
HOSTNAME = socket.gethostname().split(".")[0]
# one cache per host: login nodes and compute nodes often share the same home directory
CACHE_PATH = os.path.expanduser(CFG.get("cache") or "~/.cache/mission-control/digest_cache.%s.json" % HOSTNAME)
LEGACY_CACHE = os.path.expanduser("~/.cache/mission-control/digest_cache.json")
LIVE_ONLY = bool(CFG.get("live_only"))   # login nodes: only sessions of agents running right now
CACHE_VERSION = 2          # bump to rebuild every session's state from the start of its log
MSG_SINCE = CFG.get("msg_since") or {}   # {path: {"epoch": ..., "seq": ...}} already held by the collector
MSG_KEEP = 4000            # conversation messages kept per session in the cache
MSG_TEXT = 20000           # characters kept per message
WITH_CLAUDE = bool(CFG.get("claude", True))
LABEL = CFG.get("label", "")
CUTOFF = T0 - HOURS * 3600
PARSE_DEADLINE = T0 + float(CFG.get("parse_budget_s", 90))
os.environ["PATH"] = os.pathsep.join([os.path.expanduser("~/.local/bin"), os.path.expanduser("~/bin"),
                                      os.environ.get("PATH", "/usr/bin:/bin"), "/usr/local/bin", "/usr/bin"])

SHORTCUT_RE = re.compile(
    r"(pytest\.skip|mark\.(skip|xfail)|\bxfail\b|--no-verify|\bmock(ed|ing)?\b|\bfake\b|\bdummy\b|"
    r"\bplaceholder\b|hard-?cod(e|ed|ing)\b|\bstub(bed)?\b|"
    r"skip(ping|ped)?\s+(the\s+)?(validation|verification|check|convergence|test)|"
    r"loosen(ed|ing)?\b|relax(ed|ing)?\s+(the\s+)?(tolerance|threshold|criteri)|"
    r"reduc(e|ed|ing)\s+(the\s+)?(k-?points?|kpoint|k-?mesh|cutoff|encut|accuracy|precision|convergence|smearing)|"
    r"lower(ed|ing)?\s+(the\s+)?(cutoff|accuracy|precision|k-?points?|k-?mesh)|"
    r"cheaper\s+settings|to\s+save\s+time|for\s+speed|instead\s+of\s+(running|computing)|"
    r"synthetic\s+(data|results)|made[- ]up|assum(e|ed|ing)\s+(it\s+)?converged|good\s+enough)",
    re.I,
)
if WATCH:
    WATCH_RE = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(re.escape(w) for w in WATCH) + r")(?![A-Za-z0-9_])", re.I)
else:
    WATCH_RE = None

NOTABLE_KINDS = {"submit", "cancel", "alloc", "delete", "edit", "git", "agent", "goal", "ssh", "plan", "evaluate", "train"}
CUSTOM_METRICS = []
for _c in CFG.get("custom_metrics", []) or []:
    try:
        CUSTOM_METRICS.append((_c["label"], re.compile(_c["regex"], re.I | re.M)))
    except Exception:  # noqa
        pass


# ---------------------------------------------------------------- result metrics (DeePMD and friends)
DP_LINE = re.compile(r"(Energy|Force|Virial)\s+(MAE|RMSE)(/Natoms)?\s*:\s*([-+]?[0-9.]+(?:[eE][-+]?\d+)?)\s*(\S*)")
GENERIC_ATOM = re.compile(r"(?i)\b(?:energy|e)[ _-]*(rmse|mae)[ _-]*(?:per[ _-]?atom|/atom|/natoms)\b\s*[:=]?\s*([-+]?[0-9.]+(?:[eE][-+]?\d+)?)\s*(meV|eV)\b")
CLAIM_RE = re.compile(r"(?i)([0-9]+(?:\.[0-9]+)?(?:[eE][-+]?\d+)?)\s*(meV|eV)\s*/\s*(atom|at)\b")


def deepmd_parse(text):
    """Pull the error table out of `dp test` output (or any log that contains it)."""
    if not text or ("RMSE" not in text and "MAE" not in text and "rmse" not in text):
        return None
    body = text
    k = text.rfind("weighted average")
    if k >= 0:
        body = text[k:]
    metrics = {}
    for m in DP_LINE.finditer(body):
        q, stat, per, val = m.group(1).lower(), m.group(2).lower(), m.group(3), m.group(4)
        try:
            metrics["%s_%s%s" % (q, stat, "_atom" if per else "")] = float(val)
        except ValueError:
            pass
    for m in GENERIC_ATOM.finditer(text):
        v = float(m.group(2)) * (1e-3 if m.group(3).lower() == "mev" else 1.0)
        metrics.setdefault("energy_%s_atom" % m.group(1).lower(), v)
    for label, rx in CUSTOM_METRICS:
        mm = None
        for mm in rx.finditer(text):
            pass
        if mm:
            try:
                metrics["custom:" + label] = float(mm.group(1))
            except (ValueError, IndexError):
                pass
    if not metrics:
        return None
    n = [int(x) for x in re.findall(r"number of test data\s*:\s*(\d+)", text)]
    systems = [x.strip() for x in re.findall(r"testing system\s*:\s*(\S+)", text)]
    return {"metrics": metrics, "n_test": sum(n) if n else None, "systems": systems[:50]}


def lcurve_parse(path):
    try:
        with open(path, "rb") as f:
            head = f.read(4096).decode("utf-8", "replace")
            f.seek(0, 2)
            sz = f.tell()
            f.seek(max(0, sz - 8192))
            tail = f.read().decode("utf-8", "replace")
    except Exception:  # noqa
        return None
    hdr = next((l for l in head.splitlines() if l.startswith("#") and "rmse" in l), "")
    cols = hdr.lstrip("#").split()
    rows = [l for l in tail.splitlines() if l.strip() and not l.startswith("#")]
    if not cols or not rows:
        return None
    vals = rows[-1].split()
    if len(vals) != len(cols):
        return None
    d = dict(zip(cols, vals))
    out = {}
    for c, key in (("rmse_e_val", "energy_rmse_atom_val"), ("rmse_e_trn", "energy_rmse_atom_train"),
                   ("rmse_f_val", "force_rmse_val"), ("rmse_f_trn", "force_rmse_train"),
                   ("rmse_v_val", "virial_rmse_atom_val")):
        try:
            if c in d:
                out[key] = float(d[c])
        except ValueError:
            pass
    if not out:
        return None
    return {"metrics": out, "step": d.get("step"), "n_test": None, "systems": []}


def dp_test_args(cmd):
    m = re.search(r"\bdp\s+(?:--(?:pt|tf|jax|pd)\s+)?test\b(.*)", cmd or "")
    if not m:
        return None
    rest = m.group(1)
    def opt(*names):
        for nm in names:
            mm = re.search(r"(?:^|\s)%s(?:\s+|=)(\S+)" % re.escape(nm), rest)
            if mm:
                return mm.group(1).strip("'\"")
        return None
    n = opt("-n", "--numb-test")
    return {"model": opt("-m", "--model"), "system": opt("-s", "--system"), "datafile": opt("-f", "--datafile"),
            "n": int(n) if n and n.isdigit() else None}


def output_text(output):
    if isinstance(output, list):
        return "\n".join(text_of([x]) if isinstance(x, dict) else str(x) for x in output)
    if isinstance(output, dict):
        return output.get("output") if isinstance(output.get("output"), str) else json.dumps(output)
    if isinstance(output, str):
        t = output.strip()
        if t.startswith("{") and '"output"' in t[:200]:
            try:
                o = json.loads(t)
                if isinstance(o, dict) and isinstance(o.get("output"), str):
                    return o["output"]
            except Exception:  # noqa
                pass
        return output
    return str(output)


def run(cmd, timeout=20, inp=None):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                           timeout=timeout, input=inp)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "not found"
    except Exception as e:  # noqa
        return 1, "", str(e)


# ---------------------------------------------------------------- storage of each goal's working folder
# A full walk of a big folder can take minutes on a cluster filesystem, so it runs as its own low-priority background
# process at most every STORAGE_EVERY_MIN; each digest only reads the last finished result (and starts the next walk
# when one is due). Results: ~/.cache/mission-control/storage/<hash>.json
STORAGE_EVERY_MIN = 30
STORAGE_SCANNER = r"""
import heapq, json, os, sys, time
root, out, lock = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    os.nice(15)
except Exception:
    pass
with open(lock, "w") as f:
    f.write(str(os.getpid()))
t0, limit = time.time(), 25 * 60
VASP = set("WAVECAR CHGCAR CHG OUTCAR CONTCAR POSCAR PROCAR DOSCAR EIGENVAL LOCPOT XDATCAR OSZICAR IBZKPT PCDAT REPORT ELFCAR "
           "AECCAR0 AECCAR1 AECCAR2 WAVEDER vaspout.h5 vasprun.xml".split())
def kind(name):
    if name in VASP or name.split(".")[0] in VASP:   # WAVECAR, WAVECAR.static, OUTCAR.relax1 ... count as the VASP file
        return name.split(".")[0] if name not in VASP else name
    low = name.lower()
    parts = low.rsplit(".", 2)
    if len(parts) == 3 and parts[2] in ("gz", "bz2", "xz", "zst") and 0 < len(parts[1]) <= 6:
        return "." + parts[1] + "." + parts[2]
    if "." in low[1:]:
        ext = low.rsplit(".", 1)[1]
        if ext.isdigit():
            return "(numbered files)"
        if 0 < len(ext) <= 10:
            return "." + "".join("#" if ch.isdigit() else ch for ch in ext).replace("##", "#")
    if name.isupper() and len(name) <= 16:
        return name
    return "(no extension)"
try:
    dev = os.stat(root).st_dev
except OSError:
    dev = None
tot = files = dirs = errors = 0
types, big, partial, stack = {}, [], False, [root]
while stack and dev is not None:
    if time.time() - t0 > limit:
        partial = True
        break
    try:
        it = os.scandir(stack.pop())
    except OSError:
        errors += 1
        continue
    with it:
        for e in it:
            try:
                if e.is_symlink():
                    continue
                st = e.stat(follow_symlinks=False)
                if e.is_dir(follow_symlinks=False):
                    if st.st_dev == dev:
                        stack.append(e.path)
                        dirs += 1
                    continue
            except OSError:
                errors += 1
                continue
            sz = (getattr(st, "st_blocks", 0) or 0) * 512 or st.st_size
            tot += sz
            files += 1
            k = kind(e.name)
            t = types.get(k)
            if t:
                t[0] += sz
                t[1] += 1
            else:
                types[k] = [sz, 1]
            if len(big) < 6:
                heapq.heappush(big, (sz, e.path))
            elif sz > big[0][0]:
                heapq.heapreplace(big, (sz, e.path))
top = sorted(types.items(), key=lambda kv: -kv[1][0])
rest = top[14:]
types_out = [[k, v[0], v[1]] for k, v in top[:14]]
if rest:
    types_out.append(["(other types)", sum(v[0] for _, v in rest), sum(v[1] for _, v in rest)])
res = {"root": root, "t": time.time(), "started": t0, "secs": round(time.time() - t0, 1), "bytes": tot, "files": files, "dirs": dirs,
       "types": types_out, "biggest": [[p, b] for b, p in sorted(big, reverse=True)], "partial": partial, "errors": errors}
tmp = out + ".tmp"
with open(tmp, "w") as f:
    json.dump(res, f)
os.replace(tmp, out)
try:
    os.remove(lock)
except OSError:
    pass
"""


def storage_info(roots):
    d = os.path.expanduser("~/.cache/mission-control/storage")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return {}
    res, started = {}, 0
    for r in roots:
        h = hashlib.sha1(r.encode("utf-8", "replace")).hexdigest()[:12]
        fp, lock = os.path.join(d, h + ".json"), os.path.join(d, h + ".pid")
        cur = None
        try:
            with open(fp) as f:
                cur = json.load(f)
            res[r] = cur
        except (OSError, ValueError):
            pass
        running = False
        try:
            age = T0 - os.stat(lock).st_mtime
            with open(lock) as f:
                pid = int(f.read().strip() or 0)
            os.kill(pid, 0)
            running = age < 3 * 3600
        except (OSError, ValueError):
            running = False
        if running:
            res.setdefault(r, {})["scanning_since"] = T0 - age
        due = not cur or T0 - cur.get("started", cur.get("t", 0)) >= STORAGE_EVERY_MIN * 60
        if due and not running and started < 3 and os.path.isdir(r):
            try:
                subprocess.Popen([sys.executable, "-c", STORAGE_SCANNER, r, fp, lock], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
                started += 1
            except OSError:
                pass
    return res


def trunc(s, n):
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def iso_to_epoch(ts):
    if not ts or not isinstance(ts, str):
        return None
    try:
        s = ts.strip().replace("Z", "+00:00")
        m = re.match(r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(\.\d+)?([+-]\d\d:?\d\d)?", s)
        if not m:
            return None
        import calendar
        y, mo, d, h, mi, se = [int(m.group(i)) for i in range(1, 7)]
        frac = float(m.group(7) or 0)
        e = calendar.timegm((y, mo, d, h, mi, se, 0, 0, 0)) + frac
        tz = m.group(8)
        if tz:
            tz = tz.replace(":", "")
            sign = 1 if tz[0] == "+" else -1
            e -= sign * (int(tz[1:3]) * 3600 + int(tz[3:5]) * 60)
        return e
    except Exception:  # noqa
        return None


# ---------------------------------------------------------------- tmux + processes
def proc_table():
    uid = os.getuid()
    procs = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            st = os.stat("/proc/" + d)
            if st.st_uid != uid:
                continue
            with open("/proc/%s/stat" % d) as f:
                stat = f.read()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
            with open("/proc/%s/cmdline" % d, "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
            procs[int(d)] = {"ppid": ppid, "cmd": cmd}
        except Exception:  # noqa
            continue
    return procs


def descendants(procs, roots):
    kids = {}
    for pid, p in procs.items():
        kids.setdefault(p["ppid"], []).append(pid)
    out, stack = set(), list(roots)
    while stack:
        x = stack.pop()
        if x in out:
            continue
        out.add(x)
        stack.extend(kids.get(x, []))
    return out


def open_session_files(pids):
    found = {}
    for pid in pids:
        try:
            for fd in os.listdir("/proc/%d/fd" % pid):
                try:
                    t = os.readlink("/proc/%d/fd/%s" % (pid, fd))
                except Exception:  # noqa
                    continue
                if t.endswith(".jsonl") and ("rollout-" in t or "/.claude/projects/" in t):
                    found[t] = pid
        except Exception:  # noqa
            continue
    return found


def agent_kind(cmd):
    """'codex' or 'claude' for the agent CLI itself (not its sandbox or helper processes), else None."""
    toks = (cmd or "").split()
    if not toks:
        return None
    exe = os.path.basename(toks[0])
    if exe == "codex" or exe.startswith("codex-x86_64") or exe.startswith("codex-aarch64"):
        return "codex"
    if exe == "claude":
        return "claude"
    if exe in ("node", "nodejs", "bun") and len(toks) > 1:
        t1 = toks[1]
        if "codex" in t1 and re.search(r"(@openai/codex|/codex)(/|\.js|$)", t1):
            return "codex"
        if "claude" in t1:
            return "claude"
    return None


def proc_start(pid):
    try:
        with open("/proc/%d/stat" % pid) as f:
            ticks = float(f.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/stat") as f:
            btime = next(float(l.split()[1]) for l in f if l.startswith("btime"))
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except Exception:  # noqa
        return None


def claude_transcripts(pids, procs):
    """Claude Code writes ~/.claude/projects/<cwd with non-alphanumerics as ->/<session>.jsonl without keeping it open,
    so map each running claude process to its transcript through its working directory."""
    found = {}
    root = os.path.expanduser("~/.claude/projects")
    for pid in pids:
        p = procs.get(pid)
        if not p or agent_kind(p["cmd"]) != "claude":
            continue
        try:
            cwd = os.readlink("/proc/%d/cwd" % pid)
        except Exception:  # noqa
            continue
        d = os.path.join(root, re.sub(r"[^A-Za-z0-9]", "-", cwd))
        m = re.search(r"(?:--resume|-r|--session-id)[ =]([0-9a-f-]{36})", p["cmd"])
        if m and os.path.exists(os.path.join(d, m.group(1) + ".jsonl")):
            found[os.path.join(d, m.group(1) + ".jsonl")] = pid
            continue
        start = proc_start(pid) or 0
        best = None
        for fp in glob.glob(os.path.join(d, "*.jsonl")):
            try:
                mt = os.stat(fp).st_mtime
            except Exception:  # noqa
                continue
            if mt >= start - 120 and (best is None or mt > best[0]):
                best = (mt, fp)
        if best:
            found[best[1]] = pid
    return found


def ssh_masters(procs):
    """Shared ssh connections held on this host: sockets that this user's ssh processes listen on."""
    listening = {}
    try:
        with open("/proc/net/unix") as f:
            for line in f.read().splitlines()[1:]:
                p = line.split()
                if len(p) >= 8 and int(p[3], 16) & 0x10000:
                    listening[p[6]] = p[7]
    except Exception:  # noqa
        pass
    out = []
    for pid, v in procs.items():
        m = re.match(r"ssh: (\S+) \[mux\]", v["cmd"])
        paths = [m.group(1)] if m else []
        if not paths and os.path.basename((v["cmd"].split() or [""])[0]) == "ssh":
            try:
                for fd in os.listdir("/proc/%d/fd" % pid):
                    mm = re.match(r"socket:\[(\d+)\]", os.readlink("/proc/%d/fd/%s" % (pid, fd)))
                    if mm and mm.group(1) in listening:
                        paths.append(listening[mm.group(1)])
            except Exception:  # noqa
                continue
        for path in paths:
            final = re.sub(r"\.[A-Za-z0-9]{16}$", "", path)  # ssh binds a temporary name, then renames it
            if final != path and os.path.exists(final):
                path = final
            if os.path.exists(path) and path not in out:
                out.append(path)
    return out


def tmux_info(sess, procs):
    d = {"session": sess, "found": False}
    if not sess:
        return d, set()
    rc, out, err = run(["tmux", "has-session", "-t", "=" + sess])
    if rc != 0:
        rc2, ls, _ = run(["tmux", "ls"])
        d["error"] = "tmux session not found" if rc != 127 else "tmux not installed"
        d["available"] = [trunc(x, 120) for x in ls.strip().splitlines()[:20]]
        return d, set()
    d["found"] = True
    fmt = "#{window_index}.#{pane_index}|#{window_name}|#{window_active}|#{pane_active}|#{pane_pid}|#{pane_current_command}|#{pane_current_path}|#{pane_dead}"
    rc, out, _ = run(["tmux", "list-panes", "-s", "-t", "=" + sess, "-F", fmt])
    panes, roots = [], []
    for line in out.strip().splitlines():
        f = line.split("|")
        if len(f) < 8:
            continue
        panes.append({"id": f[0], "window": f[1], "active": f[2] == "1" and f[3] == "1", "pid": int(f[4] or 0),
                      "cmd": f[5], "path": f[6], "dead": f[7] == "1"})
        if f[4].isdigit():
            roots.append(int(f[4]))
    d["panes"] = panes
    rc, out, _ = run(["tmux", "capture-pane", "-p", "-J", "-t", "=" + sess + ":", "-S", "-220"])
    lines = out.rstrip("\n").splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    d["pane_tail"] = [trunc(x, 400) for x in lines[-220:]]
    fam = descendants(procs, roots)
    d["family_pids"] = len(fam)
    d["codex_running"] = any("codex" in procs[p]["cmd"].lower() for p in fam if p in procs)
    d["claude_running"] = any(re.search(r"(^|/|\s)claude(\s|$)", procs[p]["cmd"]) for p in fam if p in procs)
    d["agent_cmds"] = [trunc(procs[p]["cmd"], 200) for p in fam if p in procs and
                       re.search(r"codex|claude", procs[p]["cmd"], re.I)][:6]
    return d, fam


# ---------------------------------------------------------------- session parsing
def new_state(path, kind):
    return {"path": path, "kind": kind, "offset": 0, "ino": None, "size": 0, "meta": {}, "user": [], "agent": [],
            "tools": [], "notable": [], "patches": [], "plan": None, "plan_expl": None, "plan_ts": None,
            "tokens": None, "rate": None, "ctx": None, "goal_events": [], "counts": {}, "errors": 0, "calls": 0,
            "first_ts": None, "last_ts": None, "compactions": [], "shortcuts": [], "seen_msg": [], "usage_ids": [],
            "metrics": [], "claims": [], "done_claims": [],
            "msgs": [], "msg_seq": 0, "msg_epoch": hashlib.sha1(("%s%s" % (path, time.time())).encode()).hexdigest()[:10]}


def model_tag(st):
    """The model and reasoning level in force right now, e.g. 'gpt-6.2-astra xhigh' (empty if unknown)."""
    m = st.get("meta") or {}
    return " ".join(x for x in (str(m.get("ctx_model") or ""), str(m.get("ctx_effort") or "")) if x)


def add_msg(st, ts, role, text):
    st["msg_seq"] = st.get("msg_seq", 0) + 1
    item = {"seq": st["msg_seq"], "ts": ts, "role": role, "text": trunc(text, MSG_TEXT)}
    if role == "agent" and model_tag(st):
        item["mdl"] = model_tag(st)
    push(st.setdefault("msgs", []), item, MSG_KEEP)


def push(lst, item, cap):
    lst.append(item)
    if len(lst) > cap:
        del lst[: len(lst) - cap]


def note_compaction(st, ts, summary=None, pre_tokens=None):
    """One compaction: its time, how full the context was, and the summary the agent carries on from (into the
    conversation store as a "compact" message, so mission control can check what it kept)."""
    last = st["compactions"][-1] if st["compactions"] else None
    e1, e0 = iso_to_epoch(ts), iso_to_epoch(last) if last else None
    if last and e1 and e0 and abs(e1 - e0) < 120:   # Codex writes the same compaction twice (an event and an item)
        if summary and summary.strip() and not st.get("compact_text_at") == last:
            st["compact_text_at"] = last
            for m in reversed(st.get("msgs") or []):   # the plain "Context compacted" line becomes the summary
                if m.get("role") == "event" and m.get("ts") == last and str(m.get("text") or "").startswith("Context compacted"):
                    m["role"], m["text"] = "compact", trunc(summary.strip(), MSG_TEXT)
                    break
            else:
                add_msg(st, ts, "compact", summary.strip())
            for ci in reversed(st.get("compact_info") or []):
                if ci.get("ts") == last:
                    ci["chars"] = len(summary)
                    break
        return
    push(st["compactions"], ts, 50)
    used = pre_tokens or (st.get("ctx_used") or 0)
    push(st.setdefault("compact_info", []), {"ts": ts, "pre": used or None, "win": st.get("ctx") or None,
                                            "chars": len(summary or "")}, 50)
    push(st["notable"], {"ts": ts, "tool": st.get("kind") or "agent", "kind": "event", "cmd": "context compacted"}, 500)
    if summary and summary.strip():
        st["compact_text_at"] = ts
        add_msg(st, ts, "compact", summary.strip())
    else:
        add_msg(st, ts, "event", "Context compacted: the agent continued from a summary of the conversation so far.")


def backfill_compactions(st):
    """Once per session file: the summaries of compactions from before mission control kept them (read straight from the
    log, only the compaction lines; skipped for files over 1.5 GB)."""
    st["cbf"] = 1
    try:
        if os.path.getsize(st["path"]) > 1.5e9:
            return
        have = {m.get("ts") for m in st.get("msgs") or [] if m.get("role") == "compact"}
        found = []
        with open(st["path"], "rb") as f:
            for line in f:
                if b'"compacted"' not in line and b"isCompactSummary" not in line:
                    continue
                try:
                    o = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                ts, summ = o.get("timestamp"), ""
                if o.get("type") == "compacted":
                    p = o.get("payload") or {}
                    summ = p.get("message") if isinstance(p.get("message"), str) else ""
                    if not summ.strip() and isinstance(p.get("replacement_history"), list):
                        for it in reversed(p["replacement_history"]):
                            if isinstance(it, dict) and it.get("type") == "message" and it.get("role") == "user":
                                summ = text_of(it.get("content"))
                                break
                elif o.get("isCompactSummary"):
                    c = (o.get("message") or {}).get("content")
                    summ = c if isinstance(c, str) else text_of(c)
                if ts and (summ or "").strip() and ts not in have:
                    found.append((ts, summ))
                    have.add(ts)
        for ts, summ in found[-30:]:
            add_msg(st, ts, "compact", summ.strip())
            if not any(ci.get("ts") == ts for ci in st.get("compact_info") or []):
                push(st.setdefault("compact_info", []), {"ts": ts, "pre": None, "win": st.get("ctx"), "chars": len(summ)}, 50)
    except Exception:  # noqa
        pass


def text_of(content):
    if isinstance(content, str):
        return content
    out = []
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict):
                if c.get("type") in ("input_text", "output_text", "text"):
                    out.append(c.get("text") or "")
            elif isinstance(c, str):
                out.append(c)
    return "\n".join(out)


def injected(text):
    t = text.lstrip()
    return (t.startswith("<") or "AGENTS.md instructions" in t[:300] or t.startswith("# AGENTS.md")
            or t.startswith("Caveat:") or "<command-name>" in t[:200] or "<local-command" in t[:200]
            or t.startswith("[Request interrupted"))


def h(s):
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:12]


def add_user(st, ts, text, typed):
    if not text or not text.strip():
        return
    if injected(text):
        return
    key = h(re.sub(r"\s+", " ", text.strip())[:2000])
    if key in st["seen_msg"]:
        if typed:
            for u in st["user"]:
                if u["id"] == key:
                    u["typed"] = True
        return
    push(st["seen_msg"], key, 3000)
    push(st["user"], {"id": key, "ts": ts, "text": trunc(text.strip(), 3000), "typed": bool(typed)}, 400)
    add_msg(st, ts, "user", text.strip())


# the agent's answers about dashboard items ("I3fa21: intended - why"), taken from the whole reply before it is shortened
ANSWER_LINE = re.compile(r"(?im)^\W{0,6}(I[0-9a-f]{5})\W{0,3}\s*[:\-\u2013\u2014]?\s*\**\s*(fixed|intended|explained|working|need[_ ]decision|won'?t fix|wontfix)\b"
                         r"\**\s*[:\-\u2013\u2014.,]?\s*(.*)$")


def add_agent(st, ts, text):
    if not text or not text.strip():
        return
    push(st["agent"], {"ts": ts, "text": trunc(text.strip(), 1800)}, 30)
    for m in ANSWER_LINE.finditer(text):
        push(st.setdefault("item_answers", []), {"ts": ts, "id": m.group(1), "status": m.group(2), "note": trunc(m.group(3).strip(), 400)}, 120)
    add_msg(st, ts, "agent", text.strip())
    scan_shortcuts(st, ts, "agent message", text)
    for m in CLAIM_RE.finditer(text):
        ctx = text[max(0, m.start() - 120): m.end() + 40]
        if re.search(r"(?i)rmse|mae|error|accura|deviation", ctx):
            v = float(m.group(1)) * (1e-3 if m.group(2).lower() == "mev" else 1.0)
            push(st.setdefault("claims", []), {"ts": ts, "metric": "energy_%s_atom" % ("mae" if re.search(r"(?i)\bmae\b", ctx) else "rmse"),
                                               "value": v, "context": re.sub(r"\s+", " ", ctx)[:240]}, 60)
    if re.search(r"(?i)((goal|criteri\w*|target|threshold|requirement)s?\b.{0,40}\b(met|achieved|reached|satisfied|fulfilled|complete[d]?)\b|"
                 r"\b(meets|satisf(y|ies)|below) the (target|threshold|criteri\w*)|goal (is )?(complete|done|accomplished))", text):
        push(st.setdefault("done_claims", []), {"ts": ts, "text": trunc(re.sub(r"\s+", " ", text), 300)}, 20)


def scan_shortcuts(st, ts, where, text):
    if not text or (st["last_ts"] and iso_to_epoch(ts) and iso_to_epoch(ts) < CUTOFF):
        return
    for m in SHORTCUT_RE.finditer(text):
        a, b = max(0, m.start() - 90), min(len(text), m.end() + 90)
        push(st["shortcuts"], {"ts": ts, "where": where, "match": m.group(0),
                               "context": re.sub(r"\s+", " ", text[a:b])}, 60)
        break


CLASSIFY = [
    ("submit", re.compile(r"(^|[\s;&|('\"])(sbatch|qsub)\b")),
    ("cancel", re.compile(r"(^|[\s;&|('\"])(scancel|qdel)\b")),
    ("alloc", re.compile(r"(^|[\s;&|('\"])(srun|salloc)\b")),
    ("delete", re.compile(r"(^|[\s;&|('\"])(rm\s+-[a-zA-Z]*[rf]|rm\s|rmdir\s|find\s.*-delete)")),
    ("git", re.compile(r"(^|[\s;&|('\"])git\s+(commit|push|reset|checkout|rebase|merge|stash|clean)")),
    ("edit", re.compile(r"(\*\*\* Begin Patch|sed\s+-i|perl\s+-[a-z]*i|>\s*\S*(INCAR|KPOINTS|POSCAR|\.in|\.yaml|\.toml|\.json)\b)")),
    ("evaluate", re.compile(r"\bdp\s+(?:--(?:pt|tf|jax|pd)\s+)?test\b|\bmace_eval|\bnequip-evaluate|\ballegro-evaluate")),
    ("train", re.compile(r"\bdp\s+(?:--(?:pt|tf|jax|pd)\s+)?(train|freeze|compress)\b|\bmace_run_train|\bnequip-train")),
    ("monitor", re.compile(r"(^|[\s;&|('\"])(squeue|sacct|sinfo|seff|nvidia-smi|sstat|tail|cat|grep|ls|head|less|du|df)\b")),
    ("ssh", re.compile(r"(^|[\s;&|(])(ssh|scp|rsync|sftp)\s")),
]


def classify(name, cmd):
    n = (name or "").lower()
    if "plan" in n or n == "todowrite":
        return "plan"
    if "goal" in n:
        return "goal"
    if n in ("apply_patch", "edit", "multiedit", "write", "notebookedit", "str_replace_editor"):
        return "edit"
    if any(k in n for k in ("spawn", "agent", "task", "send_input", "subagent")) and n not in ("bash",):
        return "agent"
    for kind, rx in CLASSIFY:
        if rx.search(cmd or ""):
            return kind
    return "other"


def parse_patch(text):
    files, lines, cur = [], [], None
    n_add = n_del = 0
    for ln in text.splitlines():
        m = re.match(r"\*\*\* (Update|Add|Delete) File: (.*)", ln) or re.match(r"\+\+\+ b/(.*)", ln)
        if m:
            cur = m.group(m.lastindex).strip()
            if cur not in files:
                files.append(cur)
            continue
        if ln.startswith("+++") or ln.startswith("---") or ln.startswith("***"):
            continue
        if ln.startswith("+") or ln.startswith("-"):
            if ln.startswith("+"):
                n_add += 1
            else:
                n_del += 1
            body = ln[1:]
            keep = (WATCH_RE and WATCH_RE.search(body)) or SHORTCUT_RE.search(body)
            if keep or len(lines) < 24:
                lines.append([ln[0], cur or "", trunc(body, 300), bool(keep)])
    return {"files": files[:30], "add": n_add, "del": n_del, "lines": lines[:160]}


def handle_tool(st, ts, name, args, call_id):
    st["calls"] += 1
    a = args
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except Exception:  # noqa
            a = {"raw": a}
    cmd = ""
    if isinstance(a, dict):
        c = a.get("command") or a.get("cmd") or a.get("raw") or a.get("input") or a.get("patch")
        if isinstance(c, list):
            if len(c) > 2 and os.path.basename(str(c[0])) in ("bash", "sh", "zsh") and c[1] in ("-lc", "-c"):
                c = c[2:]
            cmd = " ".join(str(x) for x in c)
        elif isinstance(c, str):
            cmd = c
        elif name and name.lower() in ("edit", "multiedit", "write"):
            fp = a.get("file_path", "")
            old = a.get("old_string", "")
            new = a.get("new_string", a.get("content", ""))
            if isinstance(a.get("edits"), list):
                old = "\n".join(str(e.get("old_string", "")) for e in a["edits"] if isinstance(e, dict))
                new = "\n".join(str(e.get("new_string", "")) for e in a["edits"] if isinstance(e, dict))
            cmd = "*** Update File: %s\n%s\n%s" % (fp, "\n".join("-" + x for x in str(old).splitlines()),
                                                    "\n".join("+" + x for x in str(new).splitlines()))
        else:
            cmd = json.dumps(a)[:3000]
    kind = classify(name, cmd)
    ev = {"ts": ts, "tool": name, "kind": kind, "cmd": trunc(cmd, 700), "id": call_id}
    if kind == "plan" and isinstance(a, dict) and isinstance(a.get("plan") or a.get("todos"), list):
        pl = [x for x in (a.get("plan") or a.get("todos")) if isinstance(x, dict)]
        done = sum(1 for x in pl if str(x.get("status", "")).lower() in ("completed", "done"))
        now_s = next((x.get("step") or x.get("content") or "" for x in pl if "progress" in str(x.get("status", "")).lower()), "")
        ev["cmd"] = trunc("plan updated: %d/%d done%s%s" % (done, len(pl), (" \u00b7 now: " + now_s) if now_s else "",
                                                          (" \u00b7 " + a.get("explanation")) if a.get("explanation") else ""), 500)
    elif "*** Begin Patch" in (cmd or "") or (name or "").lower() in ("apply_patch", "edit", "multiedit", "write"):
        pp = parse_patch(cmd or "")
        olds, news = {}, {}
        for sign, fpath, body, keep in pp["lines"]:
            m = WATCH_RE.search(body) if WATCH_RE else None
            if m:
                mv = re.search(r"[=:]\s*([^\s,;#!]+)", body[m.end():] if body[m.end():].strip()[:1] in "=:" else "")
                if mv:
                    (olds if sign == "-" else news)[m.group(1).upper()] = mv.group(1)
        ch = ["%s %s\u2192%s" % (k, olds.get(k, "new"), v) for k, v in news.items() if olds.get(k, "").lower() != v.lower()]
        ev["cmd"] = trunc("edited %s (+%d/-%d)%s" % (", ".join(pp["files"][:4]) or "files", pp["add"], pp["del"],
                                                   (" \u00b7 " + ", ".join(ch)) if ch else ""), 500)
    if WATCH_RE and kind != "plan" and WATCH_RE.search(cmd or ""):
        ev["watch"] = sorted(set(m.group(1).upper() for m in WATCH_RE.finditer(cmd)))[:8]
    push(st["tools"], ev, 120)
    # every command, compactly (time, kind, a short hash of the command, exit code later): for repeats, loops and failure rates
    e = iso_to_epoch(ts)
    if e:
        push(st.setdefault("cmdlog", []), [int(e), kind, hashlib.sha1(re.sub(r"\s+", " ", cmd or name or "").strip()[:2000].encode("utf-8", "replace")).hexdigest()[:6],
                                           None, call_id], 400)
    if kind == "plan" and isinstance(a, dict):
        plan = a.get("plan") or a.get("todos")
        if isinstance(plan, list):
            st["plan"] = [{"step": trunc(p.get("step") or p.get("content") or "", 240), "status": p.get("status", "")}
                          for p in plan if isinstance(p, dict)][:60]
            st["plan_expl"] = trunc(a.get("explanation") or "", 600)
            st["plan_ts"] = ts
    if kind == "goal":
        push(st["goal_events"], {"ts": ts, "name": name, "args": trunc(json.dumps(a), 800)}, 40)
    if "*** Begin Patch" in (cmd or "") or (name or "").lower() in ("apply_patch", "edit", "multiedit", "write"):
        p = parse_patch(cmd or "")
        p["ts"] = ts
        push(st["patches"], p, 120)
    if kind in NOTABLE_KINDS or ev.get("watch"):
        push(st["notable"], dict(ev), 500)
    if kind in ("edit", "other", "submit"):
        scan_shortcuts(st, ts, "command/patch", cmd)


def handle_output(st, call_id, output, is_error=False):
    if not call_id:
        return
    text = output_text(output) or ""
    raw = output if isinstance(output, str) else ""
    code = None
    m = re.search(r'"exit_code"\s*:\s*(-?\d+)', raw or text) or re.search(r"(?:Exit code|exited with code|exit status)[:\s]+(-?\d+)", text)
    if m:
        code = int(m.group(1))
    elif is_error:
        code = 1
    job = None
    mj = re.search(r"Submitted batch job (\d+)", text)
    if mj:
        job = mj.group(1)
    found = None
    for lst in (st["notable"], st["tools"]):
        for ev in reversed(lst[-80:]):
            if ev.get("id") == call_id:
                found = found or ev
                if code is not None:
                    ev["exit"] = code
                if job:
                    ev["jobid"] = job
                if code not in (None, 0):
                    ev["err"] = trunc(text[-300:], 300)
                break
    if code not in (None, 0):
        st["errors"] += 1
    if code is not None:
        for c in reversed((st.get("cmdlog") or [])[-60:]):
            if len(c) > 4 and c[4] == call_id:
                c[3] = code
                break
    res = deepmd_parse(text) if len(text) < 2000000 else None
    if res:
        cmd = (found or {}).get("cmd", "")
        res.update({"ts": (found or {}).get("ts"), "src": "tool output", "cmd": trunc(cmd, 400), "test_args": dp_test_args(cmd)})
        push(st.setdefault("metrics", []), res, 200)


def bump(st, key):
    st["counts"][key] = st["counts"].get(key, 0) + 1


def handle_codex(st, o):
    ts = o.get("timestamp")
    t = o.get("type")
    p = o.get("payload") if isinstance(o.get("payload"), dict) else {}
    if t is None and isinstance(o.get("item"), dict):  # very old format
        it = o["item"]
        t, p = "response_item", it
        if it.get("type") == "command_execution":
            handle_tool(st, ts, "shell", {"command": it.get("command", "")}, it.get("id"))
            return
    bump(st, t or "?")
    if ts:
        st["first_ts"] = st["first_ts"] or ts
        st["last_ts"] = ts
    if t == "session_meta":
        m = p.get("meta") if isinstance(p.get("meta"), dict) else p
        keep = {}
        for k, v in m.items():
            if k in ("instructions", "base_instructions", "user_instructions"):
                continue
            if isinstance(v, (str, int, float, bool)) or v is None:
                keep[k] = trunc(v, 300) if isinstance(v, str) else v
            elif isinstance(v, dict) and len(json.dumps(v)) < 1500:
                keep[k] = v
        if isinstance(p.get("git"), dict):
            keep["git"] = {k: trunc(v, 200) for k, v in p["git"].items() if isinstance(v, str)}
        st["meta"].update(keep)
    elif t == "turn_context":
        for k in ("model", "cwd", "approval_policy", "effort", "summary"):
            if p.get(k) is not None and isinstance(p.get(k), (str, int, float)):
                st["meta"]["ctx_" + k] = trunc(p.get(k), 300)
        sp = p.get("sandbox_policy")
        if sp is not None:
            st["meta"]["ctx_sandbox"] = trunc(json.dumps(sp), 200)
    elif t == "event_msg":
        pt = p.get("type") or ""
        if pt == "user_message":
            add_user(st, ts, p.get("message") or "", True)
        elif pt == "agent_message":
            add_agent(st, ts, p.get("message") or "")
        elif pt == "token_count":
            info = p.get("info") or {}
            if isinstance(info, dict):
                st["tokens"] = info.get("total_token_usage") or st["tokens"]
                st["ctx"] = info.get("model_context_window") or st["ctx"]
                lu = info.get("last_token_usage")
                if isinstance(lu, dict) and lu.get("total_tokens"):
                    st["ctx_used"] = int(lu.get("total_tokens") or 0)
            if p.get("rate_limits"):
                st["rate"] = p.get("rate_limits")
        elif "goal" in pt:
            push(st["goal_events"], {"ts": ts, "name": pt, "args": trunc(json.dumps(p), 800)}, 40)
        elif pt in ("error", "stream_error"):
            st["errors"] += 1
            push(st["notable"], {"ts": ts, "tool": "codex", "kind": "error", "cmd": trunc(p.get("message") or json.dumps(p), 400)}, 500)
        elif pt in ("context_compacted", "compaction"):
            note_compaction(st, ts)
        elif pt == "turn_aborted":
            push(st["notable"], {"ts": ts, "tool": "codex", "kind": "event", "cmd": pt}, 500)
    elif t == "compacted":
        summ = p.get("message") if isinstance(p.get("message"), str) else ""
        if not (summ or "").strip() and isinstance(p.get("replacement_history"), list):
            for it in reversed(p["replacement_history"]):   # newer Codex: the summary is the new history's last user turn
                if isinstance(it, dict) and it.get("type") == "message" and it.get("role") == "user":
                    summ = text_of(it.get("content"))
                    break
        note_compaction(st, ts, summ)
    elif t == "response_item":
        pt = p.get("type")
        if pt == "message":
            role = p.get("role")
            txt = text_of(p.get("content"))
            if role == "user":
                add_user(st, ts, txt, False)
            elif role == "assistant":
                add_agent(st, ts, txt)
        elif pt in ("function_call", "custom_tool_call", "local_shell_call"):
            name = p.get("name") or ("shell" if pt == "local_shell_call" else pt)
            args = p.get("arguments")
            if args is None:
                args = p.get("input")
            if args is None and isinstance(p.get("action"), dict):
                args = p.get("action")
            handle_tool(st, ts, name, args, p.get("call_id") or p.get("id"))
        elif pt in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
            handle_output(st, p.get("call_id"), p.get("output"))


def handle_claude(st, o):
    t = o.get("type")
    ts = o.get("timestamp")
    if t in ("summary", None):
        return
    bump(st, t)
    if ts:
        st["first_ts"] = st["first_ts"] or ts
        st["last_ts"] = ts
    for k in ("sessionId", "cwd", "version", "gitBranch"):
        if o.get(k) and k not in st["meta"]:
            st["meta"][k] = trunc(o.get(k), 300)
    if o.get("isSidechain"):
        st["meta"]["sidechain"] = True
    tmeta = o.get("thinkingMetadata")
    if isinstance(tmeta, dict):  # Claude Code records the thinking level the user chose for each turn
        st["meta"]["ctx_effort"] = "no thinking" if tmeta.get("disabled") else trunc(str(tmeta.get("level") or "thinking"), 40)
    msg = o.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else None
    if t == "system" and o.get("subtype") == "compact_boundary":
        meta = o.get("compactMetadata") or {}
        note_compaction(st, ts, None, meta.get("preTokens") if isinstance(meta, dict) else None)
        return
    if t == "user" and o.get("isCompactSummary"):
        note_compaction(st, ts, content if isinstance(content, str) else text_of(content))
        return
    if t == "user":
        if isinstance(content, str):
            add_user(st, ts, content, True)
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    add_user(st, ts, b.get("text") or "", True)
                elif b.get("type") == "tool_result":
                    handle_output(st, b.get("tool_use_id"), b.get("content"), bool(b.get("is_error")))
    elif t == "assistant" and isinstance(msg, dict):
        mid = msg.get("id")
        u = msg.get("usage")
        if isinstance(u, dict) and mid and mid not in st["usage_ids"]:
            push(st["usage_ids"], mid, 3000)
            tok = st["tokens"] or {}
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                tok[k] = tok.get(k, 0) + int(u.get(k) or 0)
            tok["total_tokens"] = sum(tok.get(k, 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            st["tokens"] = tok
        if msg.get("model"):
            st["meta"]["ctx_model"] = msg.get("model")
        if (isinstance(content, list) and any(isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking") for b in content)
                and not st["meta"].get("ctx_effort")):
            st["meta"]["ctx_effort"] = "thinking"
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    add_agent(st, ts, b.get("text") or "")
                elif b.get("type") == "tool_use":
                    handle_tool(st, ts, b.get("name") or "tool", b.get("input") or {}, b.get("id"))


def parse_file(st, handler):
    try:
        stt = os.stat(st["path"])
    except Exception:  # noqa
        return
    if st["ino"] != stt.st_ino or stt.st_size < st["offset"]:
        path, kind = st["path"], st["kind"]
        st.clear()
        st.update(new_state(path, kind))
        st["ino"] = stt.st_ino
    st["size"] = stt.st_size
    st["catching_up"] = False
    if stt.st_size == st["offset"]:
        return
    with open(st["path"], "rb") as f:
        f.seek(st["offset"])
        while True:
            if time.time() > PARSE_DEADLINE:  # huge first read: continue next cycle from the saved offset
                st["catching_up"] = True
                break
            data = f.read(16 * 1024 * 1024)
            if not data:
                break
            end = data.rfind(b"\n")
            if end < 0:
                if len(data) < 16 * 1024 * 1024:
                    break
                st["offset"] += len(data)  # pathological single line, skip it
                continue
            chunk = data[: end + 1]
            f.seek(st["offset"] + len(chunk))
            st["offset"] += len(chunk)
            for raw in chunk.split(b"\n"):
                if not raw.strip():
                    continue
                try:
                    o = json.loads(raw.decode("utf-8", "replace"))
                except Exception:  # noqa
                    continue
                if isinstance(o, dict):
                    try:
                        handler(st, o)
                    except Exception:  # noqa
                        bump(st, "parse_error")


# ---------------------------------------------------------------- other probes
def goal_state(codex_home, extra_dirs=()):
    out = []
    pats = [os.path.join(codex_home, "*.sqlite"), os.path.join(codex_home, "*.db"), os.path.join(codex_home, "sqlite", "*.sqlite")]
    for d in extra_dirs:
        pats += [os.path.join(d, "*.sqlite"), os.path.join(d, "*.db")]
    dbs = sorted(set(f for pat in pats for f in glob.glob(pat)))
    for db in dbs[:8]:
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=3)
            tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
            for tname in tables:
                tl = tname.lower()
                if "goal" not in tl and "thread" not in tl:
                    continue
                q = '"%s"' % tname.replace('"', '""')
                cols = [c[1] for c in con.execute("pragma table_info(%s)" % q)]
                lim = 30 if "goal" in tl else 15
                rows = con.execute("select * from %s order by rowid desc limit %d" % (q, lim)).fetchall()
                out.append({"db": os.path.basename(db), "table": tname, "columns": cols,
                            "rows": [[trunc(v, 1500) if isinstance(v, str) else (v if isinstance(v, (int, float)) or v is None else trunc(repr(v), 200)) for v in r] for r in rows]})
            con.close()
        except Exception as e:  # noqa
            out.append({"db": os.path.basename(db), "error": trunc(str(e), 200)})
    return out


PROBE_V = 5
SECRETISH = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|(?:key|token|secret|password)\s*=\s*\S+|[A-Za-z0-9_\-]{40,})", re.I)


def redact(t):
    return SECRETISH.sub("[redacted]", t or "")


def agent_probe(cache, codex_home):
    """What a new session can be started with on this host: how an interactive shell runs codex/claude (aliases
    included), their versions and options, Codex's model list with its reasoning levels, and the defaults set in
    their settings. Read-only; refreshed every 6 h. Only specific settings keys are read, never credentials."""
    pr = cache.get("probe") or {}
    if pr.get("v") == PROBE_V and time.time() - pr.get("ts", 0) < 6 * 3600:
        return pr
    home = os.path.expanduser("~")
    pr = {"v": PROBE_V, "ts": time.time(), "host": HOSTNAME, "home": home, "shell": os.environ.get("SHELL", "")}
    rc, o, e = run(["bash", "-ic", "type codex; type claude; type tmux; alias 2>/dev/null | grep -E 'codex|claude'; "
                    "echo \"MC_CODEX_HOME=${CODEX_HOME:-}\"; echo \"MC_CLAUDE_DIR=${CLAUDE_CONFIG_DIR:-}\""], timeout=25, inp="")
    pr["resolve"] = redact(trunc(o, 3000))
    mm = re.search(r"MC_CODEX_HOME=(.*)", o or "")
    pr["codex_home_env"] = os.path.expanduser(mm.group(1).strip()) if mm and mm.group(1).strip() else ""
    for tool in ("codex", "claude", "tmux"):
        rc, o, e = run(["bash", "-c", "command -v %s" % tool], timeout=10)
        path = o.strip().splitlines()[0] if rc == 0 and o.strip() else ""
        info = {"path": path}
        if path:
            rc, o, e = run([path, "-V" if tool == "tmux" else "--version"], timeout=20)
            info["version"] = trunc((o or e).strip(), 200)
            if tool != "tmux":
                rc, o, e = run([path, "--help"], timeout=25)
                info["help"] = trunc(o or e, 9000)
        if path and tool != "tmux":  # a wrapper script (e.g. one that picks which Codex home to use)
            try:
                info["real"] = os.path.realpath(path)
                info["size"] = os.path.getsize(path)
                if os.path.getsize(path) < 60000:
                    with open(path, "rb") as f:
                        head = f.read(60000)
                    if head.startswith(b"#!") and b"\0" not in head:
                        info["wrapper"] = redact(head.decode("utf-8", "replace"))[:16000]
            except OSError:
                pass
        pr[tool] = info
    for d in ("~/.local/bin", "~/bin", "~/.codex-common/bin"):
        try:
            pr.setdefault("bins", {})[d] = sorted(x for x in os.listdir(os.path.expanduser(d)) if re.search(r"codex|claude", x, re.I))[:40]
        except OSError:
            pass
    # wrapper commands such as codex_chat / codex_api: which Codex home each one uses (only the relevant lines are kept)
    pr["wrappers"] = {}
    bindir = os.path.expanduser("~/.local/bin")
    for x in (pr.get("bins") or {}).get("~/.local/bin") or []:
        if not re.match(r"^codex[_-][A-Za-z0-9_-]+$", x):
            continue
        fp = os.path.join(bindir, x)
        try:
            if os.path.getsize(fp) > 60000:
                continue
            with open(fp, "rb") as f:
                txt = f.read(60000).decode("utf-8", "replace")
            if not txt.startswith("#!"):
                continue
            keep = [redact(l.strip())[:300] for l in txt.splitlines() if re.search(r"CODEX_HOME|exec|sqlite|codex", l) and not l.strip().startswith("#")]
            mm = re.search(r"CODEX_HOME=[\"']?([^\"'\s;]+)", txt)
            pr["wrappers"][x] = {"home": os.path.expanduser(mm.group(1).replace("$HOME", "~").replace("${HOME}", "~")) if mm else "",
                                 "lines": keep[:12]}
        except OSError:
            pass
    homes = []
    for h in [os.environ.get("CODEX_HOME"), codex_home, os.path.join(home, ".codex")] + sorted(glob.glob(os.path.join(home, ".codex*"))):
        if h and os.path.isdir(h) and h not in homes:
            homes.append(h)
    pr["codex_homes"] = []
    for h in homes[:6]:
        ent = {"dir": h}
        try:
            mc = json.load(open(os.path.join(h, "models_cache.json")))
            lst = mc.get("models") if isinstance(mc, dict) else mc
            models = []
            for m in lst or []:
                if not isinstance(m, dict):
                    continue
                lv = []
                for x in m.get("supported_reasoning_levels") or m.get("reasoning_levels") or []:
                    lv.append(x.get("effort") if isinstance(x, dict) else x)
                models.append({"slug": m.get("slug") or m.get("id"), "name": m.get("display_name") or m.get("slug"),
                               "levels": [x for x in lv if x], "default": m.get("default_reasoning_level"),
                               "visibility": m.get("visibility"), "desc": trunc(m.get("description") or "", 160)})
            ent["models"] = models
            ent["models_mtime"] = os.path.getmtime(os.path.join(h, "models_cache.json"))
        except Exception as ex:  # noqa
            ent["models_error"] = str(ex)[:200]
        try:
            txt = open(os.path.join(h, "config.toml")).read()
            conf = {}
            for k in ("model", "model_reasoning_effort", "profile", "approval_policy", "sandbox_mode"):
                mm = re.search(r"(?m)^\s*%s\s*=\s*\"?([^\"\n#]+)" % k, txt)
                if mm:
                    conf[k] = mm.group(1).strip()
            mm = re.search(r"(?ms)^\[features\][^\[]*?^\s*goals\s*=\s*(true|false)", txt)
            if mm:
                conf["goals"] = mm.group(1)
            ent["config"] = conf
        except Exception:  # noqa
            pass
        pr["codex_homes"].append(ent)
    try:
        st = json.load(open(os.path.join(home, ".claude", "settings.json")))
        pr["claude_settings"] = {k: st.get(k) for k in ("model", "effortLevel", "alwaysThinkingEnabled") if k in st}
        sl = st.get("statusLine")
        pr["claude_settings"]["statusLine"] = redact(json.dumps(sl))[:600] if sl else None
    except Exception:  # noqa
        pr["claude_settings"] = None
    cache["probe"] = pr
    return pr


def node_info():
    d = {"host": socket.gethostname(), "ncpu": os.cpu_count()}
    try:
        d["load"] = list(os.getloadavg())
    except Exception:  # noqa
        pass
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                mem[k] = int(v.strip().split()[0])
        d["mem_total_gb"] = round(mem.get("MemTotal", 0) / 1048576.0, 1)
        d["mem_avail_gb"] = round(mem.get("MemAvailable", 0) / 1048576.0, 1)
    except Exception:  # noqa
        pass
    rc, out, _ = run(["nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                      "--format=csv,noheader,nounits"], timeout=15)
    if rc == 0:
        gpus = []
        for line in out.strip().splitlines():
            f = [x.strip() for x in line.split(",")]
            if len(f) >= 6:
                try:
                    gpus.append({"i": f[0], "name": f[1], "util": float(f[2]), "mem_used": float(f[3]),
                                 "mem_total": float(f[4]), "temp": f[5]})
                except ValueError:
                    pass
        d["gpus"] = gpus
    user = os.environ.get("USER") or ""
    rc, out, _ = run(["ps", "-u", user or str(os.getuid()), "-o", "pid=,pcpu=,pmem=,etimes=,comm=,args=", "--sort=-pcpu"], timeout=10)
    top = []
    for line in out.strip().splitlines()[:12]:
        f = line.split(None, 5)
        if len(f) >= 5:
            top.append({"pid": f[0], "cpu": f[1], "mem": f[2], "etime": f[3], "comm": f[4], "args": trunc(f[5] if len(f) > 5 else "", 160)})
    d["top"] = top
    return d


def param_scan(roots, cache):
    scans = cache.setdefault("param_scans", {})
    ckey = "|".join(roots)
    last = scans.get(ckey)
    if last and T0 - last.get("t", 0) < SCAN_EVERY_MIN * 60 and last.get("watch") == WATCH and last.get("v") == 2:
        last["cached"] = True
        return last
    res = {"t": T0, "roots": roots, "watch": WATCH, "values": {}, "examples": {}, "files": 0, "dirs": 0, "truncated": False,
           "metric_files": [], "train_inputs": [], "codes": {}, "v": 2}
    if not roots:
        res["note"] = "no scan roots or no watched parameters"
        scans[ckey] = res
        return res
    cutoff = T0 - SCAN_HOURS * 3600
    names = set(PARAM_FILES)
    budget_end = time.time() + 25
    keys = set(WATCH)

    def code(name):
        res["codes"][name] = res["codes"].get(name, 0) + 1

    def note(k, v, path):
        res["values"].setdefault(k, {})
        res["values"][k][v] = res["values"][k].get(v, 0) + 1
        ex = res["examples"].setdefault(k, {}).setdefault(v, [])
        if len(ex) < 3:
            ex.append(path)

    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            res["dirs"] += 1
            dirnames[:] = [x for x in dirnames if not x.startswith(".") and x not in ("node_modules", "__pycache__")]
            if res["dirs"] > 40000 or time.time() > budget_end:
                res["truncated"] = True
                break
            for fn in filenames:
                low = fn.lower()
                if fn == "INCAR":
                    code("VASP")
                if fn == "POTCAR" or low.startswith("in.") or low.endswith((".lmp", ".lammps")):
                    fp = os.path.join(dirpath, fn)
                    try:
                        stt = os.stat(fp)
                        if stt.st_mtime < cutoff or stt.st_size > 12e6:
                            continue
                        with open(fp, "r", errors="replace") as f:
                            txt = f.read(12000000 if fn == "POTCAR" else 40000)
                    except Exception:  # noqa
                        continue
                    if fn == "POTCAR":   # pseudopotentials: one per element, e.g. "TITEL = PAW_PBE Hf_pv 06Sep2000"
                        for m in re.finditer(r"TITEL\s*=\s*(\S+)\s+(\S+)", txt):
                            el = re.split(r"[_.]", m.group(2))[0]
                            if "POT_" + el.upper() in keys or "POT_*" in keys:
                                note("POT_" + el.upper(), m.group(2).upper(), fp)
                            if "POT_SET" in keys:
                                note("POT_SET", m.group(1).upper(), fp)
                    elif re.search(r"^\s*pair_style\s", txt, re.M):   # a LAMMPS input
                        code("LAMMPS")
                        for k2, rx in (("LMP_PAIR_STYLE", r"^\s*pair_style\s+(\S+)"), ("LMP_UNITS", r"^\s*units\s+(\S+)"),
                                       ("LMP_TIMESTEP", r"^\s*timestep\s+(\S+)")):
                            m = re.search(rx, txt, re.M)
                            if m and k2 in keys:
                                v = m.group(1)
                                try:
                                    v = "%g" % float(v)
                                except ValueError:
                                    v = v.upper()[:40]
                                note(k2, v, fp)
                    continue
                if fn not in names:
                    fp = os.path.join(dirpath, fn)
                    try:
                        stt = os.stat(fp)
                    except Exception:  # noqa
                        continue
                    if stt.st_mtime < cutoff:
                        continue
                    if low == "lcurve.out" and len(res["metric_files"]) < 300:
                        r = lcurve_parse(fp)
                        if r:
                            r.update({"file": fp, "mtime": stt.st_mtime, "src": "training curve"})
                            res["metric_files"].append(r)
                    elif (any(k in low for k in ("test", "eval", "valid")) and low.endswith((".log", ".out", ".txt"))
                          and stt.st_size < 20e6 and len(res["metric_files"]) < 300):
                        try:
                            with open(fp, "rb") as f:
                                f.seek(max(0, stt.st_size - 65536))
                                txt = f.read().decode("utf-8", "replace")
                        except Exception:  # noqa
                            continue
                        r = deepmd_parse(txt)
                        if r:
                            r.update({"file": fp, "mtime": stt.st_mtime, "src": "test log"})
                            res["metric_files"].append(r)
                    elif low.endswith(".json") and ("input" in low or "train" in low) and stt.st_size < 2e6 and len(res["train_inputs"]) < 100:
                        try:
                            with open(fp) as f:
                                j = json.load(f)
                            tr = j.get("training", {}) if isinstance(j, dict) else {}
                            def systems(x):
                                v = (x or {}).get("systems") if isinstance(x, dict) else None
                                v = [v] if isinstance(v, str) else (v or [])
                                return [os.path.normpath(os.path.join(dirpath, str(p))) for p in v][:200]
                            t_s, v_s = systems(tr.get("training_data")), systems(tr.get("validation_data"))
                            if t_s:
                                res["train_inputs"].append({"file": fp, "mtime": stt.st_mtime, "train": t_s, "valid": v_s})
                            mo = j.get("model") if isinstance(j, dict) else None
                            if isinstance(mo, dict) and isinstance(mo.get("descriptor"), dict):   # a DeePMD training input
                                code("DeePMD")
                                de, lr, lo = mo["descriptor"], j.get("learning_rate") or {}, j.get("loss") or {}
                                fit = mo.get("fitting_net") or {}
                                for k2, v in (("DP_DESCRIPTOR", de.get("type")), ("DP_RCUT", de.get("rcut")), ("DP_RCUT_SMTH", de.get("rcut_smth")),
                                              ("DP_SEL", de.get("sel")), ("DP_NEURON", de.get("neuron")), ("DP_FIT_NEURON", fit.get("neuron")),
                                              ("DP_TYPE_MAP", mo.get("type_map")), ("DP_START_LR", lr.get("start_lr")), ("DP_STOP_LR", lr.get("stop_lr")),
                                              ("DP_NUMB_STEPS", tr.get("numb_steps")), ("DP_START_PREF_E", lo.get("start_pref_e")),
                                              ("DP_START_PREF_F", lo.get("start_pref_f")), ("DP_LIMIT_PREF_F", lo.get("limit_pref_f"))):
                                    if v is None or k2 not in keys:
                                        continue
                                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                                        v = "%g" % v
                                    elif isinstance(v, list):
                                        v = ",".join(str(x) for x in v)[:60].upper()
                                    else:
                                        v = str(v).upper()[:40]
                                    note(k2, v, fp)
                        except Exception:  # noqa
                            pass
                    continue
                if not WATCH:
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    if os.stat(fp).st_mtime < cutoff:
                        continue
                    with open(fp, "r", errors="replace") as f:
                        txt = f.read(20000)
                except Exception:  # noqa
                    continue
                res["files"] += 1
                if fn.upper().startswith("KPOINTS") and "KPOINTS" in keys:
                    ls = txt.splitlines()
                    if len(ls) >= 4 and ls[2].strip()[:1].lower() in ("g", "m"):
                        note("KPOINTS", "%s %s" % (ls[2].strip()[:1].upper(), " ".join(ls[3].split()[:3])), fp)
                    elif len(ls) >= 2:
                        note("KPOINTS", "auto/explicit (%s)" % ls[1].strip()[:12], fp)
                    continue
                for line in txt.splitlines():
                    line = re.split(r"[#!]", line, maxsplit=1)[0]
                    for part in line.split(";"):
                        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*,?\s*$", part)
                        if not m:
                            continue
                        k = m.group(1).upper()
                        if k not in keys:
                            continue
                        v = m.group(2).strip().strip("'\"")
                        try:
                            v = "%g" % float(v.lower().replace("d", "e"))
                        except ValueError:
                            v = v.upper()[:40]
                        note(k, v, fp)
    scans[ckey] = res
    if len(scans) > 30:
        for k in sorted(scans, key=lambda k: scans[k].get("t", 0))[:-30]:
            del scans[k]
    return res


def tail_file(path, nbytes=6000, nlines=25):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            sz = f.tell()
            f.seek(max(0, sz - nbytes))
            txt = f.read().decode("utf-8", "replace")
        return txt.splitlines()[-nlines:]
    except Exception:  # noqa
        return None


def git_info(cwd):
    if not cwd or not os.path.isdir(os.path.join(cwd, ".git")):
        return None
    rc, log, _ = run(["git", "-C", cwd, "log", "-8", "--pretty=format:%h|%cI|%s"], timeout=10)
    rc2, st, _ = run(["git", "-C", cwd, "status", "--porcelain"], timeout=10)
    return {"log": [trunc(x, 200) for x in log.strip().splitlines()],
            "dirty": len([x for x in st.splitlines() if x.strip()])}


# ---------------------------------------------------------------- main
def main():
    out = {"ok": True, "label": LABEL, "generated": T0, "cfg_error": CFG.get("_cfg_error"), "multi": True}
    procs = proc_table()
    wanted = CFG.get("tmux_sessions")
    if wanted is None:
        wanted = [SESSION] if SESSION else []
    auto = any(w in ("*", "auto") for w in wanted)
    explicit = [w for w in wanted if w not in ("*", "auto")]
    names = list(explicit)
    group_of, members = {}, {}
    if auto:
        rc, ls, _ = run(["tmux", "list-sessions", "-F", "#{session_name}|#{session_group}|#{session_activity}"])
        if rc == 0:
            for line in ls.splitlines():
                f = line.split("|")
                n = f[0].strip()
                if not n:
                    continue
                grp = (f[1].strip() if len(f) > 1 else "") or n
                act = int(f[2]) if len(f) > 2 and f[2].strip().isdigit() else 0
                group_of[n] = grp
                members.setdefault(grp, []).append((act, n))
            for grp, lst in members.items():
                lst.sort(reverse=True)   # most recently active member first
                n = lst[0][1]
                if n not in names:
                    names.append(n)
    tmux, fams, seen_panes = {}, {}, {}
    for n in names:
        tm, fam = tmux_info(n, procs)
        if n not in explicit and not (tm.get("codex_running") or tm.get("claude_running")):
            continue  # auto mode: only sessions that run an agent
        pk = tuple(sorted(p.get("pid") for p in tm.get("panes") or []))
        if pk and pk in seen_panes:
            continue  # the same windows under another session name (an old tmux without session groups)
        seen_panes[pk] = n
        grp = group_of.get(n, n)
        show = n if n in explicit else grp   # a group keeps one stable name however many terminals are attached
        tm["target"] = n
        tm["members"] = [x[1] for x in members.get(grp, [])] or [n]
        tm["session"] = show
        tmux[show], fams[show] = tm, fam
    all_fam = set()
    for fam in fams.values():
        all_fam |= fam
    # agents running outside tmux (screen, nohup, an IDE terminal...) get an entry of their own, so every live
    # Codex/Claude session of this user shows up without any configuration
    if auto:
        roots = []
        for pid, v in procs.items():
            if pid in all_fam or not agent_kind(v["cmd"]):
                continue
            q, nested = v["ppid"], False
            while q in procs and q > 1:
                if agent_kind(procs[q]["cmd"]):
                    nested = True
                    break
                q = procs[q]["ppid"]
            if not nested:
                roots.append(pid)
        used = set(tmux)
        for pid in sorted(roots):
            fam = descendants(procs, [pid])
            files = dict(open_session_files(fam))
            files.update(claude_transcripts(fam, procs))
            if not files:
                continue
            kind = agent_kind(procs[pid]["cmd"])
            try:
                cwd = os.readlink("/proc/%d/cwd" % pid)
            except Exception:  # noqa
                cwd = ""
            name = "%s in %s" % (kind, os.path.basename(cwd.rstrip("/")) or "~")
            if name in used:
                name = "%s (pid %d)" % (name, pid)
            used.add(name)
            tmux[name] = {"session": name, "found": True, "notmux": True, "pid": pid, "cwd": cwd, "panes": [],
                          "pane_tail": [], "family_pids": len(fam), "codex_running": kind == "codex",
                          "claude_running": kind == "claude", "agent_cmds": [trunc(procs[pid]["cmd"], 200)]}
            fams[name] = fam
            all_fam |= fam
    out["tmux"] = tmux
    open_by_goal = {}
    for n, fam in fams.items():
        for fp in list(open_session_files(fam)) + list(claude_transcripts(fam, procs)):
            open_by_goal.setdefault(fp, n)
    open_files = open_by_goal
    # other agent processes of this user (outside the watched tmux sessions)
    other_agent_pids = [p for p, v in procs.items() if re.search(r"codex|claude", v["cmd"], re.I) and p not in all_fam]
    other_open = open_session_files(other_agent_pids)
    out["host"] = HOSTNAME
    out["login"] = bool(CFG.get("login"))
    if CFG.get("report_masters"):
        out["ssh_masters"] = ssh_masters(procs)

    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        if not CFG.get("cache") and not os.path.exists(CACHE_PATH) and os.path.exists(LEGACY_CACHE) and CFG.get("label") == "node":
            os.rename(LEGACY_CACHE, CACHE_PATH)  # the shared-name cache from older versions: first compute node keeps it
        with open(CACHE_PATH) as f:
            cache = json.load(f)
    except Exception:  # noqa
        cache = {}
    if cache.get("version") != CACHE_VERSION:
        cache = {"version": CACHE_VERSION}   # rebuild once, so full conversations are captured from the beginning
    files_state = cache.get("files", {})
    cache.pop("param_scan", None)

    codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    cands = {}
    for fp in ([] if LIVE_ONLY else glob.glob(os.path.join(codex_home, "sessions", "*", "*", "*", "rollout-*.jsonl"))):
        try:
            if os.stat(fp).st_mtime >= CUTOFF:
                cands[fp] = "codex"
        except Exception:  # noqa
            pass
    if WITH_CLAUDE and not LIVE_ONLY:
        croot = os.path.expanduser("~/.claude/projects")
        for dirpath, dirnames, filenames in os.walk(croot):
            for fn in filenames:
                if fn.endswith(".jsonl"):
                    fp = os.path.join(dirpath, fn)
                    try:
                        if os.stat(fp).st_mtime >= CUTOFF:
                            cands[fp] = "claude"
                    except Exception:  # noqa
                        pass
    for fp in list(open_files) + list(other_open):
        cands[fp] = "claude" if "/.claude/" in fp else "codex"

    sessions = []
    new_files_state = {}
    for fp, kind in sorted(cands.items()):
        st = files_state.get(fp) or new_state(fp, kind)
        parse_file(st, handle_codex if kind == "codex" else handle_claude)
        if fp in open_files and not st.get("cbf"):
            backfill_compactions(st)
        new_files_state[fp] = st
        s = {k: v for k, v in st.items() if k not in ("seen_msg", "usage_ids", "offset", "ino", "msgs")}
        s["cmdlog"] = [c[:4] for c in st.get("cmdlog") or [] if c and c[0] >= T0 - 48 * 3600]   # without the call ids
        since = MSG_SINCE.get(fp) or {}
        floor = since.get("seq", 0) if since.get("epoch") == st.get("msg_epoch") else 0
        s["msgs_new"] = [m for m in st.get("msgs", []) if m["seq"] > floor]
        s["role"] = "goal" if fp in open_files else ("open" if fp in other_open else "recent")
        if s["role"] != "goal":
            s["msgs_new"] = []  # full conversations are only kept for goal sessions
        if fp in open_files:
            s["goal_session"] = open_files[fp]
        s["mtime"] = os.stat(fp).st_mtime if os.path.exists(fp) else None
        s["notable"] = s["notable"][-200:]
        s["tools"] = s["tools"][-30:]
        s["patches"] = s["patches"][-40:]
        sessions.append(s)
    cache["files"] = new_files_state
    # keep output small: all goal/open sessions plus the 25 most recently active others
    keep = [s for s in sessions if s["role"] != "recent"]
    rest = sorted([s for s in sessions if s["role"] == "recent"], key=lambda s: s.get("mtime") or 0, reverse=True)
    for s in rest[25:]:
        s["user"], s["agent"], s["notable"], s["tools"], s["patches"] = s["user"][-5:], [], [], [], []
    sessions = keep + rest

    goal_cwds = {}
    for n, tm in tmux.items():
        cw = []
        for s in sessions:
            c = s["meta"].get("cwd") or s["meta"].get("ctx_cwd")
            if s.get("goal_session") == n and c and c not in cw:
                cw.append(c)
        if not cw:
            for p in tm.get("panes") or []:
                if p.get("path") and p["path"] not in cw:
                    cw.append(p["path"])
        goal_cwds[n] = cw[:3]
    cwds = []
    for cw in goal_cwds.values():
        for c in cw:
            if c not in cwds:
                cwds.append(c)
    out["goal_cwds"] = goal_cwds
    out["sessions"] = sessions
    sq_dirs = set()
    for v in procs.values():  # e.g. codex -c sqlite_home="/var/tmp/codex-5816/chat/sqlite"
        for mm in re.finditer(r'sqlite_home=\\?"?([^"\s]+)', v["cmd"]):
            sq_dirs.add(os.path.expanduser(mm.group(1)))
        mm = re.search(r"CODEX_HOME=(\S+)", v["cmd"])
        if mm:
            sq_dirs.add(os.path.expanduser(mm.group(1)))
    out["goal_db"] = goal_state(codex_home, sorted(sq_dirs))
    out["node"] = node_info()
    try:  # Claude's usage limits, as reported to the status line of sessions mission control started
        cu = json.load(open(os.path.expanduser("~/.cache/mission-control/claude_usage.json")))
        if isinstance(cu, dict):
            out["claude_usage"] = {"t": cu.get("t"), "model": cu.get("model"), "rate_limits": cu.get("rate_limits")}
    except Exception:  # noqa
        pass
    if LABEL == "node" or CFG.get("probe"):
        try:
            out["probe"] = agent_probe(cache, codex_home)
        except Exception as e:  # noqa
            out["probe"] = {"error": str(e)[:300]}
    roots = SCAN_ROOTS or cwds[:8]
    out["params"] = {"by_root": {r: param_scan([r], cache) for r in roots}}
    dec = []
    for c in cwds[:8]:
        for name in ("DECISIONS.md", ".mc/decisions.jsonl", "decisions.jsonl"):
            t = tail_file(os.path.join(c, name))
            if t:
                dec.append({"file": os.path.join(c, name), "tail": [trunc(x, 400) for x in t]})
    out["decisions"] = dec
    specs, reqs = {}, {}
    for c in cwds[:8]:
        for name in ("START_GOAL.md", "START_TASK.md", "GOAL.md"):   # the goal's own written instructions
            fp = os.path.join(c, name)
            try:
                st = os.stat(fp)
                if st.st_size > 400000:
                    continue
                with open(fp, "r", errors="replace") as f:
                    specs.setdefault(c, {})[name] = {"text": f.read(80000), "mtime": st.st_mtime}
            except Exception:  # noqa
                continue
        fp = os.path.join(c, ".mc", "requests.jsonl")   # the agent asking you to change a contract rule
        for line in tail_file(fp, 40000, 60) or []:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):   # kept whole (lists stay lists), so the dashboard can lay the request out properly
                lim = {"id": 60, "param": 80, "value": 2000, "why": 2000, "rule": 300, "ts": 40, "scope": 600, "criterion": 2000, "criteria": 2000}
                reqs.setdefault(c, []).append({k: [trunc(re.sub(r"\s+", " ", str(x)).strip(), 700) for x in v[:24]] if isinstance(v, list) else
                                               trunc(str(v if v is not None else ""), n) for k, n in lim.items() for v in [r.get(k, "")]})
    out["specs"] = specs
    ans = {}
    for c in cwds[:8]:   # the agent's answers about dashboard items (I3fa21: fixed / intended / ...), as JSON lines
        for line in tail_file(os.path.join(c, ".mc", "answers.jsonl"), 40000, 80) or []:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("item"):
                ans.setdefault(c, []).append({k: trunc(str(r.get(k, "")), 600) for k in ("item", "status", "note", "ts", "param", "value")})
    out["answers"] = ans
    tops = []   # each goal's working folder once (a folder inside another one is counted with it)
    for c in sorted(set(cwds[:8]), key=len):
        if c and c != "/" and not any(c == t or c.startswith(t.rstrip("/") + "/") for t in tops) and c.rstrip("/") != os.path.expanduser("~"):
            tops.append(c)
    out["storage"] = storage_info(tops[:6])
    out["requests"] = reqs
    out["git"] = {c: git_info(c) for c in cwds[:8]}
    out["elapsed_s"] = round(time.time() - T0, 2)
    try:
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, CACHE_PATH)
    except Exception as e:  # noqa
        out["cache_error"] = str(e)
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa
        import traceback
        sys.stdout.write(json.dumps({"ok": False, "error": str(e), "trace": traceback.format_exc()[-2000:]}))

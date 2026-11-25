#!/usr/bin/env python3
# mission control: Claude Code status line for sessions started from the dashboard. It records Claude's usage
# limits (5-hour and weekly) for the dashboard, then shows your own status line unchanged.
import json
import os
import subprocess
import sys
import time

raw = sys.stdin.read()
try:
    d = json.loads(raw)
except ValueError:
    d = {}
rl = d.get("rate_limits")
if rl:
    p = os.path.expanduser("~/.cache/mission-control/claude_usage.json")
    try:
        tmp = "%s.%d" % (p, os.getpid())
        with open(tmp, "w") as f:
            json.dump({"t": time.time(), "model": (d.get("model") or {}).get("display_name"), "rate_limits": rl}, f)
        os.replace(tmp, p)
    except OSError:
        pass
cmd = None
try:
    cfg = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "settings.json")
    with open(cfg) as f:
        cmd = (json.load(f).get("statusLine") or {}).get("command")
except (OSError, ValueError, AttributeError):
    pass
if cmd and "mission-control/claude_statusline" not in cmd:
    try:
        r = subprocess.run(cmd, shell=True, input=raw, stdout=subprocess.PIPE, universal_newlines=True, timeout=10)
        sys.stdout.write(r.stdout)
        sys.exit(0)
    except Exception:  # noqa
        pass
parts = [(d.get("model") or {}).get("display_name") or "Claude"]
for k, lab in (("five_hour", "5 h"), ("seven_day", "week")):
    w = (rl or {}).get(k) or {}
    if w.get("used_percentage") is not None:
        parts.append("%s %d%% used" % (lab, round(w["used_percentage"])))
print(" · ".join(parts))

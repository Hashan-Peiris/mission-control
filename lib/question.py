"""Find a question or approval prompt on an agent's screen (Codex, Claude Code or a plain program in a shell).

Used by the dashboard (mc.py) and sent along with the scripts that type into a session, so it runs on the
compute node too (Python 3.6 or newer, standard library only). Only the bottom of the screen counts, and the
agent's own messages are skipped: "the approved cutoff" in something the agent wrote is not a prompt.

On a node:  python3 -c "$QPY" check < screen          exit 3 and print the question as JSON, or exit 0
            python3 -c "$QPY" keys CHOICE SIG < screen  print the tmux keys that pick CHOICE (exit 2 if the
                                                        question on the screen is no longer the one with SIG)
"""
import hashlib
import json
import re
import sys

MARKS = u"›❯>▶➤▸"           # selection arrows: › ❯ > ▶ ➤ ▸
OPT = re.compile(u"^\\s*([%s]\\s*)?(\\d{1,2})[.)]\\s+(\\S.*?)\\s*$" % MARKS)
ASK = re.compile(r"(\bAllow (this|command|edit|Codex|Claude)\b|\bApprove\b|\bDo you want to\b|\bWould you like to\b|"
                 r"\[y/N\]|\[Y/n\]|\(y/n\)|\(yes/no\)|\bPress enter to (continue|confirm)\b|\bRun this command\?|"
                 r"\bProceed\?|\bContinue\?|\bAre you sure\b|\bwaiting for (your )?(input|approval)\b)", re.I)
YN = re.compile(r"\[y/N\]|\[Y/n\]|\(y/n\)|\(yes/no\)", re.I)
PROSE = re.compile(u"^\\s*([•⏺◦●∙]|[%s])\\s" % MARKS)   # • ⏺ ◦ ● ∙ start an agent or user message
BOX = re.compile(u"^\\s*[│┃|]\\s?|\\s?[│┃|]\\s*$")         # │ borders of a dialog box
RULE = re.compile(u"^[\\s─━╭╮╰╯┄╌═-]*$")


def _clean(lines):
    out = []
    for l in lines:
        l = BOX.sub("", l.rstrip("\n").replace("\t", "    ")).rstrip()
        out.append("" if RULE.match(l) else l)
    return out


def _kept(lines):
    """The screen without the agent's messages and your earlier messages (each a block that runs to a blank line)."""
    keep, block = [], False
    for l in lines:
        if not l.strip():
            block = False
            keep.append(l)
        elif PROSE.match(l) and not OPT.match(l):
            block = True
            keep.append("")
        elif block and l[:1].isspace():
            keep.append("")
        else:
            block = False
            keep.append("" if "esc to interrupt" in l else l)
    return keep


def find(lines, bottom=22):
    """None, or {"kind": menu|yn|enter|text, "text", "context": [...], "options": [{"n", "label"}], "sel", "sig"}."""
    lines = _clean([l for l in lines if l is not None])
    while lines and not lines[-1].strip():
        lines.pop()
    lines = lines[-bottom:]
    kept = _kept(lines)
    # A numbered menu (approval prompts in Codex and Claude Code): options 1, 2, ... one under the other.
    i = len(kept) - 1
    while i >= 0:
        m = OPT.match(kept[i])
        if m and m.group(2) != "1":
            j, opts = i, []
            while j >= 0 and OPT.match(kept[j]):
                mm = OPT.match(kept[j])
                opts.insert(0, (int(mm.group(2)), mm.group(3), bool(mm.group(1)), j))
                j -= 1
            if [o[0] for o in opts] == list(range(1, len(opts) + 1)) and len(opts) >= 2:
                above = [k for k in kept[max(0, j - 8):j + 1] if k.strip()]
                q = next((k.strip() for k in reversed(above) if ASK.search(k) or k.rstrip().endswith("?")), "")
                if q or any(ASK.search(o[1]) or o[1].lower().startswith(("yes", "no")) for o in opts):
                    sel = next((o[0] for o in opts if o[2]), 1)
                    return _done("menu", q or "The agent is asking you to choose", [k.strip() for k in above if k.strip() != q][-4:],
                                 [{"n": o[0], "label": o[1]} for o in opts], sel)
            i = j
        i -= 1
    # A question in plain text near the bottom: [y/N], "Press enter to continue", "Do you want to ...".
    for k in reversed(kept[-10:]):
        if k.strip() and ASK.search(k):
            q = k.strip()
            if YN.search(q):
                return _done("yn", q, [], [{"n": "y", "label": "Yes"}, {"n": "n", "label": "No"}], None)
            if re.search(r"Press enter to (continue|confirm)", q, re.I):
                return _done("enter", q, [], [{"n": "enter", "label": "Press Enter"}], None)
            return _done("text", q, [], [], None)
    return None


def _done(kind, text, context, options, sel):
    sig = hashlib.sha1(json.dumps([kind, text, [o["label"] for o in options]]).encode("utf-8")).hexdigest()[:10]
    return {"kind": kind, "text": text[:300], "context": [c[:200] for c in context], "options": options, "sel": sel, "sig": sig}


def keys_for(q, choice):
    """The tmux key names that pick `choice`: arrows to the option, then Enter (works in both agents' menus)."""
    if q["kind"] == "menu":
        n = int(choice)
        if not any(o["n"] == n for o in q["options"]):
            return None
        d = n - (q["sel"] or 1)
        return ["Down"] * max(d, 0) + ["Up"] * max(-d, 0) + ["Enter"]
    if q["kind"] == "yn" and choice in ("y", "n"):
        return [choice, "Enter"]
    if q["kind"] == "enter" and choice == "enter":
        return ["Enter"]
    return None


def main(argv):
    data = sys.stdin.buffer.read().decode("utf-8", "replace") if hasattr(sys.stdin, "buffer") else sys.stdin.read()
    q = find(data.splitlines())
    if argv[:1] == ["check"]:
        if q:
            print(json.dumps(q))
            return 3
        return 0
    if argv[:1] == ["keys"] and len(argv) == 3:
        if not q or q["sig"] != argv[2]:
            print("The question on the screen has changed or is gone; nothing was pressed.", file=sys.stderr)
            return 2
        k = keys_for(q, argv[1])
        if not k:
            print("That isn't one of the answers on the screen.", file=sys.stderr)
            return 2
        print(" ".join(k))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

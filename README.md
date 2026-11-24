# Mission control

A dashboard and chat window for long-running Codex and Claude goal sessions on HPC clusters. It shows
what the agents are doing, whether they're sticking to the science, and whether compute is being
wasted, without you reading any reasoning traces. You can also message an agent from the page or
from your phone.

It was built for one researcher's setup and generalises from there:
- Windows PCs with WSL2 Ubuntu, sharing one folder through OneDrive (any synced folder works; a single
  machine works without one)
- Slurm clusters reached over SSH, where a script of yours opens long-lived shared connections
  (ControlMaster) to the login nodes and compute nodes. Rockfish and Skipjack are the reference
  clusters; others work through the same settings.
- agents running in tmux (or not) on compute nodes, login nodes or the PC itself

Mission control never logs in by itself. It only rides connections that already exist, so it can't
lock you out of a cluster with failed logins and never needs your credentials.

## Start it (once per machine)

Put this folder somewhere every machine can see (for example inside OneDrive), then in WSL Ubuntu:

```bash
bash /path/to/mission-control/mctl setup
```

On a machine where the folder is somewhere in OneDrive, this finds it by itself:

```bash
bash "$(ls -d /mnt/*/Users/*/OneDrive*{,/*,/*/*,/*/*/*}/mission-control 2>/dev/null | head -1)/mctl" setup
```

The first run creates `config.env` from `config.example.env`. Fill in at least:
- `CONNECT_CMD`: the alias you run to open your shared SSH connections (used in "run ..." hints)
- `NEVER_CONTACT`: hosts mission control must never touch, if any (matched anywhere in an alias, host
  name or socket name)
- `PRIMARY_HOST`: the always-on PC that collects
- `SJ_TARGET` and `NODES` if you use a second cluster or fixed compute nodes

Setup finds your cluster SSH aliases, checks each connection, installs the `mctl` command, starts
the background collector and opens the dashboard in your browser.

From then on, on every machine, just type **`mctl`**. It starts the collector if needed and opens
the dashboard. `mctl help` lists the other commands.

Only one machine collects at a time: `PRIMARY_HOST`. The others stand by and show the same dashboard
through the shared folder. A standby machine takes over by itself if the primary stops for about half
an hour, but only if it has shared connections of its own. (It judges this by watching the primary's
heartbeat file change, so a laptop with a drifting clock or a slow OneDrive doesn't jump in.)

After that you don't need to do anything else:
- The collector refreshes every 5 minutes. It restarts by itself whenever you open a WSL shell.
- Open pages keep themselves current. Opened with `mctl` (or on your phone), a page updates in place
  within about 15 s of new data, keeping your scroll position, open blocks and any message you're typing.
  Opened straight from the shared folder, it reloads every 2 minutes. Click the "Updated ..." status to
  ask the collector for fresh data right away (at most once every 4 minutes).
- If a connection drops, the dashboard turns red and tells you to run your connection script. The next
  cycle picks the connection up again.
- `dashboard.html` sits in the shared folder, so it syncs to your other devices.

## Message an agent from the dashboard

Each session page has a box under "Agent's latest words". Type as you would in the agent's terminal
and press Enter (Shift+Enter for a new line). Mission control pastes it into that tmux pane and
presses Enter, then shows the agent's screen a few seconds later. It works for Codex and Claude.
- It works on the dashboard opened with `mctl` (http://localhost:8765). The same page opened straight
  from the shared folder is read-only.
- It goes through the machine's own shared connections, so a machine without them can't send.
- If the agent's screen shows an approval or y/n question, it holds the message (typing could answer
  the question) and offers "Send anyway".
- From WSL: `mctl send --goal NAME "please continue"`. Every message sent is logged in
  ~/.local/state/mission-control/sent.log on that machine.
- Safety: the page server listens only on this machine and every send needs a secret key that only its
  own pages carry, so no website or other program can type into your sessions. `WEB_SERVER=0` in
  config.env turns it off.

## On your phone

Phone access goes through Tailscale, a private network of only your own devices; nothing is opened to
the internet. One-time setup:
1. Install Tailscale for Windows on the collecting PC (https://tailscale.com/download) and sign in.
2. Install the Tailscale app on your phone and sign in with the same account.
3. In WSL on that PC, run `mctl phone`. It shares the dashboard with your tailnet and prints the
   address to open on your phone (https://<pc-name>.<your-tailnet>.ts.net/). If Tailscale asks you to
   enable HTTPS for your tailnet, follow its link once.

The phone gets the same pages, including chat with the agents. Only your own Tailscale login is let
in; anything else from outside is refused. `tailscale serve reset` on that PC stops sharing.

## Every running agent, no configuration

Every Codex or Claude session that is running right now shows up on its own. You don't edit any files
when you start a new tmux session.
- Where it looks: every tmux session on the nodes in `NODES`, the Rockfish and Skipjack login nodes,
  this PC's WSL, and any node your connection script keeps a shared connection to on the Rockfish login
  node. Agents running outside tmux (screen, nohup, an IDE terminal) are found too and named
  "codex in <folder>".
- `dashboard.html` is the overview: one card per session, grouped by host, with what the agent is doing
  now (working, sleeping until a check, waiting for you, silent) and its latest words.
- The bar at the top of every page lists all sessions with a status dot; one click switches between them.
- Each session page opens with the agent's latest words. Every agent message shows the model and
  reasoning level that wrote it. Click the goal or the folder in the header to see them in full.
  "Full conversation" shows the whole history, colour-coded: you, the agent, sub-agents, events and
  Codex's approval reviewer (hidden by default). Search and filters sit at the top.
- Each goal has its own contract, `contract/<tmux session name>.md`. Its goal text and rules are filled in
  from that goal on first sight.
- To watch only some sessions, set `GOALS="MY_GOAL OTHER"` instead of `auto`.
  `LOGIN_DIGESTS=0` and `AUTO_NODES=0` turn off the login-node and auto-node searches.
- If a node goes down, its goals stay on the overview as "node unreachable" until it's back.
- Hide anything you don't need: hover over a block and click its x (that browser only; the
  "Hidden here" bar at the top brings it back). `mctl hide` lists the block names, and
  `mctl hide screen watchdog` or `mctl hide "codex in my-folder"` hides them on every machine;
  `mctl unhide NAME` undoes it.

## Daily use

| Command | What it does |
|---|---|
| `mctl status` | One-screen text summary of all goals (`--goal NAME` for one) |
| `mctl goals` | List the goals mission control sees |
| `mctl open` | Open the dashboard |
| `mctl peek mlip` | Live tmux screen of a goal (read-only; any unique prefix of the session name works) |
| `mctl amend --goal mlip "force target applies to bcc too"` | Add a dated amendment to that goal's contract |
| `mctl promote U1a2b3c` | Make one of your TUI instructions binding (it knows which goal it came from) |
| `mctl referee [goal]` | Second-model review (all goals if none named) |
| `mctl doctor` | Re-check every connection |
| `mctl restart` | Restart the collector |

With a single goal running, you can leave out `--goal`.

## What it checks

- **Goal**: Is the agent alive in tmux? Is it waiting for your approval, quiet, or stalled?
  It also shows the agent's own plan, context compactions (a known cause of goal drift),
  quota and token budget.
- **Science**: Each goal's contract lists watched parameters. On the first run, settings stated in your
  goal (for example "ENCUT 520", "EDIFF 1e-6") become rules. Mission control then flags:
  - every agent edit that breaks a rule
  - every recent input file that breaks a rule
  - "shortcut" wording such as "to save time", "placeholder" or "loosened"

  An instruction you typed in the TUI that names the new value counts as permission.
- **Results**: Write targets in plain English under "Acceptance criteria" in the contract, for
  example `- DeePMD test energy RMSE below 20 meV/atom` or `- force RMSE under 100 meV/Å`. Targets
  stated in your goal are picked up automatically. Each target is checked against real `dp test`
  output, from the agent's own commands or from `*test*.log` files, with lcurve.out as a fallback.
  Mission control also flags:
  - an agent saying a target is met when the test output says it isn't
  - an agent reporting a better number than any test produced
  - testing on systems that are in the model's training data
  - tests on fewer than 50 frames

  For any other number, add `- custom: label | regex with one (number) group | <= value`.
  Targets that aren't numbers go to the referee.
- **Compute**: Running and pending jobs come from Slurm, not from what the agents say. It also flags:
  - share of idle or failed core-hours over 48 h
  - bursts of quick failures while they may still be happening (cleared once a later job runs fine),
    duplicate submissions and long-pending jobs
  - low CPU efficiency
  - idle GPUs that are holding memory
  - which agent submitted each job

## Optional extras (edit `config.env`)

- **Phone pushes**: set `NTFY_TOPIC` to a long random name and subscribe to it in the free ntfy
  app. You'll get new red/amber alerts plus a morning brief. Topics on ntfy.sh are public by name,
  so pick something unguessable.
- **Referee**: set `REFEREE_CMD="claude -p"` (or `codex exec -`) and `REFEREE_EVERY_H=6`. A second
  model then reviews the contract, your instructions and the evidence every 6 h. Its verdict appears
  on the dashboard.
- **Agent decision notes**: paste `AGENT_INSTRUCTIONS.md` into the TUI once. The agents' reasons then
  show up in the timeline.

## Safety

Mission control reads session logs, the tmux screen (`capture-pane`), `squeue`/`sacct`/`sinfo`
and node load. The one thing it ever types is a message you send from a session page (or with
`mctl send`), into that session's tmux pane. It never submits or cancels jobs, never logs in by itself
and never contacts a host listed in `NEVER_CONTACT`. Each cycle sends a few hundred KB.

## Developing

Work in a clone outside OneDrive (git and OneDrive syncing the same `.git` folder don't mix), then
deploy into the shared folder the machines run from:

```bash
git clone https://github.com/Hashan-Peiris/mission-control.git ~/src/mission-control
cd ~/src/mission-control
git config core.hooksPath tools/hooks   # once: blocks commits of collected data, settings or credentials
# ...edit...
tools/check.sh                          # syntax checks (also run by the hook and by deploy)
tools/deploy.sh                         # copies the code into the shared folder
git commit -am "what changed" && git push
```

`tools/deploy.sh` copies code and docs only: your settings, contracts and collected data in the
shared folder are never touched. Every machine picks up the new code within about 5 minutes.
See SECURITY.md for what must never be committed.

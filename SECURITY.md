# Security

This repository is public. It holds code, documentation and a settings example, nothing else.

## What never goes in it

- collected data: session logs, conversation transcripts, Slurm tables, dashboards (`state/`, `goals/`, `dashboard.html`)
- goal contracts (`contract/`), which describe your research
- your settings: `config.env` and the per-machine `config.<machine>.env` written by `mctl setup`. These
  hold your cluster hosts, node names, machine names and the hosts mission control must never contact.
  Only `config.example.env`, with empty values, is committed.
- the web server's secret key and message log, which live in `~/.local/state/mission-control` on each
  machine, outside both this repository and the shared folder

Three guards keep these out:
- `.gitignore` ignores them.
- `tools/hooks/pre-commit` refuses commits that include them, that look like they contain a credential,
  or that mention one of your private words.
- `tools/hooks/pre-push` checks every tracked file for your private words before anything is pushed.

Turn the hooks on once in every clone:

```bash
git config core.hooksPath tools/hooks
```

Private words are names you never want published (compute nodes, PCs, project names, user and
allocation names). List them one per line in `private-words` in the shared folder (every machine's clone
picks that up), or in `~/.config/mission-control/private-words` or `.git/info/private-words`.
`tools/private-check.sh` runs the same check by hand.

## What mission control may do

- It reads session logs, the tmux screen, Slurm tables and node load over SSH shared connections you
  already opened. It never logs in by itself (every ssh call uses `ProxyCommand=/bin/false` and
  `BatchMode=yes`, so a missing connection fails instead of connecting) and never contacts a host
  matching `NEVER_CONTACT`.
- It acts only when you press a button on one of its pages or run the matching `mctl` command: it types a
  message you send into a session's tmux pane (held when the agent's screen shows an approval or y/n
  prompt), presses Esc for Interrupt, starts a session you set up (a login shell in tmux on one of your
  configured compute nodes, then the agent command), or ends a session after you type its exact name.
  Every one of these is logged in `sent.log`.
- When starting a session it never answers the agent's own questions (folder trust, sign-in, updates):
  you answer them in the dialog.
- It never submits or cancels jobs.
- To report Claude's usage it gives sessions it starts a status line script
  (`~/.cache/mission-control/claude_statusline.py`) that saves the usage numbers and then runs your own
  status line. It never reads credential files.

## The web page server

`lib/server.py` serves the dashboard at `http://localhost:8765` so messages can be sent from it.
- It listens on 127.0.0.1 only.
- Every send needs a per-machine secret key that only pages served by that machine carry. Requests must
  come from its own pages (Host and Origin checked), which blocks other websites and DNS-rebinding tricks.
- Phone access goes through Tailscale Serve. Requests that arrive that way are accepted only from your
  own Tailscale login. Requests without a Tailscale identity (for example through Tailscale Funnel) are
  refused.
- Every message sent is logged on that machine in `~/.local/state/mission-control/sent.log`.
- `WEB_SERVER=0` in config.env turns the server off.

## Reporting a problem

Open an issue without details, or contact the repository owner, and the details can follow privately.

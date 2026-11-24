#!/usr/bin/env bash
# Copy the code from this clone into the shared OneDrive folder that every machine runs mission control from.
# Only code and docs are copied. Your settings (config.env, config.*.env), contracts and all collected data stay as they are.
# Every machine picks the new code up on its next 5-minute cycle.
#
#   tools/deploy.sh                      find the OneDrive mission-control folder by itself
#   tools/deploy.sh /path/to/mission-control
set -euo pipefail
src="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dest="${1:-}"
if [ -z "$dest" ]; then
  for c in /mnt/*/Users/*/OneDrive*{,/*,/*/*,/*/*/*}/mission-control /mnt/*/*/OneDrive*{,/*,/*/*}/mission-control; do
    [ -f "$c/mctl" ] && dest="$c" && break
  done
fi
if [ -z "$dest" ] || [ ! -f "$dest/mctl" ]; then
  echo "Can't find the mission-control folder. Usage: tools/deploy.sh /path/to/OneDrive/.../mission-control" >&2
  exit 1
fi
dest="$(cd "$dest" && pwd)"
[ "$dest" = "$src" ] && { echo "That is this clone itself; nothing to do." >&2; exit 1; }

bash "$src/tools/check.sh"
mkdir -p "$dest/lib"
put() {  # copy next to the target, then rename, so a running collector never reads a half-written file
  cp "$src/$1" "$dest/$1.deploy.$$" && mv -f "$dest/$1.deploy.$$" "$dest/$1"
}
for f in lib/*.py lib/*.sh lib/*.md README.md AGENT_INSTRUCTIONS.md SECURITY.md config.example.env; do put "$f"; done
put mctl   # last: a new mctl makes every collector restart itself with the new code
chmod +x "$dest/mctl" 2> /dev/null || true

if [ ! -f "$dest/config.env" ]; then
  cp "$dest/config.example.env" "$dest/config.env"
  echo "Created config.env from the example: fill in your hosts (NEVER_CONTACT first)."
else  # settings added since your config.env was made
  new=$(grep -oE '^[A-Z_]+=' "$src/config.example.env" | while read -r k; do grep -q "^$k" "$dest/config.env" || printf '%s ' "${k%=}"; done)
  [ -n "$new" ] && echo "Note: config.example.env has settings your config.env lacks: $new"
fi
echo "Deployed to $dest. Every machine picks it up within about 5 minutes."

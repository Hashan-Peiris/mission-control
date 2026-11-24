#!/bin/bash
# mission-control: runs on the Rockfish login node and reaches Skipjack through the shared
# connection your connection script opened there. Expects $MC_CLUSTER_SCRIPT (the read-only Slurm snapshot), or
# $MC_PAYLOAD_B64 plus $MC_SJ_REMOTE (e.g. the session digest: base64 script, "python3 - <config>").
T="${MC_SJ_TARGET:-}"   # SJ_TARGET in config.env
[ -n "$T" ] || { echo "Skipjack through Rockfish needs SJ_TARGET (the Skipjack login host name) in config.env." >&2; exit 255; }
TL=""; for l in ${T//./ }; do [ "$l" != login ] && { TL="$l"; break; }; done   # e.g. login.cluster.example.edu -> cluster
never() {  # MC_NEVER: hosts mission control must never touch
  local w s; s=$(printf '%s' "$1" | tr 'A-Z' 'a-z')
  for w in ${MC_NEVER:-}; do w=$(printf '%s' "$w" | tr 'A-Z' 'a-z'); case "$s" in *"$w"*) return 0 ;; esac; done
  return 1
}
OPTS=(-T -o BatchMode=yes -o ProxyCommand=/bin/false -o ConnectTimeout=15 -o ServerAliveInterval=15 -o LogLevel=ERROR)
[ -n "${MC_PAYLOAD_B64:-}" ] && MC_CLUSTER_SCRIPT=$(printf '%s' "$MC_PAYLOAD_B64" | base64 -d)
run_sj() { ssh "${OPTS[@]}" "$@" "${MC_SJ_REMOTE:-bash -s}" <<< "$MC_CLUSTER_SCRIPT"; }
for n in skipjack sj "$T"; do
  if timeout 8 ssh -O check -o BatchMode=yes -o ProxyCommand=/bin/false "$n" </dev/null >/dev/null 2>&1; then
    run_sj "$n"; exit $?
  fi
done
for s in $(find "$HOME/.ssh" /tmp "/run/user/$(id -u)" -maxdepth 3 -type s -user "$(id -u)" 2>/dev/null); do
  never "$(basename "$s")" && continue
  case "$(basename "$s" | tr 'A-Z' 'a-z')" in
    *skipjack*|*"${TL:-skipjack}"*|sj|sj[@_.:-]*|*[@_.:-]sj|*[@_.:-]sj[@_.:-]*) ;;
    *) continue ;;
  esac
  if timeout 8 ssh -O check -o ControlPath="$s" -o BatchMode=yes -o ProxyCommand=/bin/false "$T" </dev/null >/dev/null 2>&1; then
    run_sj -o ControlPath="$s" -o ControlMaster=no "$T"; exit $?
  fi
done
echo "No shared Skipjack connection on Rockfish login node $(hostname -s); ${MC_CONNECT:-your connection script} opens one (it may be on another login node)." >&2
exit 255

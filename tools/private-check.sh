#!/usr/bin/env bash
# Refuses text that mentions your private words: node and machine names, project names, user names,
# anything you don't want in a public repository. The words stay outside the repository, one per line
# (# starts a comment), in any of these files:
#   .git/info/private-words
#   ~/.config/mission-control/private-words
#   private-words in the shared OneDrive mission-control folder (so every machine's clone gets them)
#
#   tools/private-check.sh            check every tracked file (the pre-push hook runs this)
#   tools/private-check.sh --staged   check only what is about to be committed (the pre-commit hook)
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
words=$(for f in "$(git rev-parse --git-dir 2> /dev/null)/info/private-words" "$HOME/.config/mission-control/private-words" \
                 /mnt/*/Users/*/OneDrive*{,/*,/*/*,/*/*/*}/mission-control/private-words; do
          [ -f "$f" ] && grep -vE '^[[:space:]]*(#|$)' "$f" | sed 's/[[:space:]]*$//'
        done | sort -u)
[ -z "$words" ] && exit 0
if [ "${1:-}" = --staged ]; then
  text=$(git diff --cached -U0 --diff-filter=ACMR | grep -E '^\+' | grep -v '^+++'; git diff --cached --name-only --diff-filter=ACMR)
else
  text=$(git ls-files; git ls-files -z | xargs -0 cat 2> /dev/null)
fi
hits=$(printf '%s\n' "$text" | grep -oiwF -f <(printf '%s\n' "$words") | sort -fu | tr '\n' ' ')
if [ -n "$hits" ]; then
  echo "private-check: found private words: $hits" >&2
  echo "  (from your private-words file). Take them out, or move them into config.env." >&2
  exit 1
fi
exit 0

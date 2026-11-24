#!/usr/bin/env bash
# Quick checks before committing or deploying: syntax of every script, Python 3.6 compatibility of the
# code that runs on the cluster nodes, and pyflakes when it is installed.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
bash -n mctl lib/*.sh tools/*.sh tools/hooks/pre-commit tools/hooks/pre-push
python3 - lib/*.py <<'EOF'
import ast, sys
for f in sys.argv[1:]:
    src = open(f, encoding="utf-8").read()
    compile(src, f, "exec")
# remote_digest.py runs on the cluster nodes, which have Python 3.6
ast.parse(open("lib/remote_digest.py", encoding="utf-8").read(), feature_version=(3, 6))
EOF
if python3 -m pyflakes --version > /dev/null 2>&1; then
  python3 -m pyflakes lib/*.py
fi
echo "checks passed"

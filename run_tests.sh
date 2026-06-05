#!/usr/bin/env bash
#
# One command to run the full Fluent test suite (backend + frontend/jsdom).
#
#   ./run_tests.sh
#
# The frontend (jsdom) tests need Node. If `node` is on PATH we install jsdom
# into tests/frontend the first time; otherwise those tests skip themselves and
# the backend tests still run.
set -euo pipefail
cd "$(dirname "$0")"

if command -v node >/dev/null 2>&1; then
  if [ ! -d tests/frontend/node_modules/jsdom ]; then
    echo "Installing jsdom for frontend tests..."
    (cd tests/frontend && npm install --silent)
  fi
else
  echo "node not found — frontend (jsdom) tests will be skipped."
fi

python manage.py test "$@"

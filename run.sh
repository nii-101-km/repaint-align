#!/usr/bin/env bash
set -eu
cd -- "$(dirname -- "$0")"
if [ ! -x .venv/bin/python ]; then
  echo 'Create .venv and install dependencies first; see README.md.' >&2
  exit 1
fi
exec .venv/bin/python app.py "$@"

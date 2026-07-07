#!/usr/bin/env sh
set -e
nanodjango manage app.py migrate
nanodjango manage app.py createsuperuser --noinput || true   # idempotent across restarts
exec nanodjango run app.py 0.0.0.0:8000

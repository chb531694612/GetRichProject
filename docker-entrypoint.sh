#!/bin/sh
set -eu

mkdir -p /app/data
chown -R app:app /app/data

exec gosu app score-fourfold "$@"

#!/bin/sh
# Run the full suite in Linux with disposable PostgreSQL/private-storage fixtures.
#
#   bun run test:linux                              the whole suite
#   bun run test:linux tests/drawingBrowser.test.ts one file
#   bun run test:linux --shard=3/4                  one CI shard
#
# Needs Python3 and a trusted local Unix Docker daemon with host networking.
# This gives repository tests Docker-daemon access, as native CI does.
set -eu
cd "$(dirname "$0")/.."
exec python3 scripts/test-linux.py "$@"

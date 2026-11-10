#!/bin/sh
# Keep existing fixture owners on the orchestrator's identified Docker daemon.
set -eu
unset DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH
endpoint=unix:///var/run/docker.sock
expected=${DOOP_TEST_DAEMON_ID:?the Linux test orchestrator must identify its daemon}
actual=$(/usr/local/bin/docker-real --host "$endpoint" info --format '{{.ID}}')
if [ "$actual" != "$expected" ]; then
  echo 'Linux test Docker authority changed; fixtures and recovery state are retained.' >&2
  exit 1
fi
exec /usr/local/bin/docker-real --host "$endpoint" "$@"

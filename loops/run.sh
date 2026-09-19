#!/bin/sh
# Options are read from /data/options.json. The Supervisor token is the credential for every
# check; s6-overlay v3 stashes the container environment in files and does not hand it to CMD,
# so it is read back from there (the Voice Bridge add-on found this first).
set -eu

if [ -z "${SUPERVISOR_TOKEN:-}" ] && [ -r /run/s6/container_environment/SUPERVISOR_TOKEN ]; then
  SUPERVISOR_TOKEN="$(cat /run/s6/container_environment/SUPERVISOR_TOKEN)"
  export SUPERVISOR_TOKEN
fi

echo "[loops] serving on :8095"
exec python3 /opt/loops/server.py --options /data/options.json --data /data --port 8095

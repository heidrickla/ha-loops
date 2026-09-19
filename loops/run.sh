#!/bin/sh
# Options are read from /data/options.json; the Supervisor token arrives in the environment.
set -eu

echo "[loops] serving on :8095"
exec python3 /opt/loops/server.py --options /data/options.json --data /data --port 8095

#!/usr/bin/env sh
# Add-on entrypoint. The Supervisor hands us a token for Home Assistant's API
# (SUPERVISOR_TOKEN), so there is nothing for the user to configure.
set -eu

LOG_LEVEL="info"
if [ -f /data/options.json ]; then
    LOG_LEVEL="$(jq -r '.log_level // "info"' /data/options.json)"
fi
export LOG_LEVEL

# exec: Python must receive SIGTERM itself, so it can put the thermostats back
# before the add-on stops.
exec python3 -m slingkoll --port 8099

#!/usr/bin/with-contenv sh
# Add-on entrypoint. The Supervisor hands us a token for Home Assistant's API
# (SUPERVISOR_TOKEN), so there is nothing for the user to configure.
set -eu

# Set here rather than only in the Dockerfile: the base image's init system
# does not pass Docker's ENV on to this script.
export PYTHONPATH=/opt/slingkoll/src
export PYTHONUNBUFFERED=1
export SLINGKOLL_DATA=/data

LOG_LEVEL="info"
if [ -f /data/options.json ]; then
    LOG_LEVEL="$(jq -r '.log_level // "info"' /data/options.json)"
fi
export LOG_LEVEL

cd /opt/slingkoll/src
# exec: Python must receive SIGTERM itself, so it can put the thermostats back
# before the add-on stops.
exec python3 -m slingkoll --port 8099

#!/usr/bin/env bash
# Add-on entrypoint.
#
# Everything the service needs to reach Home Assistant and the MQTT broker is
# handed to us by the Supervisor, so this script's only job is to turn that
# into environment variables and hand over to the application. No tokens, no
# broker passwords, nothing for the user to paste.

set -euo pipefail

OPTIONS=/data/options.json

read_option() {
    local key="$1" fallback="${2-}"
    local value
    value="$(jq -r --arg k "$key" '.[$k] // empty' "$OPTIONS" 2>/dev/null || true)"
    printf '%s' "${value:-$fallback}"
}

export HEMOPT_DATA=/data
export HEMOPT_LOG_LEVEL="$(read_option log_level info)"
export HEMOPT_PRICE_AREA="$(read_option price_area SE3)"
export HEMOPT_CONTRACT="$(read_option contract hourly)"

# Home Assistant, via the Supervisor proxy.
export HEMOPT_HA_URL="http://supervisor/core"
export HEMOPT_HA_TOKEN="${SUPERVISOR_TOKEN:-}"

# Ask the Supervisor whether an MQTT broker is available. Absence is fine; the
# service runs without one and simply stops publishing discovery entities.
if [[ -n "${SUPERVISOR_TOKEN:-}" ]]; then
    mqtt_json="$(curl -fsSL \
        -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
        http://supervisor/services/mqtt 2>/dev/null || true)"

    if [[ -n "$mqtt_json" ]] && [[ "$(jq -r '.result // empty' <<<"$mqtt_json")" == "ok" ]]; then
        export HEMOPT_MQTT_HOST="$(jq -r '.data.host // empty' <<<"$mqtt_json")"
        export HEMOPT_MQTT_PORT="$(jq -r '.data.port // 1883' <<<"$mqtt_json")"
        export HEMOPT_MQTT_USERNAME="$(jq -r '.data.username // empty' <<<"$mqtt_json")"
        export HEMOPT_MQTT_PASSWORD="$(jq -r '.data.password // empty' <<<"$mqtt_json")"
        echo "[hemopt] MQTT broker found at ${HEMOPT_MQTT_HOST}:${HEMOPT_MQTT_PORT}"
    else
        echo "[hemopt] No MQTT broker configured; Home Assistant entities are disabled."
    fi
fi

exec hemopt serve --host 0.0.0.0 --port 8099

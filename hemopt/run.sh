#!/usr/bin/env bash
# Add-on entrypoint.
#
# Everything the service needs to reach Home Assistant and the MQTT broker is
# handed to us by the Supervisor, so this script's only job is to turn that
# into environment variables and hand over to the application. No tokens, no
# broker passwords, nothing for the user to paste.

set -euo pipefail

OPTIONS=/data/options.json

# Important: do NOT use `jq … // fallback` for booleans. In jq, `false // x`
# yields x, so turning peak_enabled off in Configuration would silently stay on.
read_option() {
    local key="$1" fallback="${2-}"
    local typ value
    if [[ ! -f $OPTIONS ]]; then
        printf '%s' "$fallback"
        return
    fi
    if ! jq -e --arg k "$key" 'has($k)' "$OPTIONS" >/dev/null 2>&1; then
        printf '%s' "$fallback"
        return
    fi
    typ="$(jq -r --arg k "$key" '.[$k] | type' "$OPTIONS" 2>/dev/null || echo null)"
    if [[ $typ == "null" ]]; then
        printf '%s' "$fallback"
        return
    fi
    value="$(jq -r --arg k "$key" '.[$k]' "$OPTIONS" 2>/dev/null || true)"
    printf '%s' "$value"
}

export HEMOPT_DATA=/data
export HEMOPT_LOG_LEVEL="$(read_option log_level info)"
export HEMOPT_PRICE_AREA="$(read_option price_area SE3)"
export HEMOPT_CONTRACT="$(read_option contract hourly)"

# Peak tariff — set under the add-on's Configuration tab, not the dashboard.
export HEMOPT_PEAK_ENABLED="$(read_option peak_enabled true)"
export HEMOPT_PEAK_N="$(read_option peak_n_peaks 5)"
export HEMOPT_PEAK_PRICE="$(read_option peak_price_per_kw 67.5)"
export HEMOPT_PEAK_HOUR_START="$(read_option peak_hour_start 7)"
export HEMOPT_PEAK_HOUR_END="$(read_option peak_hour_end 21)"
export HEMOPT_PEAK_WEEKDAYS="$(read_option peak_weekdays_only true)"
export HEMOPT_TOTAL_POWER_ENTITY="$(read_option total_power_entity "")"

echo "[hemopt] peak_enabled=${HEMOPT_PEAK_ENABLED} meter=${HEMOPT_TOTAL_POWER_ENTITY:-"(none)"}"

# Home Assistant, via the Supervisor proxy. Official URL form is
# http://supervisor/core/api/… with SUPERVISOR_TOKEN as Bearer.
# With homeassistant_api: true in config.yaml there is no Info toggle — access
# is granted automatically when the add-on is installed/updated.
export HEMOPT_HA_URL="http://supervisor/core"
export HEMOPT_HA_TOKEN="${SUPERVISOR_TOKEN:-}"

echo "[hemopt] SUPERVISOR_TOKEN length=${#HEMOPT_HA_TOKEN}"
if [[ -z ${HEMOPT_HA_TOKEN} ]]; then
    echo "[hemopt] ERROR: SUPERVISOR_TOKEN saknas."
    echo "[hemopt] homeassistant_api ska vara true i tillaggets config.yaml."
    echo "[hemopt] Uppdatera/Rebuild tillagget sa Supervisorn ger en token."
fi

ha_code="$(curl -sS -o /tmp/hemopt-ha-ping.json -w '%{http_code}' \
    -H "Authorization: Bearer ${HEMOPT_HA_TOKEN}" \
    -H "Content-Type: application/json" \
    http://supervisor/core/api/config 2>/tmp/hemopt-ha-ping.err || true)"
echo "[hemopt] HA Core proxy HTTP ${ha_code:-000}"
if [[ ${ha_code:-000} != 200 ]]; then
    echo "[hemopt] HA ping body: $(head -c 240 /tmp/hemopt-ha-ping.json 2>/dev/null || true)"
    echo "[hemopt] HA ping err: $(head -c 240 /tmp/hemopt-ha-ping.err 2>/dev/null || true)"
    echo "[hemopt] Tips: Installera/uppdatera fran GitHub-repot, tryck Rebuild,"
    echo "[hemopt] och bekrafta att Loggen visar HTTP 200. Utan det blir HA/vader/MQTT roda."
fi

# Ask the Supervisor whether an MQTT broker is available. Absence is fine; the
# service runs without one and simply stops publishing discovery entities.
if [[ -n ${HEMOPT_HA_TOKEN} ]]; then
    mqtt_json="$(curl -fsSL \
        -H "Authorization: Bearer ${HEMOPT_HA_TOKEN}" \
        http://supervisor/services/mqtt 2>/dev/null || true)"

    if [[ -n $mqtt_json ]] && [[ $(jq -r '.result // empty' <<<"$mqtt_json") == "ok" ]]; then
        export HEMOPT_MQTT_HOST="$(jq -r '.data.host // empty' <<<"$mqtt_json")"
        export HEMOPT_MQTT_PORT="$(jq -r '.data.port // 1883' <<<"$mqtt_json")"
        export HEMOPT_MQTT_USERNAME="$(jq -r '.data.username // empty' <<<"$mqtt_json")"
        export HEMOPT_MQTT_PASSWORD="$(jq -r '.data.password // empty' <<<"$mqtt_json")"
        echo "[hemopt] MQTT broker found at ${HEMOPT_MQTT_HOST}:${HEMOPT_MQTT_PORT}"
    else
        echo "[hemopt] No MQTT broker configured; Home Assistant entities are disabled."
    fi
fi

echo "[hemopt] Starting on 0.0.0.0:8099 (healthz answers before the planner is ready)"
exec hemopt serve --host 0.0.0.0 --port 8099

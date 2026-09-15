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

# Install / refresh the LK Arc Climate custom component into HA's config so
# the user does not have to copy files by hand. Requires homeassistant_config:rw.
# Also injects a config entry and (once) asks Supervisor to restart HA — without
# that step climate.*_thermostat never appears (Golvvärme shows Entity not found).
install_lk_arc_climate() {
    local src="/opt/hemopt/bundled/lk_arc_climate"
    local dest="/homeassistant/custom_components/lk_arc_climate"
    local marker="$dest/.installed_by_hemopt"
    local entries="/homeassistant/.storage/core.config_entries"

    if [[ ! -d /homeassistant ]]; then
        echo "[hemopt] /homeassistant saknas (homeassistant_config ej monterad) — hoppar over LK Arc Climate"
        return
    fi
    if [[ ! -f $src/manifest.json ]]; then
        echo "[hemopt] Bundlad LK Arc Climate saknas i imagen ($src)"
        return
    fi

    mkdir -p /homeassistant/custom_components
    # Refresh on every start so add-on updates ship component fixes.
    rm -rf "$dest"
    cp -a "$src" "$dest"
    printf 'hemopt\n' >"$marker"
    echo "[hemopt] LK Arc Climate installerad i $dest"

    # Ensure a config entry exists in HA storage. Files alone are not enough —
    # without an entry there are no climate.*_thermostat entities.
    if [[ -f $entries ]]; then
        entry_status="$(python3 - "$entries" <<'PY'
import json, sys, uuid, os
from datetime import datetime, timezone

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    doc = json.load(fh)
entries = doc.setdefault("data", {}).setdefault("entries", [])
if any(e.get("domain") == "lk_arc_climate" for e in entries):
    print("present")
    raise SystemExit(0)
now = datetime.now(timezone.utc).isoformat()
entries.append(
    {
        "created_at": now,
        "modified_at": now,
        "entry_id": uuid.uuid4().hex,
        "version": 1,
        "minor_version": 1,
        "domain": "lk_arc_climate",
        "title": "LK Arc Climate",
        "data": {},
        "options": {},
        "pref_disable_new_entities": False,
        "pref_disable_polling": False,
        "source": "import",
        "unique_id": "lk_arc_climate",
        "disabled_by": None,
        "discovery_keys": {},
        "subentries": [],
    }
)
tmp = path + ".hemopt-tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
os.replace(tmp, path)
print("injected")
PY
)" || entry_status="failed"
        echo "[hemopt] LK Arc Climate config entry: ${entry_status}"
    else
        echo "[hemopt] $entries saknas ännu — HA har kanske aldrig startat klart"
    fi
}

install_lk_arc_climate

export HEMOPT_ADDON=1
export HEMOPT_DATA=/data
export HEMOPT_LOG_LEVEL="$(read_option log_level info)"
export HEMOPT_PRICE_AREA="$(read_option price_area SE3)"
export HEMOPT_CONTRACT="$(read_option contract hourly)"
export HEMOPT_SUPPLIER_MARKUP_ORE="$(read_option supplier_markup_ore "")"
export HEMOPT_CERTIFICATE_ORE="$(read_option certificate_ore "")"
export HEMOPT_BALANCING_ORE="$(read_option balancing_ore "")"
export HEMOPT_ENERGY_TAX_ORE="$(read_option energy_tax_ore "")"
export HEMOPT_TRANSFER_FEE_ORE="$(read_option transfer_fee_ore "")"

# Peak tariff — set under the add-on's Configuration tab, not the dashboard.
export HEMOPT_PEAK_ENABLED="$(read_option peak_enabled true)"
export HEMOPT_PEAK_N="$(read_option peak_n_peaks 5)"
export HEMOPT_PEAK_PRICE="$(read_option peak_price_per_kw 67.5)"
export HEMOPT_PEAK_HOUR_START="$(read_option peak_hour_start 7)"
export HEMOPT_PEAK_HOUR_END="$(read_option peak_hour_end 21)"
export HEMOPT_PEAK_WEEKDAYS="$(read_option peak_weekdays_only true)"
export HEMOPT_TOTAL_POWER_ENTITY="$(read_option total_power_entity "")"
export HEMOPT_WEATHER_ENTITY="$(read_option weather_entity "")"

echo "[hemopt] peak_enabled=${HEMOPT_PEAK_ENABLED} meter=${HEMOPT_TOTAL_POWER_ENTITY:-"(none)"}"

# Home Assistant auth.
# Prefer SUPERVISOR_TOKEN + http://supervisor/core (automatic with homeassistant_api).
# If the Supervisor gave no token, fall back to a long-lived access token from
# Configuration — never export an empty HEMOPT_HA_TOKEN (that would wipe a
# token from hemopt.yaml).
# Under `set -u`, SUPERVISOR_TOKEN may be unset — always use ${VAR:-}.
_supervisor_token="${SUPERVISOR_TOKEN:-}"
echo "[hemopt] SUPERVISOR_TOKEN length=${#_supervisor_token}"
if [[ -n $_supervisor_token ]]; then
    export HEMOPT_HA_URL="http://supervisor/core"
    export HEMOPT_HA_TOKEN="$_supervisor_token"
    echo "[hemopt] Using Supervisor token against ${HEMOPT_HA_URL}"
else
    echo "[hemopt] SUPERVISOR_TOKEN saknas."
    echo "[hemopt] Relaterade env-nycklar: $(env | cut -d= -f1 | grep -iE 'super|hass|token' | tr '\n' ' ' || true)"
    LL_TOKEN="$(read_option ha_token "")"
    HA_URL="$(read_option ha_url "http://homeassistant:8123")"
    if [[ -n $LL_TOKEN ]]; then
        export HEMOPT_HA_TOKEN="$LL_TOKEN"
        export HEMOPT_HA_URL="$HA_URL"
        echo "[hemopt] Using long-lived token from Configuration against ${HEMOPT_HA_URL} (len=${#LL_TOKEN})"
    else
        echo "[hemopt] ERROR: Ingen token tillganglig."
        echo "[hemopt] Alternativ 1: Avinstallera och installera om tillagget sa Supervisorn ger SUPERVISOR_TOKEN."
        echo "[hemopt] Alternativ 2: Skapa Long-lived access token (HA-profilen) och klistra in under Configuration → HA-token."
        echo "[hemopt]              Lat HA-URL vara http://homeassistant:8123 (standard inne i tillagg)."
    fi
fi

if [[ -n ${HEMOPT_HA_TOKEN:-} ]]; then
    ha_code="$(curl -sS -o /tmp/hemopt-ha-ping.json -w '%{http_code}' \
        -H "Authorization: Bearer ${HEMOPT_HA_TOKEN}" \
        -H "Content-Type: application/json" \
        "${HEMOPT_HA_URL%/}/api/config" 2>/tmp/hemopt-ha-ping.err || true)"
    echo "[hemopt] HA API ping HTTP ${ha_code:-000} (${HEMOPT_HA_URL%/}/api/config)"
    if [[ ${ha_code:-000} != 200 ]]; then
        echo "[hemopt] HA ping body: $(head -c 240 /tmp/hemopt-ha-ping.json 2>/dev/null || true)"
        echo "[hemopt] HA ping err: $(head -c 240 /tmp/hemopt-ha-ping.err 2>/dev/null || true)"
    else
        # One-shot: if climate.*_thermostat still missing, restart HA so the
        # freshly copied custom component + injected config entry load.
        boot_marker="/data/.lk_arc_climate_bootstrapped_0_1_25"
        if [[ ! -f $boot_marker ]]; then
            climate_count="$(curl -fsS \
                -H "Authorization: Bearer ${HEMOPT_HA_TOKEN}" \
                "${HEMOPT_HA_URL%/}/api/states" 2>/dev/null \
                | jq '[.[] | select(.entity_id|test("^climate\\.[0-9a-f]{2}(_[0-9a-f]{2}){5}_thermostat$"))] | length' \
                2>/dev/null || echo 0)"
            echo "[hemopt] LK MAC-climate entities nu: ${climate_count:-0}"
            if [[ ${climate_count:-0} -lt 1 ]]; then
                echo "[hemopt] ============================================================"
                echo "[hemopt] Golvvärme saknar climate.*_thermostat (Entity not found)."
                echo "[hemopt] Startar om Home Assistant EN gång så LK Arc Climate laddas."
                echo "[hemopt] ============================================================"
                touch "$boot_marker"
                # Prefer Supervisor restart (hassio_api); fall back to HA service.
                if [[ -n $_supervisor_token ]]; then
                    curl -sS -X POST \
                        -H "Authorization: Bearer ${_supervisor_token}" \
                        -H "Content-Type: application/json" \
                        http://supervisor/core/restart >/tmp/hemopt-ha-restart.json 2>/tmp/hemopt-ha-restart.err \
                        && echo "[hemopt] Supervisor core/restart skickad" \
                        || echo "[hemopt] Supervisor restart misslyckades: $(head -c 200 /tmp/hemopt-ha-restart.err 2>/dev/null || true)"
                else
                    curl -sS -X POST \
                        -H "Authorization: Bearer ${HEMOPT_HA_TOKEN}" \
                        -H "Content-Type: application/json" \
                        "${HEMOPT_HA_URL%/}/api/services/homeassistant/restart" \
                        -d '{}' >/tmp/hemopt-ha-restart.json 2>/tmp/hemopt-ha-restart.err \
                        && echo "[hemopt] homeassistant.restart skickad" \
                        || echo "[hemopt] HA restart misslyckades: $(head -c 200 /tmp/hemopt-ha-restart.err 2>/dev/null || true)"
                fi
            else
                touch "$boot_marker"
                echo "[hemopt] climate.*_thermostat finns redan — ingen HA-omstart behövs"
            fi
        fi
    fi
else
    echo "[hemopt] Hoppar over HA-ping — ingen token."
fi

# Ask the Supervisor whether an MQTT broker is available. Absence is fine; the
# service runs without one and simply stops publishing discovery entities.
if [[ -n $_supervisor_token ]]; then
    mqtt_json="$(curl -fsSL \
        -H "Authorization: Bearer ${_supervisor_token}" \
        http://supervisor/services/mqtt 2>/dev/null || true)"

    if [[ -n $mqtt_json ]] && [[ $(jq -r '.result // empty' <<<"$mqtt_json") == "ok" ]]; then
        export HEMOPT_MQTT_HOST="$(jq -r '.data.host // empty' <<<"$mqtt_json")"
        export HEMOPT_MQTT_PORT="$(jq -r '.data.port // 1883' <<<"$mqtt_json")"
        export HEMOPT_MQTT_USERNAME="$(jq -r '.data.username // empty' <<<"$mqtt_json")"
        export HEMOPT_MQTT_PASSWORD="$(jq -r '.data.password // empty' <<<"$mqtt_json")"
        echo "[hemopt] MQTT broker found at ${HEMOPT_MQTT_HOST}:${HEMOPT_MQTT_PORT}"
    else
        echo "[hemopt] No MQTT broker from Supervisor; MQTT disabled in add-on mode."
    fi
else
    echo "[hemopt] No SUPERVISOR_TOKEN — MQTT from Supervisor skipped."
fi

# Configuration fallback when Supervisor did not hand over Mosquitto credentials
# (common when SUPERVISOR_TOKEN is missing but Mosquitto is installed).
if [[ -z ${HEMOPT_MQTT_HOST:-} ]]; then
    MQTT_HOST="$(read_option mqtt_host "")"
    if [[ -n $MQTT_HOST ]]; then
        export HEMOPT_MQTT_HOST="$MQTT_HOST"
        export HEMOPT_MQTT_PORT="$(read_option mqtt_port 1883)"
        export HEMOPT_MQTT_USERNAME="$(read_option mqtt_username "")"
        export HEMOPT_MQTT_PASSWORD="$(read_option mqtt_password "")"
        echo "[hemopt] Using MQTT from Configuration: ${HEMOPT_MQTT_HOST}:${HEMOPT_MQTT_PORT}"
    fi
fi

echo "[hemopt] Starting on 0.0.0.0:8099 (healthz answers before the planner is ready)"
exec hemopt serve --host 0.0.0.0 --port 8099

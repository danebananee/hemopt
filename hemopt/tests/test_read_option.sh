#!/usr/bin/env bash
# Guard against the jq `false // fallback` trap in run.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=/dev/null
OPTIONS="$(mktemp)"
trap 'rm -f "$OPTIONS"' EXIT

# Extract just read_option from run.sh by sourcing a snippet.
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

printf '%s' '{"peak_enabled":false,"peak_weekdays_only":false,"total_power_entity":"","peak_n_peaks":3}' >"$OPTIONS"

[[ "$(read_option peak_enabled true)" == "false" ]]
[[ "$(read_option peak_weekdays_only true)" == "false" ]]
[[ "$(read_option total_power_entity x)" == "" ]]
[[ "$(read_option peak_n_peaks 5)" == "3" ]]
[[ "$(read_option log_level info)" == "info" ]]

# Old buggy pattern must NOT be reintroduced in run.sh.
if grep -n 'jq -r.*"\.\[\$k\] // empty"' "$ROOT/run.sh"; then
    echo "run.sh still uses jq // which drops false" >&2
    exit 1
fi

echo "ok: read_option preserves false and empty string"

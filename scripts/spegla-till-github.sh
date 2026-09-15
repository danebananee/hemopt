#!/usr/bin/env bash
# Speglar main till det publika GitHub-repot som Home Assistant klonar.
#
#   ./scripts/spegla-till-github.sh
#
# Adressen lases ur repository.json, och GITHUB_TOKEN ur miljon, sa inget
# behover skrivas in for hand. Home Assistant klonar add-on-databaser anonymt
# och nar inte Cursors Origin-repo, darfor maste main ligga pa GitHub.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"

if [[ -z ${GITHUB_TOKEN:-} ]]; then
  echo "GITHUB_TOKEN saknas. Lagg in den under Cloud Agents > Secrets." >&2
  exit 1
fi

url=$(sed -n 's#.*"url": "https://github.com/\([^"]*\)".*#\1#p' repository.json)
if [[ -z $url ]]; then
  echo "Hittade ingen GitHub-adress i repository.json." >&2
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "Det finns ocommittade andringar. Committa dem forst." >&2
  exit 1
fi

version=$(sed -n 's/^version: *"\{0,1\}\([^"]*\)"\{0,1\}/\1/p' hemopt/config.yaml)
echo "Speglar main till https://github.com/${url} (add-on version ${version})"
# Utan versionshojning i hemopt/config.yaml ser Home Assistant ingen Update-knapp.

git push "https://x-access-token:${GITHUB_TOKEN}@github.com/${url}.git" main "$@"

echo "Klart. Home Assistant hamtar den nya versionen vid nasta uppdatering."

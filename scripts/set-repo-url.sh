#!/usr/bin/env bash
# Skriver in repots riktiga GitHub-adress i add-on-manifesten.
#
#   ./scripts/set-repo-url.sh <github-anvandarnamn> [repo-namn]
#
# Home Assistant visar adressen som "Documentation"-lank i add-on-butiken, och
# vagrar lagga till en databas vars repository.json pekar pa nagot som inte gar
# att na.
set -euo pipefail

user=${1:-}
repo=${2:-hemopt}

if [[ -z $user ]]; then
  echo "Anvandning: $0 <github-anvandarnamn> [repo-namn]" >&2
  exit 1
fi

root=$(cd "$(dirname "$0")/.." && pwd)
url="https://github.com/${user}/${repo}"

files=("$root/repository.json" "$root/hemopt/config.yaml" "$root/hemopt/DOCS.md" "$root/README.md")
for file in "${files[@]}"; do
  [[ -f $file ]] || continue
  sed -i.bak "s#https://github.com/DITT-GITHUB-NAMN/hemopt#${url}#g" "$file"
  rm -f "$file.bak"
done

echo "Adressen ar nu ${url}"
# Skriptet innehaller sjalvt sokmonstret, sa det raknas inte som en traff.
if grep -rln "DITT-GITHUB-NAMN" "$root" --exclude-dir=.git |
  grep -qv "^$root/scripts/set-repo-url.sh$"; then
  echo "Obs: platshallaren finns kvar pa fler stallen, sok efter DITT-GITHUB-NAMN."
fi

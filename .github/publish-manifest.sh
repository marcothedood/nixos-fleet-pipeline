#!/usr/bin/env bash
# Commit a manifest to the deploy branch.
#
#   publish-manifest.sh <manifest.json> <message> [--resume]
#
# - Refuses a manifest whose seq is not higher than the one already
#   published. Nodes would refuse it anyway; refusing here keeps the tip of
#   the deploy branch equal to the newest manifest, for nodes and for
#   monitoring alike.
# - Keeps a fleet-wide pause: if the published manifest has status "ok" and
#   enabled false (set by hand), the new one is published with enabled
#   false too, unless --resume is given (manual rollbacks do that).
set -euo pipefail

manifest=$1
message=$2
resume=${3:-}
seq=$(jq -r .seq "$manifest")

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

if git fetch --quiet --depth=1 origin deploy; then
  git switch --quiet -c deploy FETCH_HEAD
  published=$(jq -r .seq manifest.json)
  if [ "$seq" -le "$published" ]; then
    echo "::warning::seq $seq is not newer than the published seq $published, not publishing"
    exit 0
  fi
  if [ "$resume" != --resume ] && jq -e '.status == "ok" and .enabled == false' manifest.json > /dev/null; then
    echo "::notice::the fleet is paused (enabled: false on the deploy branch); publishing seq $seq paused"
    jq '.enabled = false' "$manifest" > manifest.json
  else
    cp "$manifest" manifest.json
  fi
else
  git switch --quiet --orphan deploy
  cp "$manifest" manifest.json
fi

git add manifest.json
git commit --quiet -m "$message (seq $seq, status $(jq -r .status manifest.json), enabled $(jq -r .enabled manifest.json))"
git push --quiet origin deploy

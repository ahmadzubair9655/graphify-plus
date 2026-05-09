#!/usr/bin/env bash
# Pull the latest CVE + SAST findings reachable from your code.

set -euo pipefail

REPO="${1:-.}"

echo "→ running pip-audit"
pip-audit --format json --output /tmp/audit.json "$REPO" || true

echo "→ ingesting CVE overlay"
gp daemon security ingest /tmp/audit.json --repo "$REPO" || true

echo "→ running bandit"
bandit -r "$REPO" -f json -o /tmp/bandit.json --quiet || true

echo "→ ingesting SAST overlay"
gp daemon security ingest-sast /tmp/bandit.json --repo "$REPO" || true

echo
echo "→ symbols reachable from CVEs:"
gp daemon security vulnerable --repo "$REPO" --severity HIGH || true

echo
echo "→ symbols carrying SAST findings:"
gp daemon security risky --repo "$REPO" --severity MEDIUM || true

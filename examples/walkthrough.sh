#!/usr/bin/env bash
# Typical session: bring up the daemon, ask a structural question,
# read the receipt, get a plan, post a review.

set -euo pipefail

REPO="${1:-.}"
echo "→ initialising graph cache for $REPO"
gp init --repo "$REPO"

echo "→ starting daemon (detached)"
gp daemon start --repo "$REPO" --detach

echo "→ status"
gp daemon status --repo "$REPO"

echo "→ what's in the auth module?"
gp daemon query whats_in --repo "$REPO" -a path=auth.py --receipt || true

echo "→ planning a new feature"
gp daemon plan "add rate limiting to all REST endpoints" --repo "$REPO" || true

echo "→ session-status (is everything healthy?)"
gp daemon session-status --repo "$REPO"

echo "→ stopping daemon"
gp daemon stop --repo "$REPO"

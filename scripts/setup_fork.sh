#!/usr/bin/env bash
# Turn the current directory (normally /pc inside the verifier image, which already
# holds the built Mathlib in .lake/) into a clone of the endorser's fork of
# proofcommons/proofcommons. Safe to run again.
set -euo pipefail
UPSTREAM="proofcommons/proofcommons"
: "${GH_TOKEN:?set GH_TOKEN to the GitHub token of the endorser first, see docs/github-token.md}"

login=$(gh api user -q .login)
gh auth setup-git >/dev/null
# Create the fork if it does not exist yet; gh prints "already exists" and exits 0
# when it does. --clone=false forks without cloning and without a prompt. (--remote
# is rejected by current gh when a repository argument is given; do not use it here.)
gh repo fork "$UPSTREAM" --clone=false >/dev/null

[ -d .git ] || git init -q
git remote remove origin 2>/dev/null || true
git remote remove upstream 2>/dev/null || true
git remote add origin "https://github.com/$login/proofcommons.git"
git remote add upstream "https://github.com/$UPSTREAM.git"
git fetch -q upstream main
git checkout -q -f -B main upstream/main
git push -q -u origin main 2>/dev/null || echo "note: could not update main of the fork; continuing" >&2   # best effort
echo "ready: this directory is a clone of $login/proofcommons (origin), tracking $UPSTREAM (upstream)"

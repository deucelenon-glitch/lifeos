#!/usr/bin/env bash
# LIFEOS backup — daily sqlite backup into git.
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p backups
TS="$(date +%Y%m%d_%H%M%S)"
sqlite3 data/lifeos.db ".backup 'backups/lifeos_${TS}.db'"
echo "Backup written: backups/lifeos_${TS}.db"

# Keep only the 14 most recent backups
ls -1t backups/lifeos_*.db | tail -n +15 | xargs -r rm -f
echo "Old backups pruned."

# Commit to git (if repo exists)
if git rev-parse --git-dir >/dev/null 2>&1; then
  git add backups/ data/
  git commit -m "LIFEOS: daily backup ${TS}" >/dev/null 2>&1 || echo "Nothing new to commit."
fi
echo "Done."
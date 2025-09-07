#!/usr/bin/env bash
set -euo pipefail

# keep last N backups (change here or via env RETAIN=...)
RETAIN="${RETAIN:-14}"

cd "$(dirname "$0")/.."
mkdir -p backups

# Count and sort newest-first
mapfile -t FILES < <(ls -1t backups/*.sqlite3 2>/dev/null || true)
TOTAL="${#FILES[@]}"

# Nothing to do?
if (( TOTAL <= RETAIN )); then
  echo "[prune] total=${TOTAL}, retain=${RETAIN} — nothing to delete"
  exit 0
fi

# Delete older ones
DEL=$(( TOTAL - RETAIN ))
TO_DELETE=("${FILES[@]:RETAIN:DEL}")

echo "[prune] total=${TOTAL}, retain=${RETAIN}, deleting=${#TO_DELETE[@]}"
for f in "${TO_DELETE[@]}"; do
  echo "[prune] rm $f"
  rm -f -- "$f" || true
done

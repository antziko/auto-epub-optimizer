#!/bin/bash
# fanout.sh
# Polls INCOMING_DIR for .epub files and copies each one into every directory
# listed in FANOUT_DESTS (space-separated), then removes the original. Used to
# feed a single drop folder into multiple device-specific optimizer bookdrops.
#
# Uses the same polling + atomic-claim approach as epub-optimizer.sh so it is
# safe on NTFS mounts under WSL2 and never copies a half-written file.

INCOMING_DIR="${INCOMING_DIR:?Set INCOMING_DIR}"
FANOUT_DESTS="${FANOUT_DESTS:?Set FANOUT_DESTS (space-separated dirs)}"
POLL_INTERVAL="${POLL_INTERVAL:-5}"
LOG_FILE="${FANOUT_LOG_FILE:-/logs/fanout.log}"

mkdir -p "$(dirname "$LOG_FILE")"
mkdir -p "$INCOMING_DIR" "$INCOMING_DIR/processing"
for dest in $FANOUT_DESTS; do
  mkdir -p "$dest"
done

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "fanout started."
log "  Watching: $INCOMING_DIR"
log "  Copying to: $FANOUT_DESTS"
log "  Poll interval: ${POLL_INTERVAL}s"

while true; do
  while IFS= read -r -d '' filepath; do
    filename=$(basename "$filepath")
    staging="$INCOMING_DIR/processing/$filename"

    # Atomically claim the file so it is only fanned out once.
    if ! mv "$filepath" "$staging" 2>/dev/null; then
      continue
    fi

    if [ ! -s "$staging" ]; then
      log "WARNING: $filename is empty — skipping and removing"
      rm -f "$staging"
      continue
    fi

    ok=1
    for dest in $FANOUT_DESTS; do
      if cp "$staging" "$dest/$filename" 2>/dev/null; then
        log "Copied: $filename -> $dest"
      else
        log "ERROR: failed to copy $filename -> $dest"
        ok=0
      fi
    done

    if [ "$ok" = "1" ]; then
      rm -f "$staging"
    else
      # Leave the file in processing/ so a copy failure is visible and recoverable.
      log "ERROR: $filename kept in processing/ due to copy failure"
    fi
  done < <(find "$INCOMING_DIR" -maxdepth 1 -name "*.epub" -print0 2>/dev/null)

  sleep "$POLL_INTERVAL"
done

#!/bin/bash
# fanout.sh
# Polls INCOMING_DIR for .epub files and copies each one into every directory
# listed in FANOUT_DESTS (space-separated), then removes the original. Used to
# feed a single drop folder into multiple device-specific optimizer bookdrops.
#
# Polling (not inotify) so it works on NTFS mounts under WSL2. Two safeguards
# stop half-written files from being processed:
#   1. Files dropped over a network share (SMB/NFS) arrive incrementally, so a
#      file is only claimed once its size has stayed constant for STABLE_SECS.
#   2. Each destination copy is written to a hidden temp name and then renamed
#      in place, so an optimizer polling that destination never sees a partial.

INCOMING_DIR="${INCOMING_DIR:?Set INCOMING_DIR}"
FANOUT_DESTS="${FANOUT_DESTS:?Set FANOUT_DESTS (space-separated dirs)}"
POLL_INTERVAL="${POLL_INTERVAL:-5}"
STABLE_SECS="${STABLE_SECS:-3}"
LOG_FILE="${FANOUT_LOG_FILE:-/logs/fanout.log}"

mkdir -p "$(dirname "$LOG_FILE")"
mkdir -p "$INCOMING_DIR" "$INCOMING_DIR/processing"
for dest in $FANOUT_DESTS; do
  mkdir -p "$dest"
done

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# File size in bytes, or empty if the file is gone.
fsize() {
  stat -c %s "$1" 2>/dev/null
}

log "fanout started."
log "  Watching: $INCOMING_DIR"
log "  Copying to: $FANOUT_DESTS"
log "  Poll interval: ${POLL_INTERVAL}s (stability window ${STABLE_SECS}s)"

while true; do
  while IFS= read -r -d '' filepath; do
    filename=$(basename "$filepath")
    staging="$INCOMING_DIR/processing/$filename"

    # Wait until the file has finished arriving: size must be unchanged across
    # the stability window. If it is still growing (e.g. mid SMB upload), skip
    # it this round and pick it up on a later poll.
    size_before=$(fsize "$filepath")
    [ -z "$size_before" ] && continue
    sleep "$STABLE_SECS"
    size_after=$(fsize "$filepath")
    if [ -z "$size_after" ] || [ "$size_before" != "$size_after" ]; then
      continue
    fi

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
      # Copy to a hidden temp, then rename within the destination so the
      # finished name appears atomically to whatever is watching $dest.
      tmp="$dest/.fanout-$filename"
      if cp "$staging" "$tmp" 2>/dev/null && mv "$tmp" "$dest/$filename" 2>/dev/null; then
        log "Copied: $filename -> $dest"
      else
        rm -f "$tmp" 2>/dev/null
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

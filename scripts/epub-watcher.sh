#!/bin/bash
# epub-watcher.sh
# Watches the epub-optimizer output folder and moves new files to the Windows destination.

# Load configuration from ~/.config/epub-optimizer/.env
source "$(dirname "$0")/load-env.sh"

WATCH_DIR="$EPUB_OUTPUT_DIR"
DEST_DIR="$WATCHER_DEST_DIR"
LOG_FILE="$WATCHER_LOG_FILE"

# Ensure destination and log directories exist
mkdir -p "$DEST_DIR"
mkdir -p "$(dirname "$LOG_FILE")"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# Publish a finished EPUB into DEST_DIR safely. When DEST_DIR is on a different
# filesystem than WATCH_DIR, a plain mv is copy+delete and non-atomic, so a
# consumer watching DEST_DIR (e.g. Calibre-Web-Automated ingest) could pick up a
# half-written file. Copy to a hidden temp first, then rename within DEST_DIR so
# the final name appears atomically. Hidden (dot) name is ignored by such watchers.
move_safe() {
  local src="$1" filename="$2"
  local tmp="$DEST_DIR/.incoming-$filename"
  local dst="$DEST_DIR/$filename"
  # Self-heal: a removed destination dir would otherwise drop finished books.
  mkdir -p "$DEST_DIR"
  if mv "$src" "$tmp" 2>/dev/null && mv "$tmp" "$dst" 2>/dev/null; then
    log "Moved: $filename"
  else
    rm -f "$tmp" 2>/dev/null
    log "ERROR moving: $filename"
  fi
}

log "epub-watcher started. Watching: $WATCH_DIR"
log "Moving files to: $DEST_DIR"

# Move any files that already exist in the watch dir on startup
for f in "$WATCH_DIR"/*; do
  [ -f "$f" ] || continue
  filename=$(basename "$f")
  log "Found existing file on startup: $filename — moving to $DEST_DIR"
  move_safe "$f" "$filename"
done

# Watch for new files using inotifywait
inotifywait -m -e close_write -e moved_to --format '%f' "$WATCH_DIR" 2>>"$LOG_FILE" |
while read -r filename; do
  src="$WATCH_DIR/$filename"

  # Ignore hidden temp files and non-EPUB artifacts; the optimizer writes
  # a hidden staging file before atomically renaming the finished book.
  case "$filename" in
    .* ) continue ;;
    *.epub ) ;;
    * ) continue ;;
  esac

  # Skip if it's not a regular file (e.g. temp files)
  [ -f "$src" ] || continue

  log "Detected new file: $filename — moving to $DEST_DIR"
  move_safe "$src" "$filename"
done

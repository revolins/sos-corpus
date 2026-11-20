#!/usr/bin/env bash
# Mirror every GitHub release of revolins/sos-corpus into a local backup folder.
#
# Run daily from cron on the lab server. The weekly workflow deletes a superseded
# release only after it is more than 7 days old (scripts/prune_releases.sh), so a
# daily mirror has a week to copy it before it disappears from GitHub.
#
#   scripts/mirror_releases.sh [BACKUP_DIR]
#
# For each tag not yet mirrored: download all assets into BACKUP_DIR/<tag>/, save
# the release notes and metadata, write SHA256SUMS, and check every asset's size
# against GitHub. A tag is only marked complete (BACKUP_DIR/<tag>/.complete) once
# all of that succeeded; an interrupted download is retried on the next run.
#
# crontab example (daily 07:30):
#   30 7 * * * /path/to/sos-corpus-clean/scripts/mirror_releases.sh /path/to/release-backups
set -euo pipefail

REPO="${SOS_REPO:-revolins/sos-corpus}"
DEST="${1:-/egr/research-dselab/revolins/SOS/release-backups}"
mkdir -p "$DEST"
LOG="$DEST/mirror.log"
log() { echo "$(date -u +%FT%TZ) $*" | tee -a "$LOG"; }

tags=$(gh release list -R "$REPO" --limit 200 --json tagName -q '.[].tagName')
[ -n "$tags" ] || { log "ERROR: no releases listed for $REPO"; exit 1; }

failed=0
for tag in $tags; do
  dir="$DEST/$tag"
  [ -f "$dir/.complete" ] && continue
  log "mirroring $tag"
  mkdir -p "$dir"
  if ! gh release download "$tag" -R "$REPO" --dir "$dir" --clobber >>"$LOG" 2>&1; then
    log "ERROR: download failed for $tag"; failed=1; continue
  fi
  gh release view "$tag" -R "$REPO" \
    --json tagName,name,body,createdAt,publishedAt,isPrerelease,targetCommitish,assets \
    > "$dir/release.json"
  gh release view "$tag" -R "$REPO" --json body -q .body > "$dir/notes.md"
  # Every asset present with the size GitHub reports.
  ok=1
  while IFS=$'\t' read -r name size; do
    if [ ! -f "$dir/$name" ] || [ "$(stat -c %s "$dir/$name")" != "$size" ]; then
      log "ERROR: $tag/$name missing or wrong size (expected $size)"; ok=0
    fi
  done < <(python3 -c 'import json,sys; [print(a["name"], a["size"], sep="\t") for a in json.load(open(sys.argv[1]))["assets"]]' "$dir/release.json")
  if [ "$ok" = 1 ]; then
    (cd "$dir" && find . -maxdepth 1 -type f ! -name SHA256SUMS ! -name .complete ! -name release.json ! -name notes.md \
       -printf '%f\n' | sort | xargs -r sha256sum > SHA256SUMS)
    touch "$dir/.complete"
    log "done $tag ($(wc -l < "$dir/SHA256SUMS") asset(s))"
  else
    failed=1
  fi
done
exit "$failed"

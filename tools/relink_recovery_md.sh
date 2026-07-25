#!/bin/sh
# Repair the RECOVERY.md hardlink after an editor severed it.
#
# docs/reference/RECOVERY.md is ONE document hardlinked into both the cdda2img and
# AccuDisc repos. Most editors save atomically (write temp, rename over target),
# which allocates a new inode and silently severs the link; the two repos then hold
# look-alike files that diverge from the next edit onward. It has been severed three
# times on 2026-07-25 alone.
#
# Repair is `rm` + `ln`, which DESTROYS whatever is at the target path. So the peer
# copy must be checked for content of its own first. That check is the entire point
# of this script, because doing it inline is easy to get backwards:
#
#     diff peer mine | grep -c '^<' && rm peer && ln mine peer      # WRONG
#
# `grep -c` exits 0 when it finds matches, so that chain proceeds exactly when the
# peer HAS unique lines and aborts when it has none — the opposite of the intent. It
# was written that way once here, and only luck (the unique lines being our own
# replaced paragraph) made it harmless.
#
# Usage:  sh tools/relink_recovery_md.sh [--force]
#   --force  relink even when the peer has unique lines (they will be LOST)

set -eu

# Overridable so the refusal path can be exercised on throwaway files. A guard that
# has only ever been observed to pass is not a verified guard — that is the same
# error as the inverted chain it replaces (AccuDisc correspondence §ap.1).
MINE=${RECOVERY_MINE:-/home/kgr/Git/cdda2img/docs/reference/RECOVERY.md}
PEER=${RECOVERY_PEER:-/home/kgr/Git/accudisc/docs/reference/RECOVERY.md}
FORCE=${1:-}

[ -f "$MINE" ] || { echo "missing: $MINE" >&2; exit 2; }
[ -f "$PEER" ] || { echo "missing: $PEER" >&2; exit 2; }

if [ "$(stat -c %i "$MINE")" = "$(stat -c %i "$PEER")" ]; then
    echo "already linked: inode $(stat -c %i "$MINE"), links=$(stat -c %h "$MINE")"
    exit 0
fi

# Lines present in the peer copy and absent from ours. Anything here is content that
# `rm` would destroy — usually our own superseded text, but that must be looked at,
# not assumed.
unique=$(diff "$PEER" "$MINE" | grep -c '^<' || true)
if [ "$unique" -ne 0 ] && [ "$FORCE" != "--force" ]; then
    echo "REFUSING: $unique line(s) exist only in the peer copy:" >&2
    diff "$PEER" "$MINE" | grep '^<' >&2
    echo >&2
    echo "Review them. If they are your own superseded text, re-run with --force." >&2
    echo "If they are the peer's, merge them into $MINE first." >&2
    exit 1
fi

rm "$PEER"
ln "$MINE" "$PEER"

# Post-condition: the two names share an inode. NOT `links == 2` — that was the first
# version and it is the same reference error it is meant to catch. Link count answers
# "how many names does this inode have", which is a proxy that breaks the moment a
# third name exists (a backup, a test copy); the question is whether THESE two names
# are the same file. Found by trying to make the guard fail, which is the only way
# this kind of defect surfaces (AccuDisc correspondence §ap.1).
links=$(stat -c %h "$MINE")
[ "$(stat -c %i "$MINE")" = "$(stat -c %i "$PEER")" ] || {
    echo "relink did not take: different inodes" >&2; exit 3; }
cmp -s "$MINE" "$PEER" || {
    echo "same inode but cmp differs — impossible, investigate" >&2; exit 3; }
echo "relinked: inode $(stat -c %i "$MINE"), links=$links, byte-identical"
[ "$links" = "2" ] || echo "note: links=$links (expected 2) — another name exists" >&2

#!/usr/bin/env bash
#
# mkdist.sh — build the source distribution tarball.
#
#   make dist                 # the normal way in
#   bash tools/mkdist.sh      # equivalent
#   bash tools/mkdist.sh --allow-dirty
#
# Output: dist/cdda2img-<version>.tar.gz, unpacking into cdda2img-<version>/.
#
# ---------------------------------------------------------------------------
# WHY git archive AND NOT tar WITH AN EXCLUDE LIST
# ---------------------------------------------------------------------------
#
# `git archive` ships exactly the tracked files at a commit. That is not a
# convenience — it is the security property this script exists for. The repo
# sits alongside a large amount of material that must never be redistributed:
#
#   private/          licensed standards (IEC 60908), drive firmware, agent
#                     reports, non-redistributable sample disc images, the
#                     Guardian signing keyring
#   CLAUDE.local.md   machine-local paths and notes
#   .claude/ .remember/   local agent config and session history
#   tools/accudisc/   symlinks into a separate project's build tree
#   backups/ rips/ example/   working data, including actual disc audio
#
# All of it is gitignored, so none of it is tracked, so `git archive` cannot
# include it. With a hand-written `tar --exclude` list the guarantee runs the
# other way: everything is included unless someone remembered to exclude it, and
# the failure mode is silent — a correct-looking tarball that leaks. A new
# private directory added six months from now is covered here automatically and
# would not be there.
#
# The same mechanism gives "source only, no binaries" for free: compiled output
# (*.so, build/, dist/, __pycache__/) is gitignored too, and `tools/accudisc/` —
# which holds symlinks into AccuDisc's build tree — is ignored as a whole. There
# is no step here that strips binaries, because none can get in.
#
# ---------------------------------------------------------------------------
# WHY A DIRTY TREE IS AN ERROR
# ---------------------------------------------------------------------------
#
# `git archive HEAD` reads the commit, never the working tree. So an uncommitted
# fix is simply absent from the tarball, with no warning and no visible
# difference — you would ship the version before your change and have every
# reason to believe otherwise. Refusing to build is the only way that gets
# noticed. `--allow-dirty` exists for deliberate exceptions and says so loudly.
#
set -euo pipefail

ALLOW_DIRTY=0
[ "${1:-}" = "--allow-dirty" ] && ALLOW_DIRTY=1

if [ -t 2 ]; then
    C_B=$'\033[1m'; C_R=$'\033[31m'; C_Y=$'\033[33m'; C_0=$'\033[0m'
else
    C_B=""; C_R=""; C_Y=""; C_0=""
fi
say()  { printf '%s==>%s %s\n' "$C_B" "$C_0" "$*" >&2; }
warn() { printf '%swarning:%s %s\n' "$C_Y" "$C_0" "$*" >&2; }
die()  { printf '%serror:%s %s\n'   "$C_R" "$C_0" "$*" >&2; exit 1; }

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) \
    || die "not a git repository — this builds the tarball FROM the repo, and cannot run inside one it produced"
cd "$ROOT"

# The version lives in pyproject.toml and nowhere else (CLAUDE.md, "Key
# Constraints"), so read it from there rather than introducing a second copy.
VERSION=$(python3 - <<'PY'
import re, pathlib
text = pathlib.Path("pyproject.toml").read_text()
m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
print(m.group(1) if m else "")
PY
)
[ -n "$VERSION" ] || die "could not read version from pyproject.toml"

if [ -n "$(git status --porcelain)" ]; then
    if [ "$ALLOW_DIRTY" -eq 1 ]; then
        warn "working tree is dirty and --allow-dirty was given:"
        warn "the tarball is built from HEAD, so your uncommitted changes are NOT in it"
    else
        git status --short >&2
        die "working tree is dirty — commit first, or pass --allow-dirty (see the header)"
    fi
fi

NAME="cdda2img-$VERSION"
OUT="dist/$NAME.tar.gz"
mkdir -p dist

say "Building $OUT from $(git rev-parse --short HEAD)"
git archive --format=tar.gz --prefix="$NAME/" -o "$OUT" HEAD

# Report what went in. A tarball is opaque once written, and the one property
# worth confirming out loud is that nothing private came along.
COUNT=$(tar -tzf "$OUT" | wc -l)
SIZE=$(du -h "$OUT" | cut -f1)
say "$COUNT files, $SIZE"

# Belt and braces. `git archive` cannot include an untracked file, so this can
# only fire if something private was committed by mistake — which is exactly the
# case a guarantee-by-construction argument does not cover.
LEAKED=$(tar -tzf "$OUT" | sed "s|^$NAME/||" \
    | grep -E '^(private/|backups/|rips/|example/|\.claude/|\.remember/|CLAUDE\.local\.md|tools/accudisc/)' || true)
if [ -n "$LEAKED" ]; then
    printf '%s\n' "$LEAKED" >&2
    rm -f "$OUT"
    die "private paths found in the archive — tarball deleted. These are TRACKED files; fix the repo."
fi

printf '%s\n' "$OUT"

#!/usr/bin/env bash
#
# install.sh — install, uninstall and verify cdda2img in one command.
#
# This is a convenience wrapper around four steps you can run by hand (see
# README.md, "Installation"). It exists because only the first of them is
# obvious, the second is genuinely hard to get right, and skipping the fourth is
# how you find out three weeks later that the thing never worked.
#
#   ./install.sh                       # install
#   ./install.sh --prefix ~/.local     # ...with the man page somewhere else
#   ./install.sh uninstall             # remove it again
#   ./install.sh --print-binding       # just say where the AccuDisc wheel is
#   ./install.sh --help
#
# THE FOUR STEPS
#
#   1. The application    pipx install .            (always)
#   2. The disc engine    pipx inject cdda2img …    (if an AccuDisc wheel is found)
#   3. The man page       $PREFIX/share/man/man1/   (privileged if $PREFIX is)
#   4. The libmagic rule  ~/.magic.d/               (so `file` names .rbi images)
#
# Then it runs `cdda2img doctor`, which is the only step that can tell you
# whether the other four actually worked.
#
# ---------------------------------------------------------------------------
# WHY pipx AND NOT pip
# ---------------------------------------------------------------------------
#
# cdda2img is an application, not a library: nothing imports it, you run it. pipx
# gives it a private virtualenv with its own `av`, `numpy` and `ortools` — which
# are large, versioned, and exactly the packages a user is likely to already have
# at a different version for something else. `pip install --user` puts all of
# that on the user's own import path, where it can silently win against, or lose
# to, whatever else is there.
#
# It is also the reason step 2 is `pipx inject` rather than a second install:
# the AccuDisc binding has to land in *cdda2img's* environment, and with pipx
# that environment is a specific, nameable thing.
#
# The gate is real, not stylistic. `pipx inject` requires that `pipx install`
# created the venv, because it reads `pipx_metadata.json` to find it. A venv you
# made by hand in the same directory is inert to every pipx verb.
#
# ---------------------------------------------------------------------------
# STEP 2 — FINDING THE ACCUDISC WHEEL, AND WHY IT IS A SEARCH
# ---------------------------------------------------------------------------
#
# AccuDisc (https://github.com/HomerSlated/accudisc) is a separate project, and
# its Python binding is an API-mode cffi extension whose compiled .so records a
# RUNPATH pointing at the libaccudisc.so.0 it was built against. It is therefore
# only valid beside that library — which is why AccuDisc installs the wheel under
# its own prefix rather than to some fixed global location:
#
#     $PREFIX/share/accudisc/wheel/accudisc-<version>-cp310-abi3-<platform>.whl
#
# THE DIRECTORY IS THE CONTRACT, NOT THE FILENAME. The name carries the version
# and the ABI/platform tags and changes with both, so we glob and we never
# hardcode a filename.
#
# We look, in order:
#
#   a. --accudisc-prefix, if you passed one
#   b. `pkg-config --variable=wheeldir accudisc`
#   c. a short list of ordinary prefixes: /usr/local, /usr, ~/.local
#
# Route (b) is deliberately NOT the primary one, and is tested for a non-empty
# answer rather than for a zero exit status. Both halves of that are measured,
# not defensive:
#
#   - pkg-config does not search /usr/local/lib*/pkgconfig on every distribution,
#     so a perfectly good install can answer "no such package". Measured here.
#
#   - `pkg-config --variable=wheeldir accudisc` on this machine returns EXIT 0
#     AND AN EMPTY STRING, because the installed accudisc.pc predates the
#     wheeldir variable. An `if pkg-config ...` would take the success branch and
#     hand us "". Undefined and defined-empty are indistinguishable at the exit
#     code, so the exit code is the wrong thing to ask.
#
# NOT FINDING A WHEEL IS NOT AN ERROR. cdda2img's create, import, extract, list
# and test subcommands never touch a drive and need no engine at all; only rip,
# burn and mount do. So a missing wheel warns, names the two ways to fix it, and
# the install continues and succeeds.
#
# ---------------------------------------------------------------------------
# WHAT THIS SCRIPT WILL NOT TOUCH
# ---------------------------------------------------------------------------
#
# Your config. `$XDG_CONFIG_HOME/cdda2img/cdda2img.toml` holds drive read/write
# offsets that are measured per drive over multiple burn-and-read-back cycles.
# Overwriting one with a template would silently discard hours of measurement and
# then produce rips that are wrong by a few samples — an error with no symptom.
# `uninstall` leaves it alone for the same reason. Run `cdda2img setup` to create
# one, or copy conf/cdda2img.toml.example yourself.
#
# The recovery profiles are also not installed here: they ship *inside* the
# Python package, so pipx carries them along with the code. That is a change from
# earlier versions, where they lived in a top-level conf/ that the wheel did not
# package — so every installed copy had none of them and `rip` refused to start.
# `cdda2img doctor` now checks for them explicitly.
#
# ---------------------------------------------------------------------------
# PRIVILEGE
# ---------------------------------------------------------------------------
#
# Do not run this with sudo. Only the man page step can possibly need privilege,
# and only when $PREFIX is not writable by you; the script escalates around that
# one command by itself. Running the whole thing as root would create a pipx
# installation belonging to root, which is not the one your shell would find.
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
PREFIX=/usr/local
ACCUDISC_PREFIX=""
WANT_BINDING=1
WANT_MAN=1
WANT_MAGIC=1
WANT_DOCTOR=1
FORCE=0
DRY_RUN=0
ACTION=install

# The script's own directory, so it works from anywhere. Not $PWD: `cd /tmp &&
# ~/Git/cdda2img/install.sh` must install the checkout, not /tmp.
SRC_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)

# ---------------------------------------------------------------------------
# Output helpers. Diagnostics go to stderr so stdout stays usable for the one
# thing worth capturing (--print-binding).
# ---------------------------------------------------------------------------
if [ -t 2 ]; then
    C_B=$'\033[1m'; C_R=$'\033[31m'; C_Y=$'\033[33m'; C_G=$'\033[32m'; C_0=$'\033[0m'
else
    C_B=""; C_R=""; C_Y=""; C_G=""; C_0=""
fi
say()  { printf '%s==>%s %s\n' "$C_B" "$C_0" "$*" >&2; }
warn() { printf '%swarning:%s %s\n' "$C_Y" "$C_0" "$*" >&2; }
die()  { printf '%serror:%s %s\n'   "$C_R" "$C_0" "$*" >&2; exit 1; }
ok()   { printf '%s  ok%s %s\n'     "$C_G" "$C_0" "$*" >&2; }

# Echo a command, then run it — unless --dry-run, in which case only echo. Every
# filesystem-touching step goes through this, so --dry-run is a complete preview
# rather than an approximation of one.
run() {
    printf '    %s\n' "$*" >&2
    [ "$DRY_RUN" -eq 1 ] && return 0
    "$@"
}

usage() {
    cat <<'EOF'
usage: ./install.sh [options]             install cdda2img
       ./install.sh uninstall [options]   remove it again
       ./install.sh --print-binding       print the AccuDisc wheel path and exit

Options:
  --prefix DIR           prefix for the man page (default: /usr/local)
  --accudisc-prefix DIR  look for the AccuDisc wheel under DIR first
  --no-binding           skip the AccuDisc binding entirely
  --no-man               skip the man page
  --no-magic             skip the libmagic file-type rule
  --no-doctor            skip the post-install verification
  --force                pass --force to pipx (reinstall over an existing copy)
  -n, --dry-run          print every command without running any of them
  -h, --help             this text

Environment:
  CDDA2IMG_SUDO          privilege command to use (default: sudo, then doas)
EOF
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
    case "$1" in
        install|uninstall) ACTION=$1; shift ;;
        *) die "unknown command '$1' (expected: install, uninstall)" ;;
    esac
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)          [ $# -ge 2 ] || die "--prefix needs an argument"; PREFIX=$2; shift 2 ;;
        --accudisc-prefix) [ $# -ge 2 ] || die "--accudisc-prefix needs an argument"; ACCUDISC_PREFIX=$2; shift 2 ;;
        --no-binding)      WANT_BINDING=0; shift ;;
        --no-man)          WANT_MAN=0; shift ;;
        --no-magic)        WANT_MAGIC=0; shift ;;
        --no-doctor)       WANT_DOCTOR=0; shift ;;
        --force)           FORCE=1; shift ;;
        --print-binding)   ACTION=print-binding; shift ;;
        -n|--dry-run)      DRY_RUN=1; shift ;;
        -h|--help)         usage; exit 0 ;;
        *) die "unknown option '$1' (try --help)" ;;
    esac
done

MANDIR="$PREFIX/share/man/man1"

# ---------------------------------------------------------------------------
# Privilege: pick a command, and only if we actually need one
# ---------------------------------------------------------------------------
# Resolved lazily. A --prefix under $HOME needs nothing, and demanding sudo up
# front would fail an install that never had to escalate.
need_sudo() {
    local target=$1
    # Walk up to the nearest existing ancestor: a not-yet-created man1/ says
    # nothing about writability, but its parent does.
    while [ ! -e "$target" ] && [ "$target" != "/" ]; do
        target=$(dirname "$target")
    done
    [ ! -w "$target" ]
}

sudo_cmd() {
    if [ -n "${CDDA2IMG_SUDO:-}" ]; then
        printf '%s' "$CDDA2IMG_SUDO"
    elif command -v sudo >/dev/null 2>&1; then
        printf 'sudo'
    elif command -v doas >/dev/null 2>&1; then
        printf 'doas'
    else
        die "need to write $MANDIR but found neither sudo nor doas (try --prefix ~/.local, or --no-man)"
    fi
}

# ---------------------------------------------------------------------------
# Step 2's search. Prints the wheel path on stdout, or nothing.
# ---------------------------------------------------------------------------
find_accudisc_wheel() {
    local dir wheel

    # (a) explicit, (c) the ordinary prefixes. Same shape, so one loop.
    local -a dirs=()
    [ -n "$ACCUDISC_PREFIX" ] && dirs+=("$ACCUDISC_PREFIX/share/accudisc/wheel")

    # (b) pkg-config. Guarded on a NON-EMPTY answer, not on exit status: a .pc
    # file without the variable exits 0 and prints nothing. `|| true` because
    # `set -e` would otherwise abort the whole script on the ordinary "package
    # not found" case, which is not an error here — it is one route of three.
    local pcdir
    pcdir=$(pkg-config --variable=wheeldir accudisc 2>/dev/null || true)
    [ -n "$pcdir" ] && dirs+=("$pcdir")

    dirs+=("/usr/local/share/accudisc/wheel" "/usr/share/accudisc/wheel" "$HOME/.local/share/accudisc/wheel")

    for dir in "${dirs[@]}"; do
        [ -d "$dir" ] || continue
        # Newest last by version sort, so a prefix holding two versions after an
        # upgrade yields the newer one rather than an arbitrary one.
        wheel=$(ls -1 "$dir"/accudisc-*.whl 2>/dev/null | sort -V | tail -1)
        if [ -n "$wheel" ]; then
            printf '%s\n' "$wheel"
            return 0
        fi
    done
    return 1
}

# ---------------------------------------------------------------------------
# print-binding
# ---------------------------------------------------------------------------
if [ "$ACTION" = "print-binding" ]; then
    if wheel=$(find_accudisc_wheel); then
        printf '%s\n' "$wheel"
        exit 0
    fi
    die "no AccuDisc wheel found (searched --accudisc-prefix, pkg-config wheeldir, /usr/local, /usr, ~/.local)"
fi

command -v pipx >/dev/null 2>&1 || die "pipx not found — install it first (e.g. 'python3 -m pip install --user pipx')"

# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------
if [ "$ACTION" = "uninstall" ]; then
    say "Removing cdda2img"

    if pipx list --short 2>/dev/null | grep -q '^cdda2img '; then
        run pipx uninstall cdda2img
        ok "application removed (this takes the AccuDisc binding with it)"
    else
        warn "cdda2img is not installed via pipx — nothing to uninstall"
    fi

    manpage="$MANDIR/cdda2img.1"
    if [ -e "$manpage" ]; then
        if need_sudo "$manpage"; then
            run "$(sudo_cmd)" rm -f "$manpage"
        else
            run rm -f "$manpage"
        fi
        ok "man page removed"
    fi

    magicfile="$HOME/.magic.d/cdda2img.magic"
    if [ -e "$magicfile" ]; then
        run rm -f "$magicfile"
        ok "libmagic rule removed"
    fi

    say "Done. Your config was left alone:"
    printf '    %s\n' "${XDG_CONFIG_HOME:-$HOME/.config}/cdda2img/cdda2img.toml" >&2
    exit 0
fi

# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------
[ -f "$SRC_DIR/pyproject.toml" ] || die "$SRC_DIR does not look like the cdda2img source tree"

say "1/4  Installing the application (pipx)"
if [ "$FORCE" -eq 1 ]; then
    run pipx install --force "$SRC_DIR"
elif pipx list --short 2>/dev/null | grep -q '^cdda2img '; then
    warn "cdda2img is already installed; reinstalling over it"
    run pipx install --force "$SRC_DIR"
else
    run pipx install "$SRC_DIR"
fi
ok "cdda2img installed"

say "2/4  Installing the AccuDisc binding (disc engine)"
if [ "$WANT_BINDING" -eq 0 ]; then
    warn "skipped (--no-binding); rip, burn and mount will not work"
elif wheel=$(find_accudisc_wheel); then
    printf '    found %s\n' "$wheel" >&2
    run pipx inject cdda2img "$wheel"
    ok "disc engine available"
else
    # Not fatal, by design — see the header. Everything that does not touch a
    # drive works without this.
    warn "no AccuDisc wheel found."
    cat >&2 <<'EOF'
      rip, burn and mount need it; create, import, extract, list and test do not.
      To add it later:
          cd /path/to/accudisc && ./install.sh          # builds and installs the wheel
          cd /path/to/cdda2img && ./install.sh --no-man # re-run; it will find it
      Or, if you already have a wheel somewhere:
          pipx inject cdda2img /path/to/accudisc-*.whl
EOF
fi

say "3/4  Installing the man page"
if [ "$WANT_MAN" -eq 0 ]; then
    warn "skipped (--no-man)"
elif [ ! -f "$SRC_DIR/docs/man/cdda2img.1" ]; then
    warn "docs/man/cdda2img.1 not found in the source tree — skipping"
else
    if need_sudo "$MANDIR"; then
        SUDO=$(sudo_cmd)
        run "$SUDO" install -d "$MANDIR"
        run "$SUDO" install -m 644 "$SRC_DIR/docs/man/cdda2img.1" "$MANDIR/"
    else
        run install -d "$MANDIR"
        run install -m 644 "$SRC_DIR/docs/man/cdda2img.1" "$MANDIR/"
    fi
    ok "man cdda2img"
fi

say "4/4  Installing the libmagic rule"
# Teaches `file` to recognise .rbi containers by their RBIMAGE\0 magic. Purely
# cosmetic, entirely within $HOME, and hence never privileged: ~/.magic.d is
# searched by libmagic automatically, whereas the system magic directory belongs
# to the distribution's `file` package.
if [ "$WANT_MAGIC" -eq 0 ]; then
    warn "skipped (--no-magic)"
elif [ ! -f "$SRC_DIR/conf/magic" ]; then
    warn "conf/magic not found in the source tree — skipping"
else
    run install -d "$HOME/.magic.d"
    run install -m 644 "$SRC_DIR/conf/magic" "$HOME/.magic.d/cdda2img.magic"
    ok "file(1) will now identify .rbi images"
fi

# ---------------------------------------------------------------------------
# Verify. This is the step that distinguishes "the commands ran" from "the
# result works" — the only one that can catch a wheel that installed but cannot
# load its library, or a package whose data files did not come along.
# ---------------------------------------------------------------------------
if [ "$WANT_DOCTOR" -eq 1 ] && [ "$DRY_RUN" -eq 0 ]; then
    say "Verifying"
    if command -v cdda2img >/dev/null 2>&1; then
        # Deliberately not fatal: doctor exits 1 for a missing *required*
        # dependency, and a machine with no disc engine is a legitimate,
        # useful install. Report its verdict, do not adopt it as ours.
        cdda2img doctor || warn "doctor reported something missing (see above)"
    else
        warn "cdda2img is not on \$PATH — you may need: pipx ensurepath"
    fi
fi

say "Done."
cat >&2 <<'EOF'
    Next:
        cdda2img setup      create a config and measure your drive's offsets
        man cdda2img        the full manual
EOF

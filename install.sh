#!/bin/sh
# gh-widgets — install the renderers and their shared modules.
#
# The three renderers share ghwidgets_common.py and assert its COMMON_VERSION
# at startup, so they must be deployed together. Copying one without the other
# leaves a working-looking install that refuses to run (by design). render-
# impact.py also loads impact_loc.py beside itself. This script stages all six
# into the target directory and moves them into place, so a
# partial copy is not something you can do by accident.
#
#   ./install.sh                    # -> /usr/local/bin
#   ./install.sh /opt/gh-widgets    # -> anywhere else
#
# Note the rename: render.py installs as render-gh-widgets.py, which is the
# name the systemd units use. The scripts locate the shared module relative to
# their own file, so the rename is safe.
set -eu

# --units additionally installs the systemd units from units/ and reloads
# systemd. It is opt-in because installing the renderers is useful on any box,
# while the units carry this deployment's paths and schedule and need root.
WITH_UNITS=0
while [ $# -gt 0 ]; do
    case "$1" in
        --units) WITH_UNITS=1; shift ;;
        --)      shift; break ;;
        -*)      echo "install.sh: unknown option $1" >&2; exit 2 ;;
        *)       break ;;
    esac
done

DEST="${1:-/usr/local/bin}"
SRC="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
UNIT_DIR=/etc/systemd/system
UNITS="gh-widgets.service gh-widgets.timer gh-widgets-resync.service gh-widgets-resync.timer"

# source -> installed name
set -- \
    "render.py:render-gh-widgets.py" \
    "render-impact.py:render-impact.py" \
    "impact_loc.py:impact_loc.py" \
    "render-responsiveness.py:render-responsiveness.py" \
    "ghwidgets_common.py:ghwidgets_common.py" \
    "ghwidgets_data.py:ghwidgets_data.py"

# Verify every source exists BEFORE touching the destination, so a missing
# file aborts the whole install rather than half-applying it.
for pair in "$@"; do
    src="${pair%%:*}"
    if [ ! -f "$SRC/$src" ]; then
        echo "install.sh: missing $SRC/$src — refusing a partial install" >&2
        exit 1
    fi
done

if [ ! -d "$DEST" ]; then
    echo "install.sh: $DEST is not a directory" >&2
    exit 1
fi

# Stage under temporary names in the destination filesystem, then mv into
# place: mv within one filesystem is atomic, so no renderer ever observes a
# half-written file.
staged=""
trap 'for f in $staged; do rm -f "$f"; done' EXIT INT TERM

for pair in "$@"; do
    src="${pair%%:*}"
    dst="${pair#*:}"
    cp -- "$SRC/$src" "$DEST/.$dst.new"
    chmod 0755 "$DEST/.$dst.new"
    staged="$staged $DEST/.$dst.new"
done

for pair in "$@"; do
    dst="${pair#*:}"
    mv -- "$DEST/.$dst.new" "$DEST/$dst"
    echo "installed $DEST/$dst"
done

staged=""

# Prove the install is coherent rather than assuming it: every entry point
# loads the shared module and checks its version on startup, so --help
# exercises exactly the failure this script exists to prevent.
for dst in render-gh-widgets.py render-impact.py render-responsiveness.py; do
    if ! "$DEST/$dst" --help >/dev/null 2>&1; then
        echo "install.sh: $DEST/$dst failed to start after install" >&2
        exit 1
    fi
done
if ! PYTHONPATH="$DEST${PYTHONPATH:+:$PYTHONPATH}" \
        python3 -c 'import ghwidgets_data' >/dev/null 2>&1; then
    echo "install.sh: ghwidgets_data.py failed to import after install" >&2
    exit 1
fi
echo "verified: all renderers start and the public data module imports"

# render-impact.py's blame pass needs the parallel-blame git-fame build; stock
# git-fame blames one file per subprocess serially. Degraded, not broken — so
# warn rather than fail.
if command -v git-fame >/dev/null 2>&1; then
    # git-fame, NOT `git fame`: `git <cmd> --help` is rewritten by git into
    # `man git-<cmd>`, which reports no manual entry and hides the option.
    if ! git-fame --help 2>/dev/null | grep -q -- '--jobs'; then
        echo "install.sh: WARNING - installed git-fame has no --jobs; render-impact" >&2
        echo "                     will be several times slower. See CLAUDE.md for the pin." >&2
    fi
else
    echo "install.sh: WARNING - git-fame is not installed; render-impact cannot blame" >&2
fi

[ "$WITH_UNITS" -eq 1 ] || exit 0

# ---- systemd units -------------------------------------------------------
# One hourly unit renders every SVG in sequence; one weekly unit does the same
# with --resync and is the only thing that ignores the caches. They replaced
# five service/timer pairs whose ordering lived in `After=` chains.
if [ "$(id -u)" -ne 0 ]; then
    echo "install.sh: --units needs root to write $UNIT_DIR" >&2
    exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
    echo "install.sh: --units given but systemctl is not available" >&2
    exit 1
fi

# Same discipline as the renderers: verify every source before touching the
# destination, so a missing file aborts rather than half-applying.
for u in $UNITS; do
    if [ ! -f "$SRC/units/$u" ]; then
        echo "install.sh: missing $SRC/units/$u — refusing a partial unit install" >&2
        exit 1
    fi
done

staged=""
trap 'for f in $staged; do rm -f "$f"; done' EXIT INT TERM
for u in $UNITS; do
    cp -- "$SRC/units/$u" "$UNIT_DIR/.$u.new"
    chmod 0644 "$UNIT_DIR/.$u.new"
    staged="$staged $UNIT_DIR/.$u.new"
done
for u in $UNITS; do
    mv -- "$UNIT_DIR/.$u.new" "$UNIT_DIR/$u"
    echo "installed $UNIT_DIR/$u"
done
staged=""

systemctl daemon-reload
systemctl enable --now gh-widgets.timer gh-widgets-resync.timer >/dev/null

# Prove the units are loadable rather than assuming it: a unit with a typo
# installs fine and only fails when its timer next fires, which may be an hour
# of silence away.
for u in $UNITS; do
    if ! systemctl cat "$u" >/dev/null 2>&1; then
        echo "install.sh: $u did not load after daemon-reload" >&2
        exit 1
    fi
done
echo "verified: units loaded; timers enabled"
systemctl list-timers gh-widgets.timer gh-widgets-resync.timer --no-pager 2>/dev/null | head -4

#!/bin/sh
# gh-widgets — install the renderers and their shared module.
#
# The three renderers share ghwidgets_common.py and assert its COMMON_VERSION
# at startup, so they must be deployed together. Copying one without the other
# leaves a working-looking install that refuses to run (by design). This script
# stages all four into the target directory and moves them into place, so a
# partial copy is not something you can do by accident.
#
#   ./install.sh                    # -> /usr/local/bin
#   ./install.sh /opt/gh-widgets    # -> anywhere else
#
# Note the rename: render.py installs as render-gh-widgets.py, which is the
# name the systemd units use. The scripts locate the shared module relative to
# their own file, so the rename is safe.
set -eu

DEST="${1:-/usr/local/bin}"
SRC="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

# source -> installed name
set -- \
    "render.py:render-gh-widgets.py" \
    "render-impact.py:render-impact.py" \
    "render-responsiveness.py:render-responsiveness.py" \
    "ghwidgets_common.py:ghwidgets_common.py"

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
echo "verified: all renderers start and agree on COMMON_VERSION"

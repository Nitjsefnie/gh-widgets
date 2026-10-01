#!/bin/sh
# gh-widgets — install the renderers and their shared modules.
#
# The three renderers share ghwidgets_common.py and assert its COMMON_VERSION
# at startup, so they must be deployed together. Copying one without the other
# leaves a working-looking install that refuses to run (by design). render-
# impact.py also loads impact_loc.py beside itself. The EXIT trap rolls back
# an interrupted or failed commit: when the trap can run, the result is the
# complete prior set or the complete verified new set, never a mixed set.
# Each same-filesystem mv is atomic, but the six-file commit is not one atomic
# operation: SIGKILL or power loss cannot run a shell trap and can still leave
# a mixed set during commit. Re-running this installer repairs that state.
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

# The commit lists contain target paths, one per line; newlines in DEST are
# unsupported. Keep backups until every requested verification has passed.
committed=""
staged=""
rollback_needed=0
unit_commit_started=0
timer_enable_attempted=0
timers_enabled_before=""
timers_disabled_before=""
finalizing=0

cleanup_install() {
    saved_status=$?
    trap - EXIT INT TERM || :

    if [ "$rollback_needed" -eq 1 ] && [ "$finalizing" -eq 0 ]; then
        while IFS= read -r target || [ -n "$target" ]; do
            [ -n "$target" ] || continue
            target_dir=${target%/*}
            target_name=${target##*/}
            backup="$target_dir/.$target_name.old"
            new_file="$target_dir/.$target_name.new"
            if [ -e "$backup" ]; then
                if mv -- "$backup" "$target"; then :; fi
            elif [ ! -e "$new_file" ]; then
                rm -f -- "$target" || :
            fi
        done <<EOF
$committed
EOF

        # Reload restored units and undo only timer enables introduced by this
        # install. Every command is best-effort so set -e cannot stop rollback.
        if [ "$unit_commit_started" -eq 1 ]; then
            systemctl daemon-reload >/dev/null 2>&1 || :
            if [ "$timer_enable_attempted" -eq 1 ] &&
                    [ -n "$timers_disabled_before" ]; then
                systemctl disable --now $timers_disabled_before >/dev/null 2>&1 || :
            fi
        fi
    elif [ "$finalizing" -eq 1 ]; then
        # Verification has committed the new set. If interrupted while old
        # backups are being discarded, keep the complete new set in place.
        while IFS= read -r target || [ -n "$target" ]; do
            [ -n "$target" ] || continue
            target_dir=${target%/*}
            target_name=${target##*/}
            rm -f -- "$target_dir/.$target_name.old" || :
        done <<EOF
$committed
EOF
    fi

    while IFS= read -r staged_file || [ -n "$staged_file" ]; do
        [ -n "$staged_file" ] || continue
        rm -f -- "$staged_file" || :
    done <<EOF
$staged
EOF
    exit "$saved_status"
}

finish_install() {
    # Mark the verified set committed before deleting backups. An interrupt
    # during backup cleanup must keep the complete new set, not try to roll
    # back from an already-partly-deleted previous set.
    finalizing=1
    while IFS= read -r target || [ -n "$target" ]; do
        [ -n "$target" ] || continue
        target_dir=${target%/*}
        target_name=${target##*/}
        rm -f -- "$target_dir/.$target_name.old" || :
    done <<EOF
$committed
EOF
    rollback_needed=0
    committed=""
    finalizing=0
}

trap cleanup_install EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

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
for pair in "$@"; do
    src="${pair%%:*}"
    dst="${pair#*:}"
    staged_file="$DEST/.$dst.new"
    if [ -n "$staged" ]; then staged="$staged
$staged_file"; else staged="$staged_file"; fi
    cp -- "$SRC/$src" "$staged_file"
    chmod 0755 "$staged_file"
done

rollback_needed=1
for pair in "$@"; do
    dst="${pair#*:}"
    target="$DEST/$dst"
    if [ -n "$committed" ]; then committed="$committed
$target"; else committed="$target"; fi
    if [ -e "$target" ]; then
        mv -- "$target" "$DEST/.$dst.old"
    fi
    mv -- "$DEST/.$dst.new" "$target"
    echo "installed $target"
done

staged=""

# Prove the install is coherent rather than assuming it: every entry point
# loads the shared module and checks its version on startup, so --help
# exercises exactly the failure this script exists to prevent.
for dst in render-gh-widgets.py render-impact.py render-responsiveness.py; do
    if ! PYTHONDONTWRITEBYTECODE=1 "$DEST/$dst" --help >/dev/null 2>&1; then
        echo "install.sh: $DEST/$dst failed to start after install" >&2
        exit 1
    fi
done
if ! PYTHONDONTWRITEBYTECODE=1 \
        PYTHONPATH="$DEST${PYTHONPATH:+:$PYTHONPATH}" \
        python3 -c 'import ghwidgets_data' >/dev/null 2>&1; then
    echo "install.sh: ghwidgets_data.py failed to import after install" >&2
    exit 1
fi
echo "verified: all renderers start and the public data module imports"

# In bare mode this is the complete transaction. --units keeps these backups
# until the unit files also pass systemd's load check.
if [ "$WITH_UNITS" -eq 0 ]; then
    finish_install
fi

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
# One hourly unit renders every SVG in sequence; one weekly unit reruns the
# same three renderers, passing --resync only to the two that accept it. These
# replaced five service/timer pairs whose ordering lived in `After=` chains.
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

for u in $UNITS; do
    staged_file="$UNIT_DIR/.$u.new"
    if [ -n "$staged" ]; then staged="$staged
$staged_file"; else staged="$staged_file"; fi
    cp -- "$SRC/units/$u" "$staged_file"
    chmod 0644 "$staged_file"
done
unit_commit_started=1
for u in $UNITS; do
    target="$UNIT_DIR/$u"
    if [ -n "$committed" ]; then committed="$committed
$target"; else committed="$target"; fi
    if [ -e "$target" ]; then
        mv -- "$target" "$UNIT_DIR/.$u.old"
    fi
    mv -- "$UNIT_DIR/.$u.new" "$target"
    echo "installed $target"
done
staged=""

systemctl daemon-reload
# Record each timer's pre-install enablement before enable --now. On rollback,
# only timers disabled beforehand are disabled again.
for timer in gh-widgets.timer gh-widgets-resync.timer; do
    if systemctl is-enabled "$timer" >/dev/null 2>&1; then
        if [ -n "$timers_enabled_before" ]; then
            timers_enabled_before="$timers_enabled_before $timer"
        else
            timers_enabled_before="$timer"
        fi
    else
        if [ -n "$timers_disabled_before" ]; then
            timers_disabled_before="$timers_disabled_before $timer"
        else
            timers_disabled_before="$timer"
        fi
    fi
done
timer_enable_attempted=1
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
finish_install
systemctl list-timers gh-widgets.timer gh-widgets-resync.timer --no-pager 2>/dev/null | head -4

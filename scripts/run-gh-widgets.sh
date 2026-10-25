#!/usr/bin/env bash
# Run the serial GraphQL chain alongside the independent popularity card.
set -uo pipefail

resync=()
case "${1:-}" in
    normal) ;;
    resync) resync=(--resync) ;;
    *) echo 'usage: run-gh-widgets.sh normal|resync' >&2; exit 2 ;;
esac

run_step() {
    local name=$1 status
    shift
    "$@"
    status=$?
    if [ "$status" -ne 0 ]; then
        printf 'gh-widgets: %s failed (exit %s)\n' "$name" "$status" >&2
        return 1
    fi
}

skipped() {
    local name
    for name in "$@"; do
        printf 'gh-widgets: %s skipped (dependency failed)\n' "$name" >&2
    done
}

lookup() {
    local token orgs orgs_start orgs_us
    if [ -n "${GH_WIDGETS_DEBUG_TIMING:-}" ]; then orgs_start=${EPOCHREALTIME/./}; fi
    token=$(cat -- "${GH_WIDGETS_GH_TOKEN_FILE-$GH_TOKEN_FILE}") || return 1
    orgs=$(GH_TOKEN="$token" /usr/local/bin/gh api --paginate user/orgs --jq '.[].login' | paste -sd, -) || return 1
    if [ -n "${GH_WIDGETS_DEBUG_TIMING:-}" ]; then
        orgs_us=$((${EPOCHREALTIME/./} - orgs_start))
        printf 'orgs-lookup: %d.%06ds\n' "$((orgs_us / 1000000))" "$((orgs_us % 1000000))"
    fi
    export GH_WIDGETS_GH_EXTRA_ORGS="$orgs"
}

graphql_chain() {
    if ! run_step lookup lookup; then
        skipped profile impact responsiveness stars
        return 1
    fi
    if ! run_step profile /usr/local/bin/render-gh-widgets.py "${resync[@]}"; then
        skipped impact responsiveness stars
        return 1
    fi
    if ! run_step impact /usr/local/bin/render-impact.py "${resync[@]}" --cache-file /var/lib/gh-widgets/impact-cache.json; then
        skipped responsiveness stars
        return 1
    fi
    if ! run_step responsiveness /usr/local/bin/render-responsiveness.py --cache-file /var/lib/gh-widgets/impact-cache.json; then
        skipped stars
        return 1
    fi
    run_step stars /usr/local/bin/render-stars.py --cache-file /var/lib/gh-widgets/stars-cache.json
}

# Only this chain runs GraphQL renderers: their per-process cap of 2 is also
# the unit-wide cap. Stars runs last to keep the shared-data chain together.
graphql_chain &
chain_pid=$!
run_step popularity /usr/local/bin/render-popularity.py &
popularity_pid=$!

status=0
for pid in "$chain_pid" "$popularity_pid"; do
    wait "$pid" || status=1
done
exit "$status"

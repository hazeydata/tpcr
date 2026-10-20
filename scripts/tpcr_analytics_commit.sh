#!/bin/bash
set -u

usage() {
    cat <<'USAGE'
Usage: tpcr_analytics_commit.sh [--dry-run] [-- REGEN_CMD [ARGS...]]

Runs REGEN_CMD (default: $TPCR_PYTHON scripts/generate_analytics_json.py) and
commits docs/analytics-data only if it exits 0. With --dry-run nothing is
regenerated or written: it prints the pending diff summary and the commit it
would make.

Env (names only, values never logged):
  TPCR_GIT_AUTHOR_NAME, TPCR_GIT_AUTHOR_EMAIL   commit identity (required)
  TPCR_REPO     repo root (default: parent of this script's dir)
  TPCR_REMOTE   remote name (default: origin)
  TPCR_BRANCH   branch to push (default: current branch)
  TPCR_PYTHON   python for the default regen (default: python3)

Exit: 0 ok or nothing to do; regen rc if regen failed; 3 config error;
      4 commit failed; 5 push failed (local commit kept, pushed next run).
USAGE
}

ANALYTICS_PATH="docs/analytics-data"
DRY_RUN=0
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --) shift; break ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 3 ;;
    esac
done

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO=${TPCR_REPO:-$(dirname "$SCRIPT_DIR")}
REMOTE=${TPCR_REMOTE:-origin}
GIT_BIN=$(type -P git)
export GIT_OPTIONAL_LOCKS=0

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] tpcr_analytics_commit: $*" >&2; }
g() { "$GIT_BIN" -C "$REPO" "$@"; }

[ -n "$GIT_BIN" ] || { log "git not found"; exit 3; }
g rev-parse --git-dir >/dev/null 2>&1 || { log "not a git repo: $REPO"; exit 3; }
BRANCH=${TPCR_BRANCH:-$(g symbolic-ref --short -q HEAD)}
[ -n "$BRANCH" ] || { log "detached HEAD and TPCR_BRANCH unset"; exit 3; }

if [ -z "${TPCR_GIT_AUTHOR_NAME:-}" ] || [ -z "${TPCR_GIT_AUTHOR_EMAIL:-}" ]; then
    log "TPCR_GIT_AUTHOR_NAME and TPCR_GIT_AUTHOR_EMAIL must be set"
    exit 3
fi

MSG="Pipeline post-run: Analytics JSON updates $(date '+%Y-%m-%d')"

unpushed_count() {
    if g rev-parse -q --verify "refs/remotes/$REMOTE/$BRANCH" >/dev/null; then
        g rev-list --count "refs/remotes/$REMOTE/$BRANCH..HEAD"
    else
        g rev-list --count HEAD
    fi
}

pending_changes() {
    ! g diff --quiet HEAD -- "$ANALYTICS_PATH" || [ -n "$(g ls-files --others --exclude-standard -- "$ANALYTICS_PATH")" ]
}

if [ "$DRY_RUN" -eq 1 ]; then
    echo "dry-run: repo=$REPO branch=$BRANCH remote=$REMOTE"
    echo "dry-run: unpushed local commits: $(unpushed_count)"
    if pending_changes; then
        echo "dry-run: changes under $ANALYTICS_PATH:"
        g diff --stat HEAD -- "$ANALYTICS_PATH"
        g ls-files --others --exclude-standard -- "$ANALYTICS_PATH" | sed 's/^/ new: /'
        echo "dry-run: would commit as $TPCR_GIT_AUTHOR_NAME <$TPCR_GIT_AUTHOR_EMAIL>: $MSG"
    else
        echo "dry-run: no changes under $ANALYTICS_PATH; no commit"
    fi
    echo "dry-run: nothing written"
    exit 0
fi

if [ $# -eq 0 ]; then
    set -- "${TPCR_PYTHON:-python3}" scripts/generate_analytics_json.py
fi
log "regen: $*"
(cd "$REPO" && "$@")
rc=$?
if [ "$rc" -ne 0 ]; then
    log "regen exited $rc; not committing partial output"
    exit "$rc"
fi

if pending_changes; then
    g add -A -- "$ANALYTICS_PATH" || { log "git add failed"; exit 4; }
    if g diff --cached --quiet -- "$ANALYTICS_PATH"; then
        log "no staged changes after add"
    elif ! GIT_AUTHOR_NAME="$TPCR_GIT_AUTHOR_NAME" GIT_AUTHOR_EMAIL="$TPCR_GIT_AUTHOR_EMAIL" \
         GIT_COMMITTER_NAME="$TPCR_GIT_AUTHOR_NAME" GIT_COMMITTER_EMAIL="$TPCR_GIT_AUTHOR_EMAIL" \
         g commit -q -m "$MSG" -- "$ANALYTICS_PATH"; then
        log "commit failed"
        exit 4
    else
        log "committed $(g rev-parse --short HEAD)"
    fi
else
    log "no changes under $ANALYTICS_PATH"
fi

n=$(unpushed_count)
if [ "$n" -eq 0 ]; then
    log "nothing to push"
    exit 0
fi
if g push -q "$REMOTE" "HEAD:refs/heads/$BRANCH"; then
    log "pushed $n commit(s) to $REMOTE/$BRANCH"
    exit 0
fi
log "push failed; $n local commit(s) kept for the next run"
exit 5

#!/bin/bash
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$(cd "$HERE/../.." && pwd)/scripts/tpcr_analytics_commit.sh
SH=${TEST_BASH:-bash}
GIT_BIN=$(type -P git)
W=$(mktemp -d "${TMPDIR:-/tmp}/analytics-commit.XXXXXX")
FAILS=0
PASSES=0

ok() { PASSES=$((PASSES + 1)); echo "ok - $1"; }
bad() { FAILS=$((FAILS + 1)); echo "not ok - $1"; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (want '$3' got '$2')"; fi; }

export GIT_CONFIG_NOSYSTEM=1
export HOME="$W/home"
mkdir -p "$HOME"
"$GIT_BIN" config --global init.defaultBranch main
"$GIT_BIN" config --global user.name fixture
"$GIT_BIN" config --global user.email fixture@example.invalid

setup() {
    rm -rf "$W/remote.git" "$W/repo"
    "$GIT_BIN" init -q --bare "$W/remote.git"
    "$GIT_BIN" init -q "$W/repo"
    mkdir -p "$W/repo/docs/analytics-data" "$W/repo/scripts"
    echo '{"v":0}' > "$W/repo/docs/analytics-data/a.json"
    echo keep > "$W/repo/other.txt"
    cp "$SRC" "$W/repo/scripts/tpcr_analytics_commit.sh"
    "$GIT_BIN" -C "$W/repo" add -A
    "$GIT_BIN" -C "$W/repo" commit -q -m init
    "$GIT_BIN" -C "$W/repo" remote add origin "$W/remote.git"
    "$GIT_BIN" -C "$W/repo" push -q -u origin main
}

FAKE_TOKEN="fixture-push-token-$$-not-real"
export TPCR_GIT_PUSH_TOKEN="$FAKE_TOKEN"

run() {
    TPCR_REPO="$W/repo" TPCR_GIT_AUTHOR_NAME="Analytics Bot" TPCR_GIT_AUTHOR_EMAIL="bot@example.invalid" \
        "$SH" "$W/repo/scripts/tpcr_analytics_commit.sh" "$@" >"$W/out" 2>&1
}

count() { "$GIT_BIN" -C "$W/repo" rev-list --count HEAD; }
rcount() { "$GIT_BIN" --git-dir="$W/remote.git" rev-list --count main; }

n=$(grep -Eic 'ssh|scp|wilma|192\.168\.2\.75' "$SRC")
check "AC1 no ssh/scp/wilma references" "$n" "0"

setup
run -- sh -c 'echo "{\"v\":1}" > docs/analytics-data/a.json; exit 7'
rc=$?
check "AC2 regen failure propagates rc" "$rc" "7"
check "AC2 no commit on failed regen" "$(count)" "1"

setup
run -- true
rc=$?
check "AC3 no changes exits 0" "$rc" "0"
check "AC3 no commit when no changes" "$(count)" "1"

setup
run -- sh -c 'echo "{\"v\":2}" > docs/analytics-data/a.json; echo x > docs/analytics-data/new.json; echo dirty > other.txt'
rc=$?
check "commit+push rc" "$rc" "0"
check "one commit added" "$(count)" "2"
check "pushed" "$(rcount)" "2"
check "AC4 author from env" "$("$GIT_BIN" -C "$W/repo" log -1 --format='%an <%ae>')" "Analytics Bot <bot@example.invalid>"
check "only analytics path committed" "$("$GIT_BIN" -C "$W/repo" show --name-only --format= HEAD | tr '\n' ' ')" "docs/analytics-data/a.json docs/analytics-data/new.json "
check "other files untouched" "$("$GIT_BIN" -C "$W/repo" status --porcelain -- other.txt)" " M other.txt"

setup
TPCR_REPO="$W/repo" "$SH" "$W/repo/scripts/tpcr_analytics_commit.sh" -- true >"$W/out" 2>&1
check "AC4 missing identity is a config error" "$?" "3"

setup
"$GIT_BIN" -C "$W/repo" remote set-url origin "$W/missing.git"
run -- sh -c 'echo "{\"v\":3}" > docs/analytics-data/a.json'
rc=$?
check "AC5 push failure exits non-zero" "$rc" "5"
check "AC5 local commit kept" "$(count)" "2"
grep -q "push failed" "$W/out" && ok "AC5 push failure logged" || bad "AC5 push failure logged"
"$GIT_BIN" -C "$W/repo" remote set-url origin "$W/remote.git"
run -- true
rc=$?
check "AC5 retry rc" "$rc" "0"
check "AC5 retry made no duplicate commit" "$(count)" "2"
check "AC5 retry pushed the kept commit" "$(rcount)" "2"

setup
echo '{"v":9}' > "$W/repo/docs/analytics-data/a.json"
echo y > "$W/repo/docs/analytics-data/b.json"
idx_before=$(cksum < "$W/repo/.git/index")
run --dry-run -- sh -c 'touch REGEN_RAN'
rc=$?
check "AC6 dry-run rc" "$rc" "0"
grep -q "a.json" "$W/out" && grep -q "new: docs/analytics-data/b.json" "$W/out" && ok "AC6 diff summary shown" || bad "AC6 diff summary shown"
grep -q "would commit as Analytics Bot" "$W/out" && ok "AC6 would-be commit shown" || bad "AC6 would-be commit shown"
check "AC6 no commit" "$(count)" "1"
check "AC6 index untouched" "$(cksum < "$W/repo/.git/index")" "$idx_before"
[ ! -e "$W/repo/REGEN_RAN" ] && ok "AC6 regen not run" || bad "AC6 regen not run"
check "AC6 nothing pushed" "$(rcount)" "1"

setup
git() { echo wrapped >&2; command git "$@"; }
export -f git 2>/dev/null
run -- true
check "git shell-function wrapper does not break no-change path" "$?" "0"
unset -f git

setup
TPCR_GIT_PUSH_TOKEN=fixture-not-a-secret-value run -- sh -c 'echo "{\"v\":4}" > docs/analytics-data/a.json'
grep -q "fixture-not-a-secret-value" "$W/out" && bad "AC4 no env values in logs" || ok "AC4 no env values in logs"

setup
"$GIT_BIN" -C "$W/repo" add -A -- other.txt
echo staged > "$W/repo/other.txt"
"$GIT_BIN" -C "$W/repo" add other.txt
run -- sh -c 'echo "{\"v\":5}" > docs/analytics-data/a.json'
check "pre-staged non-analytics file not committed" "$("$GIT_BIN" -C "$W/repo" show --name-only --format= HEAD | tr '\n' ' ')" "docs/analytics-data/a.json "
check "pre-staged file stays staged" "$("$GIT_BIN" -C "$W/repo" diff --cached --name-only)" "other.txt"

setup
run -- sh -c 'echo n > docs/analytics-data/only-new.json'
check "regen adding only new files commits" "$?" "0"
check "new-file-only commit made" "$(count)" "2"
check "new-file-only commit content" "$("$GIT_BIN" -C "$W/repo" show --name-only --format= HEAD | tr '\n' ' ')" "docs/analytics-data/only-new.json "

setup
env -u TPCR_GIT_PUSH_TOKEN TPCR_REPO="$W/repo" TPCR_GIT_AUTHOR_NAME=a TPCR_GIT_AUTHOR_EMAIL=a@example.invalid \
    "$SH" "$W/repo/scripts/tpcr_analytics_commit.sh" -- sh -c 'touch REGEN_RAN' >"$W/out" 2>&1
check "missing token fails closed with 6" "$?" "6"
[ ! -e "$W/repo/REGEN_RAN" ] && ok "missing token: regen not run" || bad "missing token: regen not run"
check "missing token: no commit" "$(count)" "1"

setup
mkdir -p "$W/fakebin"
cat > "$W/fakebin/git" <<FAKE
#!/bin/bash
printf '%s\n' "\$*" >> "$W/argv.log"
args=()
for a in "\$@"; do
    if [ "\$a" = push ]; then
        printf 'protocol=https\nhost=github.com\npath=hazeydata/tpcr.git\n\n' | "$GIT_BIN" "\${args[@]}" credential fill > "$W/cred.out" 2>&1
        exit 1
    fi
    args+=("\$a")
done
exec "$GIT_BIN" "\$@"
FAKE
chmod +x "$W/fakebin/git"
"$GIT_BIN" -C "$W/repo" remote set-url origin https://github.invalid/hazeydata/tpcr.git
PATH="$W/fakebin:$PATH" run -- sh -c 'echo "{\"v\":6}" > docs/analytics-data/a.json'
check "push via https with failing transport exits 5" "$?" "5"
grep -q "^password=$FAKE_TOKEN\$" "$W/cred.out" && ok "AC4 credential helper supplies token from env" || bad "AC4 credential helper supplies token from env"
leak=0
for f in "$W/out" "$W/argv.log" "$W/repo/.git/config" "$HOME/.gitconfig"; do
    grep -qF "$FAKE_TOKEN" "$f" 2>/dev/null && { leak=1; echo "token found in $f"; }
done
check "AC4 token absent from output, argv, .git/config, gitconfig" "$leak" "0"
grep -qF "$FAKE_TOKEN" "$W/repo/.git/FETCH_HEAD" "$W/repo/.git/logs/HEAD" 2>/dev/null && bad "AC4 token absent from git logs" || ok "AC4 token absent from git logs"
"$GIT_BIN" -C "$W/repo" remote get-url origin | grep -qF "$FAKE_TOKEN" && bad "AC4 token not in remote URL" || ok "AC4 token not in remote URL"
grep -qF "$FAKE_TOKEN" "$SRC" && bad "token literal in script" || ok "no token literal in script"

rm -rf "$W"
echo "passed=$PASSES failed=$FAILS"
[ "$FAILS" -eq 0 ]

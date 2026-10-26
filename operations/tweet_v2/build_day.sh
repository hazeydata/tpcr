#!/bin/bash
# usage: build_day.sh DATE OUTDIR --history WTI_PARQUET (--predicted JSON [--observed JSON] | --csv PATH) [--park-hours CSV] [--closures CSV] [--forecast-only] [--png-only]
# Builds the forecast post (F-DATE.png, F-DATE-390.png, F-DATE.mp4, F-DATE.alt.txt) and, if observed WTI is given,
# the scorecard reply (R-DATE.png, R-DATE-390.png, R-DATE.mp4, R-DATE.alt.txt). Fails loudly on missing numbers.
# Env: PY (python with pandas+pyarrow, default python3), PYIMG (python with Pillow for the end-frame check, default $PY), CHROME (default google-chrome), FFMPEG (default ffmpeg).
# Posts nothing and makes no network calls; operations/scripts/wti_*_tweet.py call it.
set -euo pipefail
H=$(cd "$(dirname "$0")" && pwd); PY=${PY:-python3}; CHROME=${CHROME:-google-chrome}; FFMPEG=${FFMPEG:-ffmpeg}
export CHROME FFMPEG
D=$1; OUT=$2; shift 2
PNGONLY=0; ARGS=()
while [ $# -gt 0 ]; do case $1 in
  --png-only) PNGONLY=1;;
  --forecast-only) ARGS+=("$1");;
  --csv|--history|--predicted|--observed|--park-hours|--closures) ARGS+=("$1" "$2"); shift;;
  *) echo "unknown arg $1" >&2; exit 2;; esac; shift; done
mkdir -p "$OUT"; OUT=$(cd "$OUT" && pwd)
"$PY" -c "import shutil,sys,tempfile; g=shutil.disk_usage(tempfile.gettempdir()).free/2**30; sys.exit(0 if g>=2 else 'low disk on tmp: %.1fG'%g)"
"$PY" "$H/cards.py" "$D" "$OUT" ${ARGS[@]+"${ARGS[@]}"}
# All four parks closed (TPCR-TWEET-V2-CLOSED-ROW-001 R1): no card, no post.
[ -f "$OUT/skipped_all_closed" ] && { echo "skipped_all_closed $D"; exit 0; }
PROF=$(mktemp -d "${TMPDIR:-/tmp}/tpcr-shot-XXXXXX"); trap 'rm -rf "$PROF"' EXIT
shot(){ timeout 90 "$CHROME" --headless=new --no-first-run --disable-extensions --no-default-browser-check --no-sandbox --disable-gpu --hide-scrollbars --window-size=1600,900 --virtual-time-budget=2000 --user-data-dir="$PROF" --screenshot="$2" "file://$1" >/dev/null 2>&1 || true
        [ -s "$2" ] || { echo "render failed: $2" >&2; exit 1; }
        "$FFMPEG" -y -loglevel error -i "$2" -vf scale=390:-1:flags=lanczos "${2%.png}-390.png"; }
for K in F R; do
  [ -f "$OUT/$K-$D.html" ] || continue
  shot "$OUT/$K-$D.html" "$OUT/$K-$D.png"
  if [ $PNGONLY = 0 ]; then
    if [ $K = F ]; then JS=$H/anim_forecast.js; V=F; else JS=$H/anim_reply.js; V=A; fi
    "$H/render_anim.sh" "$OUT/$K-$D.html" "$JS" $V 7.0 "$OUT/$K-$D.mp4" "$OUT/.$K-$D-lastframe.png"
    "${PYIMG:-$PY}" - "$OUT/$K-$D.png" "$OUT/.$K-$D-lastframe.png" <<'PYEOF'
import sys; from PIL import Image, ImageChops
a,b=[Image.open(p).convert("RGB") for p in sys.argv[1:]]
assert ImageChops.difference(a,b).getbbox() is None, f"animation end != static card: {sys.argv[1]}"
print("anim end frame == static PNG:", sys.argv[1])
PYEOF
    rm "$OUT/.$K-$D-lastframe.png"
  fi
done
ls -1 "$OUT"

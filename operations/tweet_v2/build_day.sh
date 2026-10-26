#!/bin/bash
# usage: build_day.sh DATE [OUTDIR] [--png-only] [--csv PATH] [--history PATH]
# Builds the forecast post (F-DATE.png, F-DATE-390.png, F-DATE.mp4, F-DATE.alt.txt) and, if observed WTI exists,
# the scorecard reply (R-DATE.png, R-DATE-390.png, R-DATE.mp4, R-DATE.alt.txt). Fails loudly on missing numbers.
set -euo pipefail
H=$(dirname "$(realpath "$0")"); PY=${PY:-/workspace/venvs/venv519/bin/python}
D=$1; shift; OUT=out/$D; [ $# -gt 0 ] && [[ $1 != --* ]] && { OUT=$1; shift; }
PNGONLY=0; ARGS=()
while [ $# -gt 0 ]; do case $1 in --png-only) PNGONLY=1;; --csv|--history) ARGS+=("$1" "$2"); shift;; *) echo "unknown arg $1" >&2; exit 2;; esac; shift; done
mkdir -p "$OUT"; OUT=$(realpath "$OUT")
AV=$(df --output=avail -BG /tmp | tail -1 | tr -dc 0-9); [ "$AV" -ge 2 ] || { echo "low disk on /tmp: ${AV}G" >&2; exit 1; }
"$PY" "$H/cards.py" "$D" "$OUT" ${ARGS[@]+"${ARGS[@]}"}
shot(){ google-chrome --headless=new --no-sandbox --disable-gpu --hide-scrollbars --window-size=1600,900 --virtual-time-budget=2000 --screenshot="$2" "file://$1" >/dev/null 2>&1
        [ -s "$2" ] || { echo "render failed: $2" >&2; exit 1; }
        ffmpeg -y -loglevel error -i "$2" -vf scale=390:-1:flags=lanczos "${2%.png}-390.png"; }
for K in F R; do
  [ -f "$OUT/$K-$D.html" ] || continue
  shot "$OUT/$K-$D.html" "$OUT/$K-$D.png"
  if [ $PNGONLY = 0 ]; then
    if [ $K = F ]; then JS=$H/anim_forecast.js; V=F; else JS=$H/anim_reply.js; V=A; fi
    "$H/render_anim.sh" "$OUT/$K-$D.html" "$JS" $V 7.0 "$OUT/$K-$D.mp4" "$OUT/.$K-$D-lastframe.png"
    python3 - "$OUT/$K-$D.png" "$OUT/.$K-$D-lastframe.png" <<'PYEOF'
import sys; from PIL import Image, ImageChops
a,b=[Image.open(p).convert("RGB") for p in sys.argv[1:]]
assert ImageChops.difference(a,b).getbbox() is None, f"animation end != static card: {sys.argv[1]}"
print("anim end frame == static PNG:", sys.argv[1])
PYEOF
    rm "$OUT/.$K-$D-lastframe.png"
  fi
done
ls -1 "$OUT"

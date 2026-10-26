#!/bin/bash
# usage: render_anim.sh SRC_HTML ANIM_JS TIMELINE DURATION OUT_MP4 [FINAL_PNG]
# 30fps frames via headless Chrome (SRC_HTML + ANIM_JS), then H.264 1600x900 yuv420p +faststart.
# POSTER FRAME: the first 3 frames (0.1s) are the completed card (= the last build frame, pixel-identical),
# so X's thumbnail shows the filled card; then the normal build plays from blank.
set -euo pipefail
CHROME=${CHROME:-google-chrome}; FFMPEG=${FFMPEG:-ffmpeg}
ab(){ (cd "$(dirname "$1")" && echo "$(pwd)/$(basename "$1")"); }
SRC=$(ab "$1"); JS=$(ab "$2"); V=$3; D=$4; OUT=$5; FINAL=${6:-}
F=$(mktemp -d "${TMPDIR:-/tmp}/tpcr-fr-XXXXXX"); trap 'rm -rf "$F"' EXIT
C=$F/card.html; sed "s#</body>#<script src=\"file://$JS\"></script></body>#" "$SRC" > "$C"
N=$(python3 -c "print(int(round($D*30)))"); mkdir -p "$F/b"
render_one(){ i=$1; t=$(python3 -c "print($i/30)"); f=$F/b/$(printf %04d $i).png
  for try in 1 2 3 4; do timeout 90 "$CHROME" --headless=new --no-first-run --disable-extensions --no-default-browser-check --no-sandbox --disable-gpu --hide-scrollbars --window-size=1600,900 --virtual-time-budget=2000 --user-data-dir=$F/u$i --screenshot=$f "file://$C#v=$V&t=$t" >/dev/null 2>&1 && [ -s $f ] && break; sleep 1; done
  rm -rf $F/u$i; [ -s $f ]; }
export -f render_one; export F C V CHROME
seq 0 $((N-1)) | xargs -P10 -I{} bash -c 'render_one {}' 
LAST=$F/b/$(printf %04d $((N-1))).png
[ "$(ls $F/b | wc -l)" = "$N" ] || { echo "missing frames" >&2; exit 1; }
mkdir -p "$F/s"; for i in 0 1 2; do cp "$LAST" "$F/s/$(printf %04d $i).png"; done
for i in $(seq 0 $((N-1))); do cp "$F/b/$(printf %04d $i).png" "$F/s/$(printf %04d $((i+3))).png"; done
"$FFMPEG" -y -loglevel error -framerate 30 -i "$F/s/%04d.png" -c:v libx264 -pix_fmt yuv420p -profile:v high -crf 16 -r 30 -movflags +faststart "$OUT"
[ -n "$FINAL" ] && cp "$LAST" "$FINAL"; echo "wrote $OUT ($((N+3)) frames)"

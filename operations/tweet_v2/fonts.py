#!/usr/bin/env python
"""Bundled card fonts (TPCR-CARD-FONTS-001). Stdlib only (check_probe_png needs Pillow).

The cards ask for 'Space Grotesk' and 'Geist Mono'. Headless Chrome silently falls back to its default serif when a
family is not installed, so the cards never rely on system fonts: cards.py embeds the bundled files below as base64
data-URI @font-face rules (self-contained HTML, same font bytes everywhere; same pixels only for the same Chrome and
fontconfig), and build_day.sh renders probe_html() with the same Chrome first and refuses to build (font_unresolved, exit 3) unless both families really rendered.

usage: fonts.py probe-html OUT.html      write the font probe page
       fonts.py check-png PNG            exit 0 if both families resolved in the probe screenshot, else 3
Updating a font: replace the file in fonts/, then update its sha256 here and in fonts/README.md."""
import base64
import functools
import hashlib
import json
import os
import sys

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

# family -> (file in FONT_DIR, sha256, CSS font-weight range of the variable font)
FONTS = {
    "Space Grotesk": ("SpaceGrotesk-wght.ttf", "acad6de1fc93436f5c0f1f4137751ef04f1aea3063e7036535970ffcfbd79f72", "300 700"),
    "Geist Mono": ("GeistMono-wght.ttf", "d00e590b8eb3a59acc329b2d044fd143ae935090b7da33199ebee27cc7de8196", "100 900"),
}
# Weights the cards use, loaded and checked by the probe.
PROBE_WEIGHTS = {"Space Grotesk": (400, 700), "Geist Mono": (400, 600)}
GREEN, RED, BLUE = (0, 255, 0), (255, 0, 0), (0, 0, 255)
PROBE_POINTS = {"Space Grotesk": (400, 450), "Geist Mono": (1200, 450)}  # left half / right half of 1600x900
TOL = 8
UNRESOLVED_EXIT = 3


class FontBundleError(Exception):
    pass


def _read_verified(font_dir):
    out = {}
    for fam, (fn, sha, _w) in FONTS.items():
        p = os.path.join(font_dir, fn)
        try:
            with open(p, "rb") as f:
                b = f.read()
        except OSError as e:
            raise FontBundleError(f"font_bundle_invalid: {fam}: cannot read {fn} ({e.strerror})") from None
        got = hashlib.sha256(b).hexdigest()
        if got != sha:
            raise FontBundleError(f"font_bundle_invalid: {fam}: {fn} sha256 {got} != expected {sha}")
        out[fam] = b
    return out


def verify_bundle():
    """Every bundled font file exists and matches its pinned sha256, else FontBundleError."""
    _read_verified(FONT_DIR)


@functools.lru_cache(maxsize=None)
def _css(font_dir):
    data = _read_verified(font_dir)
    return "".join(
        f"@font-face{{font-family:'{fam}';src:url(data:font/ttf;base64,{base64.b64encode(data[fam]).decode('ascii')}) format('truetype');"
        f"font-weight:{FONTS[fam][2]};font-style:normal;font-display:block}}"
        for fam in FONTS)


def font_face_css():
    """@font-face rules (no <style> tag) for every bundled family, fonts embedded as base64 data URIs.
    Verifies the bundle first (FontBundleError). Cached per font directory."""
    return _css(FONT_DIR)


_PROBE_JS = """
(async () => {
  const fams = %s, v = {};
  const sample = "Hamburgefonstiv 0123456789 WTI MK EP HS AK";
  const ctx = document.createElement("canvas").getContext("2d");
  const w = f => { ctx.font = "40px " + f; return ctx.measureText(sample).width; };
  for (const [fam, weights] of Object.entries(fams)) {
    let ok = true;
    try {
      for (const wt of weights) {
        await document.fonts.load(`${wt} 40px '${fam}'`, sample);
        ok = ok && document.fonts.check(`${wt} 40px '${fam}'`, sample);
      }
      ok = ok && w(`'${fam}', serif`) !== w("serif") && w(`'${fam}', monospace`) !== w("monospace");
    } catch (e) { ok = false; }
    v[fam] = ok;
  }
  document.getElementById("sg").style.background = v["Space Grotesk"] ? "#00FF00" : "#FF0000";
  document.getElementById("gm").style.background = v["Geist Mono"] ? "#00FF00" : "#FF0000";
  document.title = JSON.stringify(v);
})();
"""


def probe_html():
    """Self-contained 1600x900 probe page: left half = Space Grotesk verdict, right half = Geist Mono verdict.
    Green = resolved, red = not, blue (initial) = the script never ran."""
    js = _PROBE_JS % json.dumps({f: list(PROBE_WEIGHTS[f]) for f in FONTS})
    half = "position:absolute;top:0;width:800px;height:900px;background:#0000FF"
    return (f"<!doctype html><html><head><meta charset=\"utf-8\"><title>pending</title><style>{font_face_css()}"
            f"*{{margin:0}}html,body{{width:1600px;height:900px;overflow:hidden;background:#0000FF}}</style></head>"
            f"<body><div id=\"sg\" style=\"{half};left:0\"></div><div id=\"gm\" style=\"{half};left:800px\"></div>"
            f"<script>{js}</script></body></html>")


def check_probe_png(path):
    """Families whose probe half is not green (within TOL per channel) in the screenshot at path."""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    bad = []
    for fam, (x, y) in PROBE_POINTS.items():
        px = im.getpixel((x, y)) if x < im.width and y < im.height else None
        if px is None or any(abs(a - b) > TOL for a, b in zip(px, GREEN)):
            bad.append(fam)
    return bad


def main(argv):
    if len(argv) == 2 and argv[0] == "probe-html":
        try:
            html = probe_html()
        except FontBundleError as e:
            print(str(e), file=sys.stderr); return 2
        with open(argv[1], "w") as f:
            f.write(html)
        return 0
    if len(argv) == 2 and argv[0] == "check-png":
        try:
            bad = check_probe_png(argv[1])
        except (OSError, ValueError) as e:  # missing/corrupt screenshot: nothing is proven, fail closed
            print(f"font probe screenshot unreadable: {argv[1]} ({type(e).__name__})", file=sys.stderr)
            bad = list(FONTS)
        if bad:
            print(f"font_unresolved: {', '.join(bad)} (requested font did not load in Chrome; refusing to build a card in a fallback font)",
                  file=sys.stderr)
            return UNRESOLVED_EXIT
        print("fonts resolved: " + ", ".join(FONTS))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

# Wave Stinger Transition — OBS Setup

A scene transition inspired by the HazeyData logo: layered waves (pink, blue, lavender, purple) ride from left to right and reveal the next scene, with a swoosh audio cue.

---

## Generate the Stinger

**Requirements:** Python 3, `pillow`, and `ffmpeg` in PATH.

```bash
# Install dependencies (if needed)
pip install pillow
# FFmpeg: brew install ffmpeg (Mac) or apt install ffmpeg (Linux)

# Generate the stinger (default: 1 second, 1920×1080)
python scripts/generate_stinger.py

# Options
python scripts/generate_stinger.py --duration 1.2    # Longer transition
python scripts/generate_stinger.py --no-audio       # Video only
python scripts/generate_stinger.py -o /path/to/custom.webm
```

**Output:**
- `docs/stream/assets/stinger-transition.webm` — OBS-ready video with alpha
- `docs/stream/assets/stinger-swoosh.wav` — Swoosh audio (embedded in WebM)

---

## OBS Setup

1. **Scene Transitions** → Click the dropdown (e.g. "Fade").
2. **Add** → **Stinger**.
3. **Browse** → Select `stinger-transition.webm`.
4. **Transition Point:** Set to **~85–95%** so the cut happens when the waves have mostly passed and the next scene is revealed.
5. **Preview** by switching between scenes.

### Transition Point

- **Lower (e.g. 50%):** Cut happens mid-sweep — waves still on screen when next scene appears.
- **Higher (e.g. 95%):** Cut happens as waves exit — cleaner reveal.
- **Recommended:** 90% for a smooth “waves ride away, next scene appears” feel.

---

## Streamlabs

Same idea: add a **Stinger** transition and point it at the generated WebM. Set the transition point in the stinger settings.

---

## Preview Without FFmpeg

If FFmpeg isn’t installed, open `docs/stream/stinger-preview.html` in a browser to preview the wave animation. The script still generates frames and audio; you can encode manually:

```bash
ffmpeg -y -framerate 60 -i docs/stream/assets/_stinger_frames/frame_%04d.png \
  -i docs/stream/assets/stinger-swoosh.wav \
  -c:v libvpx-vp9 -pix_fmt yuva420p -shortest \
  -c:a libopus -b:a 128k \
  docs/stream/assets/stinger-transition.webm
```

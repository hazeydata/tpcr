#!/usr/bin/env python3
"""
Generate the HazeyData wave stinger transition for OBS/Streamlabs.

Creates a WebM video with alpha transparency where layered waves (inspired by
the logo) ride from left to right, revealing the next scene. Includes a
synthesized swoosh audio.

Usage:
    python scripts/generate_stinger.py [--output PATH] [--duration SEC]

Output:
    docs/stream/assets/stinger-transition.webm (default)
    docs/stream/assets/stinger-swoosh.wav (audio only, for reference)

Requires: pillow, ffmpeg in PATH
"""

import argparse
import math
import shutil
import subprocess
import sys
from pathlib import Path

# Logo-inspired wave colors (RGBA)
WAVE_COLORS = [
    (255, 20, 100, 255),   # Vibrant pink / magenta
    (33, 150, 243, 255),   # Royal blue
    (159, 168, 218, 255),  # Lavender / periwinkle
    (103, 58, 183, 255),   # Deep purple
]

# Wave parameters: (vertical offset %, amplitude %, phase offset, thickness)
# Amplitude and phase create the layered S-curve look
WAVE_PARAMS = [
    (0.25, 0.08, 0.0, 0.12),   # Pink - front
    (0.45, 0.07, 0.15, 0.14),  # Blue
    (0.65, 0.06, 0.3, 0.13),   # Lavender
    (0.85, 0.05, 0.45, 0.12),  # Purple - back
]

FPS = 60
WIDTH = 1920
HEIGHT = 1080
DEFAULT_DURATION = 1.0  # seconds


def wave_y(x_norm: float, center: float, amplitude: float, phase: float) -> float:
    """Smooth S-curve wave. x_norm 0..1, returns y offset -1..1."""
    # Smooth sine-based wave for organic feel
    t = x_norm * 2 * math.pi + phase * 2 * math.pi
    return center + amplitude * math.sin(t) + 0.3 * math.sin(t * 2)


def generate_swoosh_audio(duration: float, sample_rate: int, out_path: Path) -> None:
    """Generate a swoosh sound effect (frequency sweep + envelope)."""
    import struct
    import wave

    n_samples = int(duration * sample_rate)
    audio = []

    for i in range(n_samples):
        t = i / sample_rate
        # Frequency sweep: high to low (whoosh feel)
        freq_start, freq_end = 1200, 200
        phase = 2 * math.pi * (freq_start * t + (freq_end - freq_start) * t * t / (2 * duration))
        sweep = 0.7 * math.sin(phase)
        # Envelope: quick attack, smooth decay
        attack = 0.02 * sample_rate
        decay = math.exp(-3 * t / duration) if duration > 0 else 1
        env = min(1, i / attack) * decay if i < attack else decay
        sample = max(-1, min(1, sweep * env))
        audio.append(int(sample * 32767))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(struct.pack(f"<{len(audio)}h", *audio))
    print(f"  Wrote swoosh: {out_path}")


def draw_frame(frame_idx: int, total_frames: int) -> "PIL.Image":
    """Draw a single frame: waves riding left to right."""
    from PIL import Image, ImageDraw

    # Progress 0..1: waves enter from left, exit right
    t = frame_idx / max(1, total_frames - 1)
    t = t * t * (3 - 2 * t)  # smoothstep

    # Wave block: starts off-left, sweeps to off-right
    block_left = int(WIDTH * (-0.2 + 1.4 * t))
    block_right = block_left + int(WIDTH * 0.6)  # wave band width

    img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    for (color, (center_pct, amp, phase, thickness)) in zip(WAVE_COLORS, WAVE_PARAMS):
        center_y = HEIGHT * center_pct
        amp_px = HEIGHT * amp
        half_thick = HEIGHT * thickness / 2

        # Build polygon: wavy band from block_left to block_right
        n_pts = 80
        top_pts, bot_pts = [], []
        for i in range(n_pts + 1):
            x = block_left + (block_right - block_left) * i / n_pts
            x_norm = (x - block_left) / max(1, block_right - block_left)
            wy = wave_y(x_norm, 0, 1, phase)
            y_center = center_y + amp_px * wy
            top_pts.append((x, y_center - half_thick))
            bot_pts.append((x, y_center + half_thick))

        polygon = top_pts + list(reversed(bot_pts))
        draw.polygon(polygon, fill=color, outline=None)

    return img


def main():
    parser = argparse.ArgumentParser(description="Generate HazeyData wave stinger transition")
    parser.add_argument("--output", "-o", type=Path, default=None,
                        help="Output WebM path (default: docs/stream/assets/stinger-transition.webm)")
    parser.add_argument("--duration", "-d", type=float, default=DEFAULT_DURATION,
                        help=f"Duration in seconds (default: {DEFAULT_DURATION})")
    parser.add_argument("--frames-dir", type=Path, default=None,
                        help="Keep temp frames (for debugging)")
    parser.add_argument("--no-audio", action="store_true", help="Skip swoosh audio")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    out_path = args.output or (repo_root / "docs" / "stream" / "assets" / "stinger-transition.webm")
    assets_dir = out_path.parent
    assets_dir.mkdir(parents=True, exist_ok=True)

    total_frames = int(args.duration * FPS)
    print(f"Generating {total_frames} frames ({args.duration}s @ {FPS} fps)...")

    try:
        from PIL import Image
    except ImportError:
        print("Error: pip install pillow")
        sys.exit(1)

    frames_dir = args.frames_dir or (assets_dir / "_stinger_frames")
    frames_dir.mkdir(parents=True, exist_ok=True)

    for i in range(total_frames):
        img = draw_frame(i, total_frames)
        frame_path = frames_dir / f"frame_{i:04d}.png"
        img.save(frame_path)
        if (i + 1) % 15 == 0:
            print(f"  Frame {i + 1}/{total_frames}")

    # Generate swoosh audio
    wav_path = assets_dir / "stinger-swoosh.wav"
    if not args.no_audio:
        generate_swoosh_audio(args.duration, 48000, wav_path)

    # Encode WebM with alpha (VP9 + yuva420p)
    # OBS stinger: transparent areas show the scene
    if not shutil.which("ffmpeg"):
        print("Error: ffmpeg not found. Install with: brew install ffmpeg (Mac) or apt install ffmpeg (Linux)")
        print(f"Frames and audio were generated. Encode manually:")
        print(f"  ffmpeg -y -framerate {FPS} -i {frames_dir}/frame_%04d.png -i {wav_path} -c:v libvpx-vp9 -pix_fmt yuva420p -shortest -c:a libopus -b:a 128k {out_path}")
        sys.exit(1)
    print("Encoding WebM (VP9 + alpha)...")
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-framerate", str(FPS),
        "-i", str(frames_dir / "frame_%04d.png"),
        "-c:v", "libvpx-vp9",
        "-pix_fmt", "yuva420p",
        "-auto-alt-ref", "0",
        "-b:v", "2M",
        "-row-mt", "1",
        "-t", str(args.duration),
    ]
    if wav_path.exists():
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-framerate", str(FPS),
            "-i", str(frames_dir / "frame_%04d.png"),
            "-i", str(wav_path),
            "-c:v", "libvpx-vp9",
            "-pix_fmt", "yuva420p",
            "-auto-alt-ref", "0",
            "-b:v", "2M",
            "-row-mt", "1",
            "-shortest",
            "-c:a", "libopus",
            "-b:a", "128k",
            str(out_path),
        ]
    else:
        ffmpeg_cmd.append(str(out_path))

    result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("FFmpeg stderr:", result.stderr)
        sys.exit(1)

    # Cleanup temp frames unless --frames-dir was given
    if not args.frames_dir:
        for f in frames_dir.glob("frame_*.png"):
            f.unlink()
        try:
            frames_dir.rmdir()
        except OSError:
            pass

    print(f"Done: {out_path}")
    print("\nOBS setup: Scene Transitions → Stinger → Browse to this file.")
    print("Transition point: set to ~95% (waves have passed, scene revealed)")


if __name__ == "__main__":
    main()

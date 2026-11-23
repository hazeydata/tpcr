"""render_anim.sh frame count must not depend on wc's padding (BSD/macOS wc -l left-pads). Stubs Chrome, ffmpeg and a
padding wc on PATH; no browser, no network."""
import os, stat, subprocess, tempfile, unittest
from pathlib import Path

SH = Path(__file__).resolve().parents[1] / "operations" / "tweet_v2" / "render_anim.sh"


def _exe(p, body):
    p.write_text("#!/bin/bash\n" + body); p.chmod(p.stat().st_mode | stat.S_IEXEC)


class RenderAnimFrameCount(unittest.TestCase):
    def run_sh(self, chrome_body):
        with tempfile.TemporaryDirectory(prefix="tpcr-ra-test-") as d:
            d = Path(d); b = d / "bin"; b.mkdir()
            _exe(b / "chrome", chrome_body)
            _exe(b / "ffmpeg", 'for a; do o=$a; done; echo mp4 > "$o"\n')
            _exe(b / "wc", 'printf "%8s\\n" "$(/usr/bin/env -i PATH=/usr/bin:/bin wc "$@" | tr -d " ")"\n')  # BSD-style pad
            (d / "c.html").write_text("<html><body>x</body></html>"); (d / "a.js").write_text("")
            env = {**os.environ, "PATH": f"{b}:{os.environ['PATH']}", "CHROME": str(b / "chrome"), "FFMPEG": str(b / "ffmpeg"), "TMPDIR": str(d)}
            r = subprocess.run(["bash", str(SH), str(d / "c.html"), str(d / "a.js"), "F", "0.2", str(d / "o.mp4"), str(d / "last.png")],
                               capture_output=True, text=True, env=env, timeout=60)
            return r, (d / "o.mp4").exists(), (d / "last.png").exists()

    def test_all_frames_with_padded_wc_succeeds(self):
        r, mp4, last = self.run_sh('for a; do case $a in --screenshot=*) echo png > "${a#--screenshot=}";; esac; done\n')
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("wrote", r.stdout); self.assertTrue(mp4 and last)

    def test_missing_frame_still_fails(self):
        # frame 0003 is never written: the render must fail, never produce an mp4
        r, mp4, _ = self.run_sh('for a; do case $a in --screenshot=*) f=${a#--screenshot=}; case $f in *0003.png) ;; *) echo png > "$f";; esac;; esac; done\n')
        self.assertNotEqual(r.returncode, 0); self.assertFalse(mp4)


    # The two cases below get past the render step (every render_one exits 0) and reach the frame-count check, so they
    # pin it to an exact numeric match: too many frames and too few must both fail with "missing frames: GOT of N".
    WRITE = 'for a; do case $a in --screenshot=*) f=${a#--screenshot=};; esac; done; echo png > "$f"\n'

    def test_extra_frame_fails_at_count_check(self):  # 0.2 s = 6 frames; frame 0 also drops a stray 9999.png -> 7
        r, mp4, _ = self.run_sh(self.WRITE + 'case $f in *0000.png) echo png > "$(dirname "$f")/9999.png";; esac\n')
        self.assertNotEqual(r.returncode, 0); self.assertFalse(mp4)
        self.assertIn("missing frames: 7 of 6", r.stderr)

    def test_short_count_past_render_fails_at_count_check(self):  # frame 5 deletes frame 0 after frame 0 was checked -> 5
        r, mp4, _ = self.run_sh(self.WRITE + 'case $f in *0005.png) d=$(dirname "$f"); until [ -s "$d/0000.png" ]; do sleep 0.1; done;'
                                ' sleep 2; rm "$d/0000.png";; esac\n')
        self.assertNotEqual(r.returncode, 0); self.assertFalse(mp4)
        self.assertIn("missing frames: 5 of 6", r.stderr)


if __name__ == "__main__":
    unittest.main()

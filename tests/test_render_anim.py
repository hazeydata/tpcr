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


if __name__ == "__main__":
    unittest.main()

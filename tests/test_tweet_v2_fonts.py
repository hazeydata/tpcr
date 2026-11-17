"""TPCR-CARD-FONTS-001: the cards render in the bundled Space Grotesk / Geist Mono, never in Chrome's silent serif fallback.

Bundle integrity (sha256, TTF family names, OFL texts), the data-URI @font-face that cards.py embeds, a real Chromium
render (Playwright) with system copies of the fonts hidden by a restricted fonts.conf, the font probe, and the
build_day.sh font gate with a stub Chrome (fails closed with font_unresolved, writes no PNG)."""
import base64
import hashlib
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TV2 = ROOT / "operations" / "tweet_v2"
CARDS = TV2 / "cards.py"
BUILD_DAY = TV2 / "build_day.sh"
FX = ROOT / "tests" / "fixtures" / "tweet_v2_samples"
DAY = "2026-09-05"  # has forecast and observed in the sample fixtures -> F and R cards
sys.path.insert(0, str(TV2))
import fonts  # noqa: E402

FONT_FACE = re.compile(r"@font-face\{font-family:'([^']+)';src:url\(data:font/ttf;base64,([A-Za-z0-9+/=]+)\) format\('truetype'\);"
                       r"font-weight:(\d+ \d+);font-style:normal;font-display:block\}")
FONT_FACE_ANY = re.compile(r"@font-face\{[^}]*\}")


def ttf_family(path):
    """Typographic family (nameID 16, else 1) from a TTF 'name' table, Windows Unicode records. Stdlib only."""
    b = Path(path).read_bytes()
    (num_tables,) = struct.unpack(">H", b[4:6])
    for i in range(num_tables):
        tag, _cs, off, _ln = struct.unpack(">4sIII", b[12 + 16 * i: 28 + 16 * i])
        if tag == b"name":
            break
    else:
        raise AssertionError("no name table")
    _fmt, count, str_off = struct.unpack(">HHH", b[off:off + 6])
    names = {}
    for j in range(count):
        pid, eid, _lang, nid, ln, so = struct.unpack(">HHHHHH", b[off + 6 + 12 * j: off + 18 + 12 * j])
        if pid == 3 and eid in (0, 1) and nid in (1, 16):
            names.setdefault(nid, b[off + str_off + so: off + str_off + so + ln].decode("utf-16-be"))
    return names.get(16) or names.get(1)


def have_deps():
    try:
        import pandas, pyarrow  # noqa: F401
        return True
    except ImportError:
        return False


def make_cards(out, extra=()):
    return subprocess.run([sys.executable, str(CARDS), DAY, str(out), "--csv", str(FX / "forecast_vs_actual_2026-09-02_06.csv"),
                           "--history", str(FX / "history_wdw4_2022-01-01_2026-09-05.parquet"),
                           "--closures", str(ROOT / "data" / "closures.csv"), *extra],
                          capture_output=True, text=True, timeout=120, cwd=ROOT)


class Bundle(unittest.TestCase):
    def test_files_exist_and_match_pinned_sha256(self):
        for fam, (fn, sha, _w) in fonts.FONTS.items():
            p = Path(fonts.FONT_DIR) / fn
            self.assertTrue(p.is_file(), p)
            self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(), sha, fam)
        fonts.verify_bundle()

    def test_ttf_family_names(self):
        self.assertEqual(ttf_family(Path(fonts.FONT_DIR) / "SpaceGrotesk-wght.ttf"), "Space Grotesk")
        self.assertEqual(ttf_family(Path(fonts.FONT_DIR) / "GeistMono-wght.ttf"), "Geist Mono")
        for fam, (fn, _s, _w) in fonts.FONTS.items():
            self.assertEqual(ttf_family(Path(fonts.FONT_DIR) / fn), fam)

    def test_ofl_licenses_bundled(self):
        for fn in ("SpaceGrotesk-OFL.txt", "GeistMono-OFL.txt"):
            self.assertIn("SIL Open Font License, Version 1.1", (Path(fonts.FONT_DIR) / fn).read_text())

    def test_weight_ranges(self):
        self.assertEqual(fonts.FONTS["Space Grotesk"][2], "300 700")
        self.assertEqual(fonts.FONTS["Geist Mono"][2], "100 900")


class TamperedBundle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="tpcr-fonts-test-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "fonts"
        shutil.copytree(fonts.FONT_DIR, self.dir)

    def tamper(self):
        p = self.dir / "GeistMono-wght.ttf"
        b = bytearray(p.read_bytes()); b[-1] ^= 1; p.write_bytes(bytes(b))

    def test_verify_bundle_raises_on_altered_file(self):
        self.tamper()
        with mock.patch.object(fonts, "FONT_DIR", str(self.dir)):
            with self.assertRaises(fonts.FontBundleError) as e:
                fonts.verify_bundle()
            self.assertTrue(str(e.exception).startswith("font_bundle_invalid:"))
            self.assertIn("Geist Mono", str(e.exception))
            with self.assertRaises(fonts.FontBundleError):
                fonts.font_face_css()

    def test_verify_bundle_raises_on_missing_file(self):
        (self.dir / "SpaceGrotesk-wght.ttf").unlink()
        with mock.patch.object(fonts, "FONT_DIR", str(self.dir)):
            with self.assertRaises(fonts.FontBundleError) as e:
                fonts.verify_bundle()
            self.assertTrue(str(e.exception).startswith("font_bundle_invalid:"))

    @unittest.skipUnless(have_deps(), "pandas/pyarrow not installed (run with the TPCR venv)")
    def test_cards_refuses_to_build_with_altered_bundle(self):
        # cards.py from a copy of operations/tweet_v2 whose fonts/ has one altered byte
        tv2 = Path(self.tmp.name) / "tweet_v2"
        shutil.copytree(TV2, tv2, ignore=shutil.ignore_patterns("__pycache__", "fonts"))
        shutil.copytree(self.dir, tv2 / "fonts")
        p = tv2 / "fonts" / "SpaceGrotesk-wght.ttf"
        b = bytearray(p.read_bytes()); b[100] ^= 1; p.write_bytes(bytes(b))
        out = Path(self.tmp.name) / "out"
        r = subprocess.run([sys.executable, str(tv2 / "cards.py"), DAY, str(out), "--csv", str(FX / "forecast_vs_actual_2026-09-02_06.csv"),
                            "--history", str(FX / "history_wdw4_2022-01-01_2026-09-05.parquet"),
                            "--closures", str(ROOT / "data" / "closures.csv"),
                            ], capture_output=True, text=True, timeout=120, cwd=ROOT,
                           env={**os.environ, "TPCR_TWEET_FORMAT_CONFIG": str(ROOT / "config" / "tweet_format.json")})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("REFUSING TO BUILD: font_bundle_invalid", r.stderr)
        self.assertFalse((out / f"F-{DAY}.html").exists())


@unittest.skipUnless(have_deps(), "pandas/pyarrow not installed (run with the TPCR venv)")
class CardsEmbedFonts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="tpcr-fonts-cards-")
        cls.out = Path(cls._tmp.name)
        r = make_cards(cls.out)
        if r.returncode != 0:
            cls._tmp.cleanup()
            raise AssertionError(r.stderr)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_font_face_data_uris_are_the_bundled_bytes(self):
        for k in ("F", "R"):
            h = (self.out / f"{k}-{DAY}.html").read_text()
            faces = {m.group(1): m for m in FONT_FACE.finditer(h)}
            self.assertEqual(set(faces), {"Space Grotesk", "Geist Mono"}, k)
            for fam, m in faces.items():
                fn, sha, w = fonts.FONTS[fam]
                self.assertEqual(hashlib.sha256(base64.b64decode(m.group(2))).hexdigest(), sha)
                self.assertEqual(m.group(3), w)
            self.assertNotIn("file://", h)
            self.assertTrue(h.startswith('<html><head><meta charset="utf-8"><style>@font-face'), k)
            self.assertIn("font-family:'Space Grotesk',sans-serif", h)
            self.assertIn(".m{font-family:'Geist Mono',monospace}", h)
        self.assertIn('font-family="\'Geist Mono\', monospace"', (self.out / f"R-{DAY}.html").read_text())


# ---------------------------------------------------------------- real Chromium (Playwright), system fonts hidden
def restricted_fonts_conf(d):
    """fonts.conf listing only the DejaVu / Liberation / Noto dirs found on this machine (so installed copies of Space
    Grotesk / Geist Mono are invisible to Chromium). None if none of them exist."""
    roots = [Path(p) for p in ("/usr/share/fonts", "/usr/local/share/fonts") if Path(p).is_dir()]
    dirs = []
    for root in roots:
        for sub in sorted(root.rglob("*")):
            if sub.is_dir() and sub.name.lower() in ("dejavu", "liberation", "liberation2", "noto"):
                dirs.append(sub)
    if not dirs:
        return None
    conf = Path(d) / "fonts.conf"
    conf.write_text('<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n<fontconfig>\n'
                    + "".join(f"  <dir>{x}</dir>\n" for x in dirs)
                    + f"  <cachedir>{Path(d) / 'fccache'}</cachedir>\n</fontconfig>\n")
    return conf


def _launch(tmp):
    """(playwright, browser) with a restricted fonts.conf, or raise SkipTest."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise unittest.SkipTest("playwright not installed")
    conf = restricted_fonts_conf(tmp)
    if conf is None:
        raise unittest.SkipTest("no dejavu/liberation/noto font dirs for the restricted fonts.conf")
    env = {**os.environ, "FONTCONFIG_FILE": str(conf)}
    pw = sync_playwright().start()
    err = None
    # Playwright's own build first (only if downloaded: a failed launch leaks temp dirs), then any other cached build
    own = [None] if Path(pw.chromium.executable_path).is_file() else []
    for exe in [*own, *cached_chromiums()]:
        try:
            return pw, pw.chromium.launch(env=env, **({"executable_path": str(exe)} if exe else {}))
        except Exception as e:  # noqa: BLE001 - try the next one
            err = e
    pw.stop()
    raise unittest.SkipTest(f"chromium does not launch: {type(err).__name__}")


def cached_chromiums():
    """Chromium binaries in the Playwright browser cache (TPCR_TEST_CHROMIUM first), found at runtime: lets the tests run
    when the installed playwright wants a newer build than the one downloaded."""
    out = [Path(os.environ["TPCR_TEST_CHROMIUM"])] if os.environ.get("TPCR_TEST_CHROMIUM") else []
    caches = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH"), Path.home() / ".cache" / "ms-playwright", Path.home() / "Library" / "Caches" / "ms-playwright"]
    for c in caches:
        if c and Path(c).is_dir():
            for pat in ("chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell", "chromium-*/chrome-linux*/chrome"):
                out += sorted(Path(c).glob(pat), reverse=True)
    return [x for x in out if x.is_file() and os.access(x, os.X_OK)]


@unittest.skipUnless(have_deps(), "pandas/pyarrow not installed (run with the TPCR venv)")
class ChromiumRendersBundledFonts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="tpcr-fonts-chromium-")
        cls.tmp = Path(cls._tmp.name)
        try:
            cls.pw, cls.browser = _launch(cls.tmp)
        except BaseException:
            cls._tmp.cleanup()
            raise
        try:
            r = make_cards(cls.tmp / "cards")
            assert r.returncode == 0, r.stderr
            cls.page = cls.browser.new_page(viewport={"width": 1600, "height": 900})
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close()
        finally:
            try:
                cls.pw.stop()
            finally:
                cls._tmp.cleanup()

    def open(self, html, name):
        p = self.tmp / name
        p.write_text(html)
        self.page.goto(p.as_uri())
        self.page.evaluate("document.fonts.ready.then(() => true)")

    def platform_font(self, selector):
        """The font Chromium actually used for most glyphs of the first node matching selector (CDP)."""
        cdp = self.page.context.new_cdp_session(self.page)
        try:
            cdp.send("DOM.enable"); cdp.send("CSS.enable")
            root = cdp.send("DOM.getDocument", {"depth": -1})["root"]["nodeId"]
            node = cdp.send("DOM.querySelector", {"nodeId": root, "selector": selector})["nodeId"]
            self.assertTrue(node, selector)
            fs = cdp.send("CSS.getPlatformFontsForNode", {"nodeId": node})["fonts"]
            self.assertTrue(fs, selector)
            return max(fs, key=lambda f: f["glyphCount"])
        finally:
            cdp.detach()

    def card(self, k):
        return (self.tmp / "cards" / f"{k}-{DAY}.html").read_text()

    def test_cards_render_in_bundled_fonts(self):
        for k in ("F", "R"):
            self.open(self.card(k), f"{k}.html")
            for sel in (".h", ".k", ".brand"):
                f = self.platform_font(sel)
                self.assertTrue(f["familyName"].startswith("Space Grotesk"), (k, sel, f))
                self.assertTrue(f["isCustomFont"], (k, sel, f))
            f = self.platform_font(".m")
            self.assertTrue(f["familyName"].startswith("Geist Mono"), (k, ".m", f))
            self.assertTrue(f["isCustomFont"], (k, ".m", f))
            # UTF-8 still decoded after ~400 KB of base64 (needs the <meta charset> ahead of the fonts)
            self.assertEqual(self.page.inner_text(".brand"), "TPCR · HazeyData")
            self.assertTrue(self.page.evaluate("document.characterSet") == "UTF-8")

    def test_negative_control_without_font_face_falls_back(self):
        # Same card minus the @font-face rules: with system copies hidden, Chromium must use something else, which
        # proves this harness can see a fallback.
        for k in ("F", "R"):
            html, n = FONT_FACE_ANY.subn("", self.card(k))
            self.assertEqual(n, 2)
            self.open(html, f"{k}-nofont.html")
            for sel in (".h", ".k", ".brand", ".m"):
                f = self.platform_font(sel)
                self.assertFalse(f["familyName"].startswith(("Space Grotesk", "Geist Mono")), (k, sel, f))
                self.assertFalse(f["isCustomFont"], (k, sel, f))

    def probe_verdict(self, html, name):
        self.open(html, name)
        self.page.wait_for_function("document.title.startsWith('{')", timeout=15000)
        png = self.tmp / (name + ".png")
        self.page.screenshot(path=str(png))
        return fonts.check_probe_png(png)

    def test_probe_resolves_with_bundled_fonts(self):
        self.assertEqual(self.probe_verdict(fonts.probe_html(), "probe.html"), [])

    def test_probe_flags_both_families_without_font_face(self):
        html, n = FONT_FACE_ANY.subn("", fonts.probe_html())
        self.assertEqual(n, 2)
        self.assertEqual(self.probe_verdict(html, "probe-nofont.html"), ["Space Grotesk", "Geist Mono"])


class ProbePng(unittest.TestCase):
    def check(self, left, right, size=(1600, 900)):
        from PIL import Image
        with tempfile.TemporaryDirectory(prefix="tpcr-fonts-png-") as d:
            im = Image.new("RGB", size, left)
            im.paste(right, (800, 0, size[0], size[1]))
            p = Path(d) / "p.png"; im.save(p)
            return fonts.check_probe_png(p)

    def setUp(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow not installed")

    def test_verdicts(self):
        G, R, B = fonts.GREEN, fonts.RED, fonts.BLUE
        self.assertEqual(self.check(G, G), [])
        self.assertEqual(self.check((5, 250, 3), G), [])  # within tolerance
        self.assertEqual(self.check(R, G), ["Space Grotesk"])
        self.assertEqual(self.check(G, B), ["Geist Mono"])
        self.assertEqual(self.check(B, B), ["Space Grotesk", "Geist Mono"])
        self.assertEqual(self.check((0, 200, 0), G), ["Space Grotesk"])  # green-ish is not green
        self.assertEqual(self.check(G, G, size=(800, 900)), ["Geist Mono"])  # too small: right half missing

    def test_probe_html_shape(self):
        h = fonts.probe_html()
        self.assertEqual(len(FONT_FACE.findall(h)), 2)
        self.assertIn("#0000FF", h)
        self.assertIn("document.title", h)


# ---------------------------------------------------------------- build_day.sh font gate, stub Chrome
def _exe(p, body):
    p.write_text("#!/bin/bash\n" + body); p.chmod(p.stat().st_mode | stat.S_IEXEC)


@unittest.skipUnless(have_deps(), "pandas/pyarrow not installed (run with the TPCR venv)")
class BuildDayFontGate(unittest.TestCase):
    def setUp(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow not installed")

    def run_build(self, color):
        """build_day.sh with a stub Chrome that writes a solid 1600x900 PNG of color for every --screenshot."""
        with tempfile.TemporaryDirectory(prefix="tpcr-fontgate-test-") as d:
            d = Path(d); b = d / "bin"; b.mkdir(); out = d / "out"; tmp = d / "tmp"; tmp.mkdir()
            _exe(b / "chrome", f'for a; do case $a in --screenshot=*) f=${{a#--screenshot=}};; esac; done\n'
                               f'"{sys.executable}" -c "import sys; from PIL import Image; Image.new(\'RGB\', (1600, 900), {tuple(color)!r}).save(sys.argv[1])" "$f"\n')
            _exe(b / "ffmpeg", 'for a; do o=$a; done; echo x > "$o"\n')
            env = {**os.environ, "PY": sys.executable, "PYIMG": sys.executable, "CHROME": str(b / "chrome"),
                   "FFMPEG": str(b / "ffmpeg"), "TMPDIR": str(tmp)}
            r = subprocess.run(["bash", str(BUILD_DAY), DAY, str(out), "--csv", str(FX / "forecast_vs_actual_2026-09-02_06.csv"),
                                "--history", str(FX / "history_wdw4_2022-01-01_2026-09-05.parquet"),
                                "--closures", str(ROOT / "data" / "closures.csv"), "--forecast-only", "--png-only"],
                               capture_output=True, text=True, env=env, timeout=180, cwd=ROOT)
            pngs = sorted(p.name for p in out.glob("*.png")) if out.exists() else []
            leftover = list(tmp.iterdir())
            return r, pngs, leftover

    def test_red_probe_fails_closed(self):
        r, pngs, leftover = self.run_build(fonts.RED)
        self.assertEqual(r.returncode, fonts.UNRESOLVED_EXIT, r.stderr)
        self.assertIn("font_unresolved: Space Grotesk, Geist Mono", r.stderr)
        self.assertIn("refusing to build a card in a fallback font", r.stderr)
        self.assertEqual([p for p in pngs if p.startswith("F-")], [])
        self.assertEqual(pngs, [])
        self.assertEqual(leftover, [])  # the probe lives in $PROF, removed by the trap

    def test_green_probe_passes_font_gate(self):
        r, pngs, leftover = self.run_build(fonts.GREEN)
        self.assertNotIn("font_unresolved", r.stderr)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"F-{DAY}.png", pngs)
        self.assertEqual(leftover, [])

    def test_font_unresolved_reaches_the_build_error(self):
        # tweet_v2.build() (both tweet jobs) turns a failed build_day.sh into the build error with the last 800 chars of
        # stderr, which each job's staying-silent Discord alert carries (predicted: "card not built, staying silent (...)";
        # observed: "staying silent (...)"): the font_unresolved line must be in it.
        sys.path.insert(0, str(ROOT / "operations" / "scripts"))
        import tweet_v2
        msg = ("font_unresolved: Space Grotesk, Geist Mono (requested font did not load in Chrome; refusing to build a card "
               "in a fallback font)")
        fake = mock.Mock(return_value=subprocess.CompletedProcess([], 3, stdout="FORECAST x\n", stderr="noise\n" * 200 + msg + "\n"))
        with tempfile.TemporaryDirectory(prefix="tpcr-fontgate-build-") as d:
            with self.assertRaises(RuntimeError) as e:
                tweet_v2.build(DAY, d, "h.parquet", "{}", runner=fake)
        self.assertIn(msg, str(e.exception))


if __name__ == "__main__":
    unittest.main()

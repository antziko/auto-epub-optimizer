import io
import sys
import unittest
from pathlib import Path

from PIL import Image, ImageDraw


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PIPELINE_DIR = PROJECT_ROOT / 'cli' / 'epubkit_pipeline'
sys.path.insert(0, str(PIPELINE_DIR))
sys.path.insert(0, str(PROJECT_ROOT / 'cli'))

from epub_processor import strip_optimizer_tag  # noqa: E402
from image_processor import ImageOptions, classify_image, process_image  # noqa: E402
from optimize import build_parser, build_options, make_shareable  # noqa: E402


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


def _jpeg(img: Image.Image, quality: int = 90) -> bytes:
    buf = io.BytesIO()
    img.convert('RGB').save(buf, 'JPEG', quality=quality)
    return buf.getvalue()


def _diagram() -> Image.Image:
    """White page, light-grey boxes holding dark-grey labels: the speckle case."""
    img = Image.new('L', (600, 400), 255)
    d = ImageDraw.Draw(img)
    for x in (40, 320):
        d.rectangle([x, 60, x + 220, 140], fill=235, outline=120)
        d.text((x + 20, 90), "Graph Retrieval", fill=70)
    d.line([150, 140, 150, 300], fill=90, width=2)
    return img


def _dark_screenshot() -> Image.Image:
    """Dark editor background with grey code text."""
    img = Image.new('L', (600, 400), 30)
    d = ImageDraw.Draw(img)
    for row in range(12):
        d.text((20, 20 + row * 28), "const app = express(); app.listen(3000);", fill=170)
    return img


def _photo() -> Image.Image:
    """Smooth gradient: no flat background, tones spread across the mid greys."""
    img = Image.new('L', (600, 400))
    img.putdata([(x * 255 // 600 + y * 255 // 400) // 2 for y in range(400) for x in range(600)])
    return img


def _decode(result) -> Image.Image:
    return Image.open(io.BytesIO(result.output_bytes)).convert('L')


def _grey_share(img: Image.Image) -> float:
    hist = img.histogram()
    return sum(hist[40:215]) / sum(hist)


class ClassifyTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(classify_image(_diagram()), 'diagram')
        self.assertEqual(classify_image(_dark_screenshot()), 'dark')
        self.assertEqual(classify_image(_photo()), 'photo')

    def test_dark_photo_with_spread_tones_is_not_inverted(self):
        # Mostly dark but with a large mid-grey subject: a photo, not a screenshot.
        img = Image.new('L', (600, 400), 20)
        ImageDraw.Draw(img).ellipse([100, 50, 500, 350], fill=140)
        self.assertEqual(classify_image(img), 'photo')


class TextModeOutputTests(unittest.TestCase):
    def test_diagram_becomes_black_and_white(self):
        [result] = process_image(_png(_diagram()), 'd.png', ImageOptions())
        out = _decode(result)
        self.assertIn('black/white', result.details)
        self.assertLess(_grey_share(out), 0.02)
        self.assertGreater(out.getpixel((100, 70)), 245)  # light box fill went white

    def test_dark_screenshot_is_inverted(self):
        [result] = process_image(_png(_dark_screenshot()), 's.png', ImageOptions())
        out = _decode(result)
        self.assertIn('inverted', result.details)
        self.assertGreater(out.getpixel((590, 390)), 245)  # background now white

    def test_photo_keeps_grey_path(self):
        [result] = process_image(_png(_photo()), 'p.png', ImageOptions())
        self.assertIn('4-level grayscale', result.details)
        self.assertNotIn('black/white', result.details)

    def test_text_modes_can_be_disabled(self):
        [result] = process_image(_png(_diagram()), 'd.png', ImageOptions(text_modes=False))
        self.assertNotIn('black/white', result.details)

    def test_small_jpeg_diagram_is_not_kept_as_original(self):
        # A JPEG that needs no resize used to be kept verbatim when re-encoding grew it,
        # which would silently skip the text mode.
        source = _jpeg(_diagram(), quality=30)
        [result] = process_image(source, 'd.jpg', ImageOptions())
        self.assertTrue(result.was_converted)
        self.assertIn('black/white', result.details)

    def test_default_bounds_serve_both_screens(self):
        img = Image.new('L', (1000, 1500), 255)
        [result] = process_image(_png(img), 'tall.png', ImageOptions(text_modes=False))
        width, height = _decode(result).size
        self.assertEqual(height, 800)
        self.assertIn(width, (533, 534))


class TitleTagTests(unittest.TestCase):
    def test_strip_optimizer_tag(self):
        self.assertEqual(strip_optimizer_tag('(x) Dune'), 'Dune')
        self.assertEqual(strip_optimizer_tag('(X4) Dune'), 'Dune')
        self.assertEqual(strip_optimizer_tag('Dune (X3)'), 'Dune')
        self.assertEqual(strip_optimizer_tag('Dune'), 'Dune')
        self.assertEqual(strip_optimizer_tag('(X11) Dune'), '(X11) Dune')
        self.assertEqual(strip_optimizer_tag('(x)'), '(x)')

    def test_cli_prefix_gets_its_space_back(self):
        # .env parsers trim the trailing space of "(x) ".
        args = build_parser().parse_args(['in.epub', '--title-prefix=(x)'])
        self.assertEqual(build_options(args).title_prefix, '(x) ')
        args = build_parser().parse_args(['in.epub', '--title-prefix=(x) '])
        self.assertEqual(build_options(args).title_prefix, '(x) ')

    def test_cli_title_override(self):
        opts = build_options(build_parser().parse_args(['in.epub', '--title=From Calibre', '--title-prefix=(x) ']))
        self.assertEqual(opts.metadata_edits, {'title': 'From Calibre'})
        self.assertEqual(build_options(build_parser().parse_args(['in.epub'])).metadata_edits, {})

    def test_cli_defaults(self):
        opts = build_options(build_parser().parse_args(['in.epub']))
        self.assertEqual((opts.max_width, opts.max_height), (800, 800))
        self.assertTrue(opts.text_modes)


class OutputPermissionTests(unittest.TestCase):
    def test_output_is_readable_by_other_users(self):
        import os
        import stat
        import tempfile
        # mkstemp makes 0600; the Calibre importer runs as a different user.
        fd, name = tempfile.mkstemp(suffix='.epub')
        os.close(fd)
        try:
            make_shareable(Path(name))
            self.assertTrue(os.stat(name).st_mode & stat.S_IROTH)
        finally:
            os.unlink(name)


if __name__ == '__main__':
    unittest.main()

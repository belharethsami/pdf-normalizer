import tempfile
import unittest
from pathlib import Path

import pymupdf
import numpy as np
from normalizer.pipeline import normalize, verify, Profile, snap_endpoints
from PIL import Image


class PipelineTests(unittest.TestCase):
    def test_endpoint_variation_is_removed_without_blurring(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            clean = np.full((450, 600, 3), 255, dtype=np.uint8)
            # One-pixel strokes, a midtone, and a color must survive unchanged.
            clean[60:390, 70::90] = 0
            clean[80::70, 40:560] = 0
            clean[180:230, 200:260] = 128
            clean[260:300, 320:380] = [30, 140, 220]
            noisy = clean.copy()
            rng = np.random.default_rng(12)
            white = np.all(clean == 255, axis=2)
            black = np.all(clean == 0, axis=2)
            noisy[white] = rng.integers(247, 256, size=(white.sum(), 1))
            noisy[black] = rng.integers(0, 9, size=(black.sum(), 1))
            doc = pymupdf.open()
            for pixels in [clean, noisy]:
                page = doc.new_page(width=288, height=216)
                pix = pymupdf.Pixmap(pymupdf.csRGB, 600, 450, pixels.tobytes(), False)
                page.insert_image(page.rect, pixmap=pix)
            doc.save(tmp/'input.pdf')
            doc.close()
            report = normalize(tmp/'input.pdf', tmp/'output.pdf')
            verify(tmp/'output.pdf', report)
            self.assertEqual(report['pages'][0]['pixels_sha256'], report['pages'][1]['pixels_sha256'])
            with pymupdf.open(tmp/'output.pdf') as output:
                for page in output:
                    pix = pymupdf.Pixmap(output, page.get_images()[0][0])
                    pixels = np.frombuffer(pix.samples, dtype=np.uint8).reshape(900, 1200, 3)
                    np.testing.assert_array_equal(pixels[::2, ::2], clean)

    def test_endpoint_cleanup_bounds_and_midtone_preservation(self):
        values = np.arange(256, dtype=np.uint8).reshape(16, 16)
        rgb = np.stack([values, values, values], axis=2)
        cleaned = np.asarray(snap_endpoints(Image.fromarray(rgb), 8))
        self.assertLessEqual(np.abs(cleaned.astype(int)-rgb).max(), 8)
        np.testing.assert_array_equal(cleaned[(rgb > 8) & (rgb < 247)], rgb[(rgb > 8) & (rgb < 247)])
        for threshold in [16, 64, 128, 192, 240]:
            np.testing.assert_array_equal(cleaned < threshold, rgb < threshold)
        with self.assertRaises(ValueError):
            Profile(endpoint_snap=9)

    def test_mixed_resolution_and_whiteouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            vector = pymupdf.open()
            page = vector.new_page(width=288, height=216)
            page.insert_text((30, 70), 'VISIBLE WORDS', fontsize=15)
            page.insert_text((30, 140), 'HIDDEN WORDS', fontsize=15)
            source = pymupdf.open()
            for dpi in (150, 300):
                image = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
                p = source.new_page(width=288, height=216)
                p.insert_image(p.rect, pixmap=image)
                p.draw_rect(pymupdf.Rect(20, 115, 200, 150), color=None, fill=(1, 1, 1))
            source.save(tmp/'input.pdf')
            report = normalize(tmp/'input.pdf', tmp/'output.pdf')
            verify(tmp/'output.pdf', report)
            self.assertTrue(report['verified'])
            with pymupdf.open(tmp/'output.pdf') as out:
                self.assertEqual(len(out), 2)
                for p in out:
                    image = pymupdf.Pixmap(out, p.get_images()[0][0])
                    arr = np.frombuffer(image.samples, dtype=np.uint8).reshape(image.height, image.width, 3)
                    # The whiteout is in the embedded pixels, with no source text retained.
                    self.assertTrue(np.all(arr[510:600, 100:790] == 255))
                    self.assertEqual(p.get_text(), '')
                    self.assertEqual(p.get_drawings(), [])
                    self.assertEqual(p.get_images()[0][2:4], (1200, 900))
            self.assertTrue(all(p['identical_2x2_blocks'] == 1 for p in report['pages']))

    def test_cancellation_and_page_limits_remove_partial_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = pymupdf.open()
            source.new_page(width=288, height=216)
            source.save(tmp/'input.pdf')
            with self.assertRaises(InterruptedError):
                normalize(tmp/'input.pdf', tmp/'cancelled.pdf', cancelled=lambda: True)
            self.assertFalse((tmp/'cancelled.pdf').exists())
            with self.assertRaises(ValueError):
                normalize(tmp/'input.pdf', tmp/'oversize.pdf', profile=Profile(max_output_pixels=10))
            self.assertFalse((tmp/'oversize.pdf').exists())

    def test_encrypted_pdf_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = pymupdf.open()
            source.new_page()
            source.save(tmp/'input.pdf', encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw='owner', user_pw='password')
            with self.assertRaisesRegex(ValueError, 'Password'):
                normalize(tmp/'input.pdf', tmp/'output.pdf')
            self.assertFalse((tmp/'output.pdf').exists())

if __name__ == '__main__':
    unittest.main()

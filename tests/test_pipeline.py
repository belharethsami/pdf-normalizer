import tempfile
import unittest
from pathlib import Path

import pymupdf
import numpy as np
from normalizer.pipeline import normalize, verify, Profile


class PipelineTests(unittest.TestCase):
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

"""Render all pages at a common native resolution, then uniformly upscale.

No pages bypass this pipeline based on their original resolution or structure.
The output contains only newly rendered pixels; source PDF objects are never copied.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import shutil
import struct
import tempfile
import zlib
from typing import Callable

import pymupdf
import numpy as np
from PIL import Image


@dataclass(frozen=True)
class Profile:
    base_dpi: int = 150
    scale: int = 2
    max_output_pixels: int = 100_000_000

    def __post_init__(self):
        if not 36 <= self.base_dpi <= 300 or self.scale not in (1, 2):
            raise ValueError('Unsupported raster profile.')


def pixel_statistics(image: Image.Image) -> dict:
    """Measure the aligned-pixel signature, independently of PDF metadata."""
    a = np.asarray(image.convert('L'))
    h, w = a.shape
    a = a[:h-h % 2, :w-w % 2]
    even = float(np.mean(a[:, 0::2] == a[:, 1::2]))
    odd = float(np.mean(a[:, 1:-1:2] == a[:, 2::2]))
    block = a[::2, ::2]
    blocks_equal = (block == a[::2, 1::2]) & (block == a[1::2, ::2]) & (block == a[1::2, 1::2])
    return {
        'even_equal': even, 'odd_equal': odd, 'equality_gap': even - odd,
        'identical_2x2_blocks': float(np.mean(blocks_equal)),
    }


class ImagePDFWriter:
    """Stream a fresh PDF; image bytes and prior pages do not accumulate in RAM."""
    def __init__(self, path: Path, page_count: int):
        self.file = path.open('wb')
        self.offsets = [0]
        self.count = page_count
        self.written = 0
        self.file.write(b'%PDF-1.7\n%\xe2\xe3\xcf\xd3\n')
        self._object(1, b'<< /Type /Catalog /Pages 2 0 R >>')
        kids = ' '.join(f'{5 + 3*i} 0 R' for i in range(page_count))
        self._object(2, f'<< /Type /Pages /Count {page_count} /Kids [{kids}] >>'.encode())
        self._object(3, b'<< /Producer (Uniform PDF Normalizer) >>')

    def _begin(self, number: int):
        assert number == len(self.offsets)
        self.offsets.append(self.file.tell())
        self.file.write(f'{number} 0 obj\n'.encode())

    def _object(self, number: int, content: bytes):
        self._begin(number)
        self.file.write(content + b'\nendobj\n')

    def add_page(self, encoded: Path, width: int, height: int, points: tuple[float, float]):
        image_id = 4 + 3*self.written
        page_id, content_id = image_id+1, image_id+2
        pw, ph = points
        self._begin(image_id)
        self.file.write((f'<< /Type /XObject /Subtype /Image /Width {width} /Height {height} '
                         f'/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /JPXDecode '
                         f'/Length {encoded.stat().st_size} >>\nstream\n').encode())
        with encoded.open('rb') as source:
            shutil.copyfileobj(source, self.file, 8*1024*1024)
        self.file.write(b'\nendstream\nendobj\n')
        self._object(page_id, (f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {pw:.6f} {ph:.6f}] '
                               f'/Resources << /XObject << /Im0 {image_id} 0 R >> >> '
                               f'/Contents {content_id} 0 R >>').encode())
        stream = f'q\n{pw:.6f} 0 0 {ph:.6f} 0 0 cm\n/Im0 Do\nQ\n'.encode()
        self._object(content_id, f'<< /Length {len(stream)} >>\nstream\n'.encode()+stream+b'endstream')
        self.written += 1

    def close(self):
        assert self.written == self.count
        xref_id = len(self.offsets)
        position = self.file.tell()
        self._begin(xref_id)
        entries = [struct.pack('>BQH', 0, 0, 65535)]
        entries.extend(struct.pack('>BQH', 1, offset, 0) for offset in self.offsets[1:])
        data = zlib.compress(b''.join(entries))
        self.file.write((f'<< /Type /XRef /Size {len(self.offsets)} /W [1 8 2] '
                         f'/Root 1 0 R /Info 3 0 R /Filter /FlateDecode /Length {len(data)} >>\nstream\n').encode())
        self.file.write(data+b'\nendstream\nendobj\n')
        self.file.write(f'startxref\n{position}\n%%EOF\n'.encode())
        self.file.close()


def normalize(source: Path, destination: Path, *, profile: Profile = Profile(),
              work_dir: Path | None = None, progress: Callable[[dict], None] | None = None,
              cancelled: Callable[[], bool] | None = None) -> dict:
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError('Write and validate a separate file before replacing the source.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(source)
    if not doc.is_pdf:
        doc.close()
        raise ValueError('The selected file is not a PDF.')
    if doc.needs_pass:
        doc.close()
        raise ValueError('Password-protected PDFs must be unlocked before processing.')
    if not 1 <= len(doc) <= 10000:
        doc.close()
        raise ValueError('PDF must contain 1–10,000 pages.')
    report = {'profile': {'base_dpi': profile.base_dpi, 'output_dpi': profile.base_dpi*profile.scale,
                          'scale': profile.scale, 'resampling': 'nearest', 'encoding': 'lossless JPEG2000',
                          'tile_size': [1024, 1024], 'image_name': 'Im0', 'colorspace': 'DeviceRGB', 'bits_per_component': 8},
              'page_count': len(doc), 'pages': []}
    writer = ImagePDFWriter(destination, len(doc))
    try:
        with tempfile.TemporaryDirectory(prefix='raster-', dir=work_dir) as scratch:
            encoded = Path(scratch) / 'page.jp2'
            for index, page in enumerate(doc):
                if cancelled and cancelled():
                    raise InterruptedError('Processing cancelled.')
                rect = page.rect
                if not all(math.isfinite(n) and n > 0 for n in (rect.width, rect.height)):
                    raise ValueError(f'Page {index+1} has invalid dimensions.')
                predicted = math.ceil(rect.width*profile.base_dpi/72)*math.ceil(rect.height*profile.base_dpi/72)*profile.scale**2
                if predicted > profile.max_output_pixels:
                    raise ValueError(f'Page {index+1} exceeds the supported physical page size.')
                # Always render the visible page at the common base resolution first.
                pix = page.get_pixmap(dpi=profile.base_dpi, colorspace=pymupdf.csRGB, alpha=False, annots=True)
                base = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
                del pix
                image = base.resize((base.width*profile.scale, base.height*profile.scale), Image.Resampling.NEAREST)
                base.close()
                pixels_hash = hashlib.sha256(image.tobytes()).hexdigest()
                metrics = pixel_statistics(image)
                image.save(encoded, format='JPEG2000', irreversible=False, mct=1, tile_size=(1024, 1024))
                writer.add_page(encoded, image.width, image.height, (rect.width, rect.height))
                row = {'page': index+1, 'width': image.width, 'height': image.height,
                       'page_width_points': rect.width, 'page_height_points': rect.height,
                       'pixels_sha256': pixels_hash, **metrics}
                report['pages'].append(row)
                image.close()
                encoded.unlink()
                pymupdf.TOOLS.store_shrink(100)
                if progress:
                    progress({'phase': 'processing', 'completed': index+1, 'total': len(doc)})
        writer.close()
    except BaseException:
        writer.file.close()
        destination.unlink(missing_ok=True)
        raise
    finally:
        doc.close()
    return report


def verify(path: Path, report: dict, *, preview_dir: Path | None = None,
           progress: Callable[[dict], None] | None = None) -> None:
    with pymupdf.open(path) as doc:
        assert len(doc) == report['page_count'] and doc.embfile_count() == 0
        assert not doc.is_form_pdf
        if preview_dir:
            preview_dir.mkdir(parents=True, exist_ok=True)
        for index, page in enumerate(doc):
            expected = report['pages'][index]
            images = page.get_images(full=True)
            assert len(images) == 1
            entry = images[0]
            assert entry[1:9] == (0, expected['width'], expected['height'], 8, 'DeviceRGB', '', 'Im0', 'JPXDecode')
            assert not page.get_drawings() and not page.get_text()
            assert not list(page.annots() or []) and not list(page.widgets() or [])
            assert abs(page.rect.width-expected['page_width_points']) < 1e-4
            assert abs(page.rect.height-expected['page_height_points']) < 1e-4
            pix = pymupdf.Pixmap(doc, entry[0])
            assert hashlib.sha256(pix.samples).hexdigest() == expected['pixels_sha256']
            image = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
            del pix
            actual = pixel_statistics(image)
            if report['profile']['scale'] == 2:
                assert actual['identical_2x2_blocks'] == 1.0
            for key, value in actual.items():
                assert abs(value-expected[key]) < 1e-12
            if preview_dir:
                image.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
                image.save(preview_dir / f'page-{index+1:02d}.png')
            image.close()
            expected['verified'] = True
            pymupdf.TOOLS.store_shrink(100)
            if progress:
                progress({'phase': 'verifying', 'completed': index+1, 'total': len(doc)})
    report['verified'] = True

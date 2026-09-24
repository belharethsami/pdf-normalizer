import hashlib
import tempfile
import time
import unittest
from pathlib import Path

import pymupdf
from fastapi.testclient import TestClient
from normalizer.server import Settings, create_app


def sample_pdf():
    with pymupdf.open() as doc:
        page = doc.new_page(width=288, height=216)
        page.insert_text((24, 60), 'PUBLIC TEST DOCUMENT', fontsize=15)
        page.insert_text((24, 120), 'COVERED CONTENT', fontsize=15)
        page.draw_rect((20, 95, 250, 130), color=None, fill=(1, 1, 1))
        return doc.tobytes()


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def app(self, **kwargs):
        return create_app(Settings(data_dir=self.root, disk_reserve_bytes=0, **kwargs))

    def new_job(self, client, data):
        response = client.post('/api/jobs', json={'name': 'test.pdf', 'size': len(data)})
        self.assertEqual(response.status_code, 201, response.text)
        row = response.json()
        return row, {'Authorization': f"Bearer {row['token']}"}

    def test_resumable_upload_auth_cancel_and_limits(self):
        data = sample_pdf()
        with TestClient(self.app(run_workers=False, chunk_bytes=512)) as client:
            row, auth = self.new_job(client, data)
            path = f"/api/jobs/{row['id']}"
            self.assertEqual(client.get(path).status_code, 404)
            self.assertEqual(client.get(path, headers={'Authorization': 'Bearer wrong'}).status_code, 404)
            self.assertEqual(client.post(path+'/complete', headers=auth).status_code, 409)
            self.assertEqual(client.patch(path+'/upload', headers={**auth, 'Upload-Offset': '0'}, content=data[:300]).json()['uploaded'], 300)
            mismatch = client.patch(path+'/upload', headers={**auth, 'Upload-Offset': '0'}, content=data[:300])
            self.assertEqual(mismatch.status_code, 409)
            self.assertEqual(mismatch.json()['uploaded'], 300)
            self.assertEqual(client.patch(path+'/upload', headers={**auth, 'Upload-Offset': '300'}, content=b'a'*513).status_code, 413)
            for offset in range(300, len(data), 512):
                self.assertEqual(client.patch(path+'/upload', headers={**auth, 'Upload-Offset': str(offset)}, content=data[offset:offset+512]).status_code, 200)
            self.assertEqual((self.root/'jobs'/row['id']/'input.pdf').read_bytes(), data)
            self.assertEqual(client.post(path+'/complete', headers=auth).json()['status'], 'queued')
            self.assertEqual(client.post(path+'/complete', headers=auth).json()['status'], 'queued')
            self.assertEqual(client.get(path+'/download', headers=auth).status_code, 409)
            self.assertEqual(client.post(path+'/cancel', headers=auth).json()['status'], 'cancelled')
            self.assertFalse((self.root/'jobs'/row['id']).exists())
            self.assertEqual(client.post('/api/jobs', json={'name':'x.pdf', 'size': 10}, headers={'Origin': 'https://untrusted.example'}).status_code, 403)
            self.assertEqual(client.post('/api/jobs', json={'name':'x.exe', 'size':10}).status_code, 400)

    def test_real_worker_finishes_and_download_supports_ranges(self):
        data = sample_pdf()
        with TestClient(self.app(workers=1)) as client:
            row, auth = self.new_job(client, data)
            path = f"/api/jobs/{row['id']}"
            self.assertEqual(client.patch(path+'/upload', headers={**auth, 'Upload-Offset':'0'}, content=data).status_code, 200)
            self.assertEqual(client.post(path+'/complete', headers=auth).status_code, 200)
            deadline = time.monotonic()+90
            while time.monotonic() < deadline:
                status = client.get(path, headers=auth).json()
                if status['status'] in ('done', 'error'):
                    break
                time.sleep(.1)
            self.assertEqual(status['status'], 'done', status)
            response = client.get(path+'/download', headers=auth)
            self.assertEqual(response.status_code, 200)
            self.assertIn('attachment', response.headers['content-disposition'])
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertEqual(client.get(path+'/download', headers={**auth, 'Range':'bytes=0-7'}).content, b'%PDF-1.7')
            self.assertEqual(client.get(path+'/download').status_code, 404)
            with pymupdf.open(stream=response.content) as doc:
                self.assertEqual(doc[0].get_text(), '')
                self.assertEqual(doc[0].get_drawings(), [])
                self.assertEqual(len(doc[0].get_images()), 1)
            self.assertFalse((self.root/'jobs'/row['id']/'input.pdf').exists())

    def test_expiry_restart_and_bad_pdf(self):
        data = sample_pdf()
        app = self.app(run_workers=False)
        with TestClient(app) as client:
            row, auth = self.new_job(client, data)
            path = f"/api/jobs/{row['id']}"
            app.state.store.update(row['id'], status='processing')
        # Service restarts preserve jobs and requeue interrupted processing.
        with TestClient(self.app(run_workers=False)) as client:
            self.assertEqual(client.get(path, headers=auth).json()['status'], 'queued')
            row2, auth2 = self.new_job(client, b'invalid')
            path2 = f"/api/jobs/{row2['id']}"
            client.patch(path2+'/upload', headers={**auth2, 'Upload-Offset':'0'}, content=b'invalid')
            self.assertEqual(client.post(path2+'/complete', headers=auth2).status_code, 422)
            app.state.store.update(row2['id'], expires=time.time()-1)
            self.assertEqual(client.get(path2, headers=auth2).status_code, 410)
        with TestClient(self.app(run_workers=False)):
            deadline = time.monotonic()+3
            while (self.root/'jobs'/row2['id']).exists() and time.monotonic()<deadline:
                time.sleep(.05)
            self.assertFalse((self.root/'jobs'/row2['id']).exists())

if __name__ == '__main__':
    unittest.main()

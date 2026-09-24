from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import sys
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
ACTIVE = ('uploading', 'queued', 'processing', 'verifying')


@dataclass
class Settings:
    data_dir: Path
    max_upload_bytes: int = 20*1024**3
    chunk_bytes: int = 8*1024**2
    workers: int = 2
    max_jobs: int = 20
    retention_seconds: int = 24*3600
    timeout_seconds: int = 6*3600
    disk_reserve_bytes: int = 1024**3
    run_workers: bool = True

    @classmethod
    def environment(cls):
        return cls(data_dir=Path(os.environ.get('DATA_DIR', './data')).resolve(),
                   max_upload_bytes=int(os.environ.get('MAX_UPLOAD_GB', '20'))*1024**3,
                   workers=int(os.environ.get('JOB_WORKERS', '2')),
                   max_jobs=int(os.environ.get('MAX_ACTIVE_JOBS', '20')),
                   retention_seconds=int(os.environ.get('RETENTION_HOURS', '24'))*3600)


class NewJob(BaseModel):
    name: str = Field(min_length=1, max_length=240)
    size: int = Field(gt=0)


class Store:
    def __init__(self, root):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        (root/'jobs').mkdir(exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, token TEXT NOT NULL, name TEXT NOT NULL,
                size INTEGER NOT NULL, uploaded INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                expires REAL NOT NULL, completed INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL DEFAULT 0, output_size INTEGER NOT NULL DEFAULT 0,
                error TEXT, client TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created)')
        salt_path = root/'client-salt'
        if not salt_path.exists():
            salt_path.write_bytes(secrets.token_bytes(32))
            salt_path.chmod(0o600)
        self.salt = salt_path.read_bytes()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root/'jobs.sqlite3', timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def directory(self, job_id):
        return self.root/'jobs'/job_id

    def get(self, job_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        return dict(row) if row else None

    def update(self, job_id, **values):
        values['updated'] = time.time()
        with self.connect() as db:
            db.execute('UPDATE jobs SET '+', '.join(f'{k}=?' for k in values)+' WHERE id=?', (*values.values(), job_id))

    def claim(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET status='processing', updated=? WHERE id=?", (time.time(), row['id']))
        return dict(row) if row else None


def create_app(settings: Settings | None = None):
    settings = settings or Settings.environment()
    store = Store(settings.data_dir)
    locks: dict[str, asyncio.Lock] = {}
    processes: dict[str, asyncio.subprocess.Process] = {}
    stopping = asyncio.Event()

    def lock_for(job_id):
        return locks.setdefault(job_id, asyncio.Lock())

    def authenticated(job_id, request, query_token=None):
        if len(job_id) != 32 or any(c not in '0123456789abcdef' for c in job_id):
            raise HTTPException(404, 'File not found.')
        header = request.headers.get('authorization', '')
        token = header[7:] if header.startswith('Bearer ') else query_token or ''
        row = store.get(job_id)
        if not row or not hmac.compare_digest(row['token'], hashlib.sha256(token.encode()).hexdigest()):
            raise HTTPException(404, 'File not found.')
        if row['expires'] < time.time():
            raise HTTPException(410, 'This file has expired. Upload it again to create a new download.')
        return row

    def public(row):
        result = {k: row[k] for k in ('id', 'name', 'size', 'uploaded', 'status', 'created', 'expires', 'completed', 'total', 'output_size', 'error')}
        if row['status'] == 'queued':
            with store.connect() as db:
                result['queue_position'] = db.execute("SELECT count(*) FROM jobs WHERE status='queued' AND created<=?", (row['created'],)).fetchone()[0]
        return result

    async def stop_process(proc):
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 8)
            except asyncio.TimeoutError:
                with suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()

    async def run_job(row):
        job_id = row['id']
        directory = store.directory(job_id)
        env = dict(os.environ)
        free = shutil.disk_usage(settings.data_dir).free-settings.disk_reserve_bytes
        env['WORKER_OUTPUT_LIMIT'] = str(max(1024**2, free // max(settings.workers, 1)))
        env['OMP_NUM_THREADS'] = '1'
        env['OPENBLAS_NUM_THREADS'] = '1'
        proc = await asyncio.create_subprocess_exec(sys.executable, '-m', 'normalizer.worker', str(directory),
                 cwd=ROOT, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        processes[job_id] = proc
        error = None
        success = False
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                async for line in proc.stdout:
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    current = store.get(job_id)
                    if not current or current['status'] == 'cancelled':
                        await stop_process(proc)
                        break
                    if event.get('phase') in ('processing', 'verifying'):
                        store.update(job_id, status=event['phase'], completed=int(event['completed']), total=int(event['total']))
                    elif event.get('phase') == 'error':
                        error = event.get('message', 'Processing failed.')[:400]
                    elif event.get('phase') == 'done':
                        success = True
                code = await proc.wait()
            current = store.get(job_id)
            if current and current['status'] != 'cancelled':
                if code == 0 and success and (directory/'output.pdf').is_file():
                    store.update(job_id, status='done', output_size=(directory/'output.pdf').stat().st_size,
                                 expires=time.time()+settings.retention_seconds)
                    (directory/'input.pdf').unlink(missing_ok=True)
                else:
                    store.update(job_id, status='error', error=error or 'The processing worker stopped. Try a smaller set of pages.')
        except asyncio.CancelledError:
            await stop_process(proc)
            current = store.get(job_id)
            if current and current['status'] in ('processing', 'verifying'):
                store.update(job_id, status='queued', completed=0)
            raise
        except TimeoutError:
            await stop_process(proc)
            store.update(job_id, status='error', error='This job exceeded the processing time limit. Try a smaller set of pages.')
        finally:
            processes.pop(job_id, None)
            (directory/'output.partial.pdf').unlink(missing_ok=True)
            for path in directory.glob('raster-*'):
                shutil.rmtree(path, ignore_errors=True)
            current = store.get(job_id)
            if current and current['status'] == 'cancelled':
                shutil.rmtree(directory, ignore_errors=True)

    async def worker_loop():
        while not stopping.is_set():
            row = store.claim()
            if row:
                try:
                    await run_job(row)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    store.update(row['id'], status='error', error='The processing service encountered an error. Please try again.')
            else:
                await asyncio.sleep(0.5)

    async def cleanup_loop():
        while not stopping.is_set():
            with store.connect() as db:
                expired = db.execute("SELECT id FROM jobs WHERE expires<? AND status NOT IN ('processing','verifying')", (time.time(),)).fetchall()
            for row in expired:
                job_id = row['id']
                async with lock_for(job_id):
                    shutil.rmtree(store.directory(job_id), ignore_errors=True)
                    with store.connect() as db:
                        db.execute('DELETE FROM jobs WHERE id=?', (job_id,))
                locks.pop(job_id, None)
            await asyncio.sleep(60)

    @asynccontextmanager
    async def lifespan(app):
        with store.connect() as db:
            db.execute("UPDATE jobs SET status='queued', completed=0 WHERE status IN ('processing','verifying')")
        tasks = [asyncio.create_task(cleanup_loop())]
        if settings.run_workers:
            tasks += [asyncio.create_task(worker_loop()) for _ in range(settings.workers)]
        yield
        stopping.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store

    @app.middleware('http')
    async def protections(request, call_next):
        # Mutations require same-origin browser requests; API job tokens are also required.
        origin = request.headers.get('origin')
        if request.method not in ('GET', 'HEAD') and origin:
            from urllib.parse import urlsplit
            if urlsplit(origin).netloc != request.headers.get('host'):
                return JSONResponse({'detail': 'Cross-origin requests are not permitted.'}, status_code=403)
        length = request.headers.get('content-length')
        limit = settings.chunk_bytes if request.url.path.endswith('/upload') else 16*1024
        if request.method in ('POST', 'PATCH') and (length is None or not length.isdigit() or int(length)>limit):
            return JSONResponse({'detail': 'Request body is missing a length or exceeds the request limit.'}, status_code=413)
        response = await call_next(request)
        response.headers.update({
            'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
            'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/healthz')
    async def health():
        with store.connect() as db:
            db.execute('SELECT 1')
        return {'status': 'ok'}

    @app.get('/api/config')
    async def config():
        return {'max_upload_bytes': settings.max_upload_bytes, 'chunk_bytes': settings.chunk_bytes,
                'retention_hours': settings.retention_seconds//3600}

    @app.post('/api/jobs', status_code=201)
    async def create_job(payload: NewJob, request: Request):
        name = payload.name.replace('\\', '/').rsplit('/', 1)[-1]
        if not name.lower().endswith('.pdf') or any(ord(c)<32 for c in name):
            raise HTTPException(400, 'Choose a PDF file.')
        if payload.size > settings.max_upload_bytes:
            raise HTTPException(413, 'This file exceeds the current upload limit.')
        client = hmac.new(store.salt, (request.client.host if request.client else 'unknown').encode(), 'sha256').hexdigest()
        now = time.time()
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            active = db.execute("SELECT count(*), coalesce(sum(size-uploaded),0) FROM jobs WHERE status IN ('uploading','queued','processing','verifying')").fetchone()
            if active[0] >= settings.max_jobs:
                raise HTTPException(503, 'The queue is full. Please try again later.')
            recent = db.execute('SELECT count(*) FROM jobs WHERE client=? AND created>?', (client, now-3600)).fetchone()[0]
            if recent >= 10:
                raise HTTPException(429, 'Too many new uploads. Please try again later.')
            if payload.size+active[1]+settings.disk_reserve_bytes > shutil.disk_usage(settings.data_dir).free:
                raise HTTPException(507, 'There is not enough storage available for this upload. Please try again later.')
            job_id, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
            directory = store.directory(job_id)
            directory.mkdir(mode=0o700)
            (directory/'input.pdf').touch(mode=0o600)
            db.execute('INSERT INTO jobs (id,token,name,size,status,created,updated,expires,client) VALUES (?,?,?,?,?,?,?,?,?)',
                       (job_id, hashlib.sha256(token.encode()).hexdigest(), name, payload.size, 'uploading', now, now, now+settings.retention_seconds, client))
        return {**public(store.get(job_id)), 'token': token}

    @app.get('/api/jobs/{job_id}')
    async def job_status(job_id: str, request: Request):
        return public(authenticated(job_id, request))

    @app.patch('/api/jobs/{job_id}/upload')
    async def upload(job_id: str, request: Request):
        authenticated(job_id, request)
        async with lock_for(job_id):
            row = authenticated(job_id, request)
            if row['status'] != 'uploading':
                raise HTTPException(409, 'This upload is already complete or cancelled.')
            try:
                offset = int(request.headers.get('upload-offset', '-1'))
            except ValueError:
                offset = -1
            if offset != row['uploaded']:
                return JSONResponse({'detail': 'Upload offset does not match.', 'uploaded': row['uploaded']}, status_code=409)
            expected = int(request.headers.get('content-length', '0'))
            if expected <= 0 or offset+expected > row['size']:
                raise HTTPException(413, 'This chunk exceeds the declared file size.')
            if shutil.disk_usage(settings.data_dir).free < expected+settings.disk_reserve_bytes:
                raise HTTPException(507, 'Upload paused because storage is temporarily full.')
            path = store.directory(job_id)/'input.pdf'
            received = 0
            with path.open('r+b') as file:
                file.truncate(offset)
                file.seek(offset)
                try:
                    async for chunk in request.stream():
                        received += len(chunk)
                        if received > expected or received > settings.chunk_bytes:
                            raise HTTPException(413, 'Chunk is too large.')
                        file.write(chunk)
                    if received != expected:
                        raise HTTPException(400, 'Chunk was interrupted. Resume the upload.')
                    file.flush()
                    os.fsync(file.fileno())
                except BaseException:
                    file.truncate(offset)
                    raise
            store.update(job_id, uploaded=offset+received)
            return {'uploaded': offset+received}

    @app.post('/api/jobs/{job_id}/complete')
    async def complete(job_id: str, request: Request):
        authenticated(job_id, request)
        async with lock_for(job_id):
            row = authenticated(job_id, request)
            if row['status'] != 'uploading':
                if row['status'] in ('queued','processing','verifying','done'):
                    return public(row)
                raise HTTPException(409, 'This upload is not active.')
            if row['uploaded'] != row['size']:
                raise HTTPException(409, 'The upload is not finished.')
            with (store.directory(job_id)/'input.pdf').open('rb') as file:
                if b'%PDF-' not in file.read(1024):
                    store.update(job_id, status='error', error='The selected file is not a PDF.')
                    raise HTTPException(422, 'The selected file is not a PDF.')
            store.update(job_id, status='queued')
            return public(store.get(job_id))

    @app.post('/api/jobs/{job_id}/cancel')
    async def cancel(job_id: str, request: Request):
        authenticated(job_id, request)
        async with lock_for(job_id):
            store.update(job_id, status='cancelled')
            if proc := processes.get(job_id):
                await stop_process(proc)
            else:
                shutil.rmtree(store.directory(job_id), ignore_errors=True)
            return {'status': 'cancelled'}

    @app.get('/api/jobs/{job_id}/download')
    async def download(job_id: str, request: Request, token: str = ''):
        row = authenticated(job_id, request, token)
        path = store.directory(job_id)/'output.pdf'
        if row['status'] != 'done' or not path.is_file():
            raise HTTPException(409, 'The download is not ready.')
        filename = row['name'][:-4]+' - normalized.pdf'
        return FileResponse(path, media_type='application/pdf', filename=filename)

    app.mount('/', StaticFiles(directory=ROOT/'static', html=True), name='website')
    return app


app = create_app()

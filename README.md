# PDF Normalizer

A PDF upload service that runs locally in Docker and can be deployed to an approved server. Every visible page is rendered at **150 dpi**, doubled with nearest-neighbor resampling, and saved as one **lossless RGB JPEG2000 image** (fixed 1024×1024 encoding tiles bound working memory) in a fresh PDF. This common base prevents mixed 150/300 dpi source sheets from retaining different 2× pixel-alignment signatures.

Visible text stays visible. Selectable text, original objects, metadata, attachments, interactive forms, links, and vector overlays are not copied. Whiteouts are baked into the pixels. Effective detail is 150 dpi; writing 300 dpi images does not restore detail. This is normalization, not a guarantee that every possible forensic difference in the source is eliminated. The output may be substantially larger.

## Run locally

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock
python scripts/package_source.py
DATA_DIR=./data uvicorn normalizer.server:app --host 127.0.0.1 --port 10000 --no-access-log
```

Open http://127.0.0.1:10000 . The local default runs two workers; set `JOB_WORKERS=1` on smaller machines.

## Production-equivalent container

```sh
docker compose up --build -d
docker compose exec --user 10001 normalizer python -m unittest discover -s tests -v
```

Open http://127.0.0.1:8767 . This address is available only on the host computer. Leave the computer awake and Docker running during processing. Jobs and downloads are saved in the `pdf-jobs` Docker volume across application restarts. The container restarts when Docker starts, unless explicitly stopped.

To stop this app without deleting its saved files, run `docker compose stop`. To start it again, run `docker compose up -d`.

The pinned Linux/amd64 image, Python dependencies, entrypoint, mounted data directory, two workers, 8 GB memory, and 4 CPU allocation match the larger Render example configuration. On an ARM Mac, Linux/amd64 runs under emulation; throughput does not predict Render performance. Docker Desktop must have enough memory allocated.

## Deployment budget and Render example

The current approved budget is **no more than $10/month**. No paid service has been provisioned. There is intentionally no default `render.yaml`: the larger-server example below exceeds that budget and must not be deployed without a new explicit spending approval.

`render.large-example.yaml` preserves the previously tested larger-server configuration for reference only. Proposed resources are **4 CPUs, 8 GB RAM, and a 100 GB persistent disk** in Oregon. At Render's September 2026 listed prices, base infrastructure is **$200/month** ($175 compute + $25 disk), excluding bandwidth, workspace fees, taxes, and other usage charges. No service is created by checking out this code.

Use one service instance and one Uvicorn process: SQLite and the job files share the persistent disk. The API runs as UID/GID 10001; only disk initialization runs as root. The startup script creates/chowns the mount root, drops privileges, and starts Uvicorn. Keep access logs disabled so private download tokens are not logged by the application. Hosting infrastructure may still log URLs; treat a download link as a secret. The service needs no external API keys.

Render disks cannot be shared across instances, and attached disks prevent zero-downtime deploys. Deployments interrupt processing; jobs restart from the source after the service returns. Uploads resume from the last acknowledged chunk. Automatic deployments are disabled. For horizontal scaling, move job storage to object storage and replace SQLite with a shared queue/database first.

## Limits and retention

- 20 GiB per upload by default, streamed in 8 MiB chunks. Memory does not grow with total upload size.
- Two concurrent processing jobs; 20 active jobs; ten new uploads per client address per hour.
- 10,000 pages, 100 million output pixels per page, 3 GiB address-space limit per worker, six-hour job timeout. Complex PDFs can exceed processing memory even below the upload limit.
- Temporary image files and partial outputs use the persistent disk. A disk reserve and per-process file-size cap protect service availability. Input size alone cannot predict output size; large jobs can still fail when storage runs out.
- Successful uploads are deleted when the verified output is ready. Results expire 24 hours later. Incomplete/failed jobs expire 24 hours after creation. Cleanup runs each minute. Cancel/delete removes the active files immediately, subject to an already-streaming download finishing.
- Render automatically snapshots persistent disks; backups may retain copies beyond the active-file retention window. This service does not promise immediate removal from provider backups.
- Each job has an unguessable token, stored hashed on the server. The browser remembers the current job locally; users sharing a browser profile can access that job. No public job listing, account, analytics, or third-party client scripts.

## Validation

```sh
python -m unittest discover -s tests -v
python scripts/http_canary.py http://127.0.0.1:8767
```

Validated on 2026-09-24: all six tests passed inside the pinned Linux/amd64 container as UID 10001. Two simultaneous 36×24-inch synthetic sheets completed with the 8 GiB / 4 CPU container limits and the 3 GiB worker limit; both decoded results passed the pixel and whiteout checks. Desktop and 390 px mobile browser flows were inspected, including job recovery after reload. This local validation uses amd64 emulation on an ARM Mac; a live Render canary remains required after deployment.

Tests exercise mixed-resolution pages and covered content, password protection, oversized pages, resumable offsets, authorization, cancellation, queue recovery, retention, and actual worker/download behavior. Before releasing a result, the worker validates page count, physical size, one image per page, image encoding/name, absence of text/vectors/forms/attachments, decoded pixel hashes, and the common 2×2 pixel pattern.

The HTTP canary generates a disposable synthetic document and submits it to the supplied service. Never use a private document for a remote canary without the owner's authorization.

## License

AGPL-3.0-or-later. `/source.zip` offers the exact application source and build instructions, generated from an explicit file allowlist at image build time. See `NOTICE` for upstream attribution.

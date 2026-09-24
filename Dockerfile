FROM python:3.12-slim-bookworm@sha256:1aaa65a85fda306ffb8b910824d4e93bdce61e212c7e87168123ea3073b41a1a
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/var/data PORT=10000 JOB_WORKERS=2 WORKER_MEMORY_MB=3072 \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY normalizer normalizer
COPY static static
COPY scripts scripts
COPY tests tests
COPY Dockerfile compose.yaml render.large-example.yaml README.md LICENSE NOTICE requirements.txt requirements-dev.txt .dockerignore .gitignore ./
RUN python scripts/package_source.py && mkdir -p /var/data && chown 10001:10001 /var/data
EXPOSE 10000
CMD ["python", "scripts/start.py"]

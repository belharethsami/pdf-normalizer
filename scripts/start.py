"""Initialize the mounted disk, then permanently drop root before serving PDFs."""
import os
from pathlib import Path

root = Path(os.environ.get('DATA_DIR', '/var/data'))
root.mkdir(parents=True, exist_ok=True)
if os.getuid() == 0:
    os.chown(root, 10001, 10001)
    os.chmod(root, 0o700)
    os.setgroups([])
    os.setgid(10001)
    os.setuid(10001)
os.umask(0o077)
os.execvp('python', ['python', '-m', 'uvicorn', 'normalizer.server:app', '--host', '0.0.0.0',
                     '--port', os.environ.get('PORT', '10000'), '--workers', '1',
                     '--no-access-log', '--timeout-graceful-shutdown', '20'])

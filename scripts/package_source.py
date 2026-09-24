"""Bundle only an explicit allowlist: never package uploads or local data."""
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parent.parent
files = ['Dockerfile', 'compose.yaml', 'render.large-example.yaml', 'README.md', 'LICENSE', 'NOTICE',
         'requirements.txt', 'requirements-dev.txt', 'requirements.lock', '.dockerignore', '.gitignore']
for directory in ('normalizer', 'static', 'scripts', 'tests'):
    files.extend(str(p.relative_to(root)) for p in (root/directory).rglob('*')
                 if p.is_file() and p.suffix in ('.py', '.html', '.css', '.js', '.svg'))
with zipfile.ZipFile(root/'static/source.zip', 'w', compression=zipfile.ZIP_DEFLATED) as archive:
    for name in sorted(files):
        archive.write(root/name, 'pdf-normalizer/'+name)

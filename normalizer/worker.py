"""Isolated, restartable command-line job runner used by the web service."""
import argparse
import json
import os
from pathlib import Path
import resource
import sys

from normalizer.pipeline import normalize, verify


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if sys.platform == 'linux':
        memory = int(os.environ.get('WORKER_MEMORY_MB', '3072'))*1024**2
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        limit = int(os.environ.get('WORKER_OUTPUT_LIMIT', str(40*1024**3)))
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    def progress(event):
        print(json.dumps(event), flush=True)
    try:
        report = normalize(directory/'input.pdf', directory/'output.partial.pdf', work_dir=directory, progress=progress)
        verify(directory/'output.partial.pdf', report, progress=progress)
        (directory/'output.partial.pdf').replace(directory/'output.pdf')
        (directory/'report.json').write_text(json.dumps(report, indent=2))
        progress({'phase': 'done', 'pages': report['page_count'], 'output_size': (directory/'output.pdf').stat().st_size})
    except ValueError as exc:
        progress({'phase': 'error', 'message': str(exc)})
        return 1
    except (MemoryError, OSError):
        progress({'phase': 'error', 'message': 'This PDF exceeds the available processing memory or storage. Try a smaller set of pages.'})
        return 1
    except Exception:
        progress({'phase': 'error', 'message': 'This PDF could not be rebuilt. It may be damaged or use an unsupported feature.'})
        return 1
    return 0

if __name__ == '__main__':
    raise SystemExit(main())

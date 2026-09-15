"""Build a local ZIP; never fetch remote URLs or read outside downloads."""
from pathlib import Path
import tempfile
from urllib.parse import urlsplit, unquote
import zipfile


def build_video_archive(rows, download_dir):
    root = Path(download_dir).resolve()
    files = []
    for row in rows:
        name = unquote(urlsplit(row.get('video_url') or '').path).rsplit('/', 1)[-1]
        file = (root / name).resolve()
        if file.parent == root and file.suffix.lower() == '.mp4' and file.is_file():
            files.append(file)
    files = list(dict.fromkeys(files))
    if not files:
        raise ValueError('Không có file video hoàn thành còn lưu trên máy.')
    handle = tempfile.NamedTemporaryFile(prefix='dola-videos-', suffix='.zip', delete=False)
    target = Path(handle.name)
    handle.close()
    try:
        with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_STORED) as archive:
            for file in files:
                archive.write(file, file.name)
        return target, len(files), len(rows)-len(files)
    except BaseException:
        target.unlink(missing_ok=True)
        raise

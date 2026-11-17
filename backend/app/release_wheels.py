"""Build an offline --require-hashes lock from already downloaded wheel files."""
from __future__ import annotations

from email.parser import BytesParser
from pathlib import Path
import re
import zipfile

from .release_files import file_record, no_link
from .release_info import ReleaseError


def wheel_lock(folder):
    folder = no_link(folder)
    records, packages = [], {}
    for path in sorted(folder.glob('*.whl')):
        record = file_record(folder, path)
        try:
            with zipfile.ZipFile(path) as archive:
                metadata = [entry for entry in archive.infolist() if entry.filename.count('/') == 1 and entry.filename.endswith('.dist-info/METADATA')]
                if len(metadata) != 1 or metadata[0].file_size > 4 * 1024**2:
                    raise ReleaseError('Wheel 元数据结构无效或过大')
                message = BytesParser().parsebytes(archive.read(metadata[0]))
        except zipfile.BadZipFile:
            raise ReleaseError('Wheel 不是有效 ZIP 文件') from None
        if len(message.get_all('Name', [])) != 1 or len(message.get_all('Version', [])) != 1:
            raise ReleaseError('Wheel 包名或版本字段无效')
        name, version = message['Name'], message['Version']
        if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]*', name) or not re.fullmatch('[A-Za-z0-9.!+_-]+', version):
            raise ReleaseError('Wheel 包名或版本不安全')
        name = re.sub(r'[-_.]+', '-', name).lower()
        parts = path.stem.split('-')
        if len(parts) not in (5, 6) or re.sub(r'[-_.]+', '-', parts[0]).lower() != name or parts[1] != version:
            raise ReleaseError('Wheel 文件名与元数据不一致')
        existing = packages.setdefault(name, {'version': version, 'hashes': []})
        if existing['version'] != version:
            raise ReleaseError('Wheelhouse 包含同一依赖的多个版本：' + name)
        existing['hashes'].append(record['sha256'])
        records.append({**record, 'name': name, 'version': version, 'tags': parts[-3:],
                        'requires_python': message.get('Requires-Python')})
    if not records:
        raise ReleaseError('Wheelhouse 为空')
    text = '# Exact wheel artifacts. Install only with --no-index --require-hashes.\n'
    text += '\n'.join(name + '==' + item['version'] + ''.join(' \\\n    --hash=sha256:' + h for h in sorted(item['hashes'])) for name, item in sorted(packages.items())) + '\n'
    return text, {'format': 'scholarflow-wheelhouse', 'version': 1, 'files': records, 'package_count': len(packages)}

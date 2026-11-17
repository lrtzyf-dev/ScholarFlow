"""Install a built frontend into the existing Windows bundle, keeping a rollback copy.

Run from any directory: python desktop/install_frontend.py
Close ScholarFlow before installing so the page cannot request mixed asset versions.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def hashes(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob('*') if path.is_file()
    }


def main() -> None:
    source = ROOT / 'frontend' / 'dist'
    bundle = ROOT / 'ScholarFlow程序' / 'ScholarFlow'
    target = bundle / '_internal' / 'frontend'
    if not (source / 'index.html').is_file() or not (source / 'assets').is_dir():
        raise SystemExit('Build frontend first: cd frontend && npm run build')
    if not (bundle / 'ScholarFlow.exe').is_file() or not target.is_dir():
        raise SystemExit('Existing ScholarFlow desktop bundle not found')

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backups = ROOT / 'ScholarFlow程序' / '界面备份'
    backup = backups / stamp
    staged = target.with_name('frontend-staging-' + stamp)
    # Check all locations before moving anything; never replace a parent directory.
    for path in (target, staged):
        if not path.resolve().is_relative_to(bundle.resolve()):
            raise SystemExit('Unexpected bundle location')
    shutil.copytree(source, staged)
    expected = hashes(source)
    if hashes(staged) != expected:
        raise SystemExit('Staged frontend checksum mismatch; existing bundle unchanged')
    backups.mkdir(parents=True, exist_ok=True)
    target.rename(backup)
    try:
        staged.rename(target)
    except OSError:
        backup.rename(target)
        raise
    manifest = {'installed_at': stamp, 'backup': str(backup), 'files': expected}
    (backups / (stamp + '.json')).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    print(json.dumps({'installed_files': len(expected), 'backup': str(backup),
                      'executable': str(bundle / 'ScholarFlow.exe')}, ensure_ascii=False))


if __name__ == '__main__':
    main()

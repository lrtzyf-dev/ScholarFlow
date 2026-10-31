"""竞赛提交包的本地可重复检查。

只读取文件，不启动服务、不访问网络、不打印环境变量值。
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    ROOT / 'README.md', ROOT / '.env.example', ROOT / 'requirements.txt',
    ROOT / 'backend' / '.env.example', ROOT / 'backend' / 'requirements.txt',
    ROOT / 'frontend' / 'package.json', ROOT / 'frontend' / 'package-lock.json',
    ROOT / 'LICENSE', ROOT / 'LICENSES.md',
    ROOT / 'submission' / '研序ScholarFlow-技术文档.pdf',
    ROOT / 'submission' / '技术文档.md', ROOT / 'submission' / '演示视频脚本.md',
    ROOT / 'submission' / '材料清单.md', ROOT / 'submission' / '在线演示说明.md',
]

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()

def pdf_pages(path: Path) -> int:
    try:
        from pypdf import PdfReader  # type: ignore
        return len(PdfReader(str(path)).pages)
    except Exception:
        # Fallback for the minimal verification environment: /Type /Page is stable
        # in ReportLab output and excludes the /Pages tree node.
        raw = path.read_bytes()
        return len(re.findall(rb'/Type\s*/Page(?:\s|/|>)', raw))

def main() -> int:
    missing = [str(p.relative_to(ROOT)) for p in REQUIRED if not p.is_file()]
    if missing:
        print('FAIL missing:', ', '.join(missing))
        return 1
    pdf = ROOT / 'submission' / '研序ScholarFlow-技术文档.pdf'
    pages = pdf_pages(pdf)
    if not (1 <= pages <= 30):
        print(f'FAIL PDF pages: {pages}')
        return 1

    suspicious = []
    key_pattern = re.compile(r'(?i)(?:sk|ak)-[A-Za-z0-9_-]{16,}')
    scan_files = [p for p in REQUIRED if p.suffix.lower() in {'.py', '.md', '.txt', '.json', '.toml', '.yml', '.yaml', '.env', '.example'}]
    scan_files += [ROOT / 'submission' / 'check_submission.py']
    for path in scan_files:
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        for match in key_pattern.finditer(text):
            line = text.count('\n', 0, match.start()) + 1
            if 'xxxxxxxx' not in match.group(0).lower() and 'example' not in path.name.lower():
                suspicious.append(f'{path.relative_to(ROOT)}:{line}')
    if suspicious:
        print('FAIL possible credential strings:', ', '.join(suspicious[:10]))
        return 1

    print(f'PASS required files: {len(REQUIRED)}')
    print(f'PASS technical PDF: {pages} pages, sha256={sha256(pdf)}')
    print('PASS secret scan: no probable credential strings in submission text')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())

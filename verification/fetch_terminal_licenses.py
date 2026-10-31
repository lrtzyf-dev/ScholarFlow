"""Save unmodified upstream notices for the native libraries bundled by pywinpty."""
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

root = Path(__file__).resolve().parents[1]
destination = root / 'desktop' / 'vendor' / 'terminal-licenses'
sources = {
    'microsoft-terminal-MIT.txt': 'https://raw.githubusercontent.com/microsoft/terminal/main/LICENSE',
    'winpty-MIT.txt': 'https://raw.githubusercontent.com/rprichard/winpty/master/LICENSE',
    'winpty-rs-MIT.txt': 'https://raw.githubusercontent.com/andfoy/winpty-rs/main/LICENSE-MIT',
}
destination.mkdir(parents=True, exist_ok=True)
manifest = []
for filename, url in sources.items():
    with urlopen(url, timeout=30) as response:
        raw = response.read(65536)
    text = raw.decode('utf-8-sig')
    if 'Permission is hereby granted' not in text or 'THE SOFTWARE IS PROVIDED' not in text:
        raise RuntimeError(f'Expected MIT notice from {url}')
    (destination / filename).write_bytes(raw)
    manifest.append({'file': filename, 'source': url, 'sha256': hashlib.sha256(raw).hexdigest()})
(destination / 'sources.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
print(json.dumps(manifest, indent=2))

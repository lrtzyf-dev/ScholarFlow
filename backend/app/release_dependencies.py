"""Installed build-environment inventory, with honest artifact/license coverage."""
from __future__ import annotations

from importlib import metadata
from pathlib import Path
import platform
import re
import sys

from .release_files import contained, file_record, no_link
from .release_info import ReleaseError, canonical_json, read_json


def normalized(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(canonical_json(value))


def save_license(output, component, source):
    source = no_link(source)
    if not source.is_file() or source.stat().st_size > 4 * 1024**2:
        return None
    # Preserve actual license bytes; do not interpret their text as instructions.
    raw = source.read_bytes()
    import hashlib
    name = 'licenses/' + component + '/' + hashlib.sha256(raw).hexdigest()[:16] + '.txt'
    destination = contained(output, name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.read_bytes() != raw:
            raise ReleaseError('许可证据路径对应的内容不一致')
        return name
    destination.write_bytes(raw)
    return name


def python_inventory(output, wheelhouse=None):
    components, pinned, licenses = [], [], []
    wheel_index = {}
    if wheelhouse:
        from packaging.utils import parse_wheel_filename
        for path in sorted(no_link(wheelhouse).glob('*.whl')):
            try:
                name, version, _, _ = parse_wheel_filename(path.name)
            except ValueError:
                raise ReleaseError('wheelhouse 中存在无效 wheel 文件名') from None
            wheel_index.setdefault((normalized(name), str(version)), []).append(file_record(wheelhouse, path))
    missing_artifacts, unknown_licenses = [], []
    grouped = {}
    for dist in metadata.distributions():
        grouped.setdefault(normalized(dist.metadata.get('Name', '')), []).append(dist)
    duplicates = [{'name': name, 'versions': [d.version for d in values]} for name, values in sorted(grouped.items()) if len(values) > 1]
    for slug, instances in sorted(grouped.items()):
        # Inventory may still be produced for a polluted environment, but the
        # lock/build is explicitly blocked. Never silently pick a distributable
        # resolution from conflicting venv/system-site-package metadata.
        dist = instances[0]
        name = dist.metadata.get('Name', '')
        if not name or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name):
            raise ReleaseError('安装依赖的包名无效')
        slug = normalized(name)
        version = dist.version
        if not re.fullmatch(r'[A-Za-z0-9.!+_-]+', version):
            raise ReleaseError('安装依赖的版本无效')
        license_expression = dist.metadata.get('License-Expression', '').strip()
        license_label = dist.metadata.get('License', '').strip()
        license_metadata_text = license_label
        if len(license_label) > 160:
            license_label = 'License text embedded in distribution metadata'
        if license_label in ('UNKNOWN', 'Unknown'):
            license_label = ''
        classifiers = [c.removeprefix('License :: ') for c in dist.metadata.get_all('Classifier', []) if c.startswith('License :: ')]
        if not license_label and not license_expression and classifiers:
            license_label = '; '.join(classifiers)
        evidence = []
        if len(license_metadata_text) > 160:
            destination = Path(output) / 'licenses' / ('python-' + slug) / 'distribution-license-metadata.txt'
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(license_metadata_text, encoding='utf-8')
            evidence.append(destination.relative_to(output).as_posix())
        for item in dist.files or []:
            # Only license/notice paths declared in installed RECORD. Never
            # follow absolute/parent paths or enumerate package configuration.
            parts = Path(str(item)).parts
            if (parts and not Path(str(item)).is_absolute() and '..' not in parts
                    and ((parts[0].endswith(('.dist-info', '.egg-info')) and any(p.lower() == 'licenses' for p in parts))
                         or re.match(r'(?i)^(licen[cs]e|copying|notice|authors|third[-_]?party[-_]?notices?)([._-].*)?$', Path(str(item)).name))):
                saved = save_license(output, 'python-' + slug, dist.locate_file(item))
                if saved:
                    evidence.append(saved)
        if not license_expression and not license_label:
            unknown_licenses.append(slug)
        artifacts = wheel_index.get((slug, version), [])
        if not artifacts:
            missing_artifacts.append(slug)
        if len(instances) > 1:
            pinned.append('# UNRESOLVED duplicate distribution: ' + slug + ' (' + ', '.join(d.version for d in instances) + ')')
        else:
            pinned.append(slug + '==' + version + ''.join(' \\\n    --hash=sha256:' + a['sha256'] for a in artifacts))
        identity = 'pkg:pypi/' + slug + '@' + version
        item = {'type': 'library', 'bom-ref': identity, 'name': name, 'version': version, 'purl': identity,
                'scope': 'optional', 'properties': [
                    {'name': 'scholarflow:inventory-scope', 'value': 'installed-build-environment; not a shipped-runtime attestation'},
                    {'name': 'scholarflow:license-evidence', 'value': ','.join(evidence)}]}
        if license_expression:
            item['licenses'] = [{'expression': license_expression}]
        elif license_label:
            item['licenses'] = [{'license': {'name': license_label}}]
        components.append(item)
        licenses.append({'component': identity, 'license_expression': license_expression or None,
                         'license_label': license_label or None, 'evidence': evidence,
                         'requires_dist': dist.metadata.get_all('Requires-Dist') or [], 'wheel_artifacts': artifacts,
                         'license_classifiers': classifiers,
                         'metadata_conflict': len(instances) > 1})
    lock = Path(output) / 'python-environment.lock.txt'
    lock.write_text('# Exact installed build environment. Use --require-hashes only when wheel coverage is complete.\n' + '\n'.join(pinned) + '\n', encoding='utf-8')
    return components, licenses, {'scope': 'installed_build_environment', 'count': len(components),
        'python': platform.python_version(), 'implementation': sys.implementation.name, 'platform': sys.platform,
        'machine': platform.machine(),
        'wheel_hashes_complete': not missing_artifacts and not duplicates, 'missing_wheel_artifacts': missing_artifacts,
        'duplicate_distributions': duplicates, 'lock_resolution_complete': not duplicates,
        'unknown_license_metadata': unknown_licenses,
        'license_text_missing': [r['component'] for r in licenses if not r['evidence']]}


def npm_inventory(root, output):
    lock = read_json(contained(root, 'frontend/package-lock.json'))
    if lock.get('lockfileVersion') not in (2, 3) or not isinstance(lock.get('packages'), dict):
        raise ReleaseError('前端依赖锁格式不受支持')
    components, records, missing = [], [], []
    for path, entry in sorted(lock['packages'].items()):
        if path == '':
            continue
        if not path.startswith('node_modules/') or not isinstance(entry, dict):
            raise ReleaseError('前端依赖锁含非标准依赖路径')
        package = contained(Path(root) / 'frontend', path)
        name = path.rsplit('node_modules/', 1)[-1]
        version = entry.get('version')
        if entry.get('link') or not isinstance(version, str) or not entry.get('integrity'):
            missing.append(path)
        identity = 'pkg:npm/' + name.replace('@', '%40') + '@' + str(version)
        evidence = []
        if package.is_dir():
            for candidate in package.iterdir():
                if re.match(r'(?i)^(licen[cs]e|copying|notice)([._-].*)?$', candidate.name) and candidate.is_file():
                    import hashlib
                    saved = save_license(output, 'npm-' + hashlib.sha256(path.encode()).hexdigest()[:16], candidate)
                    if saved:
                        evidence.append(saved)
        license_value = entry.get('license')
        component = {'type': 'library', 'bom-ref': 'npm:' + path, 'name': name, 'version': str(version), 'purl': identity,
                     'scope': 'optional' if entry.get('dev') or entry.get('optional') else 'required',
                     'properties': [{'name': 'scholarflow:inventory-scope', 'value': 'npm-lock; actual bundle reachability not yet attested'},
                                    {'name': 'scholarflow:lock-integrity', 'value': str(entry.get('integrity', ''))}]}
        if isinstance(license_value, str):
            # npm license text is not guaranteed to be a valid SPDX expression.
            component['licenses'] = [{'license': {'name': license_value}}]
        components.append(component)
        records.append({'component': identity, 'lock_path': path, 'license_label': license_value,
                        'evidence': evidence, 'integrity': entry.get('integrity'), 'installed': package.is_dir()})
    return components, records, {'count': len(components), 'integrity_missing_or_link': missing,
        'unknown_license_metadata': [r['lock_path'] for r in records if not r['license_label']],
        'license_text_missing': [r['lock_path'] for r in records if not r['evidence']]}


def collect_dependencies(root, output, wheelhouse=None):
    python, pylicenses, pycoverage = python_inventory(output, wheelhouse)
    npm, npmlicenses, npmcoverage = npm_inventory(root, output)
    third_party = []
    runtime_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if runtime_license.is_file():
        third_party.append({'source': 'CPython/LICENSE.txt', 'evidence': save_license(output, 'cpython', runtime_license)})
    source_wheels = contained(root, 'desktop/source-wheel-provenance.json')
    if source_wheels.is_file():
        provenance = read_json(source_wheels)
        write_json(Path(output) / 'source-wheel-provenance.json', provenance)
    else:
        provenance = {'artifacts': []}
    for name in ('LICENSES.md', 'LICENSE', 'desktop/vendor/ripgrep/LICENSE-MIT',
                 'desktop/vendor/ripgrep/UNLICENSE', 'desktop/vendor/ripgrep/COPYING'):
        path = contained(root, name)
        if path.is_file():
            evidence = save_license(output, 'product-notices', path)
            third_party.append({'source': name, 'evidence': evidence})
    frontend_licenses = contained(root, 'frontend/public')
    if frontend_licenses.is_dir():
        from .release_files import walk_regular
        for path in walk_regular(frontend_licenses):
            if re.search(r'(?i)(license|ofl|copying)', path.name):
                third_party.append({'source': path.relative_to(root).as_posix(), 'evidence': save_license(output, 'assets', path)})
    bom = {'bomFormat': 'CycloneDX', 'specVersion': '1.6', 'version': 1,
           'metadata': {'properties': [{'name': 'scholarflow:scope', 'value': 'build environment and npm lock; final binary SBOM pending'}]},
           'components': python + npm}
    write_json(Path(output) / 'sbom.cdx.json', bom)
    write_json(Path(output) / 'license-evidence.json', {'python': pylicenses, 'npm': npmlicenses,
        'product_and_assets': third_party, 'legal_review': 'required_before_distribution'})
    coverage = {'python': pycoverage, 'npm': npmcoverage, 'final_binary_sbom': 'not_generated',
                'license_obligations_review': 'not_completed',
                'source_wheel_license_issues': [{'name': r['name'], 'version': r['version'], 'issue': r['license_issue']}
                    for r in provenance.get('artifacts', []) if r.get('license_issue')]}
    write_json(Path(output) / 'dependency-coverage.json', coverage)
    return coverage


def verify_python_environment(folder, coverage):
    evidence = read_json(Path(folder) / 'license-evidence.json')
    expected = sorted((r['component'].split('/')[1].rsplit('@', 1)[0], r['component'].rsplit('@', 1)[1]) for r in evidence['python'])
    actual = sorted((normalized(d.metadata.get('Name', '')), d.version) for d in metadata.distributions())
    if actual != expected or coverage.get('duplicate_distributions'):
        raise ReleaseError('当前 Python 环境依赖与已准备清单不一致')
    if any(coverage.get(k) != v for k, v in {
        'python': platform.python_version(), 'implementation': sys.implementation.name,
        'platform': sys.platform, 'machine': platform.machine()}.items()):
        raise ReleaseError('当前 Python 解释器或 Windows 架构与已准备清单不一致')
    return True

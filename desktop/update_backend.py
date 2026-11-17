"""Stage, verify, install, or restore the six model-connection modules in the desktop EXE.

python desktop/update_backend.py --stage
python desktop/update_backend.py --verify <backup-directory>
python desktop/update_backend.py --apply <backup-directory>
python desktop/update_backend.py --restore <backup-directory>

Staging leaves the installed EXE unchanged, even while it is running. It creates
a candidate beside it, sharing _internal, and an immutable candidate/backup pair.
Smoke-test the candidate with a separate DATA_DIR before applying. Apply/restore
refuse to replace a running or unexpectedly changed executable.

Bytecode is compiled by the bundled Python 3.13 DLL in an isolated PowerShell
process. The current PE prefix (including icon), other CArchive entries, and all
unmodified PYZ payloads are retained byte-for-byte. No application modules run
during compilation or archive verification; no user configuration is read.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import marshal
import os
from pathlib import Path
import shutil
import struct
import subprocess
import zlib

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "ScholarFlow程序/ScholarFlow/ScholarFlow.exe"
BACKUPS = ROOT / "ScholarFlow程序/后端备份"
HOST = ROOT / "desktop/run_bundled_python.ps1"
MODULES = (
    "app.config",
    "app.api.settings",
    "app.llm",
    "app.pipeline.embeddings",
    "app.diagnostics",
    "app.model_connection",
)
NEW_MODULES = {"app.model_connection"}
COOKIE_MAGIC = b"MEI\014\013\012\013\016"
COOKIE = struct.Struct("!8sIIII64s")
ENTRY = struct.Struct("!IIIIBc")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value):
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def powershell() -> str:
    result = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    if not result:
        raise RuntimeError("PowerShell is required for the isolated Python compiler")
    return result


def assert_stopped(*paths: Path):
    names = sorted({path.stem for path in paths})
    names_ps = ",".join("'" + name.replace("'", "''") + "'" for name in names)
    command = (
        "$ErrorActionPreference='Stop'; "
        f"@(Get-Process -Name {names_ps} -ErrorAction SilentlyContinue | "
        "ForEach-Object { [pscustomobject]@{pid=$_.Id; name=$_.ProcessName; path=$_.Path} }) "
        "| ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        [powershell(), "-NoProfile", "-NonInteractive", "-EncodedCommand",
         base64.b64encode(command.encode("utf-16-le")).decode("ascii")],
        check=True, capture_output=True, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    processes = json.loads(result.stdout.strip() or "[]")
    if isinstance(processes, dict):
        processes = [processes]
    wanted = {os.path.normcase(str(path.resolve())) for path in paths}
    for process in processes:
        image = process.get("path")
        if not image or os.path.normcase(str(Path(image).resolve())) in wanted:
            raise RuntimeError(f"Close {process['name']} (PID {process['pid']}) before replacing it")


class Archive:
    def __init__(self, data: bytes):
        self.data = data
        if data[:2] != b"MZ":
            raise ValueError("Expected a PE executable")
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            raise ValueError("Invalid PE signature")
        optional = pe + 24
        magic = struct.unpack_from("<H", data, optional)[0]
        if magic not in (0x10B, 0x20B):
            raise ValueError("Unsupported PE format")
        directory = optional + (112 if magic == 0x20B else 96)
        if struct.unpack_from("<II", data, directory + 8 * 4) != (0, 0):
            raise ValueError("Signed EXEs must be rebuilt and re-signed, not patched")
        self.cookie_at = data.rfind(COOKIE_MAGIC)
        if self.cookie_at < 0 or self.cookie_at + COOKIE.size != len(data):
            raise ValueError("Expected the PyInstaller cookie at the end of the EXE")
        self.cookie = COOKIE.unpack_from(data, self.cookie_at)
        _, length, toc_offset, toc_length, version, library = self.cookie
        if version != 313 or library.rstrip(b"\0") != b"python313.dll":
            raise ValueError("Only this application's Python 3.13 archive is supported")
        self.start = self.cookie_at + COOKIE.size - length
        self.toc_at = self.start + toc_offset
        if self.start <= optional or self.toc_at + toc_length != self.cookie_at:
            raise ValueError("Invalid outer archive bounds")
        self.toc_raw = data[self.toc_at:self.cookie_at]
        self.entries = []
        position = 0
        names = set()
        while position < len(self.toc_raw):
            size, offset, packed, unpacked, compressed, kind = ENTRY.unpack_from(self.toc_raw, position)
            if size < ENTRY.size + 1 or position + size > len(self.toc_raw):
                raise ValueError("Invalid outer TOC entry")
            name = self.toc_raw[position + ENTRY.size:position + size].split(b"\0", 1)[0].decode("utf-8")
            if name in names or offset + packed > toc_offset:
                raise ValueError("Duplicate or out-of-bounds outer entry")
            names.add(name)
            self.entries.append({"name": name, "offset": offset, "packed": packed,
                                 "unpacked": unpacked, "compressed": compressed,
                                 "kind": kind, "toc_position": position})
            position += size
        pyzs = [entry for entry in self.entries if entry["kind"] == b"z"]
        if len(pyzs) != 1:
            raise ValueError("Expected exactly one PYZ entry")
        self.pyz_entry = pyzs[0]
        entry = self.pyz_entry
        if (entry["compressed"] or entry["packed"] != entry["unpacked"]
                or entry["offset"] + entry["packed"] != toc_offset
                or self.entries[-1] is not entry):
            raise ValueError("Expected an uncompressed PYZ as the final data/TOC entry")
        self.pyz = self.payload(entry)
        if self.pyz[:8] != b"PYZ\0\xf3\x0d\x0d\x0a":
            raise ValueError("Unexpected Python 3.13 PYZ magic")
        self.pyz_toc_at = struct.unpack_from("!I", self.pyz, 8)[0]
        if not 12 <= self.pyz_toc_at < len(self.pyz):
            raise ValueError("Invalid PYZ TOC bounds")
        self.pyz_toc_container = marshal.loads(self.pyz[self.pyz_toc_at:])
        self.modules = dict(self.pyz_toc_container)
        if len(self.modules) != len(self.pyz_toc_container):
            raise ValueError("Duplicate PYZ module names")
        for name, info in self.modules.items():
            if (not isinstance(name, str) or not isinstance(info, tuple) or len(info) != 3
                    or not all(isinstance(value, int) for value in info)):
                raise ValueError("Unsupported PYZ entry shape")
            kind, offset, length = info
            if kind not in (0, 1, 3) or offset < 12 or offset + length > self.pyz_toc_at:
                raise ValueError(f"Unsupported or out-of-bounds PYZ module: {name}")

    def payload(self, entry):
        start = self.start + entry["offset"]
        return self.data[start:start + entry["packed"]]

    def module_payload(self, name):
        _, offset, size = self.modules[name]
        return self.pyz[offset:offset + size]


def rebuild(original: Archive, compiled: dict[str, bytes]) -> bytes:
    # Preserve original order and every unmodified compressed payload.
    result = bytearray(original.pyz[:12])
    entries = []
    for name, (kind, _, _) in original.modules.items():
        payload = zlib.compress(compiled[name], 6) if name in compiled else original.module_payload(name)
        entries.append((name, (kind, len(result), len(payload))))
        result.extend(payload)
    for name in MODULES:
        if name not in original.modules:
            if name not in NEW_MODULES:
                raise ValueError(f"Expected existing module is absent: {name}")
            payload = zlib.compress(compiled[name], 6)
            entries.append((name, (0, len(result), len(payload))))
            result.extend(payload)
    toc_offset = len(result)
    toc = dict(entries) if isinstance(original.pyz_toc_container, dict) else entries
    result.extend(marshal.dumps(toc, 4))  # TOC has only strings, tuples and integers.
    struct.pack_into("!I", result, 8, toc_offset)
    outer_toc = bytearray(original.toc_raw)
    struct.pack_into("!II", outer_toc, original.pyz_entry["toc_position"] + 8, len(result), len(result))
    delta = len(result) - len(original.pyz)
    magic, length, old_toc_offset, toc_length, version, library = original.cookie
    cookie = COOKIE.pack(magic, length + delta, old_toc_offset + delta, toc_length, version, library)
    start = original.start + original.pyz_entry["offset"]
    return original.data[:start] + result + outer_toc + cookie


def verify_preservation(original: Archive, updated: Archive, compiled: dict[str, bytes]):
    if original.data[:original.start] != updated.data[:updated.start]:
        raise ValueError("PE prefix changed; icon/resource preservation failed")
    if len(original.entries) != len(updated.entries):
        raise ValueError("Outer archive entry count changed")
    unchanged_outer = {}
    for before, after in zip(original.entries, updated.entries):
        if before["name"] != after["name"]:
            raise ValueError("Outer entry order changed")
        if before["kind"] != b"z":
            if before != after or original.payload(before) != updated.payload(after):
                raise ValueError(f"Outer archive item changed: {before['name']}")
            unchanged_outer[before["name"]] = digest(original.payload(before))
    added = set(updated.modules) - set(original.modules)
    if added != NEW_MODULES - set(original.modules) or set(original.modules) - set(updated.modules):
        raise ValueError("Unexpected module addition or removal")
    unchanged_modules = {}
    for name, info in original.modules.items():
        if updated.modules[name][0] != info[0]:
            raise ValueError(f"Module/package type changed: {name}")
        if name not in MODULES:
            before = original.module_payload(name)
            if before != updated.module_payload(name):
                raise ValueError(f"Unrequested module changed: {name}")
            unchanged_modules[name] = digest(before)
    for name, code in compiled.items():
        if zlib.decompress(updated.module_payload(name)) != code:
            raise ValueError(f"Compiled module mismatch: {name}")
    return {"pe_prefix_bytes": original.start, "pe_prefix_sha256": digest(original.data[:original.start]),
            "outer_entries": len(original.entries), "unchanged_outer_payloads": unchanged_outer,
            "original_module_count": len(original.modules), "updated_module_count": len(updated.modules),
            "unchanged_module_count": len(unchanged_modules), "unchanged_modules": unchanged_modules,
            "added_modules": sorted(added), "pyz_magic": updated.pyz[:8].hex()}


def run_worker(runtime: Path, script: Path):
    result = subprocess.run(
        [powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
         str(HOST), "-RuntimeDirectory", str(runtime), "-ScriptPath", str(script)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError("Bundled Python compiler failed:\n" + result.stdout + result.stderr)
    return result.stdout.strip()


def compile_modules(original: Archive, backup: Path):
    rows = []
    snapshots = backup / "modules"
    snapshots.mkdir()
    records = {}
    for name in MODULES:
        source = ROOT / "backend" / (name.replace(".", "/") + ".py")
        code = source.read_bytes()
        saved = snapshots / (name + ".py")
        saved.write_bytes(code)
        old_code = snapshots / (name + ".original.marshal")
        output = snapshots / (name + ".updated.marshal")
        if name in original.modules:
            old_code.write_bytes(zlib.decompress(original.module_payload(name)))
        elif name not in NEW_MODULES:
            raise ValueError(f"Required module missing from EXE: {name}")
        rows.append((name, str(saved), str(old_code) if old_code.exists() else None, str(output)))
        records[name] = {"source": str(source), "source_sha256": digest(code), "new": old_code.exists() is False}
    worker = backup / "compile_modules.py"
    worker.write_text(
        "import sys, marshal\n"
        "assert sys.version_info[:2] == (3, 13), sys.version\n"
        f"rows = {rows!r}\n"
        "for name, source, old_path, destination in rows:\n"
        "    filename = name.replace('.', '/') + '.py'\n"
        "    if old_path:\n"
        "        old = marshal.loads(open(old_path, 'rb').read())\n"
        "        assert old.co_name == '<module>'\n"
        "        filename = old.co_filename\n"
        "    code = compile(open(source, 'rb').read(), filename, 'exec', dont_inherit=True, optimize=0)\n"
        "    payload = marshal.dumps(code, 4)\n"
        "    assert marshal.loads(payload).co_code == code.co_code\n"
        "    with open(destination, 'wb') as output: output.write(payload)\n"
        f"with open({str(backup / 'compiler-version.txt')!r}, 'w', encoding='utf-8') as output: output.write(sys.version)\n"
        "print('Compiled and verified', len(rows), 'modules with Python', sys.version.split()[0], flush=True)\n",
        encoding="utf-8",
    )
    output = run_worker(TARGET.parent / "_internal", worker)
    compiled = {name: (snapshots / (name + ".updated.marshal")).read_bytes() for name in MODULES}
    for name in MODULES:
        records[name]["bytecode_sha256"] = digest(compiled[name])
    return compiled, records, output


def verify_runtime_toc(updated: Archive, backup: Path):
    toc_path = backup / "updated-pyz-toc.marshal"
    toc_path.write_bytes(updated.pyz[updated.pyz_toc_at:])
    worker = backup / "verify_toc.py"
    worker.write_text(
        "import sys, marshal\n"
        "assert sys.version_info[:2] == (3, 13)\n"
        f"toc = dict(marshal.loads(open({str(toc_path)!r}, 'rb').read()))\n"
        f"assert len(toc) == {len(updated.modules)}\n"
        f"assert all(name in toc for name in {MODULES!r})\n"
        "print('Python 3.13 verified rebuilt PYZ index', len(toc), flush=True)\n",
        encoding="utf-8",
    )
    return run_worker(TARGET.parent / "_internal", worker)


def stage(candidate: Path | None):
    candidate = (candidate or TARGET.with_name("ScholarFlow-update-test.exe")).resolve()
    if candidate.parent != TARGET.parent.resolve() or candidate == TARGET.resolve() or candidate.suffix.lower() != ".exe":
        raise ValueError("Candidate must be a different .exe beside the installed executable")
    assert_stopped(candidate)
    if candidate.exists():
        raise ValueError("Candidate already exists; choose another --candidate name")
    original_bytes = TARGET.read_bytes()
    original = Archive(original_bytes)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = BACKUPS / stamp
    backup.mkdir(parents=True)
    (backup / "ScholarFlow-original.exe").write_bytes(original_bytes)
    compiled, sources, compiler_output = compile_modules(original, backup)
    updated_bytes = rebuild(original, compiled)
    updated = Archive(updated_bytes)
    preservation = verify_preservation(original, updated, compiled)
    runtime_output = verify_runtime_toc(updated, backup)
    if digest(TARGET.read_bytes()) != digest(original_bytes):
        raise RuntimeError("Installed executable changed while staging; rebuild from the new version")
    staged = backup / "ScholarFlow-updated.exe"
    staged.write_bytes(updated_bytes)
    record = {"schema_version": 1, "state": "staged", "created_utc": stamp,
              "target": str(TARGET), "candidate": str(candidate),
              "original_sha256": digest(original_bytes), "updated_sha256": digest(updated_bytes),
              "original_bytes": len(original_bytes), "updated_bytes": len(updated_bytes),
              "compiler": (backup / "compiler-version.txt").read_text(encoding="utf-8"),
              "sources": sources, "verification": preservation}
    save_json(backup / "manifest.json", record)
    with candidate.open("xb") as output:
        output.write(updated_bytes)
    if digest(candidate.read_bytes()) != record["updated_sha256"]:
        raise ValueError("Candidate copy checksum mismatch")
    print(json.dumps({"staged": str(candidate), "backup": str(backup), "installed_exe_changed": False,
                      "compile": compiler_output, "runtime_verification": runtime_output,
                      "unchanged_modules": preservation["unchanged_module_count"],
                      "updated_modules": list(MODULES)}, ensure_ascii=False))


def load_backup(path: Path):
    path = path.resolve()
    if not path.is_relative_to(BACKUPS.resolve()):
        raise ValueError("Backup must be inside ScholarFlow程序/后端备份")
    record = read_json(path / "manifest.json")
    if record.get("schema_version") != 1 or Path(record["target"]).resolve() != TARGET.resolve():
        raise ValueError("Backup does not describe this executable")
    before = (path / "ScholarFlow-original.exe").read_bytes()
    after = (path / "ScholarFlow-updated.exe").read_bytes()
    if digest(before) != record["original_sha256"] or digest(after) != record["updated_sha256"]:
        raise ValueError("Backup/candidate checksum mismatch")
    return path, record, before, after


def verify(path: Path):
    backup, record, before, after = load_backup(path)
    compiled = {}
    for name in MODULES:
        code = (backup / "modules" / (name + ".updated.marshal")).read_bytes()
        source = (backup / "modules" / (name + ".py")).read_bytes()
        if digest(code) != record["sources"][name]["bytecode_sha256"] or digest(source) != record["sources"][name]["source_sha256"]:
            raise ValueError(f"Module snapshot changed: {name}")
        compiled[name] = code
    details = verify_preservation(Archive(before), Archive(after), compiled)
    if details != record["verification"]:
        raise ValueError("Preservation report differs from manifest")
    print(json.dumps({"verified": str(backup), "state": record["state"],
                      "unchanged_modules": details["unchanged_module_count"],
                      "pe_prefix_unchanged": True}, ensure_ascii=False))


def install(path: Path, restore: bool = False):
    backup, record, before, after = load_backup(path)
    candidate = Path(record["candidate"])
    assert_stopped(TARGET, candidate)
    verify(backup)
    expected = record["updated_sha256"] if restore else record["original_sha256"]
    current = TARGET.read_bytes()
    if digest(current) != expected:
        raise ValueError("Installed executable changed since staging; refusing to overwrite it")
    replacement = before if restore else after
    temporary = TARGET.with_name("ScholarFlow-backend-replacement.tmp")
    if temporary.exists():
        raise ValueError("A replacement staging file already exists")
    with temporary.open("xb") as output:
        output.write(replacement)
    assert_stopped(TARGET, candidate)
    if digest(TARGET.read_bytes()) != expected:
        raise ValueError("Installed executable changed during replacement preparation")
    os.replace(temporary, TARGET)
    if digest(TARGET.read_bytes()) != digest(replacement):
        temporary.write_bytes(current)
        os.replace(temporary, TARGET)
        raise ValueError("Installed checksum mismatch; original executable restored")
    record["state"] = "restored" if restore else "applied"
    record["last_operation_utc"] = datetime.now(timezone.utc).isoformat()
    save_json(backup / "manifest.json", record)
    print(json.dumps({record["state"]: str(TARGET), "backup": str(backup)}, ensure_ascii=False))


def main():
    if os.name != "nt":
        raise SystemExit("Windows is required")
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--stage", action="store_true")
    mode.add_argument("--verify", type=Path)
    mode.add_argument("--apply", type=Path)
    mode.add_argument("--restore", type=Path)
    parser.add_argument("--candidate", type=Path)
    args = parser.parse_args()
    if args.candidate and not args.stage:
        parser.error("--candidate is only valid with --stage")
    if args.stage:
        stage(args.candidate)
    elif args.verify:
        verify(args.verify)
    else:
        install(args.restore or args.apply, restore=args.restore is not None)


if __name__ == "__main__":
    main()

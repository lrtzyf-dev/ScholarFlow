"""Install a verified desktop update beside the running version.

The original executable, runtime, user data, and shortcuts are left untouched.
After isolated smoke checks pass, shortcuts can be pointed at the new bundle.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

from update_backend import ROOT, TARGET, load_backup, verify


def hashes(directory: Path) -> dict[str, str]:
    result = {}
    for path in directory.rglob("*"):
        if path.is_file():
            with path.open("rb") as source:
                result[path.relative_to(directory).as_posix()] = hashlib.file_digest(
                    source, "sha256"
                ).hexdigest()
    return result


def install(backup: Path, destination: Path) -> dict:
    backup, record, _, candidate = load_backup(backup)
    verify(backup)
    destination = destination.resolve()
    source = TARGET.parent.resolve()
    if destination.parent != source.parent or destination == source:
        raise ValueError("Destination must be a new bundle beside the original")
    if destination.exists():
        raise ValueError("Destination already exists; refusing to overwrite a bundle")
    frontend = ROOT / "frontend" / "dist"
    runtime = source / "_internal"
    if not (frontend / "index.html").is_file() or not runtime.is_dir():
        raise ValueError("Built frontend or desktop runtime is missing")
    expected_frontend = hashes(frontend)
    expected_runtime = {
        name: digest for name, digest in hashes(runtime).items()
        if not name.startswith("frontend/")
    }
    destination.mkdir()

    def exclude_old_frontend(path: str, names: list[str]) -> list[str]:
        return ["frontend"] if Path(path).resolve() == runtime and "frontend" in names else []

    copied_runtime = destination / "_internal"
    shutil.copytree(runtime, copied_runtime, ignore=exclude_old_frontend)
    shutil.copytree(frontend, copied_runtime / "frontend")
    executable = destination / "ScholarFlow.exe"
    executable.write_bytes(candidate)
    if hashlib.sha256(executable.read_bytes()).hexdigest() != record["updated_sha256"]:
        raise ValueError("Installed executable checksum mismatch")
    actual = hashes(copied_runtime)
    if {name: digest for name, digest in actual.items() if not name.startswith("frontend/")} != expected_runtime:
        raise ValueError("Copied desktop runtime checksum mismatch")
    if hashes(copied_runtime / "frontend") != expected_frontend:
        raise ValueError("Copied frontend checksum mismatch")
    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "executable": str(executable),
        "backend_backup": str(backup),
        "executable_sha256": record["updated_sha256"],
        "frontend": expected_frontend,
        "unchanged_runtime_files": len(expected_runtime),
        "original_bundle_unchanged": True,
        "user_data_read_or_changed": False,
    }
    (destination / "model-discovery-build.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {key: value for key, value in manifest.items() if key != "frontend"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--destination", type=Path,
                        default=TARGET.parent.with_name("ScholarFlow-模型列表版"))
    args = parser.parse_args()
    print(json.dumps(install(args.backup, args.destination), ensure_ascii=False))

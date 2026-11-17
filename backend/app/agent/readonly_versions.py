"""Bounded actual-byte/project observations; no permission is granted here."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time


class VersionUnavailable(ValueError):
    """Perform the original operation when a complete version is unavailable."""


class Budget:
    def __init__(self, *, size=64 * 1024 * 1024, files=1024, seconds=2):
        self.remaining, self.files = size, files
        self.deadline = time.monotonic() + seconds

    def consume(self, size=0, files=0):
        self.remaining -= size
        self.files -= files
        if self.remaining < 0 or self.files < 0 or time.monotonic() > self.deadline:
            raise VersionUnavailable("版本核对超出读取预算")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_bytes(path, budget, *, scope=None, relative=None):
    """Hash actual bytes, never use mtime as a content version."""
    from .. import workspace
    budget.consume(files=1)
    hasher = hashlib.sha256()
    with Path(path).open("rb") as file:
        if scope:
            workspace._check_handle(file, scope, Path(path))
        before = os.fstat(file.fileno())
        if before.st_size > budget.remaining:
            raise VersionUnavailable("版本原件过大，按原工具执行")
        count = 0
        while block := file.read(128 * 1024):
            budget.consume(len(block)); count += len(block); hasher.update(block)
        after = os.fstat(file.fileno())
        # Timestamps detect a concurrent write; they never replace hashing the
        # bytes as the version. Same-size writes can otherwise evade fstat.
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) or count != after.st_size:
            raise VersionUnavailable("读取期间文件发生变化")
        if scope:
            workspace._check_handle(file, scope, Path(path))
            if workspace.scoped_path(scope, relative) != Path(path):
                raise VersionUnavailable("文件位置发生变化")
    return {"sha256": hasher.hexdigest(), "bytes": count}


def project_version(name, arguments, scope):
    from .. import workspace
    budget = Budget(size=16 * 1024 * 1024, files=1024)
    workspace.root_path(scope)
    raw = workspace.relative_path(arguments.get("path", ""))
    target = workspace.scoped_path(scope, raw)
    if name == "read_project_file":
        if set(arguments) - {"path", "offset"} or not target.is_file():
            raise VersionUnavailable("不是可复用的文本读取")
        version = file_bytes(target, budget, scope=scope, relative=raw)
        observed = workspace.read_text(scope, raw, offset=arguments.get("offset", 0))
        if observed["sha256"] != version["sha256"]:
            raise VersionUnavailable("文件读取版本发生变化")
        rules = workspace.directory_rules(scope, [raw])
        observed["rules"] = rules
        # Bind the result body, not merely pre/post hashes: A -> read B -> A
        # must never publish the B body under version A.
        return {"fingerprint": digest({"file": version, "rules": rules}),
                "result_sha256": digest(observed)}
    if name == "list_project_files":
        if set(arguments) - {"path", "offset", "q"}:
            raise VersionUnavailable("不是可复用的目录读取")
        listing = workspace.list_files(scope, raw, offset=arguments.get("offset", 0), query=arguments.get("q", ""))
        # list_files exposes a bounded page. To prove the entire listing
        # unchanged, hash its complete immediate directory, not just page one.
        if listing["limited"]:
            raise VersionUnavailable("目录未完整列举")
        entries = []
        with os.scandir(target) as iterator:
            for entry in iterator:
                budget.consume(files=1)
                path = workspace.relative_path((Path(raw) / entry.name).as_posix())
                if entry.name.casefold() in workspace.SKIP_DIRS:
                    continue
                if entry.is_symlink() or (hasattr(entry, "is_junction") and entry.is_junction()):
                    raise VersionUnavailable("链接目录不复用目录观察")
                verified = workspace.scoped_path(scope, path)
                entries.append([path, "directory" if verified.is_dir() else "file" if verified.is_file() else "other"])
        return {"fingerprint": digest(sorted(entries)), "result_sha256": digest(listing)}
    raise VersionUnavailable("没有可靠的只读版本接口")

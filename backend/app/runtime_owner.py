"""One backend per data directory, with authenticated local desktop activation."""
from __future__ import annotations

import errno
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class RuntimeInUse(RuntimeError):
    pass


class Descriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1] = 1
    instance_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    token: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    pid: int = Field(ge=1)
    kind: Literal["desktop", "service"]
    state: Literal["starting", "ready", "stopping"]
    port: int | None = Field(default=None, ge=1, le=65535)


def read_descriptor(directory: Path) -> Descriptor | None:
    try:
        with (directory / ".runtime.json").open("rb") as source:
            raw = source.read(8193)
        return Descriptor.model_validate_json(raw) if len(raw) <= 8192 else None
    except (OSError, ValidationError):
        return None


def _lock_file(source):
    source.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(source.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(source.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


class RuntimeOwner:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self._guard = threading.RLock()
        self._file = None
        self._descriptor: Descriptor | None = None
        self._serving = False
        self._activation: Callable[[], bool] | None = None

    def _acquire(self, kind: Literal["desktop", "service"]) -> bool:
        self.directory.mkdir(parents=True, exist_ok=True)
        source = (self.directory / ".runtime.lock").open("a+b")
        try:
            if os.fstat(source.fileno()).st_size == 0:
                source.write(b"0")
                source.flush()
            _lock_file(source)
        except OSError as exc:
            source.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return False
            raise
        self._file = source
        self._descriptor = Descriptor(instance_id=uuid4().hex, token=secrets.token_urlsafe(32),
                                      pid=os.getpid(), kind=kind, state="starting")
        try:
            self._publish()
        except BaseException:
            self._release()
            raise
        return True

    def _publish(self):
        handle, name = tempfile.mkstemp(prefix=".runtime-", suffix=".tmp", dir=self.directory)
        temporary = Path(name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as target:
                target.write(self._descriptor.model_dump_json())
            os.replace(temporary, self.directory / ".runtime.json")
        finally:
            temporary.unlink(missing_ok=True)

    def _release(self):
        # Remove discovery before unlocking; another owner may publish immediately.
        try:
            (self.directory / ".runtime.json").unlink(missing_ok=True)
        except OSError:
            pass  # A stale descriptor cannot acquire or keep the OS lock.
        if self._file is not None:
            self._file.close()
        self._file = self._descriptor = self._activation = None
        self._serving = False

    def reserve_desktop(self) -> bool:
        with self._guard:
            return False if self._file is not None else self._acquire("desktop")

    def set_desktop_port(self, port: int):
        with self._guard:
            if not self._descriptor or self._descriptor.kind != "desktop" or self._serving:
                raise RuntimeError("桌面实例尚未预留或已启动")
            self._descriptor.port = port
            self._publish()

    def begin_service(self):
        with self._guard:
            if self._serving or (self._descriptor and self._descriptor.state == "stopping"):
                raise RuntimeInUse("此数据目录的服务仍在运行或收尾")
            if self._file is None and not self._acquire("service"):
                raise RuntimeInUse("此数据目录已有研序服务正在运行，不能启动第二个后端")
            self._serving = True

    def ready(self):
        with self._guard:
            self._descriptor.state = "ready"
            self._publish()

    def set_activation(self, callback: Callable[[], bool]):
        with self._guard:
            self._activation = callback

    def activate(self, token: str) -> str | None:
        with self._guard:
            descriptor = self._descriptor
            if not descriptor or not token.isascii() or not hmac.compare_digest(token, descriptor.token):
                raise PermissionError("实例凭据无效")
            if descriptor.state != "ready" or self._activation is None:
                return None
            callback, instance_id = self._activation, descriptor.instance_id
        # Window methods marshal to the UI thread. Never hold the ownership guard
        # while waiting for that thread, which may concurrently begin shutdown.
        if not callback():
            return None
        with self._guard:
            return instance_id if self._descriptor is descriptor and descriptor.state == "ready" else None

    def stopping(self):
        with self._guard:
            self._activation = None
            if self._descriptor:
                self._descriptor.state = "stopping"
                try:
                    self._publish()
                except OSError:
                    pass  # Discovery failure must not prevent cancellation/cleanup.

    def finish_shutdown(self, *, idle: bool):
        with self._guard:
            self._serving = False
            if self._file is None:
                return
            self.stopping()
            if idle:
                self._release()
            # Live tool threads keep the lock until the process actually exits.


def claim_or_activate(owner: RuntimeOwner, timeout=30.0) -> Literal["primary", "activated", "service_busy", "busy"]:
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while True:
        if owner.reserve_desktop():
            return "primary"
        descriptor = read_descriptor(owner.directory)
        if descriptor and descriptor.kind == "service" and descriptor.state == "ready":
            return "service_busy"
        if descriptor and descriptor.port and descriptor.state != "stopping":
            request = urllib.request.Request(
                f"http://127.0.0.1:{descriptor.port}/api/runtime/activate", data=b"{}", method="POST",
                headers={"Content-Type": "application/json", "X-ScholarFlow-Instance": descriptor.token},
            )
            try:
                with opener.open(request, timeout=max(0.1, min(2, deadline - time.monotonic()))) as response:
                    result = json.loads(response.read(4096))
                if isinstance(result, dict) and result.get("instance_id") == descriptor.instance_id and result.get("activated") is True:
                    return "activated"
            except (OSError, ValueError):
                pass
        if time.monotonic() >= deadline:
            return "busy"
        time.sleep(0.1)


from .config import DATA_DIR  # noqa: E402

owner = RuntimeOwner(DATA_DIR)

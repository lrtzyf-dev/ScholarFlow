"""Atomic descriptor publication under transient Windows reader contention."""
import errno
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import runtime_owner as ownership


@pytest.fixture
def owned(tmp_path):
    instance = ownership.RuntimeOwner(tmp_path / "owner")
    assert instance.reserve_desktop()
    try:
        yield instance
    finally:
        instance.finish_shutdown(idle=True)


def windows_error(code):
    error = PermissionError(errno.EACCES, "synthetic descriptor contention")
    error.winerror = code
    return error


@pytest.mark.parametrize("code", [5, 32, 33])
def test_only_transient_windows_replace_errors_retry_the_same_atomic_file(owned, monkeypatch, code):
    original = (owned.directory / ".runtime.json").read_bytes()
    real_replace, calls, delays = os.replace, [], []

    def replace(source, destination):
        calls.append((source, destination))
        assert source.is_file()
        assert json.loads(source.read_text(encoding="utf-8"))["state"] == "ready"
        if len(calls) < 3:
            assert destination.read_bytes() == original
            raise windows_error(code)
        return real_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(ownership, "_WINDOWS", True)
        patch.setattr(ownership.os, "replace", replace)
        patch.setattr(ownership.time, "sleep", delays.append)
        owned.ready()
    assert len(calls) == 3 and len({source for source, _ in calls}) == 1
    assert delays == list(ownership._DESCRIPTOR_REPLACE_DELAYS[:2])
    assert ownership.read_descriptor(owned.directory).state == "ready"
    assert not list(owned.directory.glob(".runtime-*.tmp"))


def test_permanent_windows_access_denial_is_bounded_and_cleans_temporary_file(owned, monkeypatch):
    original = (owned.directory / ".runtime.json").read_bytes()
    calls, delays = [], []
    error = windows_error(5)

    def denied(source, destination):
        calls.append(source)
        assert destination.read_bytes() == original
        raise error

    with monkeypatch.context() as patch:
        patch.setattr(ownership, "_WINDOWS", True)
        patch.setattr(ownership.os, "replace", denied)
        patch.setattr(ownership.time, "sleep", delays.append)
        with pytest.raises(PermissionError) as failed:
            owned.ready()
    assert failed.value is error
    assert len(calls) == len(ownership._DESCRIPTOR_REPLACE_DELAYS) + 1
    assert delays == list(ownership._DESCRIPTOR_REPLACE_DELAYS)
    assert sum(delays) < 1
    assert (owned.directory / ".runtime.json").read_bytes() == original
    assert not list(owned.directory.glob(".runtime-*.tmp"))


@pytest.mark.parametrize("windows,error", [
    (False, windows_error(5)),
    (True, OSError(errno.ENOSPC, "synthetic disk full")),
    (True, PermissionError(errno.EACCES, "synthetic POSIX permissions")),
    (True, windows_error(112)),
    (True, RuntimeError("synthetic non-I/O failure")),
])
def test_other_platforms_and_errors_propagate_without_retry(owned, monkeypatch, windows, error):
    calls, delays = [], []

    def failed(source, destination):
        calls.append(source)
        raise error

    with monkeypatch.context() as patch:
        patch.setattr(ownership, "_WINDOWS", windows)
        patch.setattr(ownership.os, "replace", failed)
        patch.setattr(ownership.time, "sleep", delays.append)
        with pytest.raises(type(error)) as caught:
            owned.ready()
    assert caught.value is error
    assert len(calls) == 1 and not delays
    assert not list(owned.directory.glob(".runtime-*.tmp"))


def test_failed_initial_publication_releases_the_directory_lock(tmp_path, monkeypatch):
    instance = ownership.RuntimeOwner(tmp_path)

    def denied(*args):
        raise windows_error(32)

    with monkeypatch.context() as patch:
        patch.setattr(ownership, "_WINDOWS", True)
        patch.setattr(ownership.os, "replace", denied)
        patch.setattr(ownership.time, "sleep", lambda delay: None)
        with pytest.raises(PermissionError):
            instance.reserve_desktop()
    assert instance._file is None and not list(tmp_path.glob(".runtime-*.tmp"))
    replacement = ownership.RuntimeOwner(tmp_path)
    try:
        assert replacement.reserve_desktop()
    finally:
        replacement.finish_shutdown(idle=True)


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows shared-handle rename semantics")
def test_real_windows_open_reader_temporarily_blocks_publication(owned, monkeypatch):
    opened, release = threading.Event(), threading.Event()
    real_replace, failures = os.replace, []
    original = ownership.read_descriptor(owned.directory)

    def hold_reader():
        with (owned.directory / ".runtime.json").open("rb") as source:
            opened.set()
            assert release.wait(5)
            assert json.loads(source.read())["instance_id"] == original.instance_id

    def observed_replace(source, destination):
        try:
            return real_replace(source, destination)
        except OSError as exc:
            failures.append(exc.winerror)
            # Keep the real reader open for two observed rename failures, then
            # let the same atomic temporary file succeed on a later attempt.
            if len(failures) == 2:
                release.set()
            raise

    with ThreadPoolExecutor(max_workers=1) as pool:
        reader = pool.submit(hold_reader)
        assert opened.wait(5)
        try:
            with monkeypatch.context() as patch:
                patch.setattr(ownership.os, "replace", observed_replace)
                owned.ready()
        finally:
            release.set()
        reader.result(timeout=5)
    assert len(failures) >= 2 and set(failures) <= {5, 32, 33}
    assert ownership.read_descriptor(owned.directory).state == "ready"
    assert not list(owned.directory.glob(".runtime-*.tmp"))


def test_simultaneous_readers_observe_valid_descriptors_through_repeated_publications(owned):
    original = ownership.read_descriptor(owned.directory)
    stop = threading.Event()
    barrier = threading.Barrier(4)

    def read_repeatedly():
        barrier.wait(timeout=5)
        valid = 0
        while not stop.is_set():
            try:
                raw = (owned.directory / ".runtime.json").read_bytes()
            except OSError:
                continue  # A transient open denial does not expose partial JSON.
            descriptor = ownership.Descriptor.model_validate_json(raw)
            assert descriptor.instance_id == original.instance_id
            assert descriptor.token == original.token
            valid += 1
            time.sleep(0.0005)
        return valid

    with ThreadPoolExecutor(max_workers=3) as pool:
        readers = [pool.submit(read_repeatedly) for _ in range(3)]
        barrier.wait(timeout=5)
        try:
            for index in range(100):
                owned.set_desktop_port(12000 + index)
                if index == 0:
                    owned.ready()
        finally:
            stop.set()
        assert all(reader.result(timeout=5) > 0 for reader in readers)
    assert ownership.read_descriptor(owned.directory).port == 12099
    assert not list(owned.directory.glob(".runtime-*.tmp"))

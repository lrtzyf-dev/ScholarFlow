"""Actual Windows/POSIX handles; synthetic assets only, never user lock files."""
import os

import pytest

from app import release_install as module, runtime_lock_file as locks
from app.release_info import ReleaseError
from .test_release_install import setup


@pytest.mark.parametrize("content", [b"", b"existing lock payload remains byte exact"])
def test_empty_and_nonempty_locks_never_write_bytes_and_still_exclude_other_handles(tmp_path, content):
    path = tmp_path / "plain.lock"
    path.write_bytes(content)
    with module._exclusive(path):
        assert path.stat().st_size == len(content)
        with pytest.raises(ReleaseError):
            with module._exclusive(path):
                pytest.fail("second holder admitted")
    assert path.read_bytes() == content
    with module._exclusive(path):
        pass
    assert path.read_bytes() == content


@pytest.mark.parametrize("content", [b"", b"retained original research asset"])
def test_precreated_hardlink_is_rejected_without_mutating_the_actual_original(tmp_path, content):
    original, path = tmp_path / "original.bin", tmp_path / "aliased.lock"
    original.write_bytes(content)
    os.link(original, path)
    with pytest.raises(ReleaseError):
        with module._exclusive(path):
            pytest.fail("hard-linked asset locked")
    assert original.read_bytes() == content and path.read_bytes() == content


def test_actual_opened_handle_is_checked_even_when_pathname_precheck_would_be_safe(tmp_path, monkeypatch):
    path, redirected = tmp_path / "safe.lock", tmp_path / "other-asset.bin"
    path.write_bytes(b""); redirected.write_bytes(b"")
    real = os.open
    def redirect(value, flags, mode=0o777, **kwargs):
        return real(redirected if os.fspath(value) == os.fspath(path) else value, flags, mode, **kwargs)
    monkeypatch.setattr(locks.os, "open", redirect)
    with pytest.raises(locks.LockIdentityError):
        locks.open_lock_file(path)
    assert path.read_bytes() == b"" and redirected.read_bytes() == b""


@pytest.mark.parametrize("operation", ["install", "recover"])
def test_shared_installer_and_recovery_reject_hardlinked_state_lock_before_touching_data(setup, operation):
    s = setup
    original = s["data"] / "empty-preserved-asset.bin"
    original.write_bytes(b"")
    state = s["target"].parent / ("." + s["target"].name + "-updates")
    state.mkdir()
    os.link(original, state / "install.lock")
    before_data, before_program = module._files(s["data"]), module._files(s["target"])
    with pytest.raises(ReleaseError):
        if operation == "install":
            module.install(s["candidate"], s["target"], s["data"], trusted_keys=s["trust"])
        else:
            module.recover(s["target"], s["data"], trusted_keys=s["trust"])
    assert original.read_bytes() == b""
    assert module._files(s["data"]) == before_data and module._files(s["target"]) == before_program

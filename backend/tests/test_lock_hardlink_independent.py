"""Physical hardlink preservation at actual runtime/restore lock boundaries."""
import os

import pytest

from app import data_backup
from app.runtime_owner import RuntimeOwner, RuntimeInUse


def linked_empty_asset(tmp_path):
    original = tmp_path / "original-retained-empty.txt"
    original.write_bytes(b"")
    runtime = tmp_path / "runtime-data"
    runtime.mkdir()
    try:
        os.link(original, runtime / ".runtime.lock")
    except OSError as exc:
        pytest.skip("physical hardlinks unavailable: " + type(exc).__name__)
    assert original.stat().st_nlink == 2
    return original, runtime


def test_runtime_owner_does_not_write_through_a_hardlinked_lock_to_an_original_asset(tmp_path):
    original, runtime = linked_empty_asset(tmp_path)
    owner = RuntimeOwner(runtime)
    acquired = False
    try:
        acquired = owner._acquire("desktop")
    except (RuntimeInUse, OSError, ValueError):
        pass
    finally:
        owner._release()
    assert original.read_bytes() == b"", "runtime lock changed an unrelated original from empty to b'0'"
    assert not acquired, "a multiply linked runtime lock must not grant ownership"


def test_backup_restore_does_not_write_through_a_hardlinked_runtime_lock_to_an_original_asset(tmp_path):
    original, runtime = linked_empty_asset(tmp_path)
    entered = False
    try:
        with data_backup._runtime_exclusive(runtime):
            entered = True
    except (data_backup.BackupError, OSError, ValueError):
        pass
    assert original.read_bytes() == b"", "restore lock changed an unrelated original from empty to b'0'"
    assert not entered, "a multiply linked restore lock must not grant exclusive restore"

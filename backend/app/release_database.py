"""Source-preserving SQLite boundaries for offline release operations.

No configuration, database registration or application startup is imported.
Only a checked external copy is opened to observe source DB+WAL contents.
"""
from contextlib import closing, contextmanager
from pathlib import Path
import shutil
import sqlite3
import tempfile

from .release_files import file_record, no_link
from .release_info import ReleaseError


def database_files(database):
    """Freeze the main file and committed WAL, never a mutable SHM index."""
    database = no_link(Path(database))
    journal = no_link(database.with_name(database.name + "-journal"))
    if journal.exists() and journal.stat().st_size:
        raise ReleaseError("数据库仍有恢复日志，请先由原程序完成恢复")
    paths = [database]
    wal = no_link(database.with_name(database.name + "-wal"))
    if wal.exists():
        paths.append(wal)
    return [file_record(database.parent, path) for path in paths]


@contextmanager
def observation_database(database):
    """Observe a checked DB+WAL copy; mode=ro can write source sidecars."""
    database = no_link(Path(database))
    before = database_files(database)
    with tempfile.TemporaryDirectory(prefix="release-observation-") as temporary:
        root = Path(temporary)
        for record in before:
            source = no_link(database.parent / record["path"])
            shutil.copyfile(source, root / record["path"])
            if file_record(root, root / record["path"]) != record:
                raise ReleaseError("数据库快照复制不一致，未接受升级检查")
        if database_files(database) != before:
            raise ReleaseError("数据库或WAL在复制期间变化，未接受混合版本快照")
        yield root / database.name
        if database_files(database) != before:
            raise ReleaseError("数据库或WAL在检查期间变化，未接受混合版本快照")


@contextmanager
def database_reservation(database):
    """Keep a write reservation without checkpointing a crashed process's WAL.

    The option must be enabled before any source SQL. Otherwise closing our
    final connection can change the retained main file and delete its WAL even
    though the installer never executes a write statement.
    """
    database = no_link(Path(database))
    database_files(database)  # Reject hot rollback journals before opening SQL.
    no_checkpoint = getattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE", None)
    if (no_checkpoint is None or not hasattr(sqlite3.Connection, "setconfig")
            or not hasattr(sqlite3.Connection, "getconfig")):
        raise ReleaseError("当前SQLite运行环境不能保证关闭连接时保留原WAL，未开始安装或恢复")
    with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=1)) as connection:
        connection.setconfig(no_checkpoint, True)
        if connection.getconfig(no_checkpoint) is not True:
            raise ReleaseError("无法禁止关闭连接时写回原WAL，未开始安装或恢复")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield database_files(database)
        finally:
            connection.rollback()

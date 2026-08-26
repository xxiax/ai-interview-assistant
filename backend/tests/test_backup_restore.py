from __future__ import annotations

import sqlite3

from scripts.backup_sqlite import backup
from scripts.restore_sqlite import restore


def test_wal_backup_and_restore_round_trip(tmp_path):
    source = tmp_path / "source.db"
    backup_dir = tmp_path / "backups"
    restored = tmp_path / "restored.db"

    conn = sqlite3.connect(source)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE records (value TEXT NOT NULL)")
        conn.execute("INSERT INTO records VALUES ('before-backup')")
        conn.commit()
        backup_path = backup(source, backup_dir, keep=2)
    finally:
        conn.close()

    restore(backup_path, restored, force=False)
    restored_conn = sqlite3.connect(restored)
    try:
        assert restored_conn.execute("SELECT value FROM records").fetchone()[0] == (
            "before-backup"
        )
        assert restored_conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        restored_conn.close()

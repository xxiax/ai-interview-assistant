"""Create a consistent SQLite backup while the API is running in WAL mode."""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _quick_check(path: Path) -> None:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        conn.close()
    if result != "ok":
        raise RuntimeError(f"backup integrity check failed: {result}")


def backup(source: Path, destination_dir: Path, keep: int) -> Path:
    if not source.is_file():
        raise FileNotFoundError(f"database not found: {source}")
    destination_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = destination_dir / f"interview-{timestamp}.db"
    partial = destination.with_suffix(".db.partial")

    source_conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    destination_conn = sqlite3.connect(partial)
    try:
        source_conn.backup(destination_conn)
        destination_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        destination_conn.commit()
    finally:
        destination_conn.close()
        source_conn.close()

    _quick_check(partial)
    os.chmod(partial, 0o600)
    os.replace(partial, destination)
    with destination.open("r+b") as backup_file:
        os.fsync(backup_file.fileno())

    backups = sorted(destination_dir.glob("interview-*.db"), reverse=True)
    for expired in backups[max(1, keep) :]:
        expired.unlink()
    return destination


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination-dir", type=Path, required=True)
    parser.add_argument("--keep", type=int, default=14)
    args = parser.parse_args()
    print(backup(args.source, args.destination_dir, args.keep))


if __name__ == "__main__":
    main()

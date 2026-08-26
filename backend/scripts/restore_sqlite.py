"""Verify and restore a SQLite backup while the API container is stopped."""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path


def _quick_check(path: Path) -> None:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        conn.close()
    if result != "ok":
        raise RuntimeError(f"backup integrity check failed: {result}")


def restore(backup: Path, destination: Path, force: bool) -> Path:
    if not backup.is_file():
        raise FileNotFoundError(f"backup not found: {backup}")
    if destination.exists() and not force:
        raise FileExistsError("destination exists; stop the API and pass --force")
    _quick_check(backup)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(".db.restore-partial")

    source_conn = sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True)
    destination_conn = sqlite3.connect(partial)
    try:
        source_conn.backup(destination_conn)
        destination_conn.commit()
    finally:
        destination_conn.close()
        source_conn.close()

    _quick_check(partial)
    os.chmod(partial, 0o600)
    os.replace(partial, destination)
    for suffix in ("-wal", "-shm"):
        destination.with_name(destination.name + suffix).unlink(missing_ok=True)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    print(restore(args.backup, args.destination, args.force))


if __name__ == "__main__":
    main()

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn):
    conn.executescript(SCHEMA.read_text())
    conn.commit()


@contextmanager
def atomic(conn):
    """All-or-nothing posting: commit on success, roll back on any exception."""
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

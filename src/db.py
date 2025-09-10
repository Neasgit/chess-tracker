# src/db.py
from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from .config import get_db_path

# SQLAlchemy engine (used by compute_srs.py or anything needing SQLAlchemy)
_engine: Engine | None = None

def get_engine(db_path: str | None = None) -> Engine:
    global _engine
    if _engine is None:
        path = db_path or get_db_path()
        _engine = create_engine(
            f"sqlite:///{path}",
            future=True,
            echo=False,
            connect_args={"check_same_thread": False},
        )
    return _engine

# Lightweight sqlite3 helper for simple local queries in serve.py
@contextmanager
def open_sqlite():
    conn = sqlite3.connect(get_db_path())
    try:
        yield conn
    finally:
        conn.close()

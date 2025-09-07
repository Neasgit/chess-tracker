from sqlalchemy import create_engine
from pathlib import Path

_engine = None

def get_engine(db_path: str):
    global _engine
    if _engine is None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(f"sqlite+pysqlite:///{db_path}", echo=False, future=True)
    return _engine

def init_db(db_path: str, schema_path: str):
    eng = get_engine(db_path)
    sql = Path(schema_path).read_text(encoding="utf-8")
    with eng.begin() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
        conn.exec_driver_sql("PRAGMA synchronous=NORMAL;")
        for stmt in sql.split(";\n"):
            s = stmt.strip()
            if s:
                conn.exec_driver_sql(s)

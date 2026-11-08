"""Local transactional JSON document storage backed by SQLite."""
import json
from pathlib import Path
import sqlite3
from threading import RLock
from contextlib import contextmanager


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS documents (collection TEXT NOT NULL, id TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (collection,id))")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, collection, id, value):
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO documents(collection,id,value) VALUES (?,?,?) ON CONFLICT(collection,id) DO UPDATE SET value=excluded.value", (collection, id, encoded))
        return json.loads(encoded)

    def put_if_absent(self, collection, id, value):
        """Insert a seed exactly once, retaining its original immutable snapshot."""
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        with self._lock, self._connect() as db:
            db.execute("INSERT OR IGNORE INTO documents(collection,id,value) VALUES (?,?,?)", (collection, id, encoded))
            row = db.execute("SELECT value FROM documents WHERE collection=? AND id=?", (collection, id)).fetchone()
        return json.loads(row[0])

    def put_many(self, documents):
        """Commit related document replacements in one SQLite transaction."""
        rows = [(c, key, json.dumps(value, ensure_ascii=False, allow_nan=False)) for c, key, value in documents]
        with self._lock, self._connect() as db:
            db.executemany("INSERT INTO documents(collection,id,value) VALUES (?,?,?) ON CONFLICT(collection,id) DO UPDATE SET value=excluded.value", rows)

    def get(self, collection, id):
        with self._lock, self._connect() as db:
            row = db.execute("SELECT value FROM documents WHERE collection=? AND id=?", (collection, id)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, collection):
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT value FROM documents WHERE collection=? ORDER BY rowid", (collection,)).fetchall()
        return [json.loads(row[0]) for row in rows]

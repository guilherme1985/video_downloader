"""
db.py — Persistência SQLite para jobs / messages / results.

Estratégia:
  - Uma única conexão (check_same_thread=False) + Lock próprio.
    Para a carga deste app (poucas escritas/seg), é mais simples e mais
    rápido que pool ou conexão-por-thread.
  - WAL ativado: leitores não bloqueiam o escritor.
  - Estado transitório (percent em vivo, lista de workers ativos) NÃO vai
    para o disco — fica só em memória no app.
"""
import os
import sqlite3
import time
import threading
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    created_at   REAL NOT NULL,
    finished_at  REAL,
    total_links  INTEGER NOT NULL DEFAULT 0,
    format       TEXT NOT NULL DEFAULT 'best',
    is_playlist  INTEGER NOT NULL DEFAULT 0,
    dest_path    TEXT NOT NULL DEFAULT '',
    workers      INTEGER NOT NULL DEFAULT 1,
    running      INTEGER NOT NULL DEFAULT 0,
    completed    INTEGER NOT NULL DEFAULT 0,
    cancelled    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  TEXT NOT NULL,
    ts      REAL NOT NULL,
    type    TEXT NOT NULL,
    message TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_job ON messages(job_id, id);

CREATE TABLE IF NOT EXISTS results (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  TEXT NOT NULL,
    link    TEXT NOT NULL,
    title   TEXT NOT NULL DEFAULT '',
    success INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_results_job ON results(job_id, id);
"""


class JobStore:
    """Persistência thread-safe de jobs em SQLite."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            isolation_level=None,  # autocommit; usamos transações explícitas
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    # ---- Jobs ---------------------------------------------------------

    def create_job(self, job: dict) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO jobs(id, created_at, total_links, format,
                                    is_playlist, dest_path, workers, running)
                   VALUES (?,?,?,?,?,?,?,1)""",
                (job["id"], job["created_at"], job["total_links"],
                 job["format"], int(job["is_playlist"]), job["dest_path"],
                 job["workers"]),
            )

    def finalize_job(self, job_id: str, *, completed: bool,
                     cancelled: bool) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE jobs
                   SET running=0, completed=?, cancelled=?, finished_at=?
                   WHERE id=?""",
                (int(completed), int(cancelled), time.time(), job_id),
            )

    def cleanup_zombies(self) -> int:
        """No startup, marca jobs deixados como running=1 como cancelled."""
        now = time.time()
        with self._lock:
            # Pega os IDs primeiro para não depender de timestamp na 2ª query
            zombie_ids = [
                row["id"] for row in self._conn.execute(
                    "SELECT id FROM jobs WHERE running=1"
                ).fetchall()
            ]
            if not zombie_ids:
                return 0
            self._conn.execute("BEGIN")
            try:
                placeholders = ",".join("?" * len(zombie_ids))
                self._conn.execute(
                    f"""UPDATE jobs
                        SET running=0, cancelled=1, completed=1, finished_at=?
                        WHERE id IN ({placeholders})""",
                    (now, *zombie_ids),
                )
                self._conn.executemany(
                    """INSERT INTO messages(job_id, ts, type, message)
                       VALUES (?, ?, 'warning',
                               'Interrompido por reinício do servidor.')""",
                    [(jid, now) for jid in zombie_ids],
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return len(zombie_ids)

    def list_recent(self, limit: int = 20) -> list:
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, created_at, finished_at, total_links, format,
                          is_playlist, workers, running, completed, cancelled
                   FROM jobs ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            msgs = self._conn.execute(
                """SELECT ts, type, message FROM messages
                   WHERE job_id=? ORDER BY id""",
                (job_id,),
            ).fetchall()
            results = self._conn.execute(
                """SELECT link, title, success, message FROM results
                   WHERE job_id=? ORDER BY id""",
                (job_id,),
            ).fetchall()

        job = dict(row)
        job["is_playlist"] = bool(job["is_playlist"])
        job["running"] = bool(job["running"])
        job["completed"] = bool(job["completed"])
        job["cancelled"] = bool(job["cancelled"])
        job["messages"] = [dict(m) for m in msgs]
        job["results"] = [
            {**dict(r), "success": bool(r["success"])} for r in results
        ]
        return job

    def prune_old(self, keep: int) -> int:
        """Mantém apenas os `keep` jobs mais recentes (LRU)."""
        with self._lock:
            cur = self._conn.execute(
                """DELETE FROM jobs WHERE id IN (
                       SELECT id FROM jobs ORDER BY created_at DESC
                       LIMIT -1 OFFSET ?
                   )""",
                (keep,),
            )
            return cur.rowcount

    # ---- Messages / results ------------------------------------------

    def add_message(self, job_id: str, msg_type: str,
                    message: str, ts: Optional[float] = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO messages(job_id, ts, type, message)
                   VALUES (?,?,?,?)""",
                (job_id, ts or time.time(), msg_type, message),
            )

    def add_result(self, job_id: str, link: str, title: str,
                   success: bool, message: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO results(job_id, link, title, success, message)
                   VALUES (?,?,?,?,?)""",
                (job_id, link, title, int(success), message),
            )

    # ---- Util ---------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

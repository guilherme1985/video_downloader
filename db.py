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
    cancelled    INTEGER NOT NULL DEFAULT 0,
    cookie_id    TEXT NOT NULL DEFAULT ''
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
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id    TEXT NOT NULL,
    link      TEXT NOT NULL,
    title     TEXT NOT NULL DEFAULT '',
    success   INTEGER NOT NULL DEFAULT 0,
    message   TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_results_job ON results(job_id, id);

CREATE TABLE IF NOT EXISTS cookies (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    filename   TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
"""


class JobStore:
    """Persistência thread-safe de jobs em SQLite."""

    def __init__(self, db_path: str, *, checkpoint_interval_s: int = 600):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._lock = threading.Lock()
        self._stopped = threading.Event()
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
        # Migração v2.3: adiciona file_path se ainda não existir
        try:
            self._conn.execute(
                "ALTER TABLE results ADD COLUMN file_path TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass
        # Migração v2.4.4: adiciona cookie_id no job para suportar retry com cookie
        try:
            self._conn.execute(
                "ALTER TABLE jobs ADD COLUMN cookie_id TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass

        # Thread daemon de checkpoint WAL: evita que o arquivo -wal cresça
        # indefinidamente em produção longa.
        self._checkpoint_interval_s = max(0, int(checkpoint_interval_s))
        if self._checkpoint_interval_s > 0:
            self._checkpoint_thread = threading.Thread(
                target=self._checkpoint_loop, daemon=True,
                name="db-wal-checkpoint",
            )
            self._checkpoint_thread.start()

    def _checkpoint_loop(self) -> None:
        while not self._stopped.is_set():
            if self._stopped.wait(self._checkpoint_interval_s):
                return
            try:
                with self._lock:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                # Não-crítico; tenta de novo no próximo tick.
                pass

    def ping(self) -> None:
        """Healthcheck: levanta exceção se o DB não responder a SELECT 1."""
        with self._lock:
            self._conn.execute("SELECT 1").fetchone()

    # ---- Jobs ---------------------------------------------------------

    def create_job(self, job: dict) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO jobs(id, created_at, total_links, format,
                                    is_playlist, dest_path, workers, running,
                                    cookie_id)
                   VALUES (?,?,?,?,?,?,?,1,?)""",
                (job["id"], job["created_at"], job["total_links"],
                 job["format"], int(job["is_playlist"]), job["dest_path"],
                 job["workers"], job.get("cookie_id", "")),
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
                    "SELECT id FROM jobs WHERE running=1 AND finished_at IS NULL"
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

    def delete_job(self, job_id: str) -> bool:
        """Apaga um job e seus dados (cascata em messages/results)."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM jobs WHERE id=?", (job_id,)
            )
            return cur.rowcount > 0

    def delete_finished(self) -> int:
        """Apaga todos os jobs que não estão running."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM jobs WHERE running=0"
            )
            return cur.rowcount

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
                   success: bool, message: str = "",
                   file_path: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO results(job_id, link, title, success, message, file_path)
                   VALUES (?,?,?,?,?,?)""",
                (job_id, link, title, int(success), message, file_path),
            )

    # ---- Cookies ------------------------------------------------------

    def create_cookie(self, cookie_id: str, name: str, filename: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO cookies(id, name, filename, created_at)
                   VALUES (?,?,?,?)""",
                (cookie_id, name, filename, time.time()),
            )

    def list_cookies(self) -> list:
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, name, filename, created_at
                   FROM cookies ORDER BY created_at DESC"""
            ).fetchall()
        return [dict(r) for r in rows]

    def get_cookie(self, cookie_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name, filename, created_at FROM cookies WHERE id=?",
                (cookie_id,),
            ).fetchone()
        return dict(row) if row else None

    def delete_cookie(self, cookie_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM cookies WHERE id=?", (cookie_id,)
            )
            return cur.rowcount > 0

    # ---- Util ---------------------------------------------------------

    def close(self) -> None:
        self._stopped.set()
        with self._lock:
            self._conn.close()
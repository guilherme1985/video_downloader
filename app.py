"""
app.py — Flask + ThreadPoolExecutor + SQLite.

Modelo de dados em memória (jobs[id]):
  Estado transitório que NÃO vai para o DB:
    - active: dict {link: {title, percent}} dos workers em execução agora.
    - done_count: contador rápido (= len(results)).

Todo o resto (mensagens, resultados finalizados, flags) é persistido.
"""
import os
import time
import uuid
import threading
import tempfile
from concurrent.futures import ThreadPoolExecutor, Future
from collections import OrderedDict
from urllib.parse import urlparse

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, jsonify, abort
)
from werkzeug.utils import secure_filename

import download_videos
from db import JobStore


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
DEFAULT_DOWNLOAD_PATH = os.environ.get("DEFAULT_DOWNLOAD_PATH", "/mnt/nas/Downloads")
ALLOWED_DOWNLOAD_ROOT = os.environ.get("ALLOWED_DOWNLOAD_ROOT", DEFAULT_DOWNLOAD_PATH)
DB_PATH = os.environ.get("DB_PATH", "/data/jobs.db")
SECRET_KEY = os.environ.get("SECRET_KEY") or os.urandom(32).hex()

MAX_LINKS = int(os.environ.get("MAX_LINKS", "500"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "1"))
MAX_JOBS_KEPT = int(os.environ.get("MAX_JOBS_KEPT", "50"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "10"))
DEFAULT_WORKERS = int(os.environ.get("DEFAULT_WORKERS", "1"))
DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"

ALLOWED_FORMATS = {
    "best", "1080p", "720p", "480p", "audio_mp3", "audio_m4a",
}

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


# ---------------------------------------------------------------------------
# Persistência + estado em memória
# ---------------------------------------------------------------------------
store = JobStore(DB_PATH)
_zombies = store.cleanup_zombies()
if _zombies:
    app.logger.warning(f"{_zombies} job(s) zumbi(s) marcados como cancelados.")
store.prune_old(keep=MAX_JOBS_KEPT)

jobs_lock = threading.Lock()
# Só jobs em execução (ou recentes) ficam em memória; os demais
# são lidos sob demanda do DB.
jobs: "OrderedDict[str, dict]" = OrderedDict()


def _new_job(workers: int, total_links: int, fmt: str,
             is_playlist: bool, dest_path: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "running": True,
        "completed": False,
        "cancelled": False,
        "created_at": time.time(),
        "finished_at": None,
        "total_links": total_links,
        "format": fmt,
        "is_playlist": is_playlist,
        "dest_path": dest_path,
        "workers": workers,
        # Transitório (não persistido):
        "active": {},          # link -> {title, percent}
        "done_count": 0,
        # Persistido sob demanda; também cacheado em memória:
        "messages": [],
        "results": [],
        # Controle interno:
        "_executor": None,
        "_futures": [],
    }


def _register_job(job: dict) -> None:
    with jobs_lock:
        jobs[job["id"]] = job
        # Mantém só os mais recentes em memória, mas nunca despeja running
        for old_id in list(jobs.keys()):
            if len(jobs) <= MAX_JOBS_KEPT:
                break
            if not jobs[old_id].get("running"):
                jobs.pop(old_id, None)


def _get_job_or_404(job_id: str) -> dict:
    """Retorna o job vivo (em memória) ou um snapshot do DB."""
    with jobs_lock:
        job = jobs.get(job_id)
    if job is not None:
        return job

    persisted = store.get_job(job_id)
    if persisted is None:
        abort(404)

    # Snapshot read-only com campos esperados pelos templates
    persisted["active"] = {}
    persisted["done_count"] = len(persisted["results"])
    return persisted


def _is_cancelled(job_id: str) -> bool:
    with jobs_lock:
        job = jobs.get(job_id)
        return job is None or job.get("cancelled", False)


# ---------------------------------------------------------------------------
# Validações
# ---------------------------------------------------------------------------
def _validate_dest_path(raw: str) -> str:
    if not raw or not raw.strip():
        raise ValueError("Caminho de destino vazio.")
    abs_path = os.path.realpath(raw)
    abs_root = os.path.realpath(ALLOWED_DOWNLOAD_ROOT)
    if abs_path != abs_root and not abs_path.startswith(abs_root + os.sep):
        raise ValueError(
            f"Caminho fora da raiz permitida ({ALLOWED_DOWNLOAD_ROOT})."
        )
    os.makedirs(abs_path, exist_ok=True)
    return abs_path


def _validate_links(raw_text: str) -> list:
    links = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = urlparse(line)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"Link inválido: {line[:80]}")
        links.append(line)
        if len(links) > MAX_LINKS:
            raise ValueError(f"Excedeu o limite de {MAX_LINKS} links por job.")
    if not links:
        raise ValueError("Nenhum link válido encontrado.")
    return links


def _validate_workers(raw: str) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise ValueError("Número de workers inválido.")
    if n < 1 or n > MAX_WORKERS:
        raise ValueError(f"Workers deve estar entre 1 e {MAX_WORKERS}.")
    return n


# ---------------------------------------------------------------------------
# Callback (memória + DB)
# ---------------------------------------------------------------------------
def _make_callback(job_id: str):
    """
    Eventos de progresso são frequentes e ficam SÓ em memória.
    Eventos persistidos:
      - link_done   → tabela results
      - info/success/warning/error → tabela messages
    """
    def on_event(event: dict):
        etype = event.get("type")
        # Estado em memória sob lock
        with jobs_lock:
            job = jobs.get(job_id)
            if job is None:
                return

            if etype == "progress":
                link = event.get("link")
                if link is not None:
                    prev = job["active"].get(link, {})
                    job["active"][link] = {
                        "title": event.get("title", "") or prev.get("title", ""),
                        "percent": event.get("percent", 0),
                    }
                return  # progress NÃO vai pro DB

            if etype == "link_start":
                link = event.get("link")
                if link is not None:
                    job["active"][link] = {"title": "", "percent": 0}
                return  # link_start é transitório

            if etype == "link_done":
                link = event.get("link")
                title = event.get("title", "")
                success = bool(event.get("success", False))
                msg = event.get("message", "")
                job["active"].pop(link, None)
                job["results"].append({
                    "link": link, "title": title,
                    "success": success, "message": msg,
                })
                job["done_count"] = len(job["results"])
            elif etype in ("info", "success", "warning", "error"):
                m = {"type": etype, "message": event.get("message", ""),
                     "ts": time.time()}
                job["messages"].append(m)
                if len(job["messages"]) > 500:
                    job["messages"] = job["messages"][-500:]
            else:
                return

        # I/O fora do lock
        if etype == "link_done":
            store.add_result(
                job_id,
                event.get("link", ""),
                event.get("title", ""),
                bool(event.get("success", False)),
                event.get("message", ""),
            )
        elif etype in ("info", "success", "warning", "error"):
            store.add_message(job_id, etype, event.get("message", ""))

    return on_event


# ---------------------------------------------------------------------------
# Execução paralela
# ---------------------------------------------------------------------------
def _run_single_link(job_id: str, link: str, dest_path: str,
                     is_playlist: bool, fmt: str, on_event):
    """Tarefa individual submetida ao executor."""
    if _is_cancelled(job_id):
        on_event({"type": "link_done", "link": link, "success": False,
                  "title": "", "message": "Cancelado antes de iniciar"})
        return

    on_event({"type": "link_start", "link": link})
    result = download_videos.baixar_video(
        link=link,
        download_dir=dest_path,
        is_playlist=is_playlist,
        fmt=fmt,
        on_event=on_event,
        is_cancelled=lambda: _is_cancelled(job_id),
    )
    on_event({
        "type": "link_done",
        "link": link,
        "success": result["success"],
        "title": result["title"],
        "message": result["message"],
    })
    if result["success"]:
        on_event({"type": "success",
                  "message": f"OK: {result['title'] or link}"})
    else:
        on_event({"type": "error",
                  "message": f"Falha em {link}: {result['message']}"})


def _run_job(job_id: str, links: list, dest_path: str,
             is_playlist: bool, fmt: str, workers: int):
    cb = _make_callback(job_id)
    cb({"type": "info",
        "message": f"Iniciando {len(links)} link(s) com {workers} worker(s)."})

    executor = ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix=f"job-{job_id[:8]}",
    )
    with jobs_lock:
        j = jobs.get(job_id)
        if j is not None:
            j["_executor"] = executor

    futures: list = []
    try:
        for link in links:
            fut: Future = executor.submit(
                _run_single_link, job_id, link, dest_path,
                is_playlist, fmt, cb,
            )
            futures.append(fut)
        with jobs_lock:
            j = jobs.get(job_id)
            if j is not None:
                j["_futures"] = futures

        for fut in futures:
            try:
                fut.result()
            except Exception as e:
                cb({"type": "error", "message": f"Erro inesperado: {e}"})
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    cancelled = _is_cancelled(job_id)
    if cancelled:
        cb({"type": "warning", "message": "Operação cancelada pelo usuário."})
    else:
        with jobs_lock:
            j = jobs.get(job_id) or {}
            ok = sum(1 for r in j.get("results", []) if r["success"])
        total = len(links)
        cb({"type": "info",
            "message": f"Concluído. Sucesso: {ok}, falhas: {total - ok}."})

    with jobs_lock:
        j = jobs.get(job_id)
        if j is not None:
            j["running"] = False
            j["completed"] = True
            j["finished_at"] = time.time()
            j["active"].clear()
            j["_executor"] = None
            j["_futures"] = []
    store.finalize_job(job_id, completed=True, cancelled=cancelled)


# ---------------------------------------------------------------------------
# Serialização (esconde campos internos do JSON da API)
# ---------------------------------------------------------------------------
def _job_to_json(job: dict) -> dict:
    out = {k: v for k, v in job.items() if not k.startswith("_")}
    # active vira lista (mais fácil iterar no front)
    out["active"] = [{"link": link, **info}
                     for link, info in job.get("active", {}).items()]
    return out


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        try:
            dest_path = _validate_dest_path(
                request.form.get("dest_path", DEFAULT_DOWNLOAD_PATH)
            )
        except (ValueError, OSError) as e:
            flash(f"Erro no caminho de destino: {e}", "danger")
            return redirect(url_for("index"))

        download_type = request.form.get("download_type", "video")
        is_playlist = download_type == "playlist"

        fmt = request.form.get("format", "best")
        if fmt not in ALLOWED_FORMATS:
            flash("Formato inválido.", "danger")
            return redirect(url_for("index"))

        try:
            workers = _validate_workers(
                request.form.get("workers", str(DEFAULT_WORKERS))
            )
        except ValueError as e:
            flash(str(e), "danger")
            return redirect(url_for("index"))

        # Coleta links (arquivo OU textarea)
        raw_links = request.form.get("links", "")
        upload = request.files.get("file")
        if upload and upload.filename:
            safe_name = secure_filename(upload.filename) or "upload.txt"
            tmp = os.path.join(tempfile.gettempdir(),
                               f"{uuid.uuid4().hex}_{safe_name}")
            upload.save(tmp)
            try:
                with open(tmp, "r", encoding="utf-8", errors="replace") as f:
                    raw_links = f.read()
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

        try:
            links = _validate_links(raw_links)
        except ValueError as e:
            flash(str(e), "danger")
            return redirect(url_for("index"))

        # Cria + persiste + dispara
        job = _new_job(workers=workers, total_links=len(links), fmt=fmt,
                       is_playlist=is_playlist, dest_path=dest_path)
        _register_job(job)
        store.create_job(job)
        store.prune_old(keep=MAX_JOBS_KEPT)

        thread = threading.Thread(
            target=_run_job,
            args=(job["id"], links, dest_path, is_playlist, fmt, workers),
            daemon=True,
        )
        thread.start()

        return redirect(url_for("status", job_id=job["id"]))

    return render_template(
        "index.html",
        default_path=DEFAULT_DOWNLOAD_PATH,
        recent_jobs=store.list_recent(10),
        max_workers=MAX_WORKERS,
        default_workers=DEFAULT_WORKERS,
    )


@app.route("/status/<job_id>")
def status(job_id):
    job = _get_job_or_404(job_id)
    # Para o template, normaliza `active` (dict em memória) para list,
    # igual ao formato exposto pela API. Cópia rasa para não mutar o estado.
    view = dict(job)
    active = job.get("active") or {}
    view["active"] = (
        [{"link": k, **v} for k, v in active.items()]
        if isinstance(active, dict) else active
    )
    return render_template("status.html", status=view)


@app.route("/results/<job_id>")
def results(job_id):
    job = _get_job_or_404(job_id)
    return render_template("results.html", status=job)


@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = _get_job_or_404(job_id)
    return jsonify(_job_to_json(job))


@app.route("/api/cancel/<job_id>", methods=["POST"])
def api_cancel(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "not_found_or_finished"}), 404
        if not job["running"]:
            return jsonify({"ok": False, "error": "not_running"}), 400
        job["cancelled"] = True
        # Cancela futures ainda na fila (in-flight são abortados pelo hook)
        for f in job.get("_futures", []):
            f.cancel()
    return jsonify({"ok": True})


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def api_delete_job(job_id):
    """Apaga um job individual. Recusa se ainda estiver em execução."""
    with jobs_lock:
        live = jobs.get(job_id)
        if live is not None and live.get("running"):
            return jsonify({
                "ok": False,
                "error": "still_running",
                "message": "Cancele a tarefa antes de remover.",
            }), 409
        # Remove da memória se estiver lá
        jobs.pop(job_id, None)

    deleted = store.delete_job(job_id)
    if not deleted:
        return jsonify({"ok": False, "error": "not_found"}), 404
    return jsonify({"ok": True})


@app.route("/api/retry/<job_id>", methods=["POST"])
def api_retry(job_id):
    """Cria um novo job com os links que falharam no job original."""
    original = _get_job_or_404(job_id)

    if original.get("running"):
        return jsonify({"ok": False, "error": "still_running",
                        "message": "Cancele a tarefa antes de tentar novamente."}), 400

    failed_links = [r["link"] for r in original.get("results", [])
                    if not r["success"]]
    if not failed_links:
        return jsonify({"ok": False, "error": "no_failures",
                        "message": "Nenhuma falha encontrada para retentar."}), 400

    job = _new_job(
        workers=original["workers"],
        total_links=len(failed_links),
        fmt=original["format"],
        is_playlist=original["is_playlist"],
        dest_path=original["dest_path"],
    )
    _register_job(job)
    store.create_job(job)
    store.prune_old(keep=MAX_JOBS_KEPT)

    thread = threading.Thread(
        target=_run_job,
        args=(job["id"], failed_links, original["dest_path"],
              original["is_playlist"], original["format"], original["workers"]),
        daemon=True,
    )
    thread.start()

    return jsonify({"ok": True, "job_id": job["id"]})


@app.route("/api/jobs", methods=["DELETE"])
def api_delete_finished():
    """Apaga todas as tarefas que não estão em execução."""
    with jobs_lock:
        # Remove da memória os finalizados
        finished_ids = [jid for jid, j in jobs.items() if not j.get("running")]
        for jid in finished_ids:
            jobs.pop(jid, None)
    n = store.delete_finished()
    return jsonify({"ok": True, "deleted": n})


@app.errorhandler(413)
def too_large(_e):
    flash(f"Upload excede o limite de {MAX_UPLOAD_MB} MB.", "danger")
    return redirect(url_for("index")), 413


@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "not_found"}), 404
    flash("Recurso não encontrado.", "warning")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=DEBUG)
"""
app.py — Flask web app para download de vídeos via yt-dlp.

Mudanças vs. versão original:
  - Estado por job (UUID), não global compartilhado.
  - Import direto de download_videos (sem subprocess + parse JSON).
  - Validação de path e secure_filename para uploads.
  - Configuração via variáveis de ambiente.
  - Suporte a formato/qualidade e cancelamento.
"""
import os
import time
import uuid
import threading
import tempfile
from collections import OrderedDict
from urllib.parse import urlparse

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, jsonify, abort
)
from werkzeug.utils import secure_filename

import download_videos


# ---------------------------------------------------------------------------
# Configuração via env (com defaults razoáveis)
# ---------------------------------------------------------------------------
DEFAULT_DOWNLOAD_PATH = os.environ.get("DEFAULT_DOWNLOAD_PATH", "/mnt/nas/Downloads")
ALLOWED_DOWNLOAD_ROOT = os.environ.get("ALLOWED_DOWNLOAD_ROOT", DEFAULT_DOWNLOAD_PATH)
SECRET_KEY = os.environ.get("SECRET_KEY") or os.urandom(32).hex()
MAX_LINKS = int(os.environ.get("MAX_LINKS", "500"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "1"))
MAX_JOBS_KEPT = int(os.environ.get("MAX_JOBS_KEPT", "20"))
DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"

ALLOWED_FORMATS = {
    "best", "1080p", "720p", "480p", "audio_mp3", "audio_m4a"
}

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


# ---------------------------------------------------------------------------
# Estado de jobs (com lock)
# ---------------------------------------------------------------------------
jobs_lock = threading.Lock()
jobs: "OrderedDict[str, dict]" = OrderedDict()


def _new_job() -> dict:
    """Cria um job com TODAS as chaves preenchidas (corrige bug do template)."""
    return {
        "id": str(uuid.uuid4()),
        "running": False,
        "completed": False,
        "cancelled": False,
        "total_links": 0,
        "current_link": 0,
        "current_percent": 0,
        "current_title": "",
        "current_link_url": "",
        "links_status": {},
        "messages": [],
        "results": [],
        "created_at": time.time(),
        "format": "best",
        "is_playlist": False,
        "dest_path": "",
    }


def _register_job(job: dict) -> None:
    with jobs_lock:
        jobs[job["id"]] = job
        # Mantém só os N mais recentes (LRU)
        while len(jobs) > MAX_JOBS_KEPT:
            jobs.popitem(last=False)


def _get_job(job_id: str) -> dict:
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        abort(404)
    return job


def _recent_jobs(limit: int = 5) -> list:
    with jobs_lock:
        items = list(jobs.values())[-limit:][::-1]
        return [
            {
                "id": j["id"],
                "created_at": j["created_at"],
                "total_links": j["total_links"],
                "running": j["running"],
                "completed": j["completed"],
                "cancelled": j["cancelled"],
                "format": j["format"],
                "is_playlist": j["is_playlist"],
            }
            for j in items
        ]


# ---------------------------------------------------------------------------
# Validações de segurança
# ---------------------------------------------------------------------------
def _validate_dest_path(raw_path: str) -> str:
    """
    Resolve path real e garante que está sob ALLOWED_DOWNLOAD_ROOT.
    Bloqueia tentativas de path traversal (../../etc, etc.).
    """
    if not raw_path or not raw_path.strip():
        raise ValueError("Caminho de destino vazio.")

    abs_path = os.path.realpath(raw_path)
    abs_root = os.path.realpath(ALLOWED_DOWNLOAD_ROOT)

    if abs_path != abs_root and not abs_path.startswith(abs_root + os.sep):
        raise ValueError(
            f"Caminho fora da raiz permitida ({ALLOWED_DOWNLOAD_ROOT})."
        )

    os.makedirs(abs_path, exist_ok=True)
    return abs_path


def _validate_links(raw_text: str) -> list:
    """
    Quebra o texto em linhas, valida URL básica e limita a MAX_LINKS.
    Aceita comentários começando com '#'.
    """
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


# ---------------------------------------------------------------------------
# Callback: traduz eventos do download_videos em updates do job dict
# ---------------------------------------------------------------------------
def _make_callback(job_id: str):
    def on_event(event: dict):
        with jobs_lock:
            job = jobs.get(job_id)
            if job is None:
                return
            etype = event.get("type")

            if etype == "progress":
                job["current_percent"] = event.get("percent", 0)
                if event.get("title"):
                    job["current_title"] = event["title"]
                link = event.get("link")
                if link:
                    job["links_status"][link] = {
                        "percent": event.get("percent", 0),
                        "status": "downloading",
                        "title": event.get("title", ""),
                    }

            elif etype == "link_start":
                job["current_link"] = event.get("index", 0)
                job["total_links"] = event.get("total", job["total_links"])
                job["current_link_url"] = event.get("link", "")
                job["current_percent"] = 0
                job["current_title"] = ""

            elif etype == "link_done":
                link = event.get("link")
                if link:
                    job["links_status"][link] = {
                        "percent": 100 if event.get("success") else 0,
                        "status": "success" if event.get("success") else "error",
                        "title": event.get("title", ""),
                        "message": event.get("message", ""),
                    }
                job["results"].append({
                    "link": link,
                    "title": event.get("title", ""),
                    "success": event.get("success", False),
                    "message": event.get("message", ""),
                })

            elif etype in ("info", "success", "warning", "error"):
                job["messages"].append({
                    "type": etype,
                    "message": event.get("message", ""),
                    "ts": time.time(),
                })
                # Limita o histórico de mensagens para não estourar memória
                if len(job["messages"]) > 500:
                    job["messages"] = job["messages"][-500:]
    return on_event


def _is_cancelled(job_id: str) -> bool:
    with jobs_lock:
        job = jobs.get(job_id)
        return job is None or job["cancelled"]


def _run_job(job_id: str, links: list, dest_path: str,
             is_playlist: bool, fmt: str):
    """Executa o download em thread; chamado via threading.Thread."""
    cb = _make_callback(job_id)
    try:
        download_videos.processar_links(
            links=links,
            download_dir=dest_path,
            is_playlist=is_playlist,
            fmt=fmt,
            on_event=cb,
            is_cancelled=lambda: _is_cancelled(job_id),
        )
    except Exception as e:
        cb({"type": "error", "message": f"Erro inesperado: {e}"})
    finally:
        with jobs_lock:
            j = jobs.get(job_id)
            if j is not None:
                j["running"] = False
                j["completed"] = True


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        # 1) Caminho de destino (validado)
        try:
            dest_path = _validate_dest_path(
                request.form.get("dest_path", DEFAULT_DOWNLOAD_PATH)
            )
        except (ValueError, OSError) as e:
            flash(f"Erro no caminho de destino: {e}", "danger")
            return redirect(url_for("index"))

        # 2) Tipo (vídeo/playlist) e formato/qualidade
        download_type = request.form.get("download_type", "video")
        is_playlist = download_type == "playlist"

        fmt = request.form.get("format", "best")
        if fmt not in ALLOWED_FORMATS:
            flash("Formato inválido.", "danger")
            return redirect(url_for("index"))

        # 3) Coleta links: arquivo OU textarea
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

        # 4) Cria e registra o job
        job = _new_job()
        job["running"] = True
        job["total_links"] = len(links)
        job["format"] = fmt
        job["is_playlist"] = is_playlist
        job["dest_path"] = dest_path
        _register_job(job)

        # 5) Dispara em background
        thread = threading.Thread(
            target=_run_job,
            args=(job["id"], links, dest_path, is_playlist, fmt),
            daemon=True,
        )
        thread.start()

        return redirect(url_for("status", job_id=job["id"]))

    return render_template(
        "index.html",
        default_path=DEFAULT_DOWNLOAD_PATH,
        recent_jobs=_recent_jobs(),
    )


@app.route("/status/<job_id>")
def status(job_id):
    job = _get_job(job_id)
    return render_template("status.html", status=job)


@app.route("/results/<job_id>")
def results(job_id):
    job = _get_job(job_id)
    return render_template("results.html", status=job)


@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = _get_job(job_id)
    return jsonify(job)


@app.route("/api/cancel/<job_id>", methods=["POST"])
def api_cancel(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "not_found"}), 404
        if not job["running"]:
            return jsonify({"ok": False, "error": "not_running"}), 400
        job["cancelled"] = True
    return jsonify({"ok": True})


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

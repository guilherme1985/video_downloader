"""
app.py — Flask + ThreadPoolExecutor + SQLite.

Modelo de dados em memória (jobs[id]):
  Estado transitório que NÃO vai para o DB:
    - active: dict {link: {title, percent}} dos workers em execução agora.
    - done_count: contador rápido (= len(results)).

Todo o resto (mensagens, resultados finalizados, flags) é persistido.
"""
import os
import ipaddress
import json
import shutil
import time
import uuid
import threading
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, Future
from collections import OrderedDict
from urllib.parse import urlparse

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, jsonify, abort, send_file
)
from werkzeug.utils import secure_filename

import download_videos
from db import JobStore


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
APP_VERSION = "v2.5.1"

DEFAULT_DOWNLOAD_PATH = os.environ.get("DEFAULT_DOWNLOAD_PATH", "/mnt/nas/Downloads")
ALLOWED_DOWNLOAD_ROOT = os.environ.get("ALLOWED_DOWNLOAD_ROOT", DEFAULT_DOWNLOAD_PATH)
DB_PATH = os.environ.get("DB_PATH", "/data/jobs.db")
COOKIE_DIR = os.environ.get("COOKIE_DIR", "/data/cookies")
SECRET_KEY = os.environ.get("SECRET_KEY") or os.urandom(32).hex()

MAX_LINKS = int(os.environ.get("MAX_LINKS", "500"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "1"))
MAX_JOBS_KEPT = int(os.environ.get("MAX_JOBS_KEPT", "50"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "10"))
DEFAULT_WORKERS = int(os.environ.get("DEFAULT_WORKERS", "1"))
MAX_FILESIZE_MB = int(os.environ.get("MAX_FILESIZE_MB", "0"))  # 0 = sem limite
JOB_MAX_RUNTIME_S = int(os.environ.get("JOB_MAX_RUNTIME_S", "21600"))  # 6h; 0 = sem limite

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
WEBHOOK_TIMEOUT_S = int(os.environ.get("WEBHOOK_TIMEOUT_S", "5"))

DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"

# Hostnames literais sempre bloqueados (defesa em profundidade SSRF para
# uso em LAN — não impede 100% mas barra os casos óbvios sem resolução DNS).
_BLOCKED_HOSTS = frozenset({
    "localhost", "ip6-localhost", "ip6-loopback", "localhost.localdomain",
})

ALLOWED_FORMATS = {
    "best", "1080p", "720p", "480p", "audio_mp3", "audio_m4a",
}

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


@app.context_processor
def _inject_globals():
    return {"app_version": APP_VERSION}


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
             is_playlist: bool, dest_path: str,
             cookie_id: str = "") -> dict:
    now = time.time()
    deadline = (now + JOB_MAX_RUNTIME_S) if JOB_MAX_RUNTIME_S > 0 else None
    return {
        "id": str(uuid.uuid4()),
        "running": True,
        "completed": False,
        "cancelled": False,
        "created_at": now,
        "finished_at": None,
        "total_links": total_links,
        "format": fmt,
        "is_playlist": is_playlist,
        "dest_path": dest_path,
        "workers": workers,
        "cookie_id": cookie_id,
        # Transitório (não persistido):
        "active": {},          # link -> {title, percent}
        "done_count": 0,
        # Persistido sob demanda; também cacheado em memória:
        "messages": [],
        "results": [],
        # Controle interno:
        "_executor": None,
        "_futures": [],
        "_deadline": deadline,   # epoch s; None = sem limite
        "_timed_out": False,     # marca para o callback de finalize emitir warning
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


def _snapshot_live_job(job: dict) -> dict:
    """Copia raso + cópias defensivas das coleções mutáveis.

    Deve ser chamado COM o jobs_lock segurado pelo caller. Garante que
    qualquer consumidor (template, JSON serializer) itere sobre dados
    estáveis, sem race com o callback que muta active/messages/results.
    """
    snap = {k: v for k, v in job.items() if not k.startswith("_")}
    active = job.get("active") or {}
    snap["active"] = [{"link": link, **dict(info)}
                      for link, info in active.items()]
    snap["messages"] = list(job.get("messages") or [])
    snap["results"] = list(job.get("results") or [])
    snap["done_count"] = len(snap["results"])
    return snap


def _get_job_or_404(job_id: str) -> dict:
    """Retorna um SNAPSHOT consistente do job (memória ou DB)."""
    with jobs_lock:
        job = jobs.get(job_id)
        if job is not None:
            return _snapshot_live_job(job)

    persisted = store.get_job(job_id)
    if persisted is None:
        abort(404)

    persisted["active"] = []
    persisted["done_count"] = len(persisted["results"])
    return persisted


def _is_cancelled(job_id: str) -> bool:
    """True se o job foi cancelado, removido ou estourou o deadline.

    Quando o deadline expira, marca cancelled=True para que os outros
    workers parem imediatamente, e seta _timed_out para que `_run_job`
    emita uma mensagem específica no log de eventos.
    """
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return True
        if job.get("cancelled"):
            return True
        deadline = job.get("_deadline")
        if deadline is not None and time.time() > deadline:
            job["cancelled"] = True
            job["_timed_out"] = True
            return True
        return False


# ---------------------------------------------------------------------------
# Validações
# ---------------------------------------------------------------------------
def _validate_dest_path(raw: str) -> str:
    """Valida que o caminho está dentro de ALLOWED_DOWNLOAD_ROOT.

    NÃO cria a pasta — isso é feito apenas no momento do download
    (`baixar_video`), evitando que a validação tenha efeito colateral
    no filesystem (pastas vazias órfãs caso o submit seja abortado).
    """
    if not raw or not raw.strip():
        raise ValueError("Caminho de destino vazio.")
    abs_path = os.path.realpath(raw)
    abs_root = os.path.realpath(ALLOWED_DOWNLOAD_ROOT)
    if abs_path != abs_root and not abs_path.startswith(abs_root + os.sep):
        raise ValueError(
            f"Caminho fora da raiz permitida ({ALLOWED_DOWNLOAD_ROOT})."
        )
    return abs_path


def _is_blocked_host(host: str) -> bool:
    """True se o host for IP privado/loopback/link-local ou nome bloqueado.

    Apenas inspeção sintática — não resolve DNS. Suficiente para impedir os
    casos óbvios (http://127.0.0.1, http://192.168.x.y, etc).
    """
    h = (host or "").strip().lower()
    if not h:
        return True
    # Remove brackets de IPv6 (urlparse mantém os colchetes em [::1])
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if h in _BLOCKED_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def _validate_links(raw_text: str) -> list:
    links = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = urlparse(line)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"Link inválido: {line[:80]}")
        if _is_blocked_host(parsed.hostname or ""):
            raise ValueError(
                f"Host não permitido (IP interno/loopback): {line[:80]}"
            )
        if len(links) >= MAX_LINKS:
            raise ValueError(f"Excedeu o limite de {MAX_LINKS} links por job.")
        links.append(line)
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


def _cookie_path(cookie_id: str) -> str:
    return os.path.join(COOKIE_DIR, f"{cookie_id}.txt")


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
                file_path = event.get("file_path", "")
                job["active"].pop(link, None)
                job["results"].append({
                    "link": link, "title": title,
                    "success": success, "message": msg,
                    "file_path": file_path,
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
                event.get("file_path", ""),
            )
        elif etype in ("info", "success", "warning", "error"):
            store.add_message(job_id, etype, event.get("message", ""))

    return on_event


# ---------------------------------------------------------------------------
# Execução paralela
# ---------------------------------------------------------------------------
def _run_single_link(job_id: str, link: str, dest_path: str,
                     is_playlist: bool, fmt: str, on_event,
                     cookiefile=None):
    """Tarefa individual submetida ao executor."""
    if _is_cancelled(job_id):
        on_event({"type": "link_done", "link": link, "success": False,
                  "title": "", "message": "Cancelado antes de iniciar",
                  "file_path": ""})
        return

    on_event({"type": "link_start", "link": link})
    result = download_videos.baixar_video(
        link=link,
        download_dir=dest_path,
        is_playlist=is_playlist,
        fmt=fmt,
        on_event=on_event,
        is_cancelled=lambda: _is_cancelled(job_id),
        cookiefile=cookiefile,
        max_filesize_mb=MAX_FILESIZE_MB,
    )
    on_event({
        "type": "link_done",
        "link": link,
        "success": result["success"],
        "title": result["title"],
        "message": result["message"],
        "file_path": result.get("file_path", ""),
    })
    if result["success"]:
        on_event({"type": "success",
                  "message": f"OK: {result['title'] or link}"})
    else:
        on_event({"type": "error",
                  "message": f"Falha em {link}: {result['message']}"})


def _send_webhook(payload: dict) -> None:
    """POST JSON best-effort para WEBHOOK_URL. Roda em thread daemon.

    Falhas são apenas logadas — webhook NUNCA quebra o fluxo do job.
    """
    if not WEBHOOK_URL:
        return
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            WEBHOOK_URL,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"video-downloader/{APP_VERSION}",
            },
        )
        with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT_S) as resp:
            if resp.status >= 400:
                app.logger.warning(
                    "Webhook retornou HTTP %s para job %s",
                    resp.status, payload.get("job_id"),
                )
    except (urllib.error.URLError, OSError, ValueError) as e:
        app.logger.warning(
            "Falha ao enviar webhook para job %s: %s",
            payload.get("job_id"), e,
        )


def _dispatch_webhook(job_id: str, status: str) -> None:
    """Monta payload sob lock e dispara em thread daemon."""
    if not WEBHOOK_URL:
        return
    with jobs_lock:
        j = jobs.get(job_id)
        if j is None:
            return
        results = list(j.get("results") or [])
        ok = sum(1 for r in results if r.get("success"))
        payload = {
            "job_id": job_id,
            "version": APP_VERSION,
            "status": status,
            "total_links": j.get("total_links", 0),
            "success_count": ok,
            "fail_count": len(results) - ok,
            "format": j.get("format"),
            "is_playlist": j.get("is_playlist"),
            "dest_path": j.get("dest_path"),
            "workers": j.get("workers"),
            "created_at": j.get("created_at"),
            "finished_at": j.get("finished_at"),
            "duration_s": (
                (j.get("finished_at") or time.time()) - j.get("created_at", 0)
            ),
        }
    threading.Thread(
        target=_send_webhook, args=(payload,),
        daemon=True, name=f"webhook-{job_id[:8]}",
    ).start()


def _materialize_cookiefile(source: str) -> str:
    """Copia o cookiefile para um tmp privado do job.

    yt-dlp REESCREVE o cookiefile com cookies atualizados ao final do
    download. Para cookies persistentes salvos pelo usuário, isso
    corromperia o original e causaria race entre jobs concorrentes que
    reutilizam o mesmo cookie. Sempre operamos em uma cópia.
    """
    tmp = os.path.join(tempfile.gettempdir(),
                       f"{uuid.uuid4().hex}_cookies.txt")
    shutil.copyfile(source, tmp)
    return tmp


def _run_job(job_id: str, links: list, dest_path: str,
             is_playlist: bool, fmt: str, workers: int,
             cookiefile=None, delete_cookiefile=False):
    """Wrapper de safety: garante finalize_job sempre, mesmo em crash."""
    cb = _make_callback(job_id)
    # Trabalha sempre numa cópia descartável do cookiefile (ver docstring de
    # _materialize_cookiefile). delete_cookiefile só controla se o ORIGINAL
    # também deve ser removido após o job (cookies de uso único).
    working_cookiefile = None
    if cookiefile:
        try:
            working_cookiefile = _materialize_cookiefile(cookiefile)
        except OSError as e:
            cb({"type": "warning",
                "message": f"Não foi possível preparar cookie: {e}. "
                           "Job seguirá sem autenticação."})

    cb({"type": "info",
        "message": f"Iniciando {len(links)} link(s) com {workers} worker(s)."})
    if working_cookiefile:
        cb({"type": "info",
            "message": f"Cookie em uso: {os.path.basename(cookiefile)}"})

    executor = ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix=f"job-{job_id[:8]}",
    )
    with jobs_lock:
        j = jobs.get(job_id)
        if j is not None:
            j["_executor"] = executor

    cancelled = False
    try:
        futures: list = []
        for link in links:
            fut: Future = executor.submit(
                _run_single_link, job_id, link, dest_path,
                is_playlist, fmt, cb, working_cookiefile,
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
    except Exception as e:
        app.logger.exception("Falha não tratada em _run_job")
        cb({"type": "error", "message": f"Erro fatal no job: {e}"})
    finally:
        try:
            executor.shutdown(wait=True, cancel_futures=True)
        except Exception:
            app.logger.exception("Erro no shutdown do executor")

        # Tmp do cookie sempre é descartado; original só se uso único.
        if working_cookiefile:
            try:
                os.remove(working_cookiefile)
            except OSError:
                pass
        if cookiefile and delete_cookiefile:
            try:
                os.remove(cookiefile)
            except OSError:
                pass

        cancelled = _is_cancelled(job_id)
        timed_out = False
        if cancelled:
            with jobs_lock:
                j = jobs.get(job_id)
                timed_out = bool(j and j.get("_timed_out"))
            if timed_out:
                cb({"type": "warning",
                    "message": f"Tempo máximo de execução excedido "
                               f"({JOB_MAX_RUNTIME_S}s). Job cancelado."})
            else:
                cb({"type": "warning",
                    "message": "Operação cancelada pelo usuário."})
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
        try:
            store.finalize_job(job_id, completed=True, cancelled=cancelled)
        except Exception:
            app.logger.exception("Falha ao finalizar job no DB")

        webhook_status = (
            "timeout" if timed_out
            else "cancelled" if cancelled
            else "completed"
        )
        _dispatch_webhook(job_id, webhook_status)


# ---------------------------------------------------------------------------
# Serialização (snapshot já vem sanitizado por _get_job_or_404)
# ---------------------------------------------------------------------------
def _job_to_json(job: dict) -> dict:
    return {k: v for k, v in job.items() if not k.startswith("_")}


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

        # Cookie file opcional
        cookiefile_path = None
        delete_cookiefile = False
        used_cookie_id = ""  # persistido no job para que /api/retry preserve

        use_saved_id = request.form.get("use_saved_cookie_id", "").strip()
        cookies_upload = request.files.get("cookies_file")
        cookie_save_name = request.form.get("cookie_save_name", "").strip()

        if cookies_upload and cookies_upload.filename:
            # Upload tem prioridade sobre seleção de cookie salvo
            if not cookies_upload.filename.lower().endswith(".txt"):
                flash("Arquivo de cookies deve ser .txt (formato Netscape).", "danger")
                return redirect(url_for("index"))
            safe_cookie_filename = (
                secure_filename(cookies_upload.filename) or "cookies.txt"
            )
            if cookie_save_name:
                # Salvar permanentemente em /data/cookies/
                os.makedirs(COOKIE_DIR, exist_ok=True)
                cookie_id = str(uuid.uuid4())
                saved_path = _cookie_path(cookie_id)
                cookies_upload.save(saved_path)
                store.create_cookie(cookie_id, cookie_save_name,
                                    safe_cookie_filename)
                cookiefile_path = saved_path
                used_cookie_id = cookie_id
            else:
                # Uso único — deletar após o job
                tmp_cookies = os.path.join(
                    tempfile.gettempdir(),
                    f"{uuid.uuid4().hex}_cookies.txt",
                )
                cookies_upload.save(tmp_cookies)
                cookiefile_path = tmp_cookies
                delete_cookiefile = True
        elif use_saved_id:
            cookie_record = store.get_cookie(use_saved_id)
            if cookie_record:
                path = _cookie_path(use_saved_id)
                if os.path.isfile(path):
                    cookiefile_path = path
                    used_cookie_id = use_saved_id
                else:
                    flash(
                        f"Cookie '{cookie_record['name']}' não encontrado no disco. "
                        "O download prosseguirá sem autenticação.",
                        "warning",
                    )

        # Cria + persiste + dispara
        job = _new_job(workers=workers, total_links=len(links), fmt=fmt,
                       is_playlist=is_playlist, dest_path=dest_path,
                       cookie_id=used_cookie_id)
        _register_job(job)
        store.create_job(job)
        store.prune_old(keep=MAX_JOBS_KEPT)

        thread = threading.Thread(
            target=_run_job,
            args=(job["id"], links, dest_path, is_playlist, fmt,
                  workers, cookiefile_path, delete_cookiefile),
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
        saved_cookies=store.list_cookies(),
    )


@app.route("/status/<job_id>")
def status(job_id):
    job = _get_job_or_404(job_id)
    return render_template("status.html", status=job)


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

    # Propaga o cookie do job original (se ainda existir no disco).
    original_cookie_id = (original.get("cookie_id") or "").strip()
    retry_cookiefile = None
    if original_cookie_id:
        path = _cookie_path(original_cookie_id)
        if os.path.isfile(path):
            retry_cookiefile = path
        else:
            original_cookie_id = ""  # cookie removido entre jobs

    job = _new_job(
        workers=original["workers"],
        total_links=len(failed_links),
        fmt=original["format"],
        is_playlist=original["is_playlist"],
        dest_path=original["dest_path"],
        cookie_id=original_cookie_id,
    )
    _register_job(job)
    store.create_job(job)
    store.prune_old(keep=MAX_JOBS_KEPT)

    thread = threading.Thread(
        target=_run_job,
        args=(job["id"], failed_links, original["dest_path"],
              original["is_playlist"], original["format"],
              original["workers"], retry_cookiefile, False),
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


@app.route("/api/cookies")
def api_list_cookies():
    return jsonify(store.list_cookies())


@app.route("/api/cookies/<cookie_id>", methods=["DELETE"])
def api_delete_cookie(cookie_id):
    cookie = store.get_cookie(cookie_id)
    if not cookie:
        return jsonify({"ok": False, "error": "not_found"}), 404
    try:
        os.remove(_cookie_path(cookie_id))
    except OSError:
        pass
    store.delete_cookie(cookie_id)
    return jsonify({"ok": True})


@app.route("/download/<job_id>/<int:result_idx>")
def download_file(job_id, result_idx):
    """Serve um arquivo baixado diretamente para o browser."""
    job = _get_job_or_404(job_id)
    results = job.get("results", [])
    if result_idx < 0 or result_idx >= len(results):
        abort(404)
    result = results[result_idx]
    if not result.get("success") or not result.get("file_path"):
        abort(404)

    file_path = os.path.realpath(result["file_path"])
    allowed_root = os.path.realpath(ALLOWED_DOWNLOAD_ROOT)
    if file_path != allowed_root and not file_path.startswith(allowed_root + os.sep):
        abort(403)
    if not os.path.isfile(file_path):
        abort(404)

    return send_file(
        file_path,
        as_attachment=True,
        download_name=os.path.basename(file_path),
    )


@app.after_request
def _security_headers(response):
    """Headers básicos de hardening (sem CSP para não quebrar inline JS)."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy", "interest-cohort=(), browsing-topics=()"
    )
    return response


@app.route("/healthz")
def healthz():
    """Healthcheck que valida Flask + acesso ao DB."""
    try:
        store.ping()
    except Exception:
        app.logger.exception("Healthcheck falhou")
        return jsonify({"ok": False, "error": "db_unreachable"}), 503
    return jsonify({"ok": True, "version": APP_VERSION})


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
"""
download_videos.py — wrapper sobre yt-dlp orientado a callbacks.

Diferenças vs. versão original:
  - Sem prints de JSON no stdout: comunica via callback `on_event(dict)`.
  - Suporta cancelamento via predicate `is_cancelled() -> bool`.
  - `ignoreerrors=False` para reportar falhas reais por vídeo.
  - Nomes de pasta de playlist saneados (caracteres problemáticos).
  - Mantém um modo CLI para uso standalone (com saída JSON em stdout).
"""
import os
import re
import sys
import time
import json
from typing import Callable, List, Optional

import yt_dlp


# Mapeamento de format amigável -> string de formato do yt-dlp
FORMAT_MAP = {
    "best":       "bestvideo+bestaudio/best",
    "1080p":      "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "720p":       "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "480p":       "bestvideo[height<=480]+bestaudio/best[height<=480]",
    "audio_mp3":  "bestaudio/best",
    "audio_m4a":  "bestaudio[ext=m4a]/bestaudio/best",
}

EventCallback = Callable[[dict], None]
CancelChecker = Callable[[], bool]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class _Cancelled(Exception):
    """Lançada de dentro do progress_hook para abortar o download atual."""


def _safe_dirname(name: str) -> str:
    """Sanitiza nome de pasta (Windows/Linux compat)."""
    safe = "".join(c for c in name if c.isalnum() or c in " ._-()[]")
    safe = safe.strip().rstrip(".")
    return safe or "Playlist"


def _log(download_dir: str, message: str) -> None:
    log_file = os.path.join(download_dir, "download_log.txt")
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except OSError:
        pass


def _build_ydl_opts(
    output_dir: str,
    fmt: str,
    on_event: Optional[EventCallback],
    is_cancelled: Optional[CancelChecker],
    current_link: Optional[str],
    is_playlist: bool,
    cookiefile: Optional[str] = None,
    file_tracker: Optional[list] = None,
) -> dict:
    """Monta opções do yt-dlp incluindo o progress_hook."""

    def progress_hook(d):
        # Cancelamento: a única forma confiável de interromper yt-dlp
        # no meio do download é levantando exceção do hook.
        if is_cancelled and is_cancelled():
            raise _Cancelled()
        # Fallback: captura o arquivo baixado antes de qualquer pós-processamento
        if d.get("status") == "finished" and file_tracker is not None and not file_tracker:
            fn = d.get("filename") or ""
            if fn:
                file_tracker.append(os.path.abspath(fn))
        if d.get("status") != "downloading":
            return
        if not on_event:
            return

        raw = d.get("_percent_str", "0%")
        # Remove escapes ANSI que o yt-dlp às vezes adiciona
        raw = _ANSI_RE.sub("", raw).replace("%", "").strip()
        try:
            percent = float(raw)
        except ValueError:
            percent = 0.0

        title = (d.get("info_dict") or {}).get("title", "") or ""
        on_event({
            "type": "progress",
            "link": current_link,
            "percent": round(percent, 1),
            "title": title,
        })

    opts = {
        "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
        "format": FORMAT_MAP.get(fmt, FORMAT_MAP["best"]),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": not is_playlist,
        "progress_hooks": [progress_hook],
        "ignoreerrors": False,  # falhas individuais devem propagar
        "retries": 3,
        "fragment_retries": 3,
    }

    if fmt == "audio_mp3":
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
        opts.pop("merge_output_format", None)
    elif fmt == "audio_m4a":
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "m4a",
        }]
        opts.pop("merge_output_format", None)

    if cookiefile:
        opts["cookiefile"] = cookiefile

    if file_tracker is not None:
        def _pp_hook(d):
            if d.get("status") == "finished":
                fp = (d.get("info_dict") or {}).get("filepath") or ""
                if fp:
                    abs_fp = os.path.abspath(fp)
                    if file_tracker:
                        file_tracker[0] = abs_fp
                    else:
                        file_tracker.append(abs_fp)
        opts["postprocessor_hooks"] = [_pp_hook]

    return opts


def baixar_video(
    link: str,
    download_dir: str,
    is_playlist: bool = False,
    fmt: str = "best",
    on_event: Optional[EventCallback] = None,
    is_cancelled: Optional[CancelChecker] = None,
    cookiefile: Optional[str] = None,
) -> dict:
    """
    Baixa um único link (vídeo ou playlist).
    Retorna dict {success, title, message}.
    """
    os.makedirs(download_dir, exist_ok=True)
    file_tracker: list = []
    opts = _build_ydl_opts(
        output_dir=download_dir,
        fmt=fmt,
        on_event=on_event,
        is_cancelled=is_cancelled,
        current_link=link,
        is_playlist=is_playlist,
        cookiefile=cookiefile,
        file_tracker=file_tracker,
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(link, download=False)

            if is_playlist and isinstance(info, dict) and "entries" in info:
                playlist_title = info.get("title") or "Playlist"
                count = sum(1 for _ in info.get("entries", []) if _ is not None)
                if on_event:
                    on_event({
                        "type": "info",
                        "message": (
                            f"Iniciando playlist: {playlist_title} ({count} vídeos)"
                        ),
                    })
                playlist_dir = os.path.join(download_dir, _safe_dirname(playlist_title))
                os.makedirs(playlist_dir, exist_ok=True)
                opts["outtmpl"] = os.path.join(playlist_dir, "%(title)s.%(ext)s")
                with yt_dlp.YoutubeDL(opts) as ydl2:
                    ydl2.download([link])
                _log(download_dir, f"Playlist concluída: {playlist_title} ({link})")
                return {"success": True, "title": playlist_title, "message": "", "file_path": playlist_dir}

            video_title = (info or {}).get("title") or "Vídeo"
            if on_event:
                on_event({"type": "info", "message": f"Iniciando: {video_title}"})
            ydl.download([link])
            _log(download_dir, f"Concluído: {video_title} ({link})")
            file_path = file_tracker[0] if file_tracker else ""
            return {"success": True, "title": video_title, "message": "", "file_path": file_path}

    except _Cancelled:
        _log(download_dir, f"Cancelado: {link}")
        return {"success": False, "title": "", "message": "Cancelado", "file_path": ""}
    except yt_dlp.utils.DownloadError as e:
        _log(download_dir, f"Falha: {link} — {e}")
        return {"success": False, "title": "", "message": str(e), "file_path": ""}
    except Exception as e:  # rede, FS, etc
        _log(download_dir, f"Erro inesperado: {link} — {e}")
        return {"success": False, "title": "", "message": str(e), "file_path": ""}


def processar_links(
    links: List[str],
    download_dir: str,
    is_playlist: bool = False,
    fmt: str = "best",
    on_event: Optional[EventCallback] = None,
    is_cancelled: Optional[CancelChecker] = None,
    cookiefile: Optional[str] = None,
) -> None:
    """Processa uma lista de links sequencialmente, emitindo eventos."""
    os.makedirs(download_dir, exist_ok=True)
    total = len(links)

    if on_event:
        on_event({"type": "info", "message": f"Total de links: {total}"})
    _log(download_dir, f"=== Sessão iniciada ({total} links, fmt={fmt}) ===")

    falhas = 0
    for i, link in enumerate(links, start=1):
        if is_cancelled and is_cancelled():
            if on_event:
                on_event({"type": "warning",
                          "message": "Operação cancelada pelo usuário."})
            break

        if on_event:
            on_event({"type": "link_start", "index": i, "total": total, "link": link})

        result = baixar_video(
            link=link,
            download_dir=download_dir,
            is_playlist=is_playlist,
            fmt=fmt,
            on_event=on_event,
            is_cancelled=is_cancelled,
            cookiefile=cookiefile,
        )

        if not result["success"]:
            falhas += 1

        if on_event:
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

    _log(download_dir,
         f"=== Sessão encerrada (sucesso: {total - falhas} / falhas: {falhas}) ===")
    if on_event:
        on_event({
            "type": "info",
            "message": f"Concluído. Sucesso: {total - falhas}, falhas: {falhas}.",
        })


# ---------------------------------------------------------------------------
# CLI standalone (mantido por compatibilidade)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python download_videos.py <links.txt> [dest] "
              "[video|playlist] [best|1080p|720p|480p|audio_mp3|audio_m4a]")
        sys.exit(1)

    links_file = sys.argv[1]
    dest = sys.argv[2] if len(sys.argv) > 2 else "/mnt/nas/Downloads"
    is_pl = (len(sys.argv) > 3 and sys.argv[3].lower() == "playlist")
    fmt_arg = sys.argv[4] if len(sys.argv) > 4 else "best"

    if not os.path.exists(links_file):
        print(json.dumps({"type": "error",
                          "message": f"Arquivo {links_file} não encontrado."}))
        sys.exit(2)

    with open(links_file, encoding="utf-8") as f:
        link_list = [l.strip() for l in f if l.strip()]

    def cli_event(ev):
        print(json.dumps(ev, ensure_ascii=False), flush=True)

    processar_links(link_list, dest, is_pl, fmt_arg, on_event=cli_event)

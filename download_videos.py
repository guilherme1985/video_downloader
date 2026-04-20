import os
import sys
import yt_dlp
import json
import time

def baixar_video(link, download_dir, is_playlist=False, callback=None):
    os.makedirs(download_dir, exist_ok=True)
    log_file = os.path.join(download_dir, "download_log.txt")

    def progress_hook(d):
        if d['status'] == 'downloading':
            p = d.get('_percent_str', '0%')
            p = p.replace('%', '').strip()
            try:
                percent = float(p)
            except:
                percent = 0

            if callback:
                video_title = d.get('info_dict', {}).get('title', link)
                callback(link, percent, video_title)

    ydl_opts = {
        'outtmpl': os.path.join(download_dir, '%(title)s.%(ext)s'),
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'quiet': True,
        'noplaylist': not is_playlist,  # False para baixar apenas o vídeo, True para baixar a playlist
        'progress_hooks': [progress_hook],
        'ignoreerrors': True,  # Continua mesmo se houver erros
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(link, download=False)

            if is_playlist and 'entries' in info:
                # É uma playlist
                playlist_title = info.get('title', 'Playlist')
                print(json.dumps({
                    "status": "info",
                    "message": f"Iniciando download da playlist: {playlist_title}",
                    "playlist_title": playlist_title,
                    "total_videos": len(info['entries'])
                }))

                # Cria pasta específica para a playlist
                playlist_dir = os.path.join(download_dir, playlist_title)
                os.makedirs(playlist_dir, exist_ok=True)

                # Atualiza o caminho de saída para a pasta da playlist
                ydl_opts['outtmpl'] = os.path.join(playlist_dir, '%(title)s.%(ext)s')

                # Reinicia o YoutubeDL com as novas opções
                with yt_dlp.YoutubeDL(ydl_opts) as ydl2:
                    result = ydl2.download([link])

                # Registra no log
                with open(log_file, "a") as log:
                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    log.write(f"[{timestamp}] Playlist: {playlist_title} - URL: {link} - Status: Concluído\n")

                return True
            else:
                # É um vídeo único
                video_title = info.get('title', 'Vídeo')
                print(json.dumps({
                    "status": "info",
                    "message": f"Iniciando download do vídeo: {video_title}",
                    "video_title": video_title
                }))

                result = ydl.download([link])

                # Registra no log
                with open(log_file, "a") as log:
                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    log.write(f"[{timestamp}] Vídeo: {video_title} - URL: {link} - Status: Concluído\n")

                print(json.dumps({
                    "status": "success",
                    "message": f"Download concluído: {video_title}",
                    "video_title": video_title,
                    "link": link
                }))

                return True
    except Exception as e:
        error_msg = str(e)
        print(json.dumps({
            "status": "error",
            "message": f"Erro ao baixar {link}: {error_msg}",
            "link": link
        }))

        # Registra no log
        with open(log_file, "a") as log:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            log.write(f"[{timestamp}] URL: {link} - Status: Falha - Erro: {error_msg}\n")

        return False

def processar_links(links_file, download_dir, is_playlist=False):
    if not os.path.exists(links_file):
        print(json.dumps({"status": "error", "message": f"Arquivo {links_file} não encontrado."}))
        return

    with open(links_file, "r") as f:
        links = [l.strip() for l in f if l.strip()]

    if not links:
        print(json.dumps({"status": "error", "message": "Nenhum link encontrado."}))
        return

    total_links = len(links)
    print(json.dumps({"status": "info", "message": f"Total de links: {total_links}"}))

    # Inicializa o arquivo de log
    log_file = os.path.join(download_dir, "download_log.txt")
    with open(log_file, "w") as log:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log.write(f"=== Início da sessão de download: {timestamp} ===\n")

    results = []
    links_falhos = []
    for i, link in enumerate(links):
        print(json.dumps({
            "status": "progress",
            "current": i+1,
            "total": total_links,
            "link": link,
            "percent": 0
        }))

        success = baixar_video(link, download_dir, is_playlist)

        if success:
            results.append({"link": link, "success": True})
        else:
            results.append({"link": link, "success": False})
            links_falhos.append(link)

    # Salva os links que falharam
    if links_falhos:
        with open(links_file + ".falhas", "w") as f:
            for link in links_falhos:
                f.write(link + "\n")
        print(json.dumps({
            "status": "warning",
            "message": f"{len(links_falhos)} links falharam e foram salvos em {links_file}.falhas"
        }))

    # Finaliza o log
    with open(log_file, "a") as log:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log.write(f"=== Fim da sessão de download: {timestamp} ===\n")
        log.write(f"Total de links: {total_links}, Sucesso: {total_links - len(links_falhos)}, Falhas: {len(links_falhos)}\n")

    print(json.dumps({
        "status": "complete",
        "message": "Processo concluído.",
        "results": results
    }))

if __name__ == "__main__":
    # Argumentos: arquivo de links, diretório de download, tipo (video/playlist)
    links_file = sys.argv[1]
    download_path = sys.argv[2] if len(sys.argv) > 2 else "/mnt/nas/Downloads"
    is_playlist = sys.argv[3].lower() == "playlist" if len(sys.argv) > 3 else False

    print(json.dumps({"status": "info", "message": f"Usando arquivo de links: {links_file}"}))
    print(json.dumps({"status": "info", "message": f"Salvando em: {download_path}"}))
    print(json.dumps({"status": "info", "message": f"Modo: {'Playlist' if is_playlist else 'Vídeo'}"}))

    processar_links(links_file, download_path, is_playlist)

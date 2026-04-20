import os
import sys
import yt_dlp
import json

def baixar_video(link, download_dir, callback=None):
    os.makedirs(download_dir, exist_ok=True)

    def progress_hook(d):
        if d['status'] == 'downloading':
            p = d.get('_percent_str', '0%')
            p = p.replace('%', '').strip()
            try:
                percent = float(p)
            except:
                percent = 0

            if callback:
                callback(link, percent)

    ydl_opts = {
        'outtmpl': os.path.join(download_dir, '%(title)s.%(ext)s'),
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'quiet': True,
        'noplaylist': True,
        'progress_hooks': [progress_hook],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([link])
        return True
    except Exception as e:
        print(json.dumps({"link": link, "status": "error", "message": str(e)}))
        return False

def processar_links(links_file, download_dir, callback=None):
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

    links_falhos = []
    for i, link in enumerate(links):
        print(json.dumps({"status": "progress", "current": i+1, "total": total_links, "link": link, "percent": 0}))

        if not baixar_video(link, download_dir, callback):
            links_falhos.append(link)

    # Salva os links que falharam
    if links_falhos:
        with open(links_file + ".falhas", "w") as f:
            for link in links_falhos:
                f.write(link + "\n")
        print(json.dumps({"status": "warning", "message": f"{len(links_falhos)} links falharam e foram salvos em {links_file}.falhas"}))

    print(json.dumps({"status": "success", "message": "Processo concluído."}))

if __name__ == "__main__":
    # Argumentos: arquivo de links, diretório de download
    links_file = sys.argv[1]
    download_path = sys.argv[2] if len(sys.argv) > 2 else "/mnt/nas/Downloads"

    print(json.dumps({"status": "info", "message": f"Usando arquivo de links: {links_file}"}))
    print(json.dumps({"status": "info", "message": f"Salvando vídeos em: {download_path}"}))

    processar_links(links_file, download_path)

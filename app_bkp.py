from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import os
import tempfile
import subprocess
import threading
import time
import json

app = Flask(__name__)
app.secret_key = 'supersecretkey'

DEFAULT_PATH = "/mnt/nas/Downloads"
download_status = {
    "running": False,
    "completed": False,
    "total_links": 0,
    "current_link": 0,
    "current_percent": 0,
    "links_status": {}
}

@app.route('/', methods=['GET', 'POST'])
def index():
    global download_status

    if request.method == 'POST':
        dest_path = request.form.get('dest_path', DEFAULT_PATH)
        links = request.form.get('links', '')
        file = request.files.get('file')

        # Verifica se o diretório de destino existe ou pode ser criado
        try:
            os.makedirs(dest_path, exist_ok=True)
        except Exception as e:
            flash(f'Erro ao criar diretório de destino: {e}')
            return redirect(url_for('index'))

        # Cria arquivo temporário com os links
        if file and file.filename:
            txt_path = os.path.join(tempfile.gettempdir(), file.filename)
            file.save(txt_path)
        elif links.strip():
            txt_path = os.path.join(tempfile.gettempdir(), 'links.txt')
            with open(txt_path, 'w') as f:
                f.write(links)
        else:
            flash('Envie um arquivo .txt ou cole os links!')
            return redirect(url_for('index'))

        # Reseta o status do download
        download_status = {
            "running": True,
            "completed": False,
            "total_links": 0,
            "current_link": 0,
            "current_percent": 0,
            "links_status": {},
            "messages": []
        }

        # Inicia o download em uma thread separada
        def run_download():
            try:
                process = subprocess.Popen(
                    ['python', 'download_videos.py', txt_path, dest_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    universal_newlines=True
                )

                for line in process.stdout:
                    try:
                        data = json.loads(line.strip())

                        if data.get('status') == 'progress':
                            download_status["current_link"] = data.get('current', 0)
                            download_status["total_links"] = data.get('total', 0)
                            download_status["current_percent"] = data.get('percent', 0)
                            download_status["links_status"][data.get('link')] = {
                                'percent': data.get('percent', 0),
                                'status': 'downloading'
                            }
                        elif data.get('status') in ['error', 'warning', 'info', 'success']:
                            download_status["messages"].append({
                                'type': data.get('status'),
                                'message': data.get('message', '')
                            })

                            if data.get('link'):
                                download_status["links_status"][data.get('link')] = {
                                    'percent': 0,
                                    'status': 'error',
                                    'message': data.get('message', '')
                                }
                    except json.JSONDecodeError:
                        download_status["messages"].append({
                            'type': 'info',
                            'message': line.strip()
                        })

                process.wait()
                download_status["completed"] = True
                download_status["running"] = False
            except Exception as e:
                download_status["messages"].append({
                    'type': 'error',
                    'message': str(e)
                })
                download_status["completed"] = True
                download_status["running"] = False

        thread = threading.Thread(target=run_download)
        thread.daemon = True
        thread.start()

        flash('Download iniciado com sucesso!')
        return redirect(url_for('status'))

    return render_template('index.html', default_path=DEFAULT_PATH)

@app.route('/status')
def status():
    return render_template('status.html', status=download_status)

@app.route('/api/status')
def api_status():
    return jsonify(download_status)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)

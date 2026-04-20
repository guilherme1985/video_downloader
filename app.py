from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import os
import tempfile
import subprocess
import threading
import time
import json

app = Flask(__name__)
app.secret_key = 'supersecretkey'

# Pasta padrão de download
DEFAULT_PATH = "/mnt/nas/Downloads"

# Variável global para armazenar o status do download
download_status = {
    "running": False,
    "completed": False,
    "total_links": 0,
    "current_link": 0,
    "current_percent": 0,
    "current_title": "",
    "links_status": {},
    "results": []
}

# Rota principal: exibe o formulário para iniciar downloads
@app.route('/', methods=['GET', 'POST'])
def index():
    global download_status

    if request.method == 'POST':
        # Obtém o caminho de destino, links e arquivo enviado pelo usuário
        dest_path = request.form.get('dest_path', DEFAULT_PATH)
        links = request.form.get('links', '')
        file = request.files.get('file')
        download_type = request.form.get('download_type', 'video')

        # Verifica se o diretório de destino existe ou pode ser criado
        try:
            os.makedirs(dest_path, exist_ok=True)
        except Exception as e:
            flash(f'Erro ao criar diretório de destino: {e}')
            return redirect(url_for('index'))

        # Cria arquivo temporário com os links recebidos
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

        # Reseta o status do download para uma nova operação
        download_status = {
            "running": True,
            "completed": False,
            "total_links": 0,
            "current_link": 0,
            "current_percent": 0,
            "current_title": "",
            "links_status": {},
            "messages": [],
            "results": []
        }

        # Função que executa o download em uma thread separada
        def run_download():
            try:
                # Chama o script de download externo e lê as mensagens do processo
                process = subprocess.Popen(
                    ['python', 'download_videos.py', txt_path, dest_path, download_type],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    universal_newlines=True
                )

                # Processa cada linha de saída do script de download
                for line in process.stdout:
                    try:
                        data = json.loads(line.strip())

                        # Atualiza o status de progresso do download
                        if data.get('status') == 'progress':
                            download_status["current_link"] = data.get('current', 0)
                            download_status["total_links"] = data.get('total', 0)
                            download_status["current_percent"] = data.get('percent', 0)
                            download_status["links_status"][data.get('link')] = {
                                'percent': data.get('percent', 0),
                                'status': 'downloading',
                                'title': download_status.get('current_title', '')
                            }
                        # Atualiza o status quando o download é concluído
                        elif data.get('status') == 'complete':
                            download_status["results"] = data.get('results', [])
                            download_status["messages"].append({
                                'type': 'success',
                                'message': data.get('message', '')
                            })
                        # Trata mensagens de erro, aviso, informação ou sucesso
                        elif data.get('status') in ['error', 'warning', 'info', 'success']:
                            download_status["messages"].append({
                                'type': data.get('status'),
                                'message': data.get('message', '')
                            })

                            if data.get('video_title'):
                                download_status["current_title"] = data.get('video_title')

                            if data.get('link'):
                                status_type = 'error' if data.get('status') == 'error' else 'success'
                                download_status["links_status"][data.get('link')] = {
                                    'percent': 100 if status_type == 'success' else 0,
                                    'status': status_type,
                                    'message': data.get('message', ''),
                                    'title': data.get('video_title', '')
                                }
                    except json.JSONDecodeError:
                        # Caso não seja possível decodificar a linha como JSON, salva como mensagem informativa
                        download_status["messages"].append({
                            'type': 'info',
                            'message': line.strip()
                        })

                # Finaliza o status após o término do processo
                process.wait()
                download_status["completed"] = True
                download_status["running"] = False
            except Exception as e:
                # Em caso de erro, atualiza o status e salva a mensagem de erro
                download_status["messages"].append({
                    'type': 'error',
                    'message': str(e)
                })
                download_status["completed"] = True
                download_status["running"] = False

        # Inicia a thread de download
        thread = threading.Thread(target=run_download)
        thread.daemon = True
        thread.start()

        flash('Download iniciado com sucesso!')
        return redirect(url_for('status'))

    # Renderiza a página inicial
    return render_template('index.html', default_path=DEFAULT_PATH)

# Rota para exibir o status do download
@app.route('/status')
def status():
    return render_template('status.html', status=download_status)

# Rota para exibir os resultados do download
@app.route('/results')
def results():
    return render_template('results.html', status=download_status)

# Rota para retornar o status do download em formato JSON (API)
@app.route('/api/status')
def api_status():
    return jsonify(download_status)

# Inicializa o servidor Flask
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)

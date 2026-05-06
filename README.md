# Video Downloader

Aplicação Flask + yt-dlp para download de vídeos/playlists com interface
web em **dark theme** (Geist + Geist Mono), persistência em SQLite,
downloads paralelos, formatos múltiplos, cancelamento e histórico.

## 📋 Pré-requisitos

- Docker e Docker Compose

## 🚀 Como usar

### 1. Configurar `.env`

```bash
cp .env.example .env
# Gere uma SECRET_KEY forte:
echo "SECRET_KEY=$(openssl rand -hex 32)" >> .env
# Edite e ajuste HOST_DOWNLOAD_PATH, APP_UID/APP_GID conforme seu sistema
```

> **Permissões:** rode `id -u` e `id -g` no host e coloque os valores em
> `APP_UID`/`APP_GID`. Os arquivos baixados ficam com seu usuário em vez
> de `root`.

### 2. Subir

```bash
docker-compose up -d --build
```

Acesse [http://localhost:5000](http://localhost:5000).

### 3. Parar

```bash
docker-compose down              # mantém volume com DB
docker-compose down -v           # apaga TAMBÉM o histórico de jobs
```

### 4. Atualizar `yt-dlp`

O YouTube quebra extractors com frequência:

```bash
docker-compose build --no-cache && docker-compose up -d
```

## 🔧 Funcionalidades

- Download único, playlist ou lote via `.txt`
- Formatos: melhor disponível, 1080p / 720p / 480p, áudio MP3, áudio M4A
- **Downloads paralelos** (1 a `MAX_WORKERS` por job)
- **Cancelar** a qualquer momento (downloads em curso são abortados;
  pendentes na fila são descartados)
- **Histórico persistente** em SQLite — jobs continuam visíveis após
  restart do container
- **Cleanup automático** de jobs zumbis (interrompidos por restart)
- Tema light/dark

## 🧱 Arquitetura

```
┌─────────────────────────────────────────────────┐
│                Flask (1 worker, N threads)      │
│                                                  │
│   ┌──────────────┐         ┌──────────────────┐ │
│   │ jobs_lock +  │ writes  │  JobStore        │ │
│   │ jobs (memó.) │◄───────►│  (SQLite + WAL)  │ │
│   └──────┬───────┘         └──────────────────┘ │
│          │                                       │
│          │ submits                               │
│          ▼                                       │
│   ┌──────────────────────────────────────────┐  │
│   │ ThreadPoolExecutor (workers por job)     │  │
│   │   ├─ baixar_video(link1) ──► yt-dlp      │  │
│   │   ├─ baixar_video(link2) ──► yt-dlp      │  │
│   │   └─ ...                                 │  │
│   └──────────────────────────────────────────┘  │
└─────────────────────────────────────────────────┘
```

- **Memória é fonte de verdade** durante a execução (lock-protegida).
- **DB recebe escritas em transições importantes**: criação,
  mensagens, resultados, finalização. Eventos de progresso (alta
  frequência) ficam só em memória — não tocam o disco.
- Jobs antigos são lidos sob demanda do DB. Snapshot read-only.
- `cleanup_zombies` no startup: jobs com `running=1` (que ficaram para
  trás num restart) viram `cancelled=1` automaticamente.

## 🔐 Segurança

- Caminho de destino validado contra `ALLOWED_DOWNLOAD_ROOT`.
- Uploads passam por `secure_filename` + limite de tamanho.
- Container roda como usuário não-root.
- `SECRET_KEY` é obrigatório em produção (sem fallback fraco).
- Modo debug desligado por padrão.

## ⚙️ Variáveis de ambiente

| Variável                | Default              | Descrição                                       |
|-------------------------|----------------------|-------------------------------------------------|
| `SECRET_KEY`            | *(obrigatório)*      | Chave para Flask sessions/flash                 |
| `DEFAULT_DOWNLOAD_PATH` | `/mnt/nas/Downloads` | Pasta padrão no formulário                      |
| `ALLOWED_DOWNLOAD_ROOT` | `=DEFAULT`           | Raiz permitida (anti path-traversal)            |
| `DB_PATH`               | `/data/jobs.db`      | Caminho do SQLite (volume persistente)          |
| `MAX_LINKS`             | `500`                | Máximo de links por job                         |
| `MAX_UPLOAD_MB`         | `1`                  | Tamanho máximo do `.txt` enviado                |
| `MAX_JOBS_KEPT`         | `50`                 | Jobs mantidos no DB (LRU; demais são apagados)  |
| `MAX_WORKERS`           | `10`                 | Limite máximo de workers paralelos por job      |
| `DEFAULT_WORKERS`       | `1`                  | Valor inicial no formulário                     |
| `FLASK_DEBUG`           | `0`                  | `1` ativa debug (NÃO em produção)               |

> **Sobre paralelismo:** muitos workers para o **mesmo site** (YouTube)
> podem disparar rate-limit ou captcha. Um valor entre 2 e 4 costuma ser
> seguro; 1 = totalmente sequencial.

## 📂 Estrutura

```
.
├── app.py                    # Flask: rotas + ThreadPool + estado em memória
├── db.py                     # JobStore (SQLite com lock + WAL)
├── download_videos.py        # Wrapper yt-dlp (callback-based)
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
├── README.md
├── static/
│   ├── favicon.ico, metube.png
│   ├── theme.css
│   └── theme.js
└── templates/
    ├── base.html
    ├── index.html
    ├── status.html           # mostra N downloads paralelos ao vivo
    └── results.html
```

## 🧪 CLI standalone

O `download_videos.py` ainda funciona sem o Flask:

```bash
python download_videos.py links.txt /caminho/destino video best
python download_videos.py links.txt /caminho/destino playlist 720p
python download_videos.py links.txt /caminho/destino video audio_mp3
```

Eventos saem como JSON (uma linha por evento) no stdout.

## 🗃️ Manutenção do banco

O DB é leve (poucos KB para dezenas de jobs). Para inspeção manual:

```bash
docker-compose exec video-downloader sqlite3 /data/jobs.db
# .tables
# SELECT id, total_links, completed, cancelled FROM jobs ORDER BY created_at DESC LIMIT 10;
```

Backup:

```bash
docker run --rm -v vd-data:/data alpine tar czf - -C /data . > vd-backup.tar.gz
```

## 📌 Notas

- Verifique se o firewall não bloqueia a porta configurada.
- Logs por sessão são gravados em `download_log.txt` na pasta de download.
- Vídeos com restrição de idade/membros precisam de cookies — não
  suportado por agora.

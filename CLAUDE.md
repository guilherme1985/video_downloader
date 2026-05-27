# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Comandos

```bash
# Subir (build + start)
docker compose up -d --build

# Parar (mantém histórico)
docker compose down

# Logs em tempo real
docker compose logs -f

# Atualizar yt-dlp (quando extractors quebrarem)
docker compose build --no-cache && docker compose up -d

# Inspecionar banco de jobs
docker compose exec video-downloader sqlite3 /data/jobs.db
```

## Configuração inicial

```bash
cp .env.example .env
echo "SECRET_KEY=$(openssl rand -hex 32)" >> .env
# Editar .env: ajustar HOST_DOWNLOAD_PATH, APP_UID e APP_GID
```

## Arquitetura

Três arquivos principais:

- **`app.py`** — Rotas Flask, estado em memória dos jobs ativos (`jobs` dict) e `ThreadPoolExecutor`. O estado de cada job é dividido: campos transitórios (progresso em tempo real, workers ativos) ficam apenas em memória; campos duráveis (resultados, mensagens, flags) são gravados no SQLite imediatamente via `JobStore`. Roda com **1 worker gunicorn + 4 threads** — intencional, porque o estado em memória não pode ser compartilhado entre múltiplos processos.

- **`db.py`** — `JobStore`: conexão SQLite única com `threading.Lock` + WAL. No startup, faz cleanup de jobs zumbis (jobs que ficaram com `running=1` após crash são marcados como cancelados). Tabelas: `jobs`, `messages`, `results` (com cascade delete).

- **`download_videos.py`** — Wrapper do yt-dlp, baseado em callbacks (`on_event`). Funciona também como CLI standalone: `python download_videos.py links.txt /destino video best`.

## Formatos de vídeo válidos

`best`, `1080p`, `720p`, `480p`, `audio_mp3`, `audio_m4a` — validados contra o set `ALLOWED_FORMATS` em `app.py`.

## Endpoints da API

| Método | Rota | Descrição |
|--------|------|-----------|
| `GET` | `/api/status/<job_id>` | Estado completo do job |
| `POST` | `/api/cancel/<job_id>` | Cancela job em execução |
| `POST` | `/api/retry/<job_id>` | Cria novo job com os links que falharam |
| `DELETE` | `/api/jobs/<job_id>` | Remove job individual (recusa se running) |
| `DELETE` | `/api/jobs` | Remove todos os jobs finalizados |

## Variáveis de ambiente relevantes

| Variável | Default | Nota |
|---|---|---|
| `SECRET_KEY` | *(obrigatório)* | Gere com `openssl rand -hex 32` |
| `HOST_DOWNLOAD_PATH` | `/mnt/nas/Downloads` | Deve existir antes de subir o container |
| `APP_UID` / `APP_GID` | `1000` | Deve casar com o dono de `HOST_DOWNLOAD_PATH` |
| `MAX_JOBS_KEPT` | `50` | Pruning LRU do histórico |
| `MAX_WORKERS` | `10` | Limite de workers paralelos por job |

# Video Downloader

Aplicação Flask + yt-dlp para download de vídeos/playlists do YouTube e
sites compatíveis, com interface web, fila de jobs em memória, suporte a
múltiplos formatos e cancelamento.

## 📋 Pré-requisitos

- Docker e Docker Compose

## 🚀 Como usar

### 1. Configurar `.env`

```bash
cp .env.example .env
# Gere um SECRET_KEY forte:
echo "SECRET_KEY=$(openssl rand -hex 32)" >> .env
# Edite o .env e ajuste HOST_DOWNLOAD_PATH, APP_UID/APP_GID conforme seu sistema
```

> **Dica de permissões:** rode `id -u` e `id -g` no host para descobrir
> seu UID/GID e coloque-os em `APP_UID`/`APP_GID`. Os arquivos baixados
> ficarão com seu usuário em vez de `root`.

### 2. Subir a aplicação

```bash
docker-compose up -d --build
```

Acesse [http://localhost:5000](http://localhost:5000).

### 3. Parar

```bash
docker-compose down
```

### 4. Atualizar o `yt-dlp`

O YouTube quebra extractors com frequência. Quando começar a falhar,
faça rebuild para pegar a versão nova do `yt-dlp`:

```bash
docker-compose build --no-cache
docker-compose up -d
```

## 🔧 Funcionalidades

- **Download único, playlist ou lote via .txt**
- **Formatos disponíveis:** melhor disponível, 1080p / 720p / 480p,
  áudio MP3, áudio M4A
- **Múltiplos jobs** simultâneos sem conflito (cada um tem seu UUID)
- **Cancelar download** em andamento pelo botão na tela de status
- **Histórico** dos últimos N jobs (configurável) na tela inicial
- **Tema light/dark** com persistência

## 🔐 Segurança

- O caminho de destino é validado contra `ALLOWED_DOWNLOAD_ROOT` para
  bloquear path traversal.
- Uploads passam por `secure_filename` e têm limite de tamanho.
- O container roda como usuário não-root.
- `SECRET_KEY` é obrigatório (não há fallback em produção).
- Modo debug fica desligado por padrão.

## ⚙️ Variáveis de ambiente

| Variável                | Default              | Descrição                                    |
|-------------------------|----------------------|----------------------------------------------|
| `SECRET_KEY`            | *(obrigatório)*      | Chave para Flask sessions/flash              |
| `DEFAULT_DOWNLOAD_PATH` | `/mnt/nas/Downloads` | Pasta padrão no formulário                   |
| `ALLOWED_DOWNLOAD_ROOT` | `=DEFAULT`           | Raiz permitida (anti path-traversal)         |
| `MAX_LINKS`             | `500`                | Máximo de links por job                      |
| `MAX_UPLOAD_MB`         | `1`                  | Tamanho máximo do .txt enviado               |
| `MAX_JOBS_KEPT`         | `20`                 | Jobs mantidos em memória (LRU)               |
| `FLASK_DEBUG`           | `0`                  | `1` ativa debug (NÃO em produção)            |

## 📂 Estrutura

```
.
├── app.py                    # Flask: rotas + estado de jobs
├── download_videos.py        # Wrapper yt-dlp orientado a callbacks
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
├── README.md
├── static/
│   ├── favicon.ico
│   ├── metube.png
│   ├── theme.css             # Estilos compartilhados
│   └── theme.js              # Toggle light/dark
└── templates/
    ├── base.html             # Template base (extends pelos demais)
    ├── index.html
    ├── status.html           # Polling para /api/status/<id>
    └── results.html
```

## 🧪 Uso CLI (sem Flask)

O `download_videos.py` ainda funciona standalone:

```bash
python download_videos.py links.txt /caminho/destino video best
python download_videos.py links.txt /caminho/destino playlist 720p
python download_videos.py links.txt /caminho/destino video audio_mp3
```

Cada evento é emitido como JSON em uma linha do stdout — útil para
integração com outros scripts.

## 📌 Notas

- Verifique se o firewall não está bloqueando a porta configurada.
- Os logs por sessão ficam em `download_log.txt` dentro da pasta de download.
- Para vídeos restritos (idade, membros), `yt-dlp` precisa de cookies —
  uma issue conhecida e fora do escopo atual.

# Video Downloader

Aplicação Flask + yt-dlp para download de vídeos/playlists com interface web em
**dark theme** (Geist + Geist Mono), persistência em SQLite, downloads paralelos,
cancelamento e histórico de jobs.

## 📋 Pré-requisitos

- Docker e Docker Compose
- Uma pasta no host para receber os vídeos (home, NAS, disco externo — sua escolha)

## ⚡ Quickstart

```bash
git clone <repo> && cd video-downloader
cp .env.example .env
echo "SECRET_KEY=$(openssl rand -hex 32)" >> .env

# IMPORTANTE: edite .env e ajuste HOST_DOWNLOAD_PATH, APP_UID, APP_GID
# (veja a próxima seção — é onde mais se erra)

docker-compose up -d --build
```

Acesse [http://localhost:5000](http://localhost:5000).

Para parar:

```bash
docker-compose down              # mantém o histórico de jobs
docker-compose down -v           # apaga TAMBÉM o histórico
```

---

## 📁 Configuração da pasta de downloads

Cada usuário tem uma estrutura diferente, e errar aqui causa "permission denied" ou
arquivos baixados como `root`. Esta seção cobre os 3 cenários comuns no Linux.

### Como o volume funciona

A app sempre escreve em `/mnt/nas/Downloads` **dentro do container** — esse caminho
é fixo no código. O Docker mapeia esse caminho para uma pasta sua no host:

```
HOST                                  CONTAINER
/seu/caminho/aqui    <—volume—>   /mnt/nas/Downloads
        ↑                                 ↑
    você escolhe                    fixo (não mexa)
```

Você só decide o lado do host. A variável que controla isso é
`HOST_DOWNLOAD_PATH` no `.env`.

### Escolhendo seu caminho

Exemplos válidos no Linux:

| Cenário                       | `HOST_DOWNLOAD_PATH`              |
|-------------------------------|-----------------------------------|
| Pasta dedicada no home        | `/home/seu_usuario/Videos`        |
| NAS montado via NFS/SMB       | `/mnt/nas/Downloads`              |
| Disco externo / segundo HD    | `/mnt/storage/videos`             |
| Subpasta em volume já montado | `/srv/media/youtube`              |

A pasta **deve existir antes** de subir o container. Se não existir, o Docker cria
automaticamente como `root` e você fica sem permissão de escrita do lado do host.

### Criando a pasta com permissões corretas

```bash
# 1. Descubra seu UID e GID
id -u    # ex: 1000
id -g    # ex: 1000

# 2. Crie a pasta com seu usuário como dono
sudo mkdir -p /caminho/escolhido
sudo chown -R $USER:$USER /caminho/escolhido

# 3. Coloque os valores no .env
#    HOST_DOWNLOAD_PATH=/caminho/escolhido
#    APP_UID=1000
#    APP_GID=1000
```

**Por que `APP_UID`/`APP_GID`?** O container roda como user não-root. Se o UID do
user dentro do container não casar com o dono da pasta no host, ele não consegue
escrever. Casando os valores, os arquivos baixados aparecem como **seus** no host.

### Caso especial: NAS montado

Se sua pasta é um mount NFS/SMB, o UID/GID dela pode não ser o seu user normal.
Verifique:

```bash
stat -c "%u %g %n" /mnt/nas/Downloads
# Use os valores retornados em APP_UID e APP_GID
```

### Trocar a pasta depois

Basta editar `HOST_DOWNLOAD_PATH` no `.env` e:

```bash
docker-compose down              # mantém o histórico
docker-compose up -d             # sobe com a nova pasta
```

Jobs antigos continuam visíveis no histórico, mas referenciam o caminho anterior.

---

## 🔧 Funcionalidades

- Download único, playlist ou lote via `.txt`
- Formatos: melhor disponível, 1080p / 720p / 480p, áudio MP3, áudio M4A
- Downloads paralelos (1 a N workers por job, configurável no formulário)
- Cancelar a qualquer momento (em curso são abortados, pendentes descartados)
- Histórico persistente em SQLite — sobrevive a restart do container
- Cleanup automático de jobs zumbis interrompidos por restart
- CLI standalone (`download_videos.py`) para uso em scripts/cron

## ⚙️ Variáveis de ambiente

| Variável             | Default              | Descrição                                                  |
|----------------------|----------------------|------------------------------------------------------------|
| `SECRET_KEY`         | *(obrigatório)*      | Chave Flask para sessões. Gere com `openssl rand -hex 32` |
| `HOST_DOWNLOAD_PATH` | `/mnt/nas/Downloads` | Pasta no host (vira `/mnt/nas/Downloads` no container)    |
| `APP_UID`            | `1000`               | UID do user dentro do container                            |
| `APP_GID`            | `1000`               | GID do user dentro do container                            |
| `HOST_PORT`          | `5000`               | Porta exposta no host                                      |
| `MAX_LINKS`          | `500`                | Máximo de links por job                                    |
| `MAX_UPLOAD_MB`      | `1`                  | Tamanho máximo do `.txt` enviado                           |
| `MAX_JOBS_KEPT`      | `20`                 | Jobs mantidos no histórico (LRU)                           |
| `FLASK_DEBUG`        | `0`                  | `1` ativa debug — **NÃO em produção**                      |

## 🧰 Uso

### Interface web

1. Cole um link, vários links (um por linha) ou suba um `.txt`
2. Escolha tipo (vídeo / playlist), formato e número de workers paralelos
3. Confirme a pasta de destino
4. Acompanhe em `/status/<id>` — pode cancelar a qualquer momento

### CLI standalone

```bash
python download_videos.py links.txt /caminho/destino video best
python download_videos.py links.txt /caminho/destino playlist 720p
python download_videos.py links.txt /caminho/destino video audio_mp3
```

Eventos saem como JSON (uma linha por evento) no stdout.

## 🛠️ Manutenção

### Atualizar `yt-dlp`

Os extractors do YouTube quebram com frequência. Quando começar a falhar:

```bash
docker-compose build --no-cache && docker-compose up -d
```

### Inspecionar o histórico (SQLite)

```bash
docker-compose exec video-downloader sqlite3 /data/jobs.db
sqlite> .tables
sqlite> SELECT id, total_links, completed, cancelled
        FROM jobs ORDER BY created_at DESC LIMIT 10;
```

### Backup do histórico

```bash
docker run --rm -v vd-data:/data alpine tar czf - -C /data . > vd-backup.tar.gz
```

## 🚨 Troubleshooting

**"Permission denied" ao baixar**
Confira se a pasta no host pertence ao mesmo UID/GID do `.env`:
`stat -c "%u %g %n" $HOST_DOWNLOAD_PATH`

**`yt-dlp: extractor X is broken`**
Rode `docker-compose build --no-cache && docker-compose up -d`.

**Porta 5000 já em uso**
Mude `HOST_PORT` no `.env` para outra porta livre (ex: `8080`).

**Container vê a pasta vazia mesmo com vídeos lá**
Use caminho **absoluto** em `HOST_DOWNLOAD_PATH`. Caminhos relativos resolvem em
relação ao `docker-compose.yml`, não ao seu shell.

**`SECRET_KEY no .env`** ao subir
Adicione `SECRET_KEY=$(openssl rand -hex 32)` ao `.env`.

## 🔐 Segurança

- Caminho de destino validado contra `ALLOWED_DOWNLOAD_ROOT` (anti path-traversal)
- Uploads passam por `secure_filename` com limite de tamanho
- Container roda como usuário não-root
- `SECRET_KEY` obrigatório (sem fallback de produção)
- Modo debug desligado por padrão

## 📂 Estrutura do projeto

```
.
├── app.py                  # Flask: rotas + ThreadPool + estado em memória
├── db.py                   # JobStore (SQLite com lock + WAL)
├── download_videos.py      # Wrapper yt-dlp (callback-based, também CLI)
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
├── README.md
├── static/
│   ├── theme.css
│   ├── favicon.ico
│   └── metube.png
└── templates/
    ├── base.html
    ├── index.html
    ├── status.html
    └── results.html
```
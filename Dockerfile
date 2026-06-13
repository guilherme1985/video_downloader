FROM python:3.11-slim

WORKDIR /app

# Dependências de sistema (ffmpeg para merge/conversão)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Cria usuário não-root para rodar a aplicação
# UID/GID configuráveis em build-time para casar com o dono do volume no host
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g ${APP_GID} app \
    && useradd -m -u ${APP_UID} -g ${APP_GID} -s /bin/bash app

# Copia requirements primeiro para aproveitar cache do Docker
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copia o resto do projeto
COPY --chown=app:app . /app

# Diretórios para download (volume) e DB persistente
RUN mkdir -p /mnt/nas/Downloads /data \
    && chown -R app:app /mnt/nas/Downloads /data

USER app

EXPOSE 5000

# Healthcheck simples
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:5000/healthz -o /dev/null || exit 1

# Sobe via gunicorn (1 worker porque o estado de jobs é em memória; threads
# permitem múltiplas requests concorrentes). Debug é controlado via env.
# Para atualizar yt-dlp: docker-compose build --no-cache (extractors quebram
# com frequência; veja README).
CMD ["gunicorn", "--bind", "0.0.0.0:5000", \
     "--workers", "1", \
     "--threads", "4", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "app:app"]

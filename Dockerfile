FROM python:3.11-slim

WORKDIR /app

# Instala dependências do sistema necessárias para o yt-dlp
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Cria diretório de downloads
RUN mkdir -p /mnt/nas/Downloads

# Altera as permissoes
RUN chmod 777 /mnt/nas/Downloads

# Copia os arquivos do projeto
COPY . /app

# Instala as dependências Python
RUN pip install --no-cache-dir -r requirements.txt

# Expõe a porta 5000
EXPOSE 5000

# Comando para iniciar a aplicação
CMD ["python", "app.py"]

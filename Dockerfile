FROM python:3.9-slim

# Instalar dependencias del sistema
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libssl-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Establecer directorio de trabajo
WORKDIR /app

# Copiar y instalar dependencias
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el código fuente
COPY *.py ./

# Crear directorio para descargas
RUN mkdir -p /download
VOLUME /download

# Variables de entorno por defecto
ENV DOWNLOAD_DIR=/download
ENV API_ID=
ENV API_HASH=
ENV BOT_TOKEN=
ENV ENABLE_NOTIFICATIONS=true
ENV NOTIFICATION_CHAT_ID=
ENV SEGMENT_SIZE_MB=20
ENV CONCURRENT_SEGMENTS=3

# Comando para iniciar el daemon
CMD ["python", "-u", "telegram-download-daemon.py"]
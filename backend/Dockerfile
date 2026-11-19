FROM python:3.12-slim-bookworm
ARG LOCAL_WHISPER=0
ARG FACE_TRACKING=0
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fontconfig fonts-noto-core fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 10001 -m clipforge && mkdir -p /media-data && chown clipforge /media-data
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && if [ "$LOCAL_WHISPER" = 1 ]; then pip install 'faster-whisper>=1.1,<2'; fi \
    && if [ "$FACE_TRACKING" = 1 ]; then pip install 'opencv-python-headless>=4.10,<5'; fi
COPY app app
COPY alembic.ini ./
COPY migrations migrations
USER clipforge

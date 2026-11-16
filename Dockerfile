# syntax=docker/dockerfile:1

FROM python:3.11.9-alpine AS build

WORKDIR /app

# Build deps for packages without musl wheels (PyTgCrypto compiles a C extension)
RUN apk add --no-cache --virtual .build-deps gcc musl-dev

# rclone lives in the build stage so the runtime image can copy the static binary
RUN apk add --no-cache rclone

COPY requirements.txt /app/
RUN pip install --no-cache-dir \
    --trusted-host pypi.org \
    --trusted-host files.pythonhosted.org \
    --trusted-host pypi.python.org \
    -r requirements.txt \
 && find /usr/local/lib/python3.11/site-packages -name '__pycache__' -type d -prune -exec rm -rf {} +


FROM python:3.11.9-alpine AS runtime

# CI injects the tag from utils/__init__.py so `docker inspect` shows the version
# even when the image was pulled without a tag. Local builds keep "dev".
ARG VERSION=dev
ARG REVISION=unknown

LABEL org.opencontainers.image.title="tg-dl" \
      org.opencontainers.image.description="Telegram media downloader with bot, Web UI and a fixed rclone upload/delete pipeline" \
      org.opencontainers.image.source="https://github.com/QCEnjoyLL/tg-dl" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.revision="$REVISION"

# PYTHONUNBUFFERED keeps loguru output visible through `docker logs`.
# TZ decides the date used in media_datetime folder names.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# tzdata keeps media_datetime folder names correct inside alpine
RUN apk add --no-cache tzdata

# Copy installed deps from build stage
COPY --from=build /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

# rclone binary at the path the app expects by default (./rclone/rclone)
COPY --from=build /usr/bin/rclone /app/rclone/rclone

# Copy app source code
COPY . /app

RUN mkdir -p /app/downloads /app/temp /app/log /app/sessions

# The app saves the retry list (data.yaml) from a KeyboardInterrupt handler.
# Docker's default SIGTERM skips that block, so stop with SIGINT instead.
STOPSIGNAL SIGINT

CMD ["python", "media_downloader.py"]

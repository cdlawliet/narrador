# syntax=docker/dockerfile:1.7
FROM python:3.13.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -hide_banner -encoders 2>/dev/null | grep --quiet libmp3lame \
    && groupadd --gid 10001 narrador \
    && useradd --uid 10001 --gid narrador --create-home --no-log-init narrador

WORKDIR /app

COPY requirements.txt requirements-docker.txt ./
RUN python -m pip install --requirement requirements-docker.txt

COPY --chown=narrador:narrador app.py narrador.py ./
COPY --chown=narrador:narrador static ./static
COPY --chown=narrador:narrador templates ./templates

USER narrador

EXPOSE 8000

STOPSIGNAL SIGTERM

CMD ["gunicorn", "--bind=0.0.0.0:8000", "--workers=1", "--worker-class=gthread", "--threads=4", "--timeout=1800", "--graceful-timeout=60", "--keep-alive=5", "--worker-tmp-dir=/tmp", "--access-logfile=-", "--error-logfile=-", "--capture-output", "app:app"]

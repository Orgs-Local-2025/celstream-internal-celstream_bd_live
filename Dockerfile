# Celstream BD Live PoC: one image, two roles (portal / worker), selected by the compose command.
# Credentials are NEVER baked in: .env is excluded by .dockerignore and injected at runtime.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LIVE_DATABASE_PATH=/data/bd_live.db \
    LIVE_BACKUP_DIR=/backups \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

COPY requirements.lock ./
RUN pip install -r requirements.lock

# Application code and the business configuration copied from the reference project.
COPY bdlive ./bdlive
COPY config ./config
COPY prompts ./prompts
COPY .streamlit ./.streamlit
COPY app.py run_workflow.py ./

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin bdlive \
    && mkdir -p /data /backups \
    && chown -R bdlive:bdlive /data /backups
USER bdlive

EXPOSE 8501
CMD ["streamlit", "run", "app.py"]

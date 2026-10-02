# Build from the repo root:  docker build -f services/demo_ui/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp

WORKDIR /app

COPY services/demo_ui/requirements.txt services/demo_ui/requirements.txt
RUN pip install -r services/demo_ui/requirements.txt

COPY services/__init__.py services/__init__.py
COPY services/demo_ui/ services/demo_ui/
# The theme (ADR-0025): Streamlit reads .streamlit/config.toml from the working directory
COPY .streamlit/config.toml .streamlit/config.toml
# The dashboard reads these. They are baked in at build time (ADR-0023).
COPY evals/results/ evals/results/
COPY evals/judge/results/ evals/judge/results/

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001

EXPOSE 8501
# Headless, no usage statistics, no file watcher (the filesystem is read-only in the cluster).
CMD ["python", "-m", "streamlit", "run", "services/demo_ui/app.py", \
     "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true", \
     "--server.fileWatcherType=none", "--browser.gatherUsageStats=false"]

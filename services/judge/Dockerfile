# Build from the repo root:  docker build -f services/judge/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY shared/requirements.txt shared/requirements.txt
COPY services/judge/requirements.txt services/judge/requirements.txt
RUN pip install -r shared/requirements.txt -r services/judge/requirements.txt

COPY shared/ shared/
COPY services/__init__.py services/__init__.py
# The dispute store and the judge-jobs stream are shared with the supervisor
COPY services/supervisor/ services/supervisor/
COPY services/judge/ services/judge/

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001

EXPOSE 8000
CMD ["uvicorn", "services.judge.worker:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

# Build from the repo root so the image can include the `shared` package:
#   docker build -f services/ledger_agent/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first: this layer is cached until a requirements file changes.
COPY shared/requirements.txt shared/requirements.txt
COPY services/ledger_agent/requirements.txt services/ledger_agent/requirements.txt
RUN pip install -r shared/requirements.txt -r services/ledger_agent/requirements.txt

COPY shared/ shared/
COPY services/__init__.py services/__init__.py
COPY services/ledger_agent/ services/ledger_agent/

# Never run as root; the numeric UID lets Kubernetes verify runAsNonRoot.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001

EXPOSE 8000
CMD ["uvicorn", "services.ledger_agent.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

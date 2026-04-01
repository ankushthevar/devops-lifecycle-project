# ── Stage 1: Build ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt .
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt


# ── Stage 2: Production ───────────────────────────────────────────────────────
FROM python:3.12-slim AS production

LABEL org.opencontainers.image.source="https://github.com/ankushthevar/devops-lifecycle-project"
LABEL org.opencontainers.image.description="DevOps lifecycle showcase app"

COPY --from=builder /install /usr/local

# Create non-root user
RUN useradd --uid 10001 --no-create-home appuser

COPY --chown=appuser:appuser app/src /app/src

WORKDIR /app/src

USER 10001

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health/live')"

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]

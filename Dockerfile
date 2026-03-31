# ─────────────────────────────────────────
# Stage 1: Build / dependency resolution
# ─────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build tools only in this stage — never in the final image
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt .

# Install to a prefix so we can copy cleanly
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt


# ─────────────────────────────────────────
# Stage 2: Production image
# Distroless = no shell, no package manager, smaller attack surface
# ─────────────────────────────────────────
FROM gcr.io/distroless/python3-debian12:nonroot AS production

LABEL org.opencontainers.image.source="https://github.com/yourorg/devops-lifecycle-project"
LABEL org.opencontainers.image.description="Production-grade DevOps showcase app"
LABEL org.opencontainers.image.licenses="MIT"

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy only source code — no tests, no dev files
COPY --chown=nonroot:nonroot app/src /app/src

WORKDIR /app

# Distroless nonroot UID = 65532
USER nonroot

EXPOSE 8080

# Explicit array form avoids shell injection
ENTRYPOINT ["python", "-m", "uvicorn", "src.main:app"]
CMD ["--host", "0.0.0.0", "--port", "8080", "--workers", "4"]

# Health check used by both Docker and K8s liveness probe
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"]

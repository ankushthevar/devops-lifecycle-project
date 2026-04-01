"""
app/src/main.py

Production-grade FastAPI app — works standalone (no Vault/DB/Redis required).
Add those integrations on top as you scale.

Demonstrates:
  - Structured JSON logging
  - Prometheus metrics
  - Three-tier K8s health probes (/startup, /live, /ready)
  - Request-ID correlation
  - OpenTelemetry-ready (just wire in the exporter)
"""

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

import structlog
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import Counter, Gauge, Histogram
from prometheus_fastapi_instrumentator import Instrumentator

# ── Settings ──────────────────────────────────────────────────────────────────
APP_VERSION     = os.getenv("APP_VERSION", "1.0.0")
ENVIRONMENT     = os.getenv("ENVIRONMENT", "dev")
LOG_LEVEL       = os.getenv("LOG_LEVEL", "INFO")
LOG_FORMAT      = os.getenv("LOG_FORMAT", "json")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")
PORT            = int(os.getenv("PORT", "8080"))

# ── Structured Logging ────────────────────────────────────────────────────────
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer()
        if LOG_FORMAT == "json"
        else structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.getLevelName(LOG_LEVEL)
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)
log = structlog.get_logger()

# ── Prometheus Metrics ────────────────────────────────────────────────────────
APP_INFO = Gauge("app_info", "App metadata", ["version", "environment"])

HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "Request latency",
    ["method", "path", "status_code"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5],
)

ACTIVE_REQUESTS = Gauge("http_active_requests", "In-flight requests")
ITEMS_CREATED   = Counter("items_created_total", "Items created")

# ── In-memory store (swap with PostgreSQL in production) ──────────────────────
_items: dict[str, dict] = {}


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("app_starting", environment=ENVIRONMENT, version=APP_VERSION)
    APP_INFO.labels(version=APP_VERSION, environment=ENVIRONMENT).set(1)
    log.info("app_ready")
    yield
    log.info("app_stopped")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="DevOps Lifecycle Project",
    description="Production-grade FastAPI app showcasing the full DevOps lifecycle.",
    version=APP_VERSION,
    docs_url="/docs",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


# ── Middleware: Request ID + metrics ──────────────────────────────────────────
@app.middleware("http")
async def request_context_middleware(request: Request, call_next) -> Response:
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    start_time = time.perf_counter()

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
    )

    ACTIVE_REQUESTS.inc()
    try:
        response: Response = await call_next(request)
    except Exception:
        log.exception("unhandled_error")
        raise
    finally:
        ACTIVE_REQUESTS.dec()

    duration = time.perf_counter() - start_time
    HTTP_REQUEST_DURATION.labels(
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
    ).observe(duration)

    log.info("request_completed",
             status_code=response.status_code,
             duration_ms=round(duration * 1000, 2))

    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time"] = f"{duration:.4f}s"
    return response


# ── Prometheus ────────────────────────────────────────────────────────────────
Instrumentator(
    should_group_status_codes=False,
    excluded_handlers=["/health.*", "/metrics"],
).instrument(app).expose(app, endpoint="/metrics")


# ── Health Probes (K8s: startup → live → ready) ───────────────────────────────
@app.get("/health/startup", include_in_schema=False)
async def startup_probe():
    return {"status": "started"}


@app.get("/health/live", include_in_schema=False)
async def liveness_probe():
    return {"status": "alive"}


@app.get("/health/ready", include_in_schema=False)
async def readiness_probe():
    return {
        "status": "ready",
        "version": APP_VERSION,
        "environment": ENVIRONMENT,
        "checks": {"app": "ok"},
    }


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {
        "app": "DevOps Lifecycle Project",
        "version": APP_VERSION,
        "environment": ENVIRONMENT,
        "docs": "/docs",
        "metrics": "/metrics",
        "health": "/health/ready",
        "github": "https://github.com/ankushthevar/devops-lifecycle-project",
    }


@app.get("/api/v1/items")
async def list_items():
    return {"items": list(_items.values()), "count": len(_items)}


@app.post("/api/v1/items", status_code=201)
async def create_item(item: dict):
    item_id = str(uuid.uuid4())
    _items[item_id] = {"id": item_id, **item}
    ITEMS_CREATED.inc()
    log.info("item_created", item_id=item_id)
    return _items[item_id]


@app.get("/api/v1/items/{item_id}")
async def get_item(item_id: str, response: Response):
    if item_id not in _items:
        response.status_code = 404
        return {"error": "Item not found", "item_id": item_id}
    return _items[item_id]


@app.delete("/api/v1/items/{item_id}")
async def delete_item(item_id: str, response: Response):
    if item_id not in _items:
        response.status_code = 404
        return {"error": "Item not found"}
    del _items[item_id]
    log.info("item_deleted", item_id=item_id)
    return {"deleted": item_id}


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=PORT,
        reload=ENVIRONMENT == "dev",
        log_config=None,
        access_log=False,
    )

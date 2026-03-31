"""
app/src/main.py

Production-grade FastAPI app wired for the full DevOps lifecycle:
  - Structured JSON logging (correlates with Loki)
  - OpenTelemetry tracing (exports to Tempo via OTLP)
  - Prometheus metrics endpoint (scraped by Prometheus)
  - Three-tier health probes: /health/startup, /health/live, /health/ready
  - Graceful shutdown
  - Request-ID correlation across logs, traces, and responses
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
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Gauge, Histogram, generate_latest
from prometheus_fastapi_instrumentator import Instrumentator

from src.api.routes import router
from src.core.config import settings
from src.core.database import engine

# ── Structured Logging Setup ──────────────────────────────────────────────────
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        # JSON output in prod, pretty-printed in dev
        structlog.processors.JSONRenderer()
        if settings.LOG_FORMAT == "json"
        else structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.getLevelName(settings.LOG_LEVEL)
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

log = structlog.get_logger()

# ── OpenTelemetry Setup ───────────────────────────────────────────────────────
def configure_tracing() -> None:
    resource = Resource.create(
        {
            "service.name": settings.OTEL_SERVICE_NAME,
            "service.version": settings.APP_VERSION,
            "deployment.environment": settings.ENVIRONMENT,
        }
    )
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    SQLAlchemyInstrumentor().instrument(engine=engine)
    log.info("tracing_configured", endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT)


# ── Prometheus Custom Metrics ─────────────────────────────────────────────────
APP_INFO = Gauge(
    "app_info",
    "Application metadata",
    ["version", "environment"],
)

HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "path", "status_code"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5],
)

DEPENDENCY_UP = Gauge(
    "dependency_up",
    "Whether a downstream dependency is reachable (1=up, 0=down)",
    ["dependency"],
)

ACTIVE_REQUESTS = Gauge(
    "http_active_requests",
    "Number of in-flight HTTP requests",
)


# ── App Lifespan ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle hooks."""
    log.info("app_starting", environment=settings.ENVIRONMENT, version=settings.APP_VERSION)

    configure_tracing()
    APP_INFO.labels(version=settings.APP_VERSION, environment=settings.ENVIRONMENT).set(1)

    # Warm up connection pool
    from src.core.database import check_db_connection
    await check_db_connection()
    DEPENDENCY_UP.labels(dependency="postgres").set(1)

    log.info("app_ready")
    yield

    # Graceful shutdown
    log.info("app_shutting_down")
    from src.core.database import close_db_connections
    await close_db_connections()
    log.info("app_stopped")


# ── FastAPI Application ───────────────────────────────────────────────────────
app = FastAPI(
    title="DevOps Lifecycle App",
    version=settings.APP_VERSION,
    docs_url="/docs" if settings.ENVIRONMENT != "prod" else None,  # no Swagger in prod
    redoc_url=None,
    lifespan=lifespan,
)

# ── Middleware ────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next) -> Response:
    """
    Attach a unique request ID to every request.
    Propagates through logs and response headers so you can
    correlate a user complaint all the way to a Loki trace.
    """
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    start_time = time.perf_counter()

    # Bind to structlog context — every log in this request gets request_id
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
    )

    # Also inject into OTEL span
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute("http.request_id", request_id)

    ACTIVE_REQUESTS.inc()
    try:
        response: Response = await call_next(request)
    except Exception:
        log.exception("unhandled_request_error")
        raise
    finally:
        ACTIVE_REQUESTS.dec()

    duration = time.perf_counter() - start_time
    HTTP_REQUEST_DURATION.labels(
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
    ).observe(duration)

    log.info(
        "request_completed",
        status_code=response.status_code,
        duration_ms=round(duration * 1000, 2),
    )

    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time"] = f"{duration:.4f}s"
    return response


# ── Prometheus Instrumentation ────────────────────────────────────────────────
Instrumentator(
    should_group_status_codes=False,
    excluded_handlers=["/health.*", "/metrics"],
).instrument(app).expose(app, endpoint="/metrics")

FastAPIInstrumentor.instrument_app(app)

# ── Routes ────────────────────────────────────────────────────────────────────
app.include_router(router, prefix="/api/v1")


# ── Health Probes ─────────────────────────────────────────────────────────────
@app.get("/health/startup", tags=["health"], include_in_schema=False)
async def startup_probe():
    """
    K8s startup probe. Fails until the app is done initialising.
    Checked every 10s, up to 30 failures → container is killed and restarted.
    """
    return {"status": "started"}


@app.get("/health/live", tags=["health"], include_in_schema=False)
async def liveness_probe():
    """
    K8s liveness probe. A failure here causes K8s to restart the pod.
    Only fail if the process is truly stuck/deadlocked — not on transient errors.
    """
    return {"status": "alive"}


@app.get("/health/ready", tags=["health"], include_in_schema=False)
async def readiness_probe(response: Response):
    """
    K8s readiness probe. A failure removes the pod from the Service load balancer.
    Check all downstream dependencies; fail fast if any are unreachable.
    """
    from src.core.database import check_db_connection
    from src.core.cache import check_redis_connection

    checks: dict[str, str] = {}
    healthy = True

    try:
        await check_db_connection()
        checks["postgres"] = "ok"
        DEPENDENCY_UP.labels(dependency="postgres").set(1)
    except Exception as exc:
        log.warning("readiness_db_failed", error=str(exc))
        checks["postgres"] = "unreachable"
        DEPENDENCY_UP.labels(dependency="postgres").set(0)
        healthy = False

    try:
        await check_redis_connection()
        checks["redis"] = "ok"
        DEPENDENCY_UP.labels(dependency="redis").set(1)
    except Exception as exc:
        log.warning("readiness_redis_failed", error=str(exc))
        checks["redis"] = "degraded"  # Redis down = degraded, not dead
        DEPENDENCY_UP.labels(dependency="redis").set(0)

    if not healthy:
        response.status_code = 503

    return {
        "status": "ready" if healthy else "not_ready",
        "checks": checks,
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
    }


if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=8080,
        reload=settings.ENVIRONMENT == "dev",
        log_config=None,    # disable uvicorn's default logger; structlog handles it
        access_log=False,   # handled by our middleware
    )

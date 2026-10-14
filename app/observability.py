import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import Response
from opentelemetry import trace
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from app.config import Settings

STAGE_SECONDS = Histogram("rag_stage_seconds", "Latency per pipeline stage", ["stage"])
REQUESTS = Counter("rag_requests_total", "Requests", ["route", "tenant"])
CHUNKS_RETRIEVED = Histogram("rag_chunks_retrieved", "Chunks returned per query",
                             buckets=(0, 1, 2, 3, 5, 8, 13, 21))
LLM_TOKENS = Counter("rag_llm_tokens_total", "LLM tokens", ["tenant", "kind"])
RATE_LIMITED = Counter("rag_rate_limited_total", "429 responses", ["tenant"])

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
tracer = trace.get_tracer("rag")
log = structlog.get_logger()


@contextmanager
def stage(name: str, tenant_id: str | None = None):
    """Times a pipeline stage as both a Prometheus histogram sample and an OTel span."""
    start = time.perf_counter()
    with tracer.start_as_current_span(name) as span:
        if tenant_id:
            span.set_attribute("tenant.id", tenant_id)
        try:
            yield span
        finally:
            STAGE_SECONDS.labels(name).observe(time.perf_counter() - start)


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


def setup_tracing(app: FastAPI, s: Settings) -> None:
    if not s.otel_endpoint:
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "permission-aware-rag"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{s.otel_endpoint}/v1/traces")))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)


def install(app: FastAPI, s: Settings) -> None:
    setup_logging()
    setup_tracing(app, s)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request_id_var.set(rid)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=rid, path=request.url.path)
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        log.info("request", status=response.status_code, ms=round((time.perf_counter() - start) * 1000, 1))
        return response

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

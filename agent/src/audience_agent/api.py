from __future__ import annotations

import asyncio
import hmac
import logging
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi import Path as ApiPath
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from audience_agent.config import Settings
from audience_agent.contracts import (
    AnalyzeRequest,
    AnalyzeResponse,
    EvidenceCard,
    EvidenceRecord,
    FeedbackRequest,
)
from audience_agent.graph import build_graph
from audience_agent.llm import DeterministicMockModel, InsightModel, OpenAICompatibleModel
from audience_agent.middleware import RequestBodyLimitMiddleware
from audience_agent.privacy import contains_direct_identifier
from audience_agent.repository import (
    DatasetVersionConflict,
    EvidenceRepository,
    FeedbackAssociationError,
    InMemoryEvidenceRepository,
    PostgresEvidenceRepository,
)
from audience_agent.seeding import load_seed

logger = logging.getLogger("audience_agent")


@dataclass
class Runtime:
    repository: EvidenceRepository
    graph: Any
    pool: asyncpg.Pool | None = None
    checkpointer_pool: AsyncConnectionPool | None = None
    model_ready: bool = True
    checkpoint_health: Callable[[], Awaitable[bool]] | None = None

    async def checkpoint_ready(self) -> bool:
        if self.checkpoint_health is None:
            # MemorySaver needs no external service; Postgres runtime always supplies a real probe.
            return self.checkpointer_pool is None
        return await self.checkpoint_health()

    async def close(self) -> None:
        if self.checkpointer_pool is not None:
            await self.checkpointer_pool.close()
        if self.pool is not None:
            await self.pool.close()


def _resolve_path(path: Path) -> Path:
    if path.is_absolute():
        return path
    project_root = Path(__file__).resolve().parents[2]
    return project_root / path


def _build_model(settings: Settings) -> InsightModel:
    if settings.llm_mode == "mock":
        return DeterministicMockModel()
    return OpenAICompatibleModel(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        base_url=settings.openai_base_url,
        fallback_models=settings.openai_model_fallbacks,
        max_output_tokens=settings.max_output_tokens,
        max_evidence_chars=settings.max_evidence_chars,
        max_concurrent=settings.max_concurrent_llm,
        timeout_seconds=settings.llm_timeout_seconds,
        max_attempts=settings.llm_max_attempts,
        max_fallbacks=settings.llm_max_fallbacks,
    )


async def build_runtime(settings: Settings) -> Runtime:
    model = _build_model(settings)
    if settings.storage_backend == "memory":
        if settings.app_env == "production":
            raise RuntimeError("in-memory storage is forbidden in production")
        repository: EvidenceRepository = InMemoryEvidenceRepository(
            load_seed(_resolve_path(settings.seed_path))
        )
        checkpointer = MemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=None))
        return Runtime(
            repository,
            build_graph(repository, model, settings).compile(checkpointer=checkpointer),
            model_ready=True,
        )

    pool = await asyncpg.create_pool(
        settings.database_url, min_size=2, max_size=15, command_timeout=20
    )
    repository = PostgresEvidenceRepository(pool)
    checkpoint_pool = AsyncConnectionPool(
        conninfo=settings.langgraph_database_url,
        min_size=2,
        max_size=12,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )

    async def checkpoint_health() -> bool:
        async with checkpoint_pool.connection() as conn:
            await conn.execute("SELECT 1")
        return True

    try:
        await checkpoint_pool.open(wait=True)
        checkpointer = AsyncPostgresSaver(
            checkpoint_pool,
            serde=JsonPlusSerializer(allowed_msgpack_modules=None),
        )
        await checkpointer.setup()
        graph = build_graph(repository, model, settings).compile(checkpointer=checkpointer)
        return Runtime(
            repository,
            graph,
            pool,
            checkpoint_pool,
            model_ready=True,
            checkpoint_health=checkpoint_health,
        )
    except Exception:
        await checkpoint_pool.close()
        await pool.close()
        raise


def create_app(
    settings: Settings | None = None,
    *,
    runtime_override: Runtime | None = None,
) -> FastAPI:
    configured = settings or Settings.from_env()
    api_key_grants = configured.api_key_map()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime = runtime_override or await build_runtime(configured)
        app.state.runtime = runtime
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            if runtime_override is None:
                await runtime.close()

    app = FastAPI(
        title="Audience Insight Agent",
        version="0.2.0",
        description="Evidence-first, workspace-scoped audience insight API.",
        lifespan=lifespan,
        docs_url="/docs" if configured.app_env != "production" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if configured.app_env != "production" else None,
    )
    app.state.configured = configured

    @app.exception_handler(RequestValidationError)
    async def sanitized_validation_error(request: Request, exc: RequestValidationError):
        # Never echo offending values, which may themselves contain direct identifiers.
        errors = [
            {
                "location": list(item.get("loc", ())),
                "message": item.get("msg", "invalid input"),
                "type": item.get("type", ""),
            }
            for item in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.exception_handler(HTTPException)
    async def consistent_http_error(request: Request, exc: HTTPException):
        if exc.status_code != 422:
            return await http_exception_handler(request, exc)
        message = exc.detail if isinstance(exc.detail, str) else "request validation failed"
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {"location": ["request"], "message": message, "type": "value_error"}
                ]
            },
        )

    def require_workspace(required_role: str):
        async def workspace_auth(
            workspace_id: str = ApiPath(min_length=1, max_length=100),
            x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        ) -> str:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", workspace_id):
                raise HTTPException(status_code=422, detail="workspace_id has invalid characters")
            if contains_direct_identifier(workspace_id):
                raise HTTPException(
                    status_code=422, detail="workspace_id contains a direct personal identifier"
                )
            if not x_api_key:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="API key required"
                )
            authorized_grant = None
            for candidate, grant in api_key_grants.items():
                if hmac.compare_digest(candidate, x_api_key):
                    authorized_grant = grant
                    break
            if authorized_grant is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key"
                )
            if not hmac.compare_digest(authorized_grant.workspace_id, workspace_id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail="Workspace access denied"
                )
            if required_role not in authorized_grant.roles:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail="API key lacks required role"
                )
            return authorized_grant.workspace_id

        return workspace_auth

    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=2_000_000)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/readyz", tags=["ops"])
    async def readyz(request: Request) -> dict[str, str]:
        if not getattr(request.app.state, "ready", False):
            raise HTTPException(status_code=503, detail="runtime is not ready")
        runtime: Runtime = request.app.state.runtime
        try:
            storage_ready = await runtime.repository.health()
            checkpoint_ready = await runtime.checkpoint_ready()
        except Exception as exc:
            logger.warning("readiness_check_failed", extra={"reason": type(exc).__name__})
            raise HTTPException(status_code=503, detail="runtime dependencies are not ready") from exc
        if not storage_ready or not checkpoint_ready or not runtime.model_ready:
            raise HTTPException(status_code=503, detail="runtime dependencies are not ready")
        return {"status": "ready"}

    @app.post(
        "/v1/workspaces/{workspace_id}/insights",
        response_model=AnalyzeResponse,
        tags=["agent"],
        responses={504: {"description": "The total insight deadline was exceeded"}},
    )
    async def analyze(
        body: AnalyzeRequest,
        request: Request,
        workspace_id: str = ApiPath(min_length=1, max_length=100),
        authorized_workspace: str = Depends(require_workspace("insight:read")),
    ) -> AnalyzeResponse:
        if len(body.question) > configured.max_question_chars:
            raise HTTPException(
                status_code=413, detail="question exceeds configured character limit"
            )
        run_id = str(uuid4())
        public_thread_id = body.thread_id or uuid4().hex
        internal_thread_id = f"{authorized_workspace}:{public_thread_id}"
        runtime: Runtime = request.app.state.runtime
        started = time.monotonic()
        # asyncio.timeout_at uses loop.time(); uvloop and time.monotonic() are not interchangeable.
        deadline = asyncio.get_running_loop().time() + configured.request_timeout_seconds
        try:
            async with asyncio.timeout_at(deadline):
                result = await runtime.graph.ainvoke(
                    {
                        "workspace_id": authorized_workspace,
                        "question": body.question,
                        "run_id": run_id,
                        "deadline": deadline,
                    },
                    config={"configurable": {"thread_id": internal_thread_id}},
                )
                answer = result.get("answer", {})
                claims = answer.get("claims", [])
                cited_ids = list(
                    dict.fromkeys(
                        source_id for claim in claims for source_id in claim.get("evidence_ids", [])
                    )
                )
                dataset_version = result.get("dataset_version")
                evidence_records = (
                    await runtime.repository.get_many(
                        authorized_workspace, dataset_version, cited_ids
                    )
                    if dataset_version
                    else []
                )
                await runtime.repository.record_run(
                    authorized_workspace, internal_thread_id, run_id, dataset_version
                )
        except TimeoutError as exc:
            logger.warning(
                "insight_deadline_exceeded",
                extra={"run_id": run_id, "workspace_id": authorized_workspace},
            )
            raise HTTPException(
                status_code=504, detail="insight deadline exceeded; no unverified answer returned"
            ) from exc
        except Exception as exc:
            logger.error(
                "insight_run_failed",
                extra={
                    "run_id": run_id,
                    "workspace_id": authorized_workspace,
                    "error_type": type(exc).__name__,
                },
            )
            raise HTTPException(
                status_code=502, detail="insight generation failed; no unverified answer returned"
            ) from exc

        cards = [
            EvidenceCard(
                id=item.id,
                source_material=item.source_material,
                source_file=item.source_file,
                source_sheet=item.source_sheet,
                source_field=item.source_field,
                source_row=item.source_row,
                content=item.content,
                confidence=item.confidence,
                dataset_version=item.dataset_version,
            )
            for item in evidence_records
        ]
        elapsed_ms = int((time.monotonic() - started) * 1000)
        usage = result.get("token_usage", {})
        logger.info(
            "insight_run_completed",
            extra={
                "run_id": run_id,
                "workspace_id": authorized_workspace,
                "thread_id": public_thread_id,
                "intent": result.get("intent"),
                "status": result.get("status", "abstained"),
                "citation_count": len(cited_ids),
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "model": usage.get("model", "unknown"),
                "elapsed_ms": elapsed_ms,
            },
        )
        return AnalyzeResponse(
            run_id=run_id,
            thread_id=public_thread_id,
            status=result.get("status", "abstained"),
            dataset_version=dataset_version,
            intent=result.get("intent"),
            claims=claims,
            evidence=cards,
            limitations=answer.get("limitations", []),
            next_steps=answer.get("next_steps", []),
            token_usage=usage,
        )

    @app.post("/v1/workspaces/{workspace_id}/feedback", status_code=202, tags=["feedback"])
    async def feedback(
        body: FeedbackRequest,
        request: Request,
        workspace_id: str = ApiPath(min_length=1, max_length=100),
        authorized_workspace: str = Depends(require_workspace("feedback:write")),
    ) -> dict[str, str]:
        runtime: Runtime = request.app.state.runtime
        internal_thread_id = f"{authorized_workspace}:{body.thread_id}"
        try:
            await runtime.repository.save_feedback(
                authorized_workspace,
                internal_thread_id,
                str(body.run_id),
                body.decision,
                body.notes,
                body.dataset_version,
            )
        except FeedbackAssociationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        logger.info(
            "feedback_saved",
            extra={
                "workspace_id": authorized_workspace,
                "run_id": str(body.run_id),
                "decision": body.decision,
            },
        )
        return {"status": "accepted", "run_id": str(body.run_id)}

    @app.post(
        "/v1/workspaces/{workspace_id}/evidence",
        status_code=202,
        tags=["ingestion"],
        responses={409: {"description": "Active or retired dataset versions are immutable"}},
    )
    async def ingest_evidence(
        body: list[EvidenceRecord],
        request: Request,
        workspace_id: str = ApiPath(min_length=1, max_length=100),
        authorized_workspace: str = Depends(require_workspace("evidence:write")),
    ) -> dict[str, int | str]:
        if not body or len(body) > 200:
            raise HTTPException(
                status_code=413, detail="batch must contain 1..200 evidence records"
            )
        if any(item.workspace_id != authorized_workspace for item in body):
            raise HTTPException(status_code=403, detail="evidence workspace does not match API key")
        runtime: Runtime = request.app.state.runtime
        versions = {item.dataset_version for item in body}
        if len(versions) != 1:
            raise HTTPException(
                status_code=422, detail="one request must contain one dataset_version"
            )
        try:
            inserted = await runtime.repository.ingest(body)
        except DatasetVersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            logger.error(
                "evidence_ingestion_failed",
                extra={"workspace_id": authorized_workspace, "error_type": type(exc).__name__},
            )
            raise HTTPException(status_code=500, detail="evidence ingestion failed") from exc
        return {
            "status": "staged",
            "records_upserted": inserted,
            "dataset_version": next(iter(versions)),
        }

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        validation = schema.get("components", {}).get("schemas", {}).get("ValidationError")
        if validation is not None:
            validation["properties"] = {
                "location": {
                    "type": "array",
                    "items": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
                    "title": "Location",
                },
                "message": {"type": "string", "title": "Message"},
                "type": {"type": "string", "title": "Error Type"},
            }
            validation["required"] = ["location", "message", "type"]
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]

    delivery_root = Path(__file__).resolve().parents[3]
    evidence_dir = delivery_root / "evidence"
    meeting_snapshot = evidence_dir / "meeting_snapshot.json"
    if meeting_snapshot.is_file():

        @app.get("/evidence/insights.json", include_in_schema=False)
        async def static_meeting_snapshot() -> FileResponse:
            # The lightweight meeting view must never receive the detailed-analysis schema.
            return FileResponse(meeting_snapshot, media_type="application/json")

    if evidence_dir.is_dir():
        app.mount("/evidence", StaticFiles(directory=evidence_dir, check_dir=True), name="evidence")

    prototype_dir = delivery_root / "prototype"
    if prototype_dir.is_dir():
        # Mounted after API routes: /docs, /openapi.json, health and /v1 remain authoritative.
        app.mount(
            "/prototype",
            StaticFiles(directory=prototype_dir, html=True, check_dir=True),
            name="prototype",
        )
    return app


app = create_app()

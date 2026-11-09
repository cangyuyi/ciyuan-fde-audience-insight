from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from audience_agent.privacy import contains_direct_identifier


@dataclass(frozen=True)
class ApiKeyGrant:
    workspace_id: str
    roles: frozenset[str]


@dataclass(frozen=True)
class Settings:
    app_env: str
    storage_backend: str
    llm_mode: str
    database_url: str
    langgraph_database_url: str
    api_keys_json: str
    openai_api_key: str
    openai_base_url: str | None
    openai_model: str
    openai_model_fallbacks: tuple[str, ...]
    max_question_chars: int
    max_evidence_items: int
    max_evidence_chars: int
    max_output_tokens: int
    max_concurrent_llm: int
    request_timeout_seconds: float
    llm_timeout_seconds: float
    llm_max_attempts: int
    llm_max_fallbacks: int
    seed_path: Path
    migrations_path: Path

    @classmethod
    def from_env(cls, *, load_file: bool = True, validate: bool = True) -> Settings:
        if load_file:
            load_dotenv(override=False)
        app_env = os.getenv("APP_ENV", "development").strip().lower()
        llm_mode = os.getenv("LLM_MODE", "mock" if app_env == "development" else "openai").lower()
        storage = os.getenv(
            "STORAGE_BACKEND", "memory" if app_env == "development" else "postgres"
        ).lower()
        db_url = os.getenv(
            "DATABASE_URL", "postgresql://audience:change-me@localhost:5432/audience"
        )
        checkpoint_url = os.getenv("LANGGRAPH_DATABASE_URL", db_url)
        app_root = Path(os.getenv("APP_ROOT", Path(__file__).resolve().parents[2])).resolve()

        def resolve_path(env_name: str, default: str) -> Path:
            value = Path(os.getenv(env_name, default))
            return value if value.is_absolute() else app_root / value

        settings = cls(
            app_env=app_env,
            storage_backend=storage,
            llm_mode=llm_mode,
            database_url=db_url,
            langgraph_database_url=checkpoint_url,
            api_keys_json=os.getenv("API_KEYS_JSON", "{}"),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_base_url=os.getenv("OPENAI_BASE_URL") or None,
            openai_model=os.getenv("OPENAI_MODEL", ""),
            openai_model_fallbacks=tuple(
                x.strip() for x in os.getenv("OPENAI_MODEL_FALLBACKS", "").split(",") if x.strip()
            ),
            max_question_chars=int(os.getenv("MAX_QUESTION_CHARS", "1200")),
            max_evidence_items=int(os.getenv("MAX_EVIDENCE_ITEMS", "8")),
            max_evidence_chars=int(os.getenv("MAX_EVIDENCE_CHARS", "12000")),
            max_output_tokens=int(os.getenv("MAX_OUTPUT_TOKENS", "1200")),
            max_concurrent_llm=int(os.getenv("MAX_CONCURRENT_LLM", "8")),
            request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "30")),
            llm_timeout_seconds=float(os.getenv("LLM_ATTEMPT_TIMEOUT_SECONDS", "20")),
            llm_max_attempts=int(os.getenv("LLM_MAX_ATTEMPTS", "2")),
            llm_max_fallbacks=int(os.getenv("LLM_MAX_FALLBACKS", "1")),
            seed_path=resolve_path("SEED_EVIDENCE_PATH", "data/evidence.raw.json"),
            migrations_path=resolve_path("MIGRATIONS_PATH", "migrations"),
        )
        if validate:
            settings.validate()
        return settings

    def validate(self) -> None:
        if self.app_env not in {"development", "test", "staging", "production"}:
            raise ValueError("APP_ENV must be development, test, staging or production")
        if self.storage_backend not in {"memory", "postgres"}:
            raise ValueError("STORAGE_BACKEND must be memory or postgres")
        if self.llm_mode not in {"mock", "openai"}:
            raise ValueError("LLM_MODE must be mock or openai")
        if self.llm_mode == "openai" and (not self.openai_api_key or not self.openai_model):
            raise ValueError("LLM_MODE=openai requires OPENAI_API_KEY and OPENAI_MODEL")
        if not 1 <= self.max_question_chars <= 10_000:
            raise ValueError("MAX_QUESTION_CHARS must be within 1..10000")
        if not 1 <= self.max_evidence_items <= 20:
            raise ValueError("MAX_EVIDENCE_ITEMS must be within 1..20")
        if not 1_000 <= self.max_evidence_chars <= 50_000:
            raise ValueError("MAX_EVIDENCE_CHARS must be within 1000..50000")
        if not 1 <= self.max_output_tokens <= 8_000:
            raise ValueError("MAX_OUTPUT_TOKENS must be within 1..8000")
        if not 1 <= self.max_concurrent_llm <= 256:
            raise ValueError("MAX_CONCURRENT_LLM must be within 1..256")
        if not 0.05 <= self.request_timeout_seconds <= 300:
            raise ValueError("REQUEST_TIMEOUT_SECONDS must be within 0.05..300")
        if not 0.05 <= self.llm_timeout_seconds <= self.request_timeout_seconds:
            raise ValueError("LLM_ATTEMPT_TIMEOUT_SECONDS must be within 0.05..request timeout")
        if not 1 <= self.llm_max_attempts <= 10:
            raise ValueError("LLM_MAX_ATTEMPTS must be within 1..10")
        if not 0 <= self.llm_max_fallbacks < self.llm_max_attempts:
            raise ValueError("LLM_MAX_FALLBACKS must be non-negative and below LLM_MAX_ATTEMPTS")
        if self.app_env == "production":
            if not os.getenv("DATABASE_URL"):
                raise ValueError("Production requires an explicit DATABASE_URL")
            if self.storage_backend != "postgres":
                raise ValueError("Production requires STORAGE_BACKEND=postgres")
            if self.llm_mode != "openai":
                raise ValueError("Production requires LLM_MODE=openai")
            if not self.openai_api_key or not self.openai_model:
                raise ValueError("Production requires OPENAI_API_KEY and OPENAI_MODEL")
            self.api_key_map()  # fail closed before starting the web server

    def api_key_map(self) -> dict[str, ApiKeyGrant]:
        try:
            parsed = json.loads(self.api_keys_json)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "API_KEYS_JSON must be a JSON object mapping tokens to grants"
            ) from exc
        if not isinstance(parsed, dict):
            raise ValueError("API_KEYS_JSON must be an object")
        grants: dict[str, ApiKeyGrant] = {}
        allowed_roles = {"insight:read", "feedback:write", "evidence:write"}
        for token, raw_grant in parsed.items():
            if not isinstance(token, str) or len(token) < 8:
                raise ValueError("API key tokens must be strings of at least 8 characters")
            # Backward-compatible shorthand is development-only and grants read access only.
            if isinstance(raw_grant, str) and self.app_env in {"development", "test"}:
                workspace_id, roles = raw_grant, {"insight:read"}
            elif isinstance(raw_grant, dict):
                workspace_id = raw_grant.get("workspace")
                raw_roles = raw_grant.get("roles", [])
                if not isinstance(raw_roles, list) or any(
                    not isinstance(role, str) for role in raw_roles
                ):
                    raise ValueError("API key roles must be a list of strings")
                roles = set(raw_roles)
            else:
                raise ValueError("each API key must map to {workspace, roles}")
            if not isinstance(workspace_id, str) or not workspace_id:
                raise ValueError("API key workspace must be a non-empty string")
            if contains_direct_identifier(workspace_id):
                raise ValueError("API key workspace must not contain direct personal identifiers")
            if not roles or not roles.issubset(allowed_roles):
                raise ValueError(
                    f"API key roles must be a non-empty subset of {sorted(allowed_roles)}"
                )
            grants[token] = ApiKeyGrant(workspace_id=workspace_id, roles=frozenset(roles))
        if self.app_env == "production" and not any(
            "insight:read" in g.roles for g in grants.values()
        ):
            raise ValueError("Production requires a workspace-scoped insight:read API key")
        return grants

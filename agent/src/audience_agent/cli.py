from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from audience_agent.config import Settings
from audience_agent.seeding import load_seed


async def migrate(settings: Settings) -> None:
    conn = await asyncpg.connect(settings.database_url)
    try:
        migration_files = sorted(_resolve(settings.migrations_path).glob("*.sql"))
        if not migration_files:
            raise RuntimeError("no SQL migrations found")
        for migration in migration_files:
            await conn.execute(migration.read_text(encoding="utf-8"))
            print(f"applied {migration.name}")
    finally:
        await conn.close()


async def seed(settings: Settings) -> None:
    if settings.app_env == "production":
        raise RuntimeError("sample seeding is disabled in production")
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=4)
    try:
        from audience_agent.repository import PostgresEvidenceRepository

        repo = PostgresEvidenceRepository(pool)
        records = load_seed(_resolve(settings.seed_path), workspace_id="demo")
        version = records[0].dataset_version
        if await repo.active_dataset_version("demo") == version:
            existing = await repo.get_many("demo", version, [item.id for item in records])
            expected = sorted((item.model_dump(mode="json") for item in records), key=lambda x: x["id"])
            actual = sorted((item.model_dump(mode="json") for item in existing), key=lambda x: x["id"])
            if actual != expected:
                raise RuntimeError("active raw snapshot differs; regenerate a new content-hashed version")
            count = 0
        else:
            count = await repo.ingest(records)
            await repo.activate_dataset("demo", version)
        print(
            json.dumps(
                {
                    "workspace": "demo",
                    "dataset_version": version,
                    "records_upserted": count,
                    "status": "active",
                }
            )
        )
    finally:
        await pool.close()


def _resolve(path: Path) -> Path:
    if path.is_absolute():
        return path
    return Path(os.getenv("APP_ROOT", Path(__file__).resolve().parents[2])) / path


async def activate(settings: Settings, workspace_id: str, dataset_version: str) -> None:
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=2)
    try:
        from audience_agent.repository import PostgresEvidenceRepository

        await PostgresEvidenceRepository(pool).activate_dataset(workspace_id, dataset_version)
        print(
            json.dumps(
                {"workspace": workspace_id, "dataset_version": dataset_version, "status": "active"}
            )
        )
    finally:
        await pool.close()


def main() -> None:
    load_dotenv(override=False)
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if command in {"-h", "--help", "help"}:
        print(
            "usage: audience-agent [serve|migrate|seed|activate WORKSPACE VERSION|"
            "raw-import ROOT [--output-dir DIR]|openapi]"
        )
        return
    if command == "raw-import":
        from audience_agent.raw_ingest import main as raw_import_main

        raise SystemExit(raw_import_main(sys.argv[2:]))
    if command == "openapi":
        from audience_agent.api import create_app

        output = _resolve(Path("docs/openapi.json"))
        output.write_text(
            json.dumps(create_app().openapi(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(output)
        return
    settings = Settings.from_env(load_file=False, validate=command == "serve")
    if command == "migrate":
        asyncio.run(migrate(settings))
    elif command == "seed":
        asyncio.run(seed(settings))
    elif command == "activate" and len(sys.argv) == 4:
        asyncio.run(activate(settings, sys.argv[2], sys.argv[3]))
    elif command == "serve":
        import uvicorn

        uvicorn.run("audience_agent.api:app", host="0.0.0.0", port=8000, workers=1)
    else:
        raise SystemExit(
            "usage: audience-agent [serve|migrate|seed|activate WORKSPACE VERSION|"
            "raw-import ROOT [--output-dir DIR]|openapi]"
        )


if __name__ == "__main__":
    main()

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID, uuid4

import asyncpg

from audience_agent.contracts import EvidenceRecord


class DatasetVersionConflict(RuntimeError):
    """Raised when an immutable active/retired snapshot is mutated or reactivated."""


class FeedbackAssociationError(ValueError):
    """Raised when feedback does not match a previously recorded insight run."""


class EvidenceRepository(Protocol):
    async def health(self) -> bool: ...
    async def active_dataset_version(self, workspace_id: str) -> str | None: ...
    async def search(
        self, workspace_id: str, dataset_version: str, domain: str, question: str, limit: int
    ) -> list[EvidenceRecord]: ...
    async def search_domains(
        self,
        workspace_id: str,
        dataset_version: str,
        domains: Sequence[str],
        question: str,
        limit: int,
    ) -> list[EvidenceRecord]: ...
    async def get_many(
        self, workspace_id: str, dataset_version: str, record_ids: Sequence[str]
    ) -> list[EvidenceRecord]: ...
    async def ingest(self, records: Sequence[EvidenceRecord]) -> int: ...
    async def activate_dataset(self, workspace_id: str, dataset_version: str) -> None: ...
    async def record_run(
        self, workspace_id: str, thread_id: str, run_id: str, dataset_version: str | None
    ) -> None: ...
    async def save_feedback(
        self,
        workspace_id: str,
        thread_id: str,
        run_id: str,
        decision: str,
        notes: str,
        dataset_version: str | None = None,
    ) -> None: ...


def _combine_domain_results(
    groups: Sequence[Sequence[EvidenceRecord]], limit: int
) -> list[EvidenceRecord]:
    """Guarantee material diversity before filling remaining cross-domain slots."""
    selected: list[EvidenceRecord] = []
    seen: set[str] = set()
    for material in ("M2", "M3", "M4", "M5"):
        match = next(
            (item for group in groups for item in group if item.source_material == material), None
        )
        if match is not None and match.id not in seen:
            selected.append(match)
            seen.add(match.id)
    for index in range(limit):
        for group in groups:
            if index < len(group) and group[index].id not in seen:
                selected.append(group[index])
                seen.add(group[index].id)
                if len(selected) == limit:
                    return selected
    return selected[:limit]


class PostgresEvidenceRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def health(self) -> bool:
        async with self.pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True

    async def active_dataset_version(self, workspace_id: str) -> str | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT dataset_version FROM workspace_datasets WHERE workspace_id=$1 AND status='active'",
                workspace_id,
            )

    async def search(
        self, workspace_id: str, dataset_version: str, domain: str, question: str, limit: int
    ) -> list[EvidenceRecord]:
        # All request values are bound parameters; model text never becomes SQL.
        query = """
            SELECT id, workspace_id, dataset_version, domain, record_type, content,
                   source_material, source_file, source_sheet, source_field, source_row,
                   confidence, metadata
            FROM evidence_records
            WHERE workspace_id = $1 AND dataset_version = $2 AND domain = $3
            ORDER BY similarity(content, $4) DESC,
                     CASE confidence WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END DESC,
                     id
            LIMIT $5
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                query, workspace_id, dataset_version, domain, question[:1_200], limit
            )
        return [self._record(row) for row in rows]

    async def search_domains(
        self,
        workspace_id: str,
        dataset_version: str,
        domains: Sequence[str],
        question: str,
        limit: int,
    ) -> list[EvidenceRecord]:
        groups = [
            await self.search(workspace_id, dataset_version, domain, question, limit)
            for domain in domains
        ]
        return _combine_domain_results(groups, limit)

    async def get_many(
        self, workspace_id: str, dataset_version: str, record_ids: Sequence[str]
    ) -> list[EvidenceRecord]:
        if not record_ids:
            return []
        query = """
            SELECT id, workspace_id, dataset_version, domain, record_type, content,
                   source_material, source_file, source_sheet, source_field, source_row,
                   confidence, metadata
            FROM evidence_records
            WHERE workspace_id = $1 AND dataset_version = $2 AND id = ANY($3::text[])
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, workspace_id, dataset_version, list(record_ids))
        by_id = {row["id"]: self._record(row) for row in rows}
        return [by_id[key] for key in record_ids if key in by_id]

    async def ingest(self, records: Sequence[EvidenceRecord]) -> int:
        if not records:
            return 0
        versions = {(item.workspace_id, item.dataset_version) for item in records}
        if len(versions) != 1:
            raise ValueError(
                "one ingestion batch must contain exactly one workspace and dataset version"
            )
        workspace_id, dataset_version = next(iter(versions))
        batch_id = uuid4()
        upsert = """
            INSERT INTO evidence_records (
                id, workspace_id, dataset_version, domain, record_type, content,
                source_material, source_file, source_sheet, source_field, source_row,
                confidence, metadata, updated_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,now())
            ON CONFLICT (workspace_id, dataset_version, id) DO UPDATE SET
                domain=EXCLUDED.domain, record_type=EXCLUDED.record_type,
                content=EXCLUDED.content, source_material=EXCLUDED.source_material,
                source_file=EXCLUDED.source_file, source_sheet=EXCLUDED.source_sheet,
                source_field=EXCLUDED.source_field, source_row=EXCLUDED.source_row,
                confidence=EXCLUDED.confidence, metadata=EXCLUDED.metadata, updated_at=now()
        """
        async with self.pool.acquire() as conn:
            # Dataset status, batch metadata and every evidence write commit or roll back together.
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", workspace_id)
                current_status = await conn.fetchval(
                    "SELECT status FROM workspace_datasets WHERE workspace_id=$1 "
                    "AND dataset_version=$2 FOR UPDATE",
                    workspace_id,
                    dataset_version,
                )
                if current_status in {"active", "retired"}:
                    raise DatasetVersionConflict(
                        f"dataset version {dataset_version} is {current_status} and immutable"
                    )
                if current_status is None:
                    await conn.execute(
                        "INSERT INTO workspace_datasets(workspace_id,dataset_version,status) "
                        "VALUES($1,$2,'staged')",
                        workspace_id,
                        dataset_version,
                    )
                await conn.execute(
                    "INSERT INTO ingestion_batches(id,workspace_id,dataset_version,source_name,status) "
                    "VALUES($1,$2,$3,$4,'started')",
                    batch_id,
                    workspace_id,
                    dataset_version,
                    records[0].source_file[:300],
                )
                for item in records:
                    await conn.execute(
                        upsert,
                        item.id,
                        item.workspace_id,
                        item.dataset_version,
                        item.domain,
                        item.record_type,
                        item.content,
                        item.source_material,
                        item.source_file,
                        item.source_sheet,
                        item.source_field,
                        item.source_row,
                        item.confidence,
                        json.dumps(item.metadata, ensure_ascii=False),
                    )
                await conn.execute(
                    "UPDATE ingestion_batches SET row_count=$2,status='completed',"
                    "finished_at=now() WHERE id=$1",
                    batch_id,
                    len(records),
                )
        return len(records)

    async def activate_dataset(self, workspace_id: str, dataset_version: str) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", workspace_id)
                current_status = await conn.fetchval(
                    "SELECT status FROM workspace_datasets WHERE workspace_id=$1 "
                    "AND dataset_version=$2 FOR UPDATE",
                    workspace_id,
                    dataset_version,
                )
                if current_status != "staged":
                    raise DatasetVersionConflict("only a staged dataset version can be activated")
                count = await conn.fetchval(
                    "SELECT count(*) FROM evidence_records WHERE workspace_id=$1 "
                    "AND dataset_version=$2",
                    workspace_id,
                    dataset_version,
                )
                if not count:
                    raise ValueError("cannot activate a dataset version with no evidence records")
                await conn.execute(
                    "UPDATE workspace_datasets SET status='retired' WHERE workspace_id=$1 "
                    "AND status='active'",
                    workspace_id,
                )
                await conn.execute(
                    "UPDATE workspace_datasets SET status='active', activated_at=now() "
                    "WHERE workspace_id=$1 AND dataset_version=$2",
                    workspace_id,
                    dataset_version,
                )

    async def record_run(
        self, workspace_id: str, thread_id: str, run_id: str, dataset_version: str | None
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO insight_runs(workspace_id,thread_id,run_id,dataset_version) "
                "VALUES($1,$2,$3::uuid,$4)",
                workspace_id,
                thread_id,
                UUID(run_id),
                dataset_version,
            )

    async def save_feedback(
        self,
        workspace_id: str,
        thread_id: str,
        run_id: str,
        decision: str,
        notes: str,
        dataset_version: str | None = None,
    ) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT thread_id,dataset_version FROM insight_runs "
                    "WHERE workspace_id=$1 AND run_id=$2::uuid",
                    workspace_id,
                    UUID(run_id),
                )
                if (
                    row is None
                    or row["thread_id"] != thread_id
                    or row["dataset_version"] != dataset_version
                ):
                    raise FeedbackAssociationError(
                        "feedback run_id, thread_id and dataset_version do not match"
                    )
                await conn.execute(
                    "INSERT INTO insight_feedback"
                    "(id,workspace_id,thread_id,run_id,dataset_version,decision,notes) "
                    "VALUES(gen_random_uuid(),$1,$2,$3::uuid,$4,$5,$6)",
                    workspace_id,
                    thread_id,
                    UUID(run_id),
                    dataset_version,
                    decision,
                    notes,
                )

    @staticmethod
    def _record(row: asyncpg.Record) -> EvidenceRecord:
        metadata = row["metadata"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        return EvidenceRecord(
            id=row["id"],
            workspace_id=row["workspace_id"],
            dataset_version=row["dataset_version"],
            domain=row["domain"],
            record_type=row["record_type"],
            content=row["content"],
            source_material=row["source_material"],
            source_file=row["source_file"],
            source_sheet=row["source_sheet"],
            source_field=row["source_field"],
            source_row=row["source_row"],
            confidence=row["confidence"],
            metadata=metadata or {},
        )


class InMemoryEvidenceRepository:
    """Development/test only. Production startup forbids this storage backend."""

    def __init__(self, records: Sequence[EvidenceRecord] = ()) -> None:
        self.records: dict[tuple[str, str, str], EvidenceRecord] = {}
        self.active_versions: dict[str, str] = {}
        self.dataset_statuses: dict[tuple[str, str], str] = {}
        self.feedback: list[dict[str, str]] = []
        self.runs: dict[tuple[str, str], dict[str, str | None]] = {}
        self.staged_versions: set[tuple[str, str]] = set()
        self._lock = asyncio.Lock()
        for item in records:
            self.records[(item.workspace_id, item.dataset_version, item.id)] = item
            self.active_versions[item.workspace_id] = item.dataset_version
            self.dataset_statuses[(item.workspace_id, item.dataset_version)] = "active"

    async def health(self) -> bool:
        return True

    async def active_dataset_version(self, workspace_id: str) -> str | None:
        return self.active_versions.get(workspace_id)

    async def search(
        self, workspace_id: str, dataset_version: str, domain: str, question: str, limit: int
    ) -> list[EvidenceRecord]:
        candidates = [
            item
            for (ws, version, _), item in self.records.items()
            if ws == workspace_id and version == dataset_version and item.domain == domain
        ]
        q = set(question.lower())
        type_rank = {"fact": 4, "quote": 3, "hypothesis": 2, "opinion": 2}
        candidates.sort(
            key=lambda x: (
                sum(1 for char in set(x.content.lower()) if char in q),
                type_rank.get(x.record_type, 1),
            ),
            reverse=True,
        )
        return candidates[:limit]

    async def search_domains(
        self,
        workspace_id: str,
        dataset_version: str,
        domains: Sequence[str],
        question: str,
        limit: int,
    ) -> list[EvidenceRecord]:
        groups = [
            await self.search(workspace_id, dataset_version, domain, question, limit)
            for domain in domains
        ]
        return _combine_domain_results(groups, limit)

    async def get_many(
        self, workspace_id: str, dataset_version: str, record_ids: Sequence[str]
    ) -> list[EvidenceRecord]:
        return [
            self.records[(workspace_id, dataset_version, key)]
            for key in record_ids
            if (workspace_id, dataset_version, key) in self.records
        ]

    async def ingest(self, records: Sequence[EvidenceRecord]) -> int:
        if not records:
            return 0
        versions = {(item.workspace_id, item.dataset_version) for item in records}
        if len(versions) != 1:
            raise ValueError(
                "one ingestion batch must contain exactly one workspace and dataset version"
            )
        workspace_id, dataset_version = next(iter(versions))
        async with self._lock:
            current_status = self.dataset_statuses.get((workspace_id, dataset_version))
            if current_status in {"active", "retired"}:
                raise DatasetVersionConflict(
                    f"dataset version {dataset_version} is {current_status} and immutable"
                )
            for item in records:
                self.records[(item.workspace_id, item.dataset_version, item.id)] = item
            self.dataset_statuses[(workspace_id, dataset_version)] = "staged"
            self.staged_versions.add((workspace_id, dataset_version))
        return len(records)

    async def activate_dataset(self, workspace_id: str, dataset_version: str) -> None:
        async with self._lock:
            key = (workspace_id, dataset_version)
            if self.dataset_statuses.get(key) != "staged":
                raise DatasetVersionConflict("only a staged dataset version can be activated")
            if not any(
                ws == workspace_id and version == dataset_version
                for ws, version, _ in self.records
            ):
                raise ValueError("cannot activate a dataset version with no evidence records")
            previous = self.active_versions.get(workspace_id)
            if previous:
                self.dataset_statuses[(workspace_id, previous)] = "retired"
            self.active_versions[workspace_id] = dataset_version
            self.dataset_statuses[key] = "active"
            self.staged_versions.discard(key)

    async def record_run(
        self, workspace_id: str, thread_id: str, run_id: str, dataset_version: str | None
    ) -> None:
        async with self._lock:
            self.runs[(workspace_id, run_id)] = {
                "thread_id": thread_id,
                "dataset_version": dataset_version,
            }

    async def save_feedback(
        self,
        workspace_id: str,
        thread_id: str,
        run_id: str,
        decision: str,
        notes: str,
        dataset_version: str | None = None,
    ) -> None:
        async with self._lock:
            run = self.runs.get((workspace_id, run_id))
            if (
                run is None
                or run["thread_id"] != thread_id
                or run["dataset_version"] != dataset_version
            ):
                raise FeedbackAssociationError(
                    "feedback run_id, thread_id and dataset_version do not match"
                )
            self.feedback.append(
                {
                    "workspace_id": workspace_id,
                    "thread_id": thread_id,
                    "run_id": run_id,
                    "decision": decision,
                    "dataset_version": dataset_version or "",
                    "notes": notes,
                }
            )

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS workspace_datasets (
    workspace_id TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('staged','active','retired')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at TIMESTAMPTZ,
    PRIMARY KEY (workspace_id, dataset_version)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_dataset_per_workspace
    ON workspace_datasets (workspace_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS evidence_records (
    id TEXT NOT NULL, 
    workspace_id TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    domain TEXT NOT NULL CHECK (domain IN ('live_roi','audience_profile','experience','fulfillment')),
    record_type TEXT NOT NULL CHECK (record_type IN ('fact','quote','caveat','recommendation')),
    content TEXT NOT NULL,
    source_material TEXT NOT NULL,
    source_file TEXT NOT NULL,
    source_sheet TEXT NOT NULL,
    source_field TEXT NOT NULL,
    source_row TEXT,
    observed_at TIMESTAMPTZ,
    confidence TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, dataset_version, id),
    FOREIGN KEY (workspace_id, dataset_version) REFERENCES workspace_datasets(workspace_id, dataset_version) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS evidence_workspace_domain_idx ON evidence_records (workspace_id, dataset_version, domain);
CREATE INDEX IF NOT EXISTS evidence_content_trgm_idx ON evidence_records USING GIN (content gin_trgm_ops);
CREATE INDEX IF NOT EXISTS evidence_metadata_gin_idx ON evidence_records USING GIN (metadata);

CREATE TABLE IF NOT EXISTS insight_feedback (
    id UUID PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    run_id UUID NOT NULL,
    dataset_version TEXT,
    decision TEXT NOT NULL CHECK (decision IN ('accepted','edited','rejected','needs_validation')),
    notes TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS feedback_workspace_created_idx ON insight_feedback (workspace_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ingestion_batches (
    id UUID PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    source_name TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('started','completed','failed')),
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    FOREIGN KEY (workspace_id, dataset_version) REFERENCES workspace_datasets(workspace_id, dataset_version) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ingestion_workspace_version_idx ON ingestion_batches (workspace_id, dataset_version);

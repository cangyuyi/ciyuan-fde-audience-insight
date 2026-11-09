-- Evidence snapshot integrity, cross-domain taxonomy and verifiable feedback lineage.
ALTER TABLE evidence_records DROP CONSTRAINT IF EXISTS evidence_records_domain_check;
ALTER TABLE evidence_records
    ADD CONSTRAINT evidence_records_domain_check
    CHECK (domain IN ('live_roi','audience_profile','experience','fulfillment','cross_domain'));
ALTER TABLE evidence_records DROP CONSTRAINT IF EXISTS evidence_records_record_type_check;
ALTER TABLE evidence_records
    ADD CONSTRAINT evidence_records_record_type_check
    CHECK (record_type IN ('fact','quote','caveat','recommendation','opinion','hypothesis'));

CREATE TABLE IF NOT EXISTS insight_runs (
    workspace_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    run_id UUID NOT NULL,
    dataset_version TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, run_id)
);
CREATE INDEX IF NOT EXISTS insight_runs_thread_idx ON insight_runs (workspace_id, thread_id, created_at DESC);

CREATE OR REPLACE FUNCTION enforce_staged_evidence_mutation() RETURNS trigger AS $$
DECLARE
    dataset_status TEXT;
BEGIN
    -- UPDATE must protect both sides: active/retired OLD rows cannot be moved to a staged key.
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        SELECT status INTO dataset_status FROM workspace_datasets
            WHERE workspace_id = OLD.workspace_id AND dataset_version = OLD.dataset_version;
        IF dataset_status IS NULL OR dataset_status <> 'staged' THEN
            RAISE EXCEPTION 'source evidence dataset %/% is immutable or missing',
                OLD.workspace_id, OLD.dataset_version;
        END IF;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        SELECT status INTO dataset_status FROM workspace_datasets
            WHERE workspace_id = NEW.workspace_id AND dataset_version = NEW.dataset_version;
        IF dataset_status IS NULL OR dataset_status <> 'staged' THEN
            RAISE EXCEPTION 'target evidence dataset %/% is immutable or missing',
                NEW.workspace_id, NEW.dataset_version;
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS evidence_records_require_staged ON evidence_records;
CREATE TRIGGER evidence_records_require_staged
    BEFORE INSERT OR UPDATE OR DELETE ON evidence_records
    FOR EACH ROW EXECUTE FUNCTION enforce_staged_evidence_mutation();

CREATE OR REPLACE FUNCTION enforce_dataset_lifecycle() RETURNS trigger AS $$
BEGIN
    IF OLD.status = 'retired' AND NEW.status <> 'retired' THEN
        RAISE EXCEPTION 'retired dataset versions cannot be reactivated';
    END IF;
    IF OLD.status = 'active' AND NEW.status NOT IN ('active', 'retired') THEN
        RAISE EXCEPTION 'active dataset versions can only retire';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS workspace_datasets_lifecycle ON workspace_datasets;
CREATE TRIGGER workspace_datasets_lifecycle
    BEFORE UPDATE ON workspace_datasets
    FOR EACH ROW EXECUTE FUNCTION enforce_dataset_lifecycle();

-- Server-created fixture identities only. No legacy database import is implied.
CREATE TABLE IF NOT EXISTS rust_controller.fixture_osdeploy_origins (
    create_request_id uuid PRIMARY KEY CHECK (create_request_id <> '00000000-0000-0000-0000-000000000000'),
    run_id uuid NOT NULL UNIQUE REFERENCES rust_controller.osdeploy_runs(run_id),
    source_namespace text NOT NULL CHECK (source_namespace = 'rust-owned-fixture-v1'),
    claim_kind text NOT NULL CHECK (claim_kind = 'text'),
    claim_value text NOT NULL CHECK (claim_value = run_id::text),
    workflow_sha256 text NOT NULL CHECK (workflow_sha256 ~ '^[0-9a-f]{64}$'),
    UNIQUE(source_namespace, claim_kind, claim_value)
);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='rust_controller.fixture_osdeploy_origins'::regclass AND tgname='fixture_origin_no_mutation' AND NOT tgisinternal) THEN
        CREATE TRIGGER fixture_origin_no_mutation BEFORE UPDATE OR DELETE ON rust_controller.fixture_osdeploy_origins FOR EACH ROW EXECUTE FUNCTION rust_controller.reject_native_mutation();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='rust_controller.fixture_osdeploy_origins'::regclass AND tgname='fixture_origin_no_truncate' AND NOT tgisinternal) THEN
        CREATE TRIGGER fixture_origin_no_truncate BEFORE TRUNCATE ON rust_controller.fixture_osdeploy_origins FOR EACH STATEMENT EXECUTE FUNCTION rust_controller.reject_native_mutation();
    END IF;
END $$;

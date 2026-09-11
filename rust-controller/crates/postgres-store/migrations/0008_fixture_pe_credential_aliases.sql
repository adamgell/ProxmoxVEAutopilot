-- Permanent ownership of credential digests. Raw credentials are never stored.
CREATE UNIQUE INDEX IF NOT EXISTS fixture_pe_session_owner
    ON rust_controller.fixture_pe_boot_sessions(run_id,operation_id,attempt_id,package_sha256);
CREATE TABLE IF NOT EXISTS rust_controller.fixture_pe_credential_aliases (
    alias_sha256 bytea PRIMARY KEY CHECK (octet_length(alias_sha256) = 32),
    operation_id uuid NOT NULL REFERENCES rust_controller.fixture_pe_boot_sessions(operation_id),
    run_id uuid NOT NULL REFERENCES rust_controller.fixture_osdeploy_origins(run_id),
    attempt_id uuid NOT NULL,
    package_sha256 text NOT NULL CHECK (package_sha256 ~ '^[0-9a-f]{64}$'),
    expires_at bigint NOT NULL CHECK (expires_at > 0),
    created_at timestamptz NOT NULL,
    FOREIGN KEY (run_id, operation_id) REFERENCES rust_controller.osdeploy_operation_plans(run_id,operation_id),
    FOREIGN KEY (attempt_id, operation_id) REFERENCES rust_controller.attempts(attempt_id,operation_id),
    FOREIGN KEY (run_id,operation_id,attempt_id,package_sha256)
        REFERENCES rust_controller.fixture_pe_boot_sessions(run_id,operation_id,attempt_id,package_sha256)
);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='rust_controller.fixture_pe_credential_aliases'::regclass AND tgname='fixture_alias_no_mutation' AND NOT tgisinternal) THEN
        CREATE TRIGGER fixture_alias_no_mutation BEFORE UPDATE OR DELETE ON rust_controller.fixture_pe_credential_aliases FOR EACH ROW EXECUTE FUNCTION rust_controller.reject_native_mutation();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='rust_controller.fixture_pe_credential_aliases'::regclass AND tgname='fixture_alias_no_truncate' AND NOT tgisinternal) THEN
        CREATE TRIGGER fixture_alias_no_truncate BEFORE TRUNCATE ON rust_controller.fixture_pe_credential_aliases FOR EACH STATEMENT EXECUTE FUNCTION rust_controller.reject_native_mutation();
    END IF;
END $$;

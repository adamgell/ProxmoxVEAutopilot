-- Immutable reconstruction metadata only. Credentials live in a private fixture sink.
CREATE UNIQUE INDEX IF NOT EXISTS fixture_alias_complete_owner ON rust_controller.fixture_pe_credential_aliases(alias_sha256,operation_id,run_id,attempt_id,package_sha256,expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS fixture_origin_sink_owner ON rust_controller.fixture_osdeploy_origins(run_id,credential_sink_id);
CREATE UNIQUE INDEX IF NOT EXISTS fixture_session_dispatch_owner ON rust_controller.fixture_pe_boot_sessions(operation_id,dispatch_event_id);
CREATE TABLE IF NOT EXISTS rust_controller.fixture_pe_deliveries (
    operation_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    dispatch_event_id uuid NOT NULL,
    package_sha256 text NOT NULL,
    alias_sha256 bytea NOT NULL,
    expires_at bigint NOT NULL,
    sink_id uuid NOT NULL,
    FOREIGN KEY(alias_sha256,operation_id,run_id,attempt_id,package_sha256,expires_at) REFERENCES rust_controller.fixture_pe_credential_aliases(alias_sha256,operation_id,run_id,attempt_id,package_sha256,expires_at),
    FOREIGN KEY(run_id,sink_id) REFERENCES rust_controller.fixture_osdeploy_origins(run_id,credential_sink_id),
    FOREIGN KEY(operation_id,dispatch_event_id) REFERENCES rust_controller.fixture_pe_boot_sessions(operation_id,dispatch_event_id)
);
CREATE TABLE IF NOT EXISTS rust_controller.fixture_pe_delivery_acks (
    operation_id uuid PRIMARY KEY REFERENCES rust_controller.fixture_pe_deliveries(operation_id),
    acknowledged_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS rust_controller.fixture_pe_delivery_exposures (
    operation_id uuid PRIMARY KEY REFERENCES rust_controller.fixture_pe_delivery_acks(operation_id),
    lease_acquisition_event_id uuid NOT NULL REFERENCES rust_controller.osdeploy_lease_epochs(acquisition_event_id),
    generation bigint NOT NULL CHECK(generation > 0),
    worker_id text NOT NULL CHECK(length(worker_id)>0),
    exposed_at timestamptz NOT NULL
);
DO $$ DECLARE t text; BEGIN
    FOREACH t IN ARRAY ARRAY['fixture_pe_deliveries','fixture_pe_delivery_acks','fixture_pe_delivery_exposures'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid=('rust_controller.'||t)::regclass AND tgname='fixture_delivery_no_mutation' AND NOT tgisinternal) THEN
            EXECUTE format('CREATE TRIGGER fixture_delivery_no_mutation BEFORE UPDATE OR DELETE ON rust_controller.%I FOR EACH ROW EXECUTE FUNCTION rust_controller.reject_native_mutation()',t);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid=('rust_controller.'||t)::regclass AND tgname='fixture_delivery_no_truncate' AND NOT tgisinternal) THEN
            EXECUTE format('CREATE TRIGGER fixture_delivery_no_truncate BEFORE TRUNCATE ON rust_controller.%I FOR EACH STATEMENT EXECUTE FUNCTION rust_controller.reject_native_mutation()',t);
        END IF;
    END LOOP;
END $$;

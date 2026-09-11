-- Selected supervisor evidence is immutable. Consumption means possible exposure,
-- never acknowledged success; no physical-send capability is issued by this table.
CREATE TABLE IF NOT EXISTS rust_controller.fixture_stop_outbox (
    operation_id uuid PRIMARY KEY REFERENCES rust_controller.osdeploy_pve_dispatches(operation_id),
    attempt_id uuid NOT NULL,
    lease_token uuid NOT NULL,
    generation bigint NOT NULL CHECK(generation>0),
    admission_sha256 text NOT NULL CHECK(admission_sha256 ~ '^[0-9a-f]{64}$'),
    admission_json text NOT NULL CHECK(octet_length(admission_json)<=65536),
    sample_json text NOT NULL CHECK(octet_length(sample_json)<=65536),
    selected_at timestamptz NOT NULL,
    FOREIGN KEY(attempt_id,operation_id) REFERENCES rust_controller.attempts(attempt_id,operation_id)
);
CREATE TABLE IF NOT EXISTS rust_controller.fixture_stop_outbox_consumptions (
    operation_id uuid PRIMARY KEY REFERENCES rust_controller.fixture_stop_outbox(operation_id),
    consumed_at timestamptz NOT NULL
);
DO $$ DECLARE t text; BEGIN
    FOREACH t IN ARRAY ARRAY['fixture_stop_outbox','fixture_stop_outbox_consumptions'] LOOP
        IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid=('rust_controller.'||t)::regclass AND tgname='stop_outbox_immutable' AND NOT tgisinternal) THEN
            EXECUTE format('CREATE TRIGGER stop_outbox_immutable BEFORE UPDATE OR DELETE ON rust_controller.%I FOR EACH ROW EXECUTE FUNCTION rust_controller.reject_native_mutation()',t);
            EXECUTE format('CREATE TRIGGER stop_outbox_no_truncate BEFORE TRUNCATE ON rust_controller.%I FOR EACH STATEMENT EXECUTE FUNCTION rust_controller.reject_native_mutation()',t);
        END IF;
    END LOOP;
END $$;

-- Physical fixture-submit outcomes are immutable bookkeeping.  This table is
-- deliberately not a release permit and carries no stopped-power assertion.
ALTER TABLE rust_controller.fixture_stop_outbox
    ADD COLUMN IF NOT EXISTS provenance_sha256 text
    CHECK(provenance_sha256 IS NULL OR provenance_sha256 ~ '^[0-9a-f]{64}$');

CREATE TABLE IF NOT EXISTS rust_controller.fixture_stop_release_outcomes (
    operation_id uuid PRIMARY KEY
        REFERENCES rust_controller.fixture_stop_outbox(operation_id),
    attempt_id uuid NOT NULL,
    lease_owner uuid NOT NULL,
    generation bigint NOT NULL CHECK(generation > 0),
    request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[0-9a-f]{64}$'),
    admission_sha256 text NOT NULL CHECK(admission_sha256 ~ '^[0-9a-f]{64}$'),
    sample_sha256 text NOT NULL CHECK(sample_sha256 ~ '^[0-9a-f]{64}$'),
    provenance_sha256 text NOT NULL CHECK(provenance_sha256 ~ '^[0-9a-f]{64}$'),
    state text NOT NULL CHECK(state IN ('accepted','refused','ambiguous')),
    submit_sequence bigint NOT NULL CHECK(submit_sequence > 0),
    receipt_json jsonb,
    receipt_sha256 text CHECK(receipt_sha256 IS NULL OR receipt_sha256 ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL,
    CHECK ((state = 'accepted') = (receipt_json IS NOT NULL AND receipt_sha256 IS NOT NULL)),
    CHECK (state <> 'accepted' OR (receipt_json IS NOT NULL AND receipt_sha256 IS NOT NULL)),
    FOREIGN KEY(attempt_id, operation_id)
        REFERENCES rust_controller.attempts(attempt_id, operation_id)
);
DO $$ BEGIN
    IF NOT EXISTS(SELECT 1 FROM pg_trigger
        WHERE tgrelid='rust_controller.fixture_stop_release_outcomes'::regclass
          AND tgname='stop_release_outcome_immutable' AND NOT tgisinternal) THEN
        CREATE TRIGGER stop_release_outcome_immutable
            BEFORE UPDATE OR DELETE ON rust_controller.fixture_stop_release_outcomes
            FOR EACH ROW EXECUTE FUNCTION rust_controller.reject_native_mutation();
    END IF;
    IF NOT EXISTS(SELECT 1 FROM pg_trigger
        WHERE tgrelid='rust_controller.fixture_stop_release_outcomes'::regclass
          AND tgname='stop_release_outcome_no_truncate' AND NOT tgisinternal) THEN
        CREATE TRIGGER stop_release_outcome_no_truncate
            BEFORE TRUNCATE ON rust_controller.fixture_stop_release_outcomes
            FOR EACH STATEMENT EXECUTE FUNCTION rust_controller.reject_native_mutation();
    END IF;
END $$;

-- Original fixture response evidence only; never dispatch or callback authority.
CREATE TABLE IF NOT EXISTS rust_controller.fixture_start_pe_responses (
    operation_id uuid PRIMARY KEY REFERENCES rust_controller.osdeploy_pve_receipts(operation_id),
    identity_canonical_json text NOT NULL CHECK (octet_length(identity_canonical_json) BETWEEN 1 AND 8192 AND jsonb_typeof(identity_canonical_json::jsonb)='object'),
    request_envelope bytea NOT NULL CHECK (octet_length(request_envelope) BETWEEN 1 AND 65536),
    predecessor_identity_canonical_json text NOT NULL CHECK (octet_length(predecessor_identity_canonical_json) BETWEEN 1 AND 8192 AND jsonb_typeof(predecessor_identity_canonical_json::jsonb)='object'),
    predecessor_request_envelope bytea NOT NULL CHECK (octet_length(predecessor_request_envelope) BETWEEN 1 AND 65536),
    response_envelope bytea NOT NULL CHECK (octet_length(response_envelope) BETWEEN 1 AND 65536),
    response_sha256 text GENERATED ALWAYS AS (encode(sha256(response_envelope),'hex')) STORED
);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='rust_controller.fixture_start_pe_responses'::regclass AND tgname='fixture_response_no_mutation' AND NOT tgisinternal) THEN
        CREATE TRIGGER fixture_response_no_mutation BEFORE UPDATE OR DELETE ON rust_controller.fixture_start_pe_responses FOR EACH ROW EXECUTE FUNCTION rust_controller.reject_native_mutation();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='rust_controller.fixture_start_pe_responses'::regclass AND tgname='fixture_response_no_truncate' AND NOT tgisinternal) THEN
        CREATE TRIGGER fixture_response_no_truncate BEFORE TRUNCATE ON rust_controller.fixture_start_pe_responses FOR EACH STATEMENT EXECUTE FUNCTION rust_controller.reject_native_mutation();
    END IF;
END $$;

-- No backfill: historical responses have no original route evidence.
ALTER TABLE rust_controller.fixture_start_pe_responses
    ADD COLUMN IF NOT EXISTS provenance_sha256 text
    CHECK (provenance_sha256 IS NULL OR provenance_sha256 ~ '^[0-9a-f]{64}$');

-- NULL preserves historical physical-only fixtures. Non-NULL requires delivery
-- to the named sink. The existing origin mutation trigger protects this column.
ALTER TABLE rust_controller.fixture_osdeploy_origins
    ADD COLUMN IF NOT EXISTS credential_sink_id uuid
    CHECK (credential_sink_id <> '00000000-0000-0000-0000-000000000000');

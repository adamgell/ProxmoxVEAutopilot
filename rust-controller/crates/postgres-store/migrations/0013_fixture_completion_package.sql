-- Existing origins retain their original bytes and bearer alias ownership.
-- The origin's existing immutable-row trigger also protects this selector.
ALTER TABLE rust_controller.fixture_osdeploy_origins
    ADD COLUMN IF NOT EXISTS completion_package boolean NOT NULL DEFAULT false;
ALTER TABLE rust_controller.fixture_osdeploy_origins
    DROP CONSTRAINT IF EXISTS fixture_completion_package_requires_delivery;
ALTER TABLE rust_controller.fixture_osdeploy_origins
    ADD CONSTRAINT fixture_completion_package_requires_delivery
    CHECK (NOT completion_package OR credential_sink_id IS NOT NULL);

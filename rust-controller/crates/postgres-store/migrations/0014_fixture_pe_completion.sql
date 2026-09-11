ALTER TABLE rust_controller.osdeploy_decisions DROP CONSTRAINT IF EXISTS osdeploy_decisions_action_resolution;
ALTER TABLE rust_controller.osdeploy_decisions ADD CONSTRAINT osdeploy_decisions_action_resolution CHECK (CASE
    WHEN action IN ('stage_activated','lease_acquired','evaluation_started','lease_renewed',
        'lease_reclaimed_same_attempt','credential_delivery_reclaimed','run_cancelled',
        'residual_lease_revoked','reconciliation_scheduled') THEN resolution IS NULL
    WHEN action='pve_dispatch_committed' THEN resolution IS NOT DISTINCT FROM 'ready'
    WHEN action='fixture_pe_registered' THEN resolution IS NOT DISTINCT FROM 'satisfied'
    WHEN action='fixture_pe_completed' THEN resolution IS NOT NULL AND resolution IN ('satisfied','failed')
    WHEN action='fixture_grace_waiting' THEN resolution IS NOT DISTINCT FROM 'waiting'
    WHEN action='pve_evaluated' THEN resolution IS NOT NULL AND resolution IN
        ('waiting','satisfied','failed','blocked','unknown','conflicted')
    WHEN action='evaluation_reparked' THEN resolution IS NOT DISTINCT FROM 'waiting'
    WHEN action='stage_cancelled_unexposed' THEN resolution IS NOT DISTINCT FROM 'blocked'
    WHEN action IN ('scope_expired_before_activation','activated_scope_expired',
        'stage_cancelled_exposed','lease_expired_uncertain') THEN resolution IS NOT DISTINCT FROM 'unknown'
    ELSE false END);
CREATE TABLE IF NOT EXISTS rust_controller.fixture_pe_completions (
    operation_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    selected_event_id uuid NOT NULL UNIQUE,
    registration_event_id uuid NOT NULL REFERENCES rust_controller.fixture_pe_registrations(selected_event_id),
    alias_sha256 bytea NOT NULL REFERENCES rust_controller.fixture_pe_credential_aliases(alias_sha256),
    report_canonical_json text NOT NULL CHECK(octet_length(report_canonical_json)<=4096),
    report_sha256 text NOT NULL CHECK(report_sha256 ~ '^[0-9a-f]{64}$'),
    selected_at timestamptz NOT NULL,
    succeeded boolean NOT NULL,
    FOREIGN KEY(run_id,operation_id) REFERENCES rust_controller.osdeploy_operation_plans(run_id,operation_id),
    FOREIGN KEY(attempt_id,operation_id) REFERENCES rust_controller.attempts(attempt_id,operation_id),
    FOREIGN KEY(selected_event_id,operation_id) REFERENCES rust_controller.osdeploy_decisions(event_id,operation_id)
);
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='rust_controller.fixture_pe_completions'::regclass AND tgname='fixture_completion_no_mutation' AND NOT tgisinternal) THEN
        CREATE TRIGGER fixture_completion_no_mutation BEFORE UPDATE OR DELETE ON rust_controller.fixture_pe_completions FOR EACH ROW EXECUTE FUNCTION rust_controller.reject_native_mutation();
        CREATE TRIGGER fixture_completion_no_truncate BEFORE TRUNCATE ON rust_controller.fixture_pe_completions FOR EACH STATEMENT EXECUTE FUNCTION rust_controller.reject_native_mutation();
    END IF;
END $$;

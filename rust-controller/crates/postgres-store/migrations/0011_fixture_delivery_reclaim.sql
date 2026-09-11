-- Extend closed history discriminators without rewriting existing events.
DO $$
DECLARE c record;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint
        WHERE conrelid='rust_controller.osdeploy_decisions'::regclass
          AND contype='c' AND pg_get_constraintdef(oid) LIKE '%lease_reclaimed_same_attempt%'
    LOOP
        EXECUTE format('ALTER TABLE rust_controller.osdeploy_decisions DROP CONSTRAINT %I', c.conname);
    END LOOP;
END $$;
ALTER TABLE rust_controller.osdeploy_decisions ADD CONSTRAINT osdeploy_decisions_action_resolution CHECK (CASE
    WHEN action IN ('stage_activated','lease_acquired','evaluation_started','lease_renewed',
        'lease_reclaimed_same_attempt','credential_delivery_reclaimed','run_cancelled',
        'residual_lease_revoked','reconciliation_scheduled') THEN resolution IS NULL
    WHEN action='pve_dispatch_committed' THEN resolution IS NOT DISTINCT FROM 'ready'
    -- Later immutable registration rows survive idempotent migration replay.
    WHEN action='fixture_pe_registered' THEN resolution IS NOT DISTINCT FROM 'satisfied'
    WHEN action='pve_evaluated' THEN resolution IS NOT NULL AND resolution IN
        ('waiting','satisfied','failed','blocked','unknown','conflicted')
    WHEN action='evaluation_reparked' THEN resolution IS NOT DISTINCT FROM 'waiting'
    WHEN action='stage_cancelled_unexposed' THEN resolution IS NOT DISTINCT FROM 'blocked'
    WHEN action IN ('scope_expired_before_activation','activated_scope_expired',
        'stage_cancelled_exposed','lease_expired_uncertain') THEN resolution IS NOT DISTINCT FROM 'unknown'
    ELSE false END);
ALTER TABLE rust_controller.osdeploy_lease_epochs DROP CONSTRAINT IF EXISTS osdeploy_lease_epochs_purpose_check;
ALTER TABLE rust_controller.osdeploy_lease_epochs ADD CONSTRAINT osdeploy_lease_epochs_purpose_check
    CHECK (purpose IN ('initial_evaluation','resume_evaluation','reclaimed_evaluation','reclaimed_credential_delivery'));

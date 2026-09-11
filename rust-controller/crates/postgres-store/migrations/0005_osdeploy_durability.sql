-- PgStore owns the transaction for this additive migration. Execution history
-- is immutable; the final scheduling table is only a rebuildable projection.
CREATE TABLE IF NOT EXISTS rust_controller.osdeploy_decisions (
    operation_id uuid NOT NULL,
    decision_revision bigint NOT NULL CHECK (decision_revision > 0),
    event_id uuid NOT NULL UNIQUE,
    run_id uuid NOT NULL,
    attempt_id uuid,
    action text NOT NULL,
    resolution text,
    workflow_sha256 text NOT NULL CHECK (workflow_sha256 ~ '^[0-9a-f]{64}$'),
    stage_sha256 text NOT NULL CHECK (stage_sha256 ~ '^[0-9a-f]{64}$'),
    generation bigint NOT NULL CHECK (generation > 0),
    evaluated_at timestamptz NOT NULL,
    payload_canonical_json text NOT NULL CHECK (
        octet_length(payload_canonical_json) <= 65536
        AND jsonb_typeof(payload_canonical_json::jsonb) = 'object'
    ),
    PRIMARY KEY (operation_id, decision_revision),
    UNIQUE (event_id, operation_id),
    FOREIGN KEY (run_id, operation_id)
        REFERENCES rust_controller.osdeploy_operation_plans(run_id, operation_id),
    FOREIGN KEY (event_id, operation_id)
        REFERENCES rust_controller.journal_events(event_id, operation_id),
    FOREIGN KEY (attempt_id, operation_id)
        REFERENCES rust_controller.attempts(attempt_id, operation_id),
    CHECK (CASE
        WHEN action IN ('stage_activated', 'lease_acquired', 'evaluation_started',
            'lease_renewed', 'lease_reclaimed_same_attempt', 'run_cancelled',
            'residual_lease_revoked', 'reconciliation_scheduled') THEN resolution IS NULL
        WHEN action = 'pve_dispatch_committed' THEN resolution IS NOT DISTINCT FROM 'ready'
        WHEN action = 'pve_evaluated' THEN resolution IS NOT NULL AND
            resolution IN ('waiting', 'satisfied', 'failed', 'blocked', 'unknown', 'conflicted')
        WHEN action = 'evaluation_reparked' THEN resolution IS NOT DISTINCT FROM 'waiting'
        WHEN action = 'stage_cancelled_unexposed' THEN resolution IS NOT DISTINCT FROM 'blocked'
        WHEN action IN ('scope_expired_before_activation', 'activated_scope_expired',
            'stage_cancelled_exposed', 'lease_expired_uncertain')
            THEN resolution IS NOT DISTINCT FROM 'unknown'
        ELSE false
    END),
    CHECK (CASE
        WHEN action = 'scope_expired_before_activation' THEN attempt_id IS NULL
        WHEN action IN ('stage_cancelled_unexposed', 'run_cancelled') THEN true
        ELSE attempt_id IS NOT NULL
    END)
);

CREATE TABLE IF NOT EXISTS rust_controller.osdeploy_deadlines (
    run_id uuid NOT NULL,
    scope_key text NOT NULL CHECK (scope_key IN (
        'mutation_clone', 'mutation_disk_capacity', 'mutation_configure_pe',
        'mutation_start_pe', 'mutation_pe_ensure_stopped', 'mutation_configure_disk',
        'mutation_start_disk', 'pe_registration', 'pe_completion', 'shutdown_grace', 'full_os'
    )),
    anchor_operation_id uuid NOT NULL,
    anchor_event_id uuid NOT NULL,
    opened_at timestamptz NOT NULL,
    budget_seconds integer NOT NULL CHECK (budget_seconds BETWEEN 1 AND 86400),
    deadline_at timestamptz NOT NULL,
    PRIMARY KEY (run_id, scope_key),
    FOREIGN KEY (run_id, anchor_operation_id)
        REFERENCES rust_controller.osdeploy_operation_plans(run_id, operation_id),
    FOREIGN KEY (anchor_event_id, anchor_operation_id)
        REFERENCES rust_controller.journal_events(event_id, operation_id),
    CONSTRAINT osdeploy_deadline_anchor_decision_fk
        FOREIGN KEY (anchor_event_id, anchor_operation_id)
        REFERENCES rust_controller.osdeploy_decisions(event_id, operation_id)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK (deadline_at = opened_at + budget_seconds * interval '1 second')
);

CREATE TABLE IF NOT EXISTS rust_controller.osdeploy_attempt_bindings (
    operation_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    attempt_id uuid NOT NULL UNIQUE,
    scope_key text NOT NULL,
    activation_event_id uuid NOT NULL,
    activated_at timestamptz NOT NULL,
    deadline_at timestamptz NOT NULL,
    activation_mode text NOT NULL CHECK (activation_mode IN ('leased', 'parked')),
    UNIQUE (operation_id, attempt_id),
    FOREIGN KEY (run_id, operation_id)
        REFERENCES rust_controller.osdeploy_operation_plans(run_id, operation_id),
    FOREIGN KEY (attempt_id, operation_id)
        REFERENCES rust_controller.attempts(attempt_id, operation_id),
    FOREIGN KEY (run_id, scope_key)
        REFERENCES rust_controller.osdeploy_deadlines(run_id, scope_key),
    FOREIGN KEY (activation_event_id, operation_id)
        REFERENCES rust_controller.journal_events(event_id, operation_id),
    CONSTRAINT osdeploy_binding_activation_decision_fk
        FOREIGN KEY (activation_event_id, operation_id)
        REFERENCES rust_controller.osdeploy_decisions(event_id, operation_id)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK (deadline_at > activated_at)
);

CREATE TABLE IF NOT EXISTS rust_controller.osdeploy_lease_epochs (
    acquisition_event_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL,
    run_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    executor_kind text NOT NULL CHECK (executor_kind = 'rust'),
    generation bigint NOT NULL CHECK (generation > 0),
    worker_id text NOT NULL CHECK (btrim(worker_id) <> ''),
    lease_token_sha256 text NOT NULL UNIQUE CHECK (lease_token_sha256 ~ '^[0-9a-f]{64}$'),
    acquired_at timestamptz NOT NULL,
    initial_expires_at timestamptz NOT NULL,
    deadline_at timestamptz NOT NULL,
    purpose text NOT NULL CHECK (purpose IN ('initial_evaluation', 'resume_evaluation', 'reclaimed_evaluation')),
    UNIQUE (acquisition_event_id, operation_id),
    FOREIGN KEY (run_id, operation_id)
        REFERENCES rust_controller.osdeploy_operation_plans(run_id, operation_id),
    FOREIGN KEY (attempt_id, operation_id)
        REFERENCES rust_controller.attempts(attempt_id, operation_id),
    FOREIGN KEY (acquisition_event_id, operation_id)
        REFERENCES rust_controller.journal_events(event_id, operation_id),
    CONSTRAINT osdeploy_epoch_acquisition_decision_fk
        FOREIGN KEY (acquisition_event_id, operation_id)
        REFERENCES rust_controller.osdeploy_decisions(event_id, operation_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (operation_id, attempt_id)
        REFERENCES rust_controller.osdeploy_attempt_bindings(operation_id, attempt_id),
    CHECK (acquired_at < initial_expires_at AND initial_expires_at <= deadline_at)
);

CREATE TABLE IF NOT EXISTS rust_controller.osdeploy_pve_evidence (
    event_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL,
    run_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    evidence_revision bigint NOT NULL CHECK (evidence_revision > 0),
    evidence_sha256 text NOT NULL CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
    source text NOT NULL CHECK (source = 'fake_pve'),
    evidence_canonical_json text NOT NULL CHECK (
        octet_length(evidence_canonical_json) <= 1048576
        AND jsonb_typeof(evidence_canonical_json::jsonb) = 'object'
    ),
    UNIQUE (operation_id, evidence_revision),
    UNIQUE (event_id, operation_id),
    FOREIGN KEY (run_id, operation_id)
        REFERENCES rust_controller.osdeploy_operation_plans(run_id, operation_id),
    FOREIGN KEY (attempt_id, operation_id)
        REFERENCES rust_controller.attempts(attempt_id, operation_id),
    FOREIGN KEY (event_id, operation_id)
        REFERENCES rust_controller.journal_events(event_id, operation_id),
    FOREIGN KEY (operation_id, attempt_id)
        REFERENCES rust_controller.osdeploy_attempt_bindings(operation_id, attempt_id)
);

CREATE TABLE IF NOT EXISTS rust_controller.osdeploy_pve_dispatches (
    operation_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    dispatch_event_id uuid NOT NULL UNIQUE,
    dispatch_revision bigint NOT NULL CHECK (dispatch_revision > 0),
    preflight_event_id uuid NOT NULL,
    workflow_sha256 text NOT NULL CHECK (workflow_sha256 ~ '^[0-9a-f]{64}$'),
    pve_plan_sha256 text NOT NULL CHECK (pve_plan_sha256 ~ '^[0-9a-f]{64}$'),
    request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    request_canonical_json text NOT NULL CHECK (
        octet_length(request_canonical_json) <= 1048576
        AND jsonb_typeof(request_canonical_json::jsonb) = 'object'
    ),
    source text NOT NULL CHECK (source = 'fake_pve'),
    original_generation bigint NOT NULL CHECK (original_generation > 0),
    dispatched_at timestamptz NOT NULL,
    lease_acquisition_event_id uuid NOT NULL,
    FOREIGN KEY (run_id, operation_id)
        REFERENCES rust_controller.osdeploy_operation_plans(run_id, operation_id),
    FOREIGN KEY (attempt_id, operation_id)
        REFERENCES rust_controller.attempts(attempt_id, operation_id),
    FOREIGN KEY (operation_id, attempt_id)
        REFERENCES rust_controller.osdeploy_attempt_bindings(operation_id, attempt_id),
    FOREIGN KEY (dispatch_event_id, operation_id)
        REFERENCES rust_controller.journal_events(event_id, operation_id),
    CONSTRAINT osdeploy_dispatch_decision_fk
        FOREIGN KEY (dispatch_event_id, operation_id)
        REFERENCES rust_controller.osdeploy_decisions(event_id, operation_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (preflight_event_id, operation_id)
        REFERENCES rust_controller.journal_events(event_id, operation_id),
    FOREIGN KEY (preflight_event_id, operation_id)
        REFERENCES rust_controller.osdeploy_pve_evidence(event_id, operation_id),
    FOREIGN KEY (lease_acquisition_event_id, operation_id)
        REFERENCES rust_controller.osdeploy_lease_epochs(acquisition_event_id, operation_id)
);

CREATE TABLE IF NOT EXISTS rust_controller.osdeploy_pve_receipts (
    operation_id uuid PRIMARY KEY REFERENCES rust_controller.osdeploy_pve_dispatches(operation_id),
    receipt_event_id uuid NOT NULL UNIQUE,
    receipt_kind text NOT NULL CHECK (receipt_kind IN ('task', 'synchronous')),
    upid text,
    accepted_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL,
    FOREIGN KEY (receipt_event_id, operation_id)
        REFERENCES rust_controller.journal_events(event_id, operation_id),
    CHECK ((receipt_kind = 'task' AND upid IS NOT NULL AND btrim(upid) <> '')
        OR (receipt_kind = 'synchronous' AND upid IS NULL)),
    CHECK (recorded_at >= accepted_at)
);

CREATE TABLE IF NOT EXISTS rust_controller.osdeploy_run_cancellations (
    run_id uuid PRIMARY KEY REFERENCES rust_controller.osdeploy_runs(run_id),
    anchor_operation_id uuid NOT NULL,
    decision_event_id uuid NOT NULL UNIQUE,
    generation bigint NOT NULL CHECK (generation > 0),
    requested_at timestamptz NOT NULL,
    FOREIGN KEY (run_id, anchor_operation_id)
        REFERENCES rust_controller.osdeploy_operation_plans(run_id, operation_id),
    FOREIGN KEY (decision_event_id, anchor_operation_id)
        REFERENCES rust_controller.journal_events(event_id, operation_id),
    CONSTRAINT osdeploy_cancellation_decision_fk
        FOREIGN KEY (decision_event_id, anchor_operation_id)
        REFERENCES rust_controller.osdeploy_decisions(event_id, operation_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE IF NOT EXISTS rust_controller.osdeploy_schedule_projection (
    operation_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    mode text NOT NULL CHECK (mode IN ('waiting', 'unknown_reconciliation')),
    basis_event_id uuid NOT NULL,
    basis_revision bigint NOT NULL CHECK (basis_revision > 0),
    scope_key text NOT NULL,
    next_check_at timestamptz,
    unavailable_count smallint NOT NULL CHECK (unavailable_count BETWEEN 0 AND 4),
    rebuilt_through_revision bigint NOT NULL CHECK (rebuilt_through_revision > 0),
    FOREIGN KEY (run_id, operation_id)
        REFERENCES rust_controller.osdeploy_operation_plans(run_id, operation_id),
    FOREIGN KEY (attempt_id, operation_id)
        REFERENCES rust_controller.attempts(attempt_id, operation_id),
    FOREIGN KEY (operation_id, attempt_id)
        REFERENCES rust_controller.osdeploy_attempt_bindings(operation_id, attempt_id),
    FOREIGN KEY (basis_event_id, operation_id)
        REFERENCES rust_controller.journal_events(event_id, operation_id),
    CONSTRAINT osdeploy_schedule_basis_decision_fk
        FOREIGN KEY (basis_event_id, operation_id)
        REFERENCES rust_controller.osdeploy_decisions(event_id, operation_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (run_id, scope_key)
        REFERENCES rust_controller.osdeploy_deadlines(run_id, scope_key),
    CHECK (mode <> 'waiting' OR next_check_at IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_osdeploy_schedule_due
    ON rust_controller.osdeploy_schedule_projection(mode, next_check_at, operation_id);
CREATE INDEX IF NOT EXISTS idx_osdeploy_deadline_due
    ON rust_controller.osdeploy_deadlines(deadline_at, run_id, scope_key);
CREATE INDEX IF NOT EXISTS idx_osdeploy_selected_decision
    ON rust_controller.osdeploy_decisions(operation_id, decision_revision DESC)
    WHERE resolution IS NOT NULL AND resolution <> 'ready';

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'osdeploy_decisions', 'osdeploy_deadlines', 'osdeploy_attempt_bindings',
        'osdeploy_lease_epochs', 'osdeploy_pve_evidence', 'osdeploy_pve_dispatches',
        'osdeploy_pve_receipts', 'osdeploy_run_cancellations'
    ] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_trigger
            WHERE tgrelid = ('rust_controller.' || table_name)::regclass
            AND tgname = 'osdeploy_no_mutation' AND NOT tgisinternal) THEN
            EXECUTE format('CREATE TRIGGER osdeploy_no_mutation BEFORE UPDATE OR DELETE ON rust_controller.%I FOR EACH ROW EXECUTE FUNCTION rust_controller.reject_native_mutation()', table_name);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_trigger
            WHERE tgrelid = ('rust_controller.' || table_name)::regclass
            AND tgname = 'osdeploy_no_truncate' AND NOT tgisinternal) THEN
            EXECUTE format('CREATE TRIGGER osdeploy_no_truncate BEFORE TRUNCATE ON rust_controller.%I FOR EACH STATEMENT EXECUTE FUNCTION rust_controller.reject_native_mutation()', table_name);
        END IF;
    END LOOP;
END $$;

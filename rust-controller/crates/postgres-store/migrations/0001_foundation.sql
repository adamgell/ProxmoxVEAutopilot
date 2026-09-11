CREATE SCHEMA IF NOT EXISTS rust_controller;

CREATE TABLE IF NOT EXISTS rust_controller.operations (
    operation_id uuid PRIMARY KEY CHECK ((get_byte(uuid_send(operation_id), 6) >> 4) = 7),
    workflow_kind text NOT NULL CHECK (
        workflow_kind IN ('cloud_osd', 'os_deploy', 'task_sequence', 'synthetic_long_sleep')
    ),
    run_id uuid NOT NULL CHECK (run_id <> '00000000-0000-0000-0000-000000000000'::uuid),
    operation_key text NOT NULL CHECK (btrim(operation_key) <> ''),
    contract_version smallint NOT NULL CHECK (contract_version > 0),
    state text NOT NULL CHECK (
        state IN (
            'pending', 'leased', 'running', 'waiting', 'cancelling',
            'satisfied', 'failed', 'blocked', 'unknown', 'conflicted'
        )
    ),
    revision bigint NOT NULL CHECK (revision >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (workflow_kind, run_id, operation_key, contract_version)
);

CREATE TABLE IF NOT EXISTS rust_controller.commands (
    idempotency_key text PRIMARY KEY CHECK (btrim(idempotency_key) <> ''),
    operation_id uuid NOT NULL REFERENCES rust_controller.operations(operation_id),
    payload_digest text NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS rust_controller.attempts (
    attempt_id uuid PRIMARY KEY CHECK ((get_byte(uuid_send(attempt_id), 6) >> 4) = 7),
    operation_id uuid NOT NULL REFERENCES rust_controller.operations(operation_id),
    attempt_number integer NOT NULL CHECK (attempt_number > 0),
    state text NOT NULL CHECK (
        state IN (
            'pending', 'leased', 'running', 'waiting', 'cancelling',
            'satisfied', 'failed', 'blocked', 'unknown', 'conflicted'
        )
    ),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    deadline_at timestamptz NOT NULL DEFAULT (clock_timestamp() + interval '5 minutes'),
    completed_at timestamptz,
    CHECK (deadline_at > started_at),
    CHECK (completed_at IS NULL OR completed_at >= started_at),
    UNIQUE (operation_id, attempt_number),
    UNIQUE (attempt_id, operation_id)
);

CREATE TABLE IF NOT EXISTS rust_controller.journal_events (
    event_id uuid PRIMARY KEY CHECK ((get_byte(uuid_send(event_id), 6) >> 4) = 7),
    operation_id uuid NOT NULL REFERENCES rust_controller.operations(operation_id),
    attempt_id uuid,
    aggregate_revision bigint NOT NULL CHECK (aggregate_revision > 0),
    semantic_key text NOT NULL CHECK (btrim(semantic_key) <> ''),
    payload_digest text NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
    event_kind text NOT NULL CHECK (
        event_kind IN (
            'command_accepted', 'attempt_started', 'execution_state_changed',
            'evidence_recorded', 'decision_recorded'
        )
    ),
    execution_state text CHECK (
        execution_state IS NULL OR execution_state IN (
            'pending', 'leased', 'running', 'waiting', 'cancelling',
            'satisfied', 'failed', 'blocked', 'unknown', 'conflicted'
        )
    ),
    payload jsonb NOT NULL,
    observed_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((event_kind = 'execution_state_changed') = (execution_state IS NOT NULL)),
    UNIQUE (operation_id, semantic_key),
    UNIQUE (operation_id, aggregate_revision),
    UNIQUE (event_id, operation_id),
    FOREIGN KEY (attempt_id, operation_id)
        REFERENCES rust_controller.attempts(attempt_id, operation_id)
);

CREATE TABLE IF NOT EXISTS rust_controller.outbox (
    outbox_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id uuid NOT NULL,
    operation_id uuid NOT NULL,
    topic text NOT NULL CHECK (btrim(topic) <> ''),
    payload jsonb NOT NULL,
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    claimed_at timestamptz,
    claim_expires_at timestamptz,
    claim_token uuid,
    delivered_at timestamptz,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (event_id),
    FOREIGN KEY (event_id, operation_id)
        REFERENCES rust_controller.journal_events(event_id, operation_id),
    CHECK (delivered_at IS NULL OR claimed_at IS NOT NULL),
    CHECK ((claimed_at IS NULL) = (claim_token IS NULL)),
    CHECK ((claimed_at IS NULL) = (claim_expires_at IS NULL)),
    CHECK (claimed_at IS NULL OR claimed_at >= created_at),
    CHECK (claim_expires_at IS NULL OR claim_expires_at > claimed_at),
    CHECK (delivered_at IS NULL OR delivered_at >= claimed_at)
);

CREATE INDEX IF NOT EXISTS idx_outbox_delivery_eligible
    ON rust_controller.outbox (outbox_id, available_at, claim_expires_at)
    WHERE delivered_at IS NULL;

CREATE TABLE IF NOT EXISTS rust_controller.operation_projection (
    operation_id uuid PRIMARY KEY REFERENCES rust_controller.operations(operation_id),
    state text NOT NULL CHECK (
        state IN (
            'pending', 'leased', 'running', 'waiting', 'cancelling',
            'satisfied', 'failed', 'blocked', 'unknown', 'conflicted'
        )
    ),
    revision bigint NOT NULL CHECK (revision >= 0),
    last_event_id uuid,
    rebuilt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (last_event_id, operation_id)
        REFERENCES rust_controller.journal_events(event_id, operation_id),
    CHECK ((revision = 0) = (last_event_id IS NULL))
);

CREATE TABLE IF NOT EXISTS rust_controller.orchestration_authority (
    singleton_key smallint PRIMARY KEY DEFAULT 1 CHECK (singleton_key = 1),
    executor_kind text NOT NULL CHECK (executor_kind IN ('python', 'rust')),
    generation bigint NOT NULL CHECK (generation > 0),
    changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    change_reference text NOT NULL CHECK (btrim(change_reference) <> '')
);

CREATE TABLE IF NOT EXISTS rust_controller.worker_leases (
    operation_id uuid PRIMARY KEY REFERENCES rust_controller.operations(operation_id),
    attempt_id uuid NOT NULL,
    executor_kind text NOT NULL CHECK (executor_kind IN ('python', 'rust')),
    generation bigint NOT NULL CHECK (generation > 0),
    worker_id text NOT NULL CHECK (btrim(worker_id) <> ''),
    lease_token text NOT NULL UNIQUE CHECK (btrim(lease_token) <> ''),
    acquired_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    heartbeat_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_expires_at timestamptz NOT NULL DEFAULT (clock_timestamp() + interval '30 seconds'),
    deadline_at timestamptz NOT NULL DEFAULT (clock_timestamp() + interval '5 minutes'),
    CHECK (heartbeat_at >= acquired_at),
    CHECK (lease_expires_at > acquired_at),
    CHECK (deadline_at >= lease_expires_at),
    FOREIGN KEY (attempt_id, operation_id)
        REFERENCES rust_controller.attempts(attempt_id, operation_id)
);

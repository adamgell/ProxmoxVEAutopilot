-- Fixture boot arming only. This record is not VM or callback authentication.
CREATE TABLE IF NOT EXISTS rust_controller.fixture_pe_boot_sessions (
    operation_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    attempt_id uuid NOT NULL UNIQUE,
    dispatch_event_id uuid NOT NULL UNIQUE,
    package_sha256 text NOT NULL CHECK (package_sha256 ~ '^[0-9a-f]{64}$'),
    package_canonical_bytes bytea NOT NULL,
    original_generation bigint NOT NULL CHECK (original_generation > 0),
    worker_id text NOT NULL CHECK (length(worker_id) > 0),
    lease_acquisition_event_id uuid NOT NULL,
    opened_at timestamptz NOT NULL,
    registration_deadline timestamptz NOT NULL CHECK (registration_deadline > opened_at),
    FOREIGN KEY (run_id, operation_id) REFERENCES rust_controller.osdeploy_operation_plans(run_id,operation_id),
    FOREIGN KEY (attempt_id, operation_id) REFERENCES rust_controller.attempts(attempt_id,operation_id),
    FOREIGN KEY (dispatch_event_id, operation_id) REFERENCES rust_controller.osdeploy_decisions(event_id,operation_id),
    FOREIGN KEY (lease_acquisition_event_id, operation_id) REFERENCES rust_controller.osdeploy_decisions(event_id,operation_id),
    CHECK (package_sha256 = encode(sha256(package_canonical_bytes),'hex'))
);

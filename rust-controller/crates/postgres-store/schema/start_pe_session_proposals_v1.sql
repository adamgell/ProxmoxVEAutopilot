-- Opt-in fixture-only proposal schema; deliberately not in PgStore::migrate.
-- No armed/consumed state or authenticated witness exists in this version.
CREATE TABLE rust_controller.osdeploy_start_pe_session_proposals (
 session_id uuid PRIMARY KEY CHECK(session_id <> '00000000-0000-0000-0000-000000000000'),
 run_id uuid NOT NULL,
 start_operation uuid NOT NULL UNIQUE,
 registration_operation uuid NOT NULL UNIQUE,
 attempt_id uuid NOT NULL,
 proposal_generation uuid NOT NULL CHECK(proposal_generation <> '00000000-0000-0000-0000-000000000000'),
 proposal_owner uuid NOT NULL CHECK(proposal_owner <> '00000000-0000-0000-0000-000000000000'),
 original_authority_generation bigint NOT NULL CHECK(original_authority_generation > 0),
 request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[0-9a-f]{64}$'),
 dispatch_event_id uuid NOT NULL,
 scope_key text NOT NULL CHECK(scope_key='pe_registration'),
 opened_at timestamptz NOT NULL,
 budget_seconds integer NOT NULL CHECK(budget_seconds BETWEEN 1 AND 86400),
 deadline_at timestamptz NOT NULL,
 witness_state text NOT NULL CHECK(witness_state='unavailable'),
 CHECK(start_operation <> registration_operation),
 CHECK(deadline_at=opened_at + budget_seconds * interval '1 second'),
 FOREIGN KEY(run_id,start_operation) REFERENCES rust_controller.osdeploy_operation_plans(run_id,operation_id),
 FOREIGN KEY(run_id,registration_operation) REFERENCES rust_controller.osdeploy_operation_plans(run_id,operation_id),
 FOREIGN KEY(attempt_id,start_operation) REFERENCES rust_controller.attempts(attempt_id,operation_id),
 FOREIGN KEY(start_operation) REFERENCES rust_controller.osdeploy_pve_dispatches(operation_id),
 FOREIGN KEY(dispatch_event_id,start_operation) REFERENCES rust_controller.osdeploy_decisions(event_id,operation_id),
 FOREIGN KEY(run_id,scope_key) REFERENCES rust_controller.osdeploy_deadlines(run_id,scope_key)
);
CREATE TRIGGER start_pe_proposal_no_mutation BEFORE UPDATE OR DELETE
 ON rust_controller.osdeploy_start_pe_session_proposals FOR EACH ROW
 EXECUTE FUNCTION rust_controller.reject_native_mutation();
CREATE TRIGGER start_pe_proposal_no_truncate BEFORE TRUNCATE
 ON rust_controller.osdeploy_start_pe_session_proposals FOR EACH STATEMENT
 EXECUTE FUNCTION rust_controller.reject_native_mutation();

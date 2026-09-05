ALTER TABLE rust_controller.operations DROP CONSTRAINT IF EXISTS operations_workflow_kind_check;
ALTER TABLE rust_controller.operations ADD CONSTRAINT operations_workflow_kind_check CHECK (
 workflow_kind IN ('cloud_osd','os_deploy','task_sequence','synthetic_long_sleep','native_pve_vm_boot')
);
CREATE TABLE IF NOT EXISTS rust_controller.native_vm_reservations (
 cluster_key text NOT NULL,
 vmid integer NOT NULL CHECK(vmid>0),
 vm_uuid uuid NOT NULL,
 mac text NOT NULL,
 run_id uuid NOT NULL UNIQUE,
 plan_digest text NOT NULL CHECK(plan_digest ~ '^[0-9a-f]{64}$'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(cluster_key,vmid), UNIQUE(cluster_key,vm_uuid), UNIQUE(cluster_key,mac)
);
CREATE TABLE IF NOT EXISTS rust_controller.native_operation_plans (
 operation_id uuid PRIMARY KEY REFERENCES rust_controller.operations(operation_id),
 predecessor_id uuid REFERENCES rust_controller.native_operation_plans(operation_id),
 step text NOT NULL CHECK(step IN ('clone','configure','start')),
 payload_digest text NOT NULL CHECK(payload_digest ~ '^[0-9a-f]{64}$'),
 plan jsonb NOT NULL,
 CHECK(predecessor_id IS NULL OR predecessor_id<>operation_id)
);
CREATE TABLE IF NOT EXISTS rust_controller.native_dispatches (
 operation_id uuid PRIMARY KEY REFERENCES rust_controller.native_operation_plans(operation_id),
 attempt_id uuid NOT NULL,
 plan_digest text NOT NULL CHECK(plan_digest ~ '^[0-9a-f]{64}$'),
 generation bigint NOT NULL CHECK(generation>0),
 dispatch_revision bigint NOT NULL CHECK(dispatch_revision>0),
 request_digest text NOT NULL CHECK(request_digest ~ '^[0-9a-f]{64}$'),
 request_marker uuid NOT NULL UNIQUE,
 preflight_event_id uuid NOT NULL,
 dispatched_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(attempt_id,operation_id) REFERENCES rust_controller.attempts(attempt_id,operation_id),
 FOREIGN KEY(preflight_event_id,operation_id) REFERENCES rust_controller.journal_events(event_id,operation_id)
);
CREATE TABLE IF NOT EXISTS rust_controller.native_receipts (
 operation_id uuid PRIMARY KEY REFERENCES rust_controller.native_dispatches(operation_id),
 receipt_kind text NOT NULL CHECK(receipt_kind IN ('task','synchronous')),
 upid text,
 recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK((receipt_kind='task')=(upid IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS rust_controller.native_decisions (
 operation_id uuid NOT NULL REFERENCES rust_controller.native_operation_plans(operation_id),
 decision_revision bigint NOT NULL CHECK(decision_revision>0),
 attempt_id uuid NOT NULL,
 evidence_event_id uuid NOT NULL,
 plan_digest text NOT NULL CHECK(plan_digest ~ '^[0-9a-f]{64}$'),
 generation bigint NOT NULL CHECK(generation>0),
 decision text NOT NULL CHECK(decision IN ('satisfied','failed','blocked','unknown','conflicted')),
 reason text NOT NULL,
 PRIMARY KEY(operation_id,decision_revision),
 FOREIGN KEY(attempt_id,operation_id) REFERENCES rust_controller.attempts(attempt_id,operation_id),
 FOREIGN KEY(evidence_event_id,operation_id) REFERENCES rust_controller.journal_events(event_id,operation_id)
);
CREATE TABLE IF NOT EXISTS rust_controller.native_run_cancellations (
 run_id uuid PRIMARY KEY REFERENCES rust_controller.native_vm_reservations(run_id),
 clone_operation_id uuid NOT NULL REFERENCES rust_controller.native_operation_plans(operation_id),
 decision_event_id uuid NOT NULL,
 generation bigint NOT NULL CHECK(generation>0),
 requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(decision_event_id,clone_operation_id) REFERENCES rust_controller.journal_events(event_id,operation_id)
);
CREATE OR REPLACE FUNCTION rust_controller.reject_native_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'native durable record is immutable' USING ERRCODE='23514';
END $$;
DO $$
DECLARE table_name text;
BEGIN
 FOREACH table_name IN ARRAY ARRAY['native_vm_reservations','native_operation_plans','native_dispatches','native_receipts','native_decisions','native_run_cancellations'] LOOP
  IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid=('rust_controller.'||table_name)::regclass AND tgname='native_no_mutation' AND NOT tgisinternal) THEN
   EXECUTE format('CREATE TRIGGER native_no_mutation BEFORE UPDATE OR DELETE ON rust_controller.%I FOR EACH ROW EXECUTE FUNCTION rust_controller.reject_native_mutation()',table_name);
   EXECUTE format('CREATE TRIGGER native_no_truncate BEFORE TRUNCATE ON rust_controller.%I FOR EACH STATEMENT EXECUTE FUNCTION rust_controller.reject_native_mutation()',table_name);
  END IF;
 END LOOP;
END $$;

CREATE TABLE IF NOT EXISTS rust_controller.osdeploy_runs (
 run_id uuid PRIMARY KEY REFERENCES rust_controller.native_vm_reservations(run_id),
 contract_version smallint NOT NULL CHECK(contract_version=1),
 workflow_sha256 text NOT NULL CHECK(workflow_sha256 ~ '^[0-9a-f]{64}$'),
 plan_canonical_json text NOT NULL CHECK(octet_length(plan_canonical_json)<=65536 AND jsonb_typeof(plan_canonical_json::jsonb)='object'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS rust_controller.osdeploy_operation_plans (
 operation_id uuid PRIMARY KEY REFERENCES rust_controller.operations(operation_id),
 run_id uuid NOT NULL REFERENCES rust_controller.osdeploy_runs(run_id),
 ordinal smallint NOT NULL CHECK(ordinal BETWEEN 0 AND 15),
 stage text NOT NULL,
 predecessor_id uuid,
 dependency_kind text NOT NULL,
 command_sha256 text NOT NULL CHECK(command_sha256 ~ '^[0-9a-f]{64}$'),
 pve_plan_sha256 text CHECK(pve_plan_sha256 ~ '^[0-9a-f]{64}$'),
 UNIQUE(run_id,ordinal), UNIQUE(run_id,stage), UNIQUE(run_id,operation_id),
 FOREIGN KEY(run_id,predecessor_id) REFERENCES rust_controller.osdeploy_operation_plans(run_id,operation_id),
 CHECK(predecessor_id IS NULL OR predecessor_id<>operation_id),
 CHECK((ordinal=0)=(predecessor_id IS NULL)),
 CHECK(dependency_kind=CASE ordinal WHEN 0 THEN 'intake' WHEN 7 THEN 'grace_gate' ELSE 'satisfied' END),
 CHECK((pve_plan_sha256 IS NOT NULL)=(ordinal IN (0,1,2,3,7,8,9))),
 CHECK(stage=(ARRAY['clone','disk_capacity','configure_pe','start_pe','pe_register','pe_complete','pe_shutdown_grace','pe_ensure_stopped','configure_disk','start_disk','install_qga','verify_qga','install_qga_watchdog','install_agent','agent_heartbeat','verify_operational'])[ordinal+1])
);
CREATE TABLE IF NOT EXISTS rust_controller.osdeploy_agent_reservations (
 expected_agent_id text PRIMARY KEY,
 run_id uuid NOT NULL UNIQUE REFERENCES rust_controller.osdeploy_runs(run_id),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
DO $$
DECLARE table_name text;
BEGIN
 FOREACH table_name IN ARRAY ARRAY['osdeploy_runs','osdeploy_operation_plans','osdeploy_agent_reservations'] LOOP
  IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid=('rust_controller.'||table_name)::regclass AND tgname='osdeploy_no_mutation' AND NOT tgisinternal) THEN
   EXECUTE format('CREATE TRIGGER osdeploy_no_mutation BEFORE UPDATE OR DELETE ON rust_controller.%I FOR EACH ROW EXECUTE FUNCTION rust_controller.reject_native_mutation()',table_name);
  END IF;
  IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid=('rust_controller.'||table_name)::regclass AND tgname='osdeploy_no_truncate' AND NOT tgisinternal) THEN
   EXECUTE format('CREATE TRIGGER osdeploy_no_truncate BEFORE TRUNCATE ON rust_controller.%I FOR EACH STATEMENT EXECUTE FUNCTION rust_controller.reject_native_mutation()',table_name);
  END IF;
 END LOOP;
END $$;

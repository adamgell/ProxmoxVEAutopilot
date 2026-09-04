DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE connamespace = 'rust_controller'::regnamespace
          AND conrelid = 'rust_controller.worker_leases'::regclass
          AND conname = 'worker_leases_lease_token_uuid_v7'
    ) THEN
        ALTER TABLE rust_controller.worker_leases
            ADD CONSTRAINT worker_leases_lease_token_uuid_v7 CHECK (
                CASE
                    WHEN lease_token ~* '^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                    THEN (get_byte(uuid_send(lease_token::uuid), 6) >> 4) = 7
                    ELSE false
                END
            ) NOT VALID;
    END IF;
END
$$;

-- The singleton may be inserted once for legitimate bootstrap. After that,
-- deletion and truncation are forbidden so no prior generation can be reused.
CREATE OR REPLACE FUNCTION rust_controller.reject_authority_removal()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'orchestration authority cannot be removed after bootstrap'
        USING ERRCODE = '23514';
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgrelid = 'rust_controller.orchestration_authority'::regclass
          AND tgname = 'trg_orchestration_authority_no_delete'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_orchestration_authority_no_delete
            BEFORE DELETE ON rust_controller.orchestration_authority
            FOR EACH ROW
            EXECUTE FUNCTION rust_controller.reject_authority_removal();
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgrelid = 'rust_controller.orchestration_authority'::regclass
          AND tgname = 'trg_orchestration_authority_no_truncate'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_orchestration_authority_no_truncate
            BEFORE TRUNCATE ON rust_controller.orchestration_authority
            FOR EACH STATEMENT
            EXECUTE FUNCTION rust_controller.reject_authority_removal();
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_operations_scheduler_pending
    ON rust_controller.operations (workflow_kind, created_at, operation_id)
    WHERE state = 'pending';

CREATE INDEX IF NOT EXISTS idx_worker_leases_expiry
    ON rust_controller.worker_leases (lease_expires_at, operation_id);

CREATE OR REPLACE FUNCTION rust_controller.enforce_authority_generation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.generation <> OLD.generation + 1 THEN
        RAISE EXCEPTION 'authority generation must advance exactly once from %', OLD.generation
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgrelid = 'rust_controller.orchestration_authority'::regclass
          AND tgname = 'trg_orchestration_authority_generation'
          AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_orchestration_authority_generation
            BEFORE UPDATE ON rust_controller.orchestration_authority
            FOR EACH ROW
            EXECUTE FUNCTION rust_controller.enforce_authority_generation();
    END IF;
END
$$;

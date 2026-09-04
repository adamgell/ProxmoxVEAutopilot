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

CREATE INDEX IF NOT EXISTS idx_operations_scheduler_pending
    ON rust_controller.operations (workflow_kind, created_at, operation_id)
    WHERE state = 'pending';

CREATE INDEX IF NOT EXISTS idx_worker_leases_expiry
    ON rust_controller.worker_leases (lease_expires_at, operation_id);

mod osdeploy_support;
use controller_domain::{CommandEnvelope, OperationId, RunId, SemanticOperationKey, WorkflowKind};
use osdeploy_adapter::OsDeployStage;
use osdeploy_support::*;
use postgres_store::{OsDeployOperationPlanV1, OsDeployStoreError, SchedulerError, StoreError};
use serde_json::json;

#[test]
fn stage_mapping_and_exact_hash_object() {
    let plan = plan();
    let actions = [
        Some("clone"),
        Some("ensure_capacity"),
        Some("configure_pe"),
        Some("start_pe"),
        None,
        None,
        None,
        Some("ensure_stopped"),
        Some("configure_disk"),
        Some("start_disk"),
        None,
        None,
        None,
        None,
        None,
        None,
    ];
    for (stage, action) in OsDeployStage::ALL.into_iter().zip(actions) {
        let derived = OsDeployOperationPlanV1::derive(&plan, stage).unwrap();
        let value = serde_json::to_value(&derived).unwrap();
        assert_eq!(value.as_object().unwrap().len(), 4);
        assert_eq!(value["contract_version"], 1);
        assert_eq!(value["workflow_sha256"], SHA);
        assert_eq!(value["pve"]["action"].as_str(), action);
        if action.is_none() {
            assert!(value["pve"].is_null());
        }
        assert_eq!(
            derived.fingerprint().unwrap(),
            event_journal::payload_digest(&value).unwrap()
        );
    }
    // Independent compact sorted JSON hashed with SHA-256, outside derive.
    assert_eq!(
        OsDeployOperationPlanV1::derive(&plan, OsDeployStage::VerifyOperational)
            .unwrap()
            .fingerprint()
            .unwrap(),
        "3655bb385ded888344b632c15b6901832e287309b9a51af1ce7606898a0e3e20"
    );
}

#[tokio::test]
async fn registration_and_reload_are_atomic_and_idempotent() {
    let f = Fixture::new().await;
    let run = RunId::new();
    let plan = plan();
    let first = f.store.enqueue_osdeploy(run, &plan).await.unwrap();
    let again = f.other.enqueue_osdeploy(run, &plan).await.unwrap();
    assert_eq!(first, again);
    let loaded = f.store.load_osdeploy_registration(run).await.unwrap();
    assert_eq!(loaded.plan(), &plan);
    assert_eq!(loaded.ids(), &first);
    assert_eq!(first.operations().len(), 16);
    f.assert_fresh_counts().await;
    for stage in OsDeployStage::ALL {
        assert_eq!(loaded.stage(stage).stage(), stage);
        assert_eq!(first.operation(stage).as_uuid().get_version_num(), 7);
        let operation = f
            .store
            .load_operation(first.operation(stage))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(
            operation.state(),
            controller_domain::ExecutionState::Pending
        );
        assert_eq!(operation.revision(), 0);
    }
    let before = f.snapshot().await;
    assert!(
        f.scheduler()
            .claim_next(WorkflowKind::OsDeploy, 1)
            .await
            .unwrap()
            .is_none()
    );
    assert!(
        f.other_scheduler()
            .claim_next_bound(WorkflowKind::OsDeploy, 1, 1, first.workflow_sha256())
            .await
            .unwrap()
            .is_none()
    );
    assert!(matches!(
        f.scheduler()
            .request_cancel(first.operation(OsDeployStage::Clone))
            .await,
        Err(SchedulerError::PlanBindingMismatch)
    ));
    assert_eq!(f.snapshot().await, before);
}

#[tokio::test]
async fn raw_osdeploy_intake_is_rejected_without_writes() {
    let f = Fixture::new().await;
    let command = CommandEnvelope::new(
        "raw-osdeploy",
        SemanticOperationKey::new(WorkflowKind::OsDeploy, RunId::new(), "raw", 1).unwrap(),
        "a".repeat(64),
    )
    .unwrap();
    let before = f.snapshot().await;
    assert!(matches!(
        f.store.append_command(OperationId::new(), &command).await,
        Err(StoreError::TypedWorkflowRequired)
    ));
    assert_eq!(f.snapshot().await, before);
}

#[tokio::test]
async fn osdeploy_claim_guard_precedes_database_access() {
    let f = Fixture::new().await;
    f.pool.close().await;
    assert!(
        f.scheduler()
            .claim_next(WorkflowKind::OsDeploy, 1)
            .await
            .unwrap()
            .is_none()
    );
    assert!(
        f.scheduler()
            .claim_next_bound(WorkflowKind::OsDeploy, 1, 65535, "invalid")
            .await
            .unwrap()
            .is_none()
    );
    assert!(matches!(
        f.scheduler().claim_next(WorkflowKind::OsDeploy, 0).await,
        Err(SchedulerError::InvalidCap { cap: 0 })
    ));
    assert!(matches!(
        f.scheduler()
            .claim_next_bound(WorkflowKind::OsDeploy, 0, 1, SHA)
            .await,
        Err(SchedulerError::InvalidCap { cap: 0 })
    ));
    assert!(
        f.scheduler()
            .claim_next(WorkflowKind::NativePveVmBoot, 0)
            .await
            .unwrap()
            .is_none()
    );
}

#[test]
fn every_stage_binds_non_pve_inputs_but_preserves_the_pve_subset() {
    let original = plan();
    let changed = altered(|v| v["policy"]["pe_seconds"] = json!(7000));
    for stage in OsDeployStage::ALL {
        let a = OsDeployOperationPlanV1::derive(&original, stage).unwrap();
        let b = OsDeployOperationPlanV1::derive(&changed, stage).unwrap();
        assert_ne!(a.fingerprint().unwrap(), b.fingerprint().unwrap());
        assert_eq!(a.pve(), b.pve());
    }
    let unusable = altered(|v| v["disk_serial"] = json!("A".repeat(21)));
    for stage in OsDeployStage::ALL {
        assert_eq!(
            OsDeployOperationPlanV1::derive(&unusable, stage),
            Err(OsDeployStoreError::Validation)
        );
    }
}

#[tokio::test]
async fn concurrent_duplicate_registration_returns_one_manifest() {
    let f = Fixture::new().await;
    let run = RunId::new();
    let plan = plan();
    let (a, b) = tokio::join!(
        f.store.enqueue_osdeploy(run, &plan),
        f.other.enqueue_osdeploy(run, &plan)
    );
    assert_eq!(a.unwrap(), b.unwrap());
    f.assert_fresh_counts().await;
}

#[tokio::test]
async fn changed_full_plan_and_both_native_family_directions_conflict() {
    let f = Fixture::new().await;
    let run = RunId::new();
    let plan = plan();
    f.store.enqueue_osdeploy(run, &plan).await.unwrap();
    let before = f.snapshot().await;
    let changed = altered(|v| v["policy"]["pe_seconds"] = json!(7000));
    assert_eq!(
        f.other.enqueue_osdeploy(run, &changed).await,
        Err(OsDeployStoreError::Conflict)
    );
    assert_eq!(
        f.other.enqueue_native_vm(run, plan.vm()).await,
        Err(postgres_store::NativeStoreError::Conflict)
    );
    assert_eq!(f.snapshot().await, before);
    let f = Fixture::new().await;
    f.store.enqueue_native_vm(run, plan.vm()).await.unwrap();
    let before = f.snapshot().await;
    assert_eq!(
        f.other.enqueue_osdeploy(run, &plan).await,
        Err(OsDeployStoreError::Conflict)
    );
    assert_eq!(f.snapshot().await, before);
}

#[tokio::test]
async fn vm_identity_and_global_normalized_agent_reservations_are_exclusive() {
    let f = Fixture::new().await;
    f.store
        .enqueue_osdeploy(RunId::new(), &plan())
        .await
        .unwrap();
    let before = f.snapshot().await;
    for collision in ["vmid", "uuid", "mac", "agent"] {
        let changed = altered(|v| {
            if collision != "vmid" {
                v["vm"]["target_vmid"] = json!(902);
            }
            if collision != "uuid" {
                v["vm"]["uuid"] = json!("88888888-8888-4888-8888-888888888888");
            }
            if collision != "mac" {
                v["vm"]["mac"] = json!("02:00:00:00:00:02");
            }
            if collision != "agent" {
                v["names"]["requested_name"] = json!("Another");
                v["names"]["windows_name"] = json!("Another");
                v["names"]["expected_agent_id"] = json!("agent-another");
            } else {
                v["vm"]["cluster_key"] = json!("other-cluster");
                v["names"]["requested_name"] = json!("LabVM01");
            }
        });
        assert_eq!(
            f.other.enqueue_osdeploy(RunId::new(), &changed).await,
            Err(OsDeployStoreError::Conflict),
            "{collision}"
        );
        assert_eq!(f.snapshot().await, before);
    }
}

#[tokio::test]
async fn serial_admission_and_fixed_errors_precede_any_database_writes() {
    let f = Fixture::new().await;
    let before = f.snapshot().await;
    let bad = altered(|v| v["disk_serial"] = json!("A".repeat(21)));
    assert_eq!(
        f.store.enqueue_osdeploy(RunId::new(), &bad).await,
        Err(OsDeployStoreError::Validation)
    );
    assert_eq!(f.snapshot().await, before);
    let good = altered(|v| v["disk_serial"] = json!("A".repeat(20)));
    f.store.enqueue_osdeploy(RunId::new(), &good).await.unwrap();
    f.assert_fresh_counts().await;
    f.pool.close().await;
    assert_eq!(
        f.store.enqueue_osdeploy(RunId::new(), &bad).await,
        Err(OsDeployStoreError::Validation)
    );
    assert_eq!(
        f.store.enqueue_osdeploy(RunId::new(), &good).await,
        Err(OsDeployStoreError::StorageUnavailable)
    );
    assert_eq!(
        f.store.load_osdeploy_registration(RunId::new()).await,
        Err(OsDeployStoreError::StorageUnavailable)
    );
    for (version, hash) in [
        (1, "invalid".to_owned()),
        (32768, "a".repeat(64)),
        (1, "a".repeat(64)),
    ] {
        let command = CommandEnvelope::new(
            "invalid",
            SemanticOperationKey::new(WorkflowKind::OsDeploy, RunId::new(), "raw", version)
                .unwrap(),
            hash,
        )
        .unwrap();
        let error = f
            .store
            .append_command(OperationId::new(), &command)
            .await
            .unwrap_err();
        assert!(match version {
            32768 => matches!(error, StoreError::ContractVersionOutOfRange),
            _ if command.payload_digest() == "invalid" =>
                matches!(error, StoreError::InvalidPayloadDigest),
            _ => matches!(error, StoreError::TypedWorkflowRequired),
        });
    }
    assert_eq!(
        OsDeployStoreError::Validation.to_string(),
        "OSDeploy registration validation failed"
    );
    assert_eq!(
        OsDeployStoreError::Conflict.to_string(),
        "OSDeploy registration conflict"
    );
    assert_eq!(
        OsDeployStoreError::StorageUnavailable.to_string(),
        "OSDeploy registration storage unavailable"
    );
}

fn generic_command(run: RunId, key: &str) -> CommandEnvelope {
    CommandEnvelope::new(
        key,
        SemanticOperationKey::new(WorkflowKind::TaskSequence, run, "generic", 1).unwrap(),
        "a".repeat(64),
    )
    .unwrap()
}

#[tokio::test]
async fn reload_rejects_corrupted_complete_manifest_and_reservations() {
    for (table, query) in [
        (
            "osdeploy_operation_plans",
            "DELETE FROM rust_controller.osdeploy_operation_plans WHERE ordinal=15",
        ),
        (
            "osdeploy_operation_plans",
            "UPDATE rust_controller.osdeploy_operation_plans SET predecessor_id=(SELECT operation_id FROM rust_controller.osdeploy_operation_plans WHERE ordinal=0) WHERE ordinal=2",
        ),
        (
            "osdeploy_operation_plans",
            "UPDATE rust_controller.osdeploy_operation_plans SET command_sha256=repeat('e',64) WHERE ordinal=4",
        ),
        (
            "osdeploy_operation_plans",
            "UPDATE rust_controller.osdeploy_operation_plans SET pve_plan_sha256=repeat('e',64) WHERE ordinal=0",
        ),
        (
            "osdeploy_agent_reservations",
            "DELETE FROM rust_controller.osdeploy_agent_reservations",
        ),
        (
            "osdeploy_agent_reservations",
            "UPDATE rust_controller.osdeploy_agent_reservations SET expected_agent_id='agent-wrong'",
        ),
        (
            "native_vm_reservations",
            "UPDATE rust_controller.native_vm_reservations SET cluster_key='wrong-cluster'",
        ),
        (
            "native_vm_reservations",
            "UPDATE rust_controller.native_vm_reservations SET vmid=902",
        ),
        (
            "native_vm_reservations",
            "UPDATE rust_controller.native_vm_reservations SET vm_uuid='88888888-8888-4888-8888-888888888888'",
        ),
        (
            "native_vm_reservations",
            "UPDATE rust_controller.native_vm_reservations SET mac='02:00:00:00:00:02'",
        ),
        (
            "native_vm_reservations",
            "UPDATE rust_controller.native_vm_reservations SET plan_digest=repeat('e',64)",
        ),
        (
            "osdeploy_runs",
            "UPDATE rust_controller.osdeploy_runs SET workflow_sha256=repeat('e',64)",
        ),
        (
            "osdeploy_runs",
            "UPDATE rust_controller.osdeploy_runs SET plan_canonical_json=replace(plan_canonical_json,'\"contract_version\":1','\"contract_version\":1,\"contract_version\":1')",
        ),
        (
            "commands",
            "DELETE FROM rust_controller.commands WHERE operation_id=(SELECT operation_id FROM rust_controller.osdeploy_operation_plans WHERE ordinal=15)",
        ),
        (
            "commands",
            "UPDATE rust_controller.commands SET payload_digest=repeat('e',64) WHERE operation_id=(SELECT operation_id FROM rust_controller.osdeploy_operation_plans WHERE ordinal=4)",
        ),
        (
            "commands",
            "UPDATE rust_controller.commands SET idempotency_key='wrong-key' WHERE operation_id=(SELECT operation_id FROM rust_controller.osdeploy_operation_plans WHERE ordinal=4)",
        ),
        (
            "commands",
            "INSERT INTO rust_controller.commands(idempotency_key,operation_id,payload_digest) SELECT 'extra-alias',operation_id,payload_digest FROM rust_controller.commands LIMIT 1",
        ),
        (
            "operations",
            "UPDATE rust_controller.operations SET workflow_kind='task_sequence' WHERE operation_key='osdeploy.clone.v1'",
        ),
        (
            "operations",
            "UPDATE rust_controller.operations SET operation_key='wrong-stage' WHERE operation_key='osdeploy.clone.v1'",
        ),
        (
            "operations",
            "UPDATE rust_controller.operations SET contract_version=2 WHERE operation_key='osdeploy.clone.v1'",
        ),
        (
            "operations",
            "UPDATE rust_controller.operations SET run_id='99999999-9999-4999-8999-999999999999' WHERE operation_key='osdeploy.clone.v1'",
        ),
    ] {
        let f = Fixture::new().await;
        let run = RunId::new();
        f.store.enqueue_osdeploy(run, &plan()).await.unwrap();
        if matches!(table, "operations" | "commands") {
            sqlx::query(query).execute(&f.pool).await.unwrap();
        } else {
            f.corrupt_immutable(table, query).await;
        }
        let before = f.snapshot().await;
        assert_eq!(
            f.other.load_osdeploy_registration(run).await,
            Err(OsDeployStoreError::Validation),
            "{query}"
        );
        let replay = f.other.enqueue_osdeploy(run, &plan()).await;
        assert_eq!(
            replay,
            Err(if query.contains("SET workflow_sha256") {
                OsDeployStoreError::Conflict
            } else {
                OsDeployStoreError::Validation
            }),
            "{query}"
        );
        assert_eq!(f.snapshot().await, before);
    }
}

#[tokio::test]
async fn reload_rejects_extra_operations_and_accepts_later_admitted_state() {
    let f = Fixture::new().await;
    let run = RunId::new();
    let ids = f.store.enqueue_osdeploy(run, &plan()).await.unwrap();
    sqlx::query(
        "UPDATE rust_controller.operations SET state='satisfied',revision=5 WHERE operation_id=$1",
    )
    .bind(ids.operation(OsDeployStage::Clone).as_uuid())
    .execute(&f.pool)
    .await
    .unwrap();
    assert_eq!(
        f.other.load_osdeploy_registration(run).await.unwrap().ids(),
        &ids
    );
    assert_eq!(f.other.enqueue_osdeploy(run, &plan()).await.unwrap(), ids);
    sqlx::query("INSERT INTO rust_controller.operations(operation_id,workflow_kind,run_id,operation_key,contract_version,state,revision) VALUES($1,'task_sequence',$2,'extra',1,'pending',0)").bind(OperationId::new().as_uuid()).bind(run.as_uuid()).execute(&f.pool).await.unwrap();
    assert_eq!(
        f.other.load_osdeploy_registration(run).await,
        Err(OsDeployStoreError::Validation)
    );
}

#[tokio::test]
async fn migration_preserves_history_and_repairs_each_missing_immutability_guard() {
    let f = Fixture::new().await;
    f.store
        .enqueue_osdeploy(RunId::new(), &plan())
        .await
        .unwrap();
    let before = f.snapshot().await;
    for (table, column) in [
        ("osdeploy_runs", "run_id"),
        ("osdeploy_operation_plans", "operation_id"),
        ("osdeploy_agent_reservations", "run_id"),
    ] {
        for trigger in ["osdeploy_no_mutation", "osdeploy_no_truncate"] {
            sqlx::query(&format!(
                "DROP TRIGGER {trigger} ON rust_controller.{table}"
            ))
            .execute(&f.pool)
            .await
            .unwrap();
            f.store.migrate().await.unwrap();
            let guards: i64 = sqlx::query_scalar("SELECT count(*) FROM pg_trigger WHERE tgrelid=$1::regclass AND tgname IN ('osdeploy_no_mutation','osdeploy_no_truncate') AND NOT tgisinternal AND tgenabled='O'").bind(format!("rust_controller.{table}")).fetch_one(&f.pool).await.unwrap();
            assert_eq!(guards, 2);
        }
        for query in [
            format!("UPDATE rust_controller.{table} SET {column}={column}"),
            format!("DELETE FROM rust_controller.{table}"),
            format!("TRUNCATE rust_controller.{table} CASCADE"),
        ] {
            let error = sqlx::query(&query).execute(&f.pool).await.unwrap_err();
            assert_eq!(
                error.as_database_error().unwrap().code().as_deref(),
                Some("23514")
            );
            assert_eq!(f.snapshot().await, before);
        }
    }
    f.store.migrate().await.unwrap();
    assert_eq!(f.snapshot().await, before);
}

#[tokio::test]
async fn schema_rejects_invalid_stage_local_shapes_even_without_mutation_trigger() {
    let f = Fixture::new().await;
    f.store
        .enqueue_osdeploy(RunId::new(), &plan())
        .await
        .unwrap();
    let before = f.snapshot().await;
    for expression in [
        "ordinal=16",
        "ordinal=-1",
        "stage='bogus'",
        "stage='start_disk'",
        "dependency_kind='satisfied'",
        "predecessor_id=operation_id",
        "pve_plan_sha256=NULL",
        "command_sha256='INVALID'",
        "pve_plan_sha256='INVALID'",
    ] {
        let mut tx = f.pool.begin().await.unwrap();
        sqlx::query("ALTER TABLE rust_controller.osdeploy_operation_plans DISABLE TRIGGER osdeploy_no_mutation").execute(&mut *tx).await.unwrap();
        let error = sqlx::query(&format!(
            "UPDATE rust_controller.osdeploy_operation_plans SET {expression} WHERE ordinal=0"
        ))
        .execute(&mut *tx)
        .await
        .unwrap_err();
        assert!(
            matches!(
                error.as_database_error().unwrap().code().as_deref(),
                Some("23514") | Some("23505")
            ),
            "{expression}"
        );
        tx.rollback().await.unwrap();
    }
    for query in [
        "UPDATE rust_controller.osdeploy_operation_plans SET pve_plan_sha256=repeat('a',64) WHERE ordinal=4",
        "UPDATE rust_controller.osdeploy_operation_plans SET predecessor_id=NULL WHERE ordinal=7",
        "UPDATE rust_controller.osdeploy_operation_plans SET dependency_kind='satisfied' WHERE ordinal=7",
    ] {
        let mut tx = f.pool.begin().await.unwrap();
        sqlx::query("ALTER TABLE rust_controller.osdeploy_operation_plans DISABLE TRIGGER osdeploy_no_mutation").execute(&mut *tx).await.unwrap();
        let error = sqlx::query(query).execute(&mut *tx).await.unwrap_err();
        assert_eq!(
            error.as_database_error().unwrap().code().as_deref(),
            Some("23514")
        );
        tx.rollback().await.unwrap();
    }
    assert_eq!(f.snapshot().await, before);
}

#[tokio::test]
async fn late_command_collision_rolls_back_the_entire_manifest() {
    let f = Fixture::new().await;
    let run = RunId::new();
    let collision = format!(
        "osdeploy:{}:{}",
        run.as_uuid(),
        OsDeployStage::VerifyOperational.operation_key()
    );
    f.store
        .append_command(
            OperationId::new(),
            &generic_command(RunId::new(), &collision),
        )
        .await
        .unwrap();
    let before = f.snapshot().await;
    assert_eq!(
        f.other.enqueue_osdeploy(run, &plan()).await,
        Err(OsDeployStoreError::Conflict)
    );
    assert_eq!(f.snapshot().await, before);
}

#[tokio::test]
async fn generic_and_typed_same_run_race_is_exclusive_in_both_lock_orders() {
    for typed_first in [false, true] {
        let f = Fixture::new().await;
        let run = RunId::new();
        let mut barrier = f.pool.begin().await.unwrap();
        sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1,0))")
            .bind(format!("native:run:{}", run.as_uuid()))
            .execute(&mut *barrier)
            .await
            .unwrap();
        let typed_store = f.store.clone();
        let generic_store = f.other.clone();
        let typed = async move { typed_store.enqueue_osdeploy(run, &plan()).await };
        let generic = async move {
            generic_store
                .append_command(OperationId::new(), &generic_command(run, "generic-race"))
                .await
        };
        let (typed_task, generic_task) = if typed_first {
            let first = tokio::spawn(typed);
            f.wait_run_lock_waiters(1).await;
            (first, tokio::spawn(generic))
        } else {
            let first = tokio::spawn(generic);
            f.wait_run_lock_waiters(1).await;
            (tokio::spawn(typed), first)
        };
        f.wait_run_lock_waiters(2).await;
        barrier.commit().await.unwrap();
        let typed = typed_task.await.unwrap();
        let generic = generic_task.await.unwrap();
        if typed_first {
            typed.unwrap();
            assert!(matches!(generic, Err(StoreError::TypedWorkflowRequired)));
            f.assert_fresh_counts().await;
        } else {
            generic.unwrap();
            assert_eq!(typed, Err(OsDeployStoreError::Conflict));
            let snapshot = f.snapshot().await;
            for table in ["operations", "commands", "operation_projection"] {
                assert_eq!(snapshot[table].as_array().unwrap().len(), 1);
            }
            for table in [
                "osdeploy_runs",
                "osdeploy_operation_plans",
                "osdeploy_agent_reservations",
                "native_vm_reservations",
                "attempts",
                "worker_leases",
                "journal_events",
                "outbox",
            ] {
                assert_eq!(snapshot[table], json!([]));
            }
        }
    }
}

#[tokio::test]
async fn historical_osdeploy_grants_cannot_use_any_generic_execution_path() {
    use controller_domain::ExecutionState;
    for started in [false, true] {
        let f = Fixture::new().await;
        let run = RunId::new();
        let operation = OperationId::new();
        let command = CommandEnvelope::new(
            "historical",
            SemanticOperationKey::new(WorkflowKind::SyntheticLongSleep, run, "historical", 1)
                .unwrap(),
            "a".repeat(64),
        )
        .unwrap();
        f.store.append_command(operation, &command).await.unwrap();
        let scheduler = f.scheduler();
        let grant = scheduler
            .claim_next(WorkflowKind::SyntheticLongSleep, 1)
            .await
            .unwrap()
            .unwrap();
        if started {
            scheduler.start(&grant).await.unwrap();
        }
        sqlx::query(
            "UPDATE rust_controller.operations SET workflow_kind='os_deploy' WHERE operation_id=$1",
        )
        .bind(operation.as_uuid())
        .execute(&f.pool)
        .await
        .unwrap();
        let before = f.snapshot().await;
        assert!(
            scheduler
                .claim_next(WorkflowKind::OsDeploy, 1)
                .await
                .unwrap()
                .is_none()
        );
        assert!(
            scheduler
                .claim_next_bound(WorkflowKind::OsDeploy, 1, 1, command.payload_digest())
                .await
                .unwrap()
                .is_none()
        );
        assert!(matches!(
            scheduler.start(&grant).await,
            Err(SchedulerError::PlanBindingMismatch)
        ));
        assert_eq!(f.snapshot().await, before);
        assert!(matches!(
            scheduler
                .start_bound(&grant, WorkflowKind::OsDeploy, 1, command.payload_digest())
                .await,
            Err(SchedulerError::PlanBindingMismatch)
        ));
        assert_eq!(f.snapshot().await, before);
        assert!(matches!(
            scheduler.request_cancel(operation).await,
            Err(SchedulerError::PlanBindingMismatch)
        ));
        assert_eq!(f.snapshot().await, before);
        assert!(matches!(
            scheduler.request_cancel_bound(&grant).await,
            Err(SchedulerError::PlanBindingMismatch)
        ));
        assert_eq!(f.snapshot().await, before);
        assert!(matches!(
            scheduler.finalize(&grant, ExecutionState::Satisfied).await,
            Err(SchedulerError::PlanBindingMismatch)
        ));
        assert_eq!(f.snapshot().await, before);
        assert!(matches!(
            scheduler.continuation(&grant).await,
            Err(SchedulerError::PlanBindingMismatch)
        ));
        assert_eq!(f.snapshot().await, before);
        assert!(matches!(
            scheduler.heartbeat(&grant).await,
            Err(SchedulerError::PlanBindingMismatch)
        ));
        assert_eq!(f.snapshot().await, before);
        assert!(matches!(
            f.other
                .append_command(OperationId::new(), &generic_command(run, "cannot-adopt"))
                .await,
            Err(StoreError::TypedWorkflowRequired)
        ));
        assert_eq!(f.snapshot().await, before);
        sqlx::query("UPDATE rust_controller.worker_leases SET acquired_at=clock_timestamp()-interval '3 seconds',heartbeat_at=clock_timestamp()-interval '2 seconds',lease_expires_at=clock_timestamp()-interval '1 second' WHERE operation_id=$1").bind(operation.as_uuid()).execute(&f.pool).await.unwrap();
        let before = f.snapshot().await;
        scheduler.reap_expired().await.unwrap();
        assert_eq!(f.snapshot().await, before);
    }
}

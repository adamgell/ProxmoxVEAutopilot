mod support;

use controller_domain::{EventId, ExecutionState, RunId, WorkflowKind};
use event_journal::{EventKind, JournalEvent, payload_digest};
use postgres_store::NativeStoreError;
use pve_port::*;
use serde_json::json;
use support::*;

#[tokio::test]
async fn snapshot_retains_latest_typed_evidence_after_generic_observations() {
    let f = Fixture::new().await;
    let (ids, grant, _, typed_event, typed_revision) = f.ready().await;
    let original = f.store.load_native_operation(ids.clone_id()).await.unwrap();
    let typed_facts = original.evidence().unwrap().1.clone();
    let mut revision = typed_revision;
    let mut generic_event = None;
    // The first observation is ordinary generic JSON. Later native-shaped but
    // invalid observations cross a decoder page without becoming authority.
    for index in 0..34 {
        let payload = if index == 0 {
            json!({"observation":"ordinary generic evidence"})
        } else {
            json!({"binding":{},"plan":{},"observation":index})
        };
        let event = JournalEvent::new(
            EventId::new(),
            ids.clone_id(),
            Some(grant.attempt_id()),
            revision + 1,
            format!("generic-observation-{index}"),
            payload_digest(&payload).unwrap(),
            EventKind::EvidenceRecorded,
            payload,
            chrono::Utc::now(),
        )
        .unwrap();
        f.store.append_event(revision, &event).await.unwrap();
        revision += 1;
        generic_event = Some(event.event_id());
        if index == 0 || index == 33 {
            let snapshot = f.other.load_native_operation(ids.clone_id()).await.unwrap();
            assert_eq!(snapshot.revision(), revision);
            assert_eq!(
                snapshot.evidence(),
                Some(&(typed_event, typed_facts.clone()))
            );
            assert_eq!(
                snapshot
                    .evidence()
                    .unwrap()
                    .1
                    .facts()
                    .binding
                    .evidence_fence(),
                (typed_revision - 1) as u64
            );
            assert_eq!(snapshot.state(), ExecutionState::Running);
        }
    }
    let before = f.counts().await;
    assert_eq!(
        f.scheduler()
            .decide_native(&grant, generic_event.unwrap(), revision)
            .await,
        Err(NativeStoreError::Validation)
    );
    assert_eq!(f.counts().await, before);
    let newer_facts = preflight(ids.run_id(), &grant, revision);
    let newer_event = f
        .store
        .record_native_evidence(ids.clone_id(), grant.attempt_id(), revision, &newer_facts)
        .await
        .unwrap();
    let snapshot = f.other.load_native_operation(ids.clone_id()).await.unwrap();
    assert_eq!(snapshot.revision(), revision + 1);
    assert_eq!(snapshot.evidence(), Some(&(newer_event, newer_facts)));
    assert_ne!(newer_event, typed_event);
}

#[tokio::test]
async fn command_helper_preserves_validation_before_database_access() {
    use controller_domain::{CommandEnvelope, OperationId, SemanticOperationKey};
    let f = Fixture::new().await;
    f.pool.close().await;
    for (version, hash) in [(1, "invalid".to_owned()), (32768, "a".repeat(64))] {
        let command = CommandEnvelope::new(
            "invalid-intake",
            SemanticOperationKey::new(
                WorkflowKind::SyntheticLongSleep,
                RunId::new(),
                "invalid",
                version,
            )
            .unwrap(),
            hash,
        )
        .unwrap();
        let result = f.store.append_command(OperationId::new(), &command).await;
        if version == 1 {
            assert!(matches!(
                result,
                Err(postgres_store::StoreError::InvalidPayloadDigest)
            ))
        } else {
            assert!(matches!(
                result,
                Err(postgres_store::StoreError::ContractVersionOutOfRange)
            ))
        }
    }
}

#[tokio::test]
async fn typed_configure_and_start_dispatches_use_owned_requests_and_receipts() {
    let f = Fixture::new().await;
    let (ids, clone_request) = f.satisfied_clone().await;
    let clone = f.store.load_native_operation(ids.clone_id()).await.unwrap();
    for operation in [ids.configure_id(), ids.start_id()] {
        let grant = f.start_step(operation).await;
        let snapshot = f.store.load_native_operation(operation).await.unwrap();
        let is_start = operation == ids.start_id();
        let facts = owned_evidence(&snapshot, &clone, is_start, false);
        let current = facts
            .facts()
            .target_config
            .as_ref()
            .unwrap()
            .result
            .as_ref()
            .unwrap();
        let bound = clone
            .evidence()
            .unwrap()
            .1
            .facts()
            .target_config
            .as_ref()
            .unwrap()
            .result
            .as_ref()
            .unwrap();
        let hash = if is_start {
            digest(
                &StartRequest::new(vm(), &clone_request, bound, current, chrono::Utc::now())
                    .unwrap(),
            )
        } else {
            digest(
                &ConfigureRequest::new(vm(), &clone_request, bound, current, chrono::Utc::now())
                    .unwrap(),
            )
        };
        let event = f
            .store
            .record_native_evidence(operation, grant.attempt_id(), snapshot.revision(), &facts)
            .await
            .unwrap();
        let scheduler = f.scheduler();
        let permit = scheduler
            .begin_native_dispatch(
                &grant,
                snapshot.revision() + 1,
                event,
                &hash,
                uuid::Uuid::now_v7(),
            )
            .await
            .unwrap();
        let receipt = if is_start {
            MutationReceipt::Task(
                Upid::parse("UPID:pve-test:00000001:00000002:00000003:qmstart:9010:proof@pve:")
                    .unwrap(),
            )
        } else {
            MutationReceipt::SynchronousAccepted
        };
        scheduler
            .record_native_receipt(&permit, &receipt)
            .await
            .unwrap();
        let snapshot = f.store.load_native_operation(operation).await.unwrap();
        let mut input = owned_evidence(&snapshot, &clone, true, is_start)
            .facts()
            .clone();
        if let MutationReceipt::Task(upid) = receipt {
            let t = chrono::Utc::now();
            input.task = Some(NativeRead::new(
                t,
                Ok(TaskStatus::new(upid, TaskState::CompleteSuccess, t)),
            ));
            input.collected_at = t;
            input.evaluated_at = t;
        }
        let event = f
            .store
            .record_native_evidence(
                operation,
                grant.attempt_id(),
                snapshot.revision(),
                &NativeEvidence::new(input).unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(
            scheduler
                .decide_native(&grant, event, snapshot.revision() + 1)
                .await
                .unwrap(),
            ExecutionState::Satisfied
        );
    }
    let counts = f.counts().await;
    assert_eq!(counts.dispatches, 3);
    assert_eq!(counts.receipts, 3);
    assert_eq!(counts.decisions, 3);
    assert_eq!(counts.events, counts.outbox);
}

#[tokio::test]
async fn old_generation_cannot_record_an_active_native_decision() {
    let f = Fixture::new().await;
    let (ids, grant, _, event, revision) = f.ready().await;
    let scheduler = f.scheduler();
    scheduler
        .transition_authority(postgres_store::ExecutorKind::Rust, "decision-fence")
        .await
        .unwrap();
    let before = f.counts().await;
    assert_eq!(
        scheduler.decide_native(&grant, event, revision).await,
        Err(NativeStoreError::FenceMismatch)
    );
    assert_eq!(f.counts().await, before);
    assert_eq!(
        f.store
            .load_native_operation(ids.clone_id())
            .await
            .unwrap()
            .state(),
        ExecutionState::Running
    );
}

#[tokio::test]
async fn dispatch_rechecks_expiry_after_a_blocked_durable_write() {
    let f = Fixture::new().await;
    let (ids, grant, request, event, revision) = f.ready().await;
    let before = f.counts().await;
    sqlx::query("UPDATE rust_controller.worker_leases SET lease_expires_at=clock_timestamp()+interval '1 second' WHERE operation_id=$1").bind(ids.clone_id().as_uuid()).execute(&f.pool).await.unwrap();
    let mut guard = f.pool.begin().await.unwrap();
    sqlx::query("LOCK TABLE rust_controller.journal_events IN SHARE MODE")
        .execute(&mut *guard)
        .await
        .unwrap();
    let scheduler = f.other_scheduler();
    let send = tokio::spawn(async move {
        scheduler
            .begin_native_dispatch(
                &grant,
                revision,
                event,
                &request.request_digest(),
                request.request_marker(),
            )
            .await
    });
    tokio::time::sleep(std::time::Duration::from_millis(1200)).await;
    guard.commit().await.unwrap();
    assert!(
        send.await.unwrap().is_err(),
        "expired transaction returned a send permit"
    );
    assert_eq!(f.counts().await, before);
}

#[tokio::test]
async fn foundation_database_upgrade_preserves_four_previous_workflows() {
    use controller_domain::{CommandEnvelope, OperationId, SemanticOperationKey};
    let f = Fixture::foundation().await;
    let mut operations = Vec::new();
    for (index, kind) in [
        WorkflowKind::CloudOsd,
        WorkflowKind::OsDeploy,
        WorkflowKind::TaskSequence,
        WorkflowKind::SyntheticLongSleep,
    ]
    .into_iter()
    .enumerate()
    {
        let id = OperationId::new();
        let command = CommandEnvelope::new(
            format!("foundation-{index}"),
            SemanticOperationKey::new(kind, RunId::new(), "foundation", 1).unwrap(),
            "a".repeat(64),
        )
        .unwrap();
        f.store.append_command(id, &command).await.unwrap();
        operations.push(id);
    }
    f.store.migrate().await.unwrap();
    f.store
        .enqueue_native_vm(RunId::new(), &vm())
        .await
        .unwrap();
    for id in operations {
        let projection = f.store.load_operation(id).await.unwrap().unwrap();
        assert_eq!(projection.state(), ExecutionState::Pending);
        assert_eq!(projection.revision(), 0);
    }
    assert_eq!(f.counts().await.operations, 7);
    assert_eq!(f.counts().await.commands, 7);
    assert_eq!(f.counts().await.plans, 3);
}

#[tokio::test]
async fn every_populated_native_table_rejects_update_delete_and_truncate() {
    let f = Fixture::new().await;
    let (ids, _) = f.satisfied_clone().await;
    f.scheduler().cancel_native_run(ids.run_id()).await.unwrap();
    let before = f.counts().await;
    for (table, column) in [
        ("native_vm_reservations", "run_id"),
        ("native_operation_plans", "operation_id"),
        ("native_dispatches", "operation_id"),
        ("native_receipts", "operation_id"),
        ("native_decisions", "operation_id"),
        ("native_run_cancellations", "run_id"),
    ] {
        for query in [
            format!("UPDATE rust_controller.{table} SET {column}={column}"),
            format!("DELETE FROM rust_controller.{table}"),
            format!("TRUNCATE rust_controller.{table} CASCADE"),
        ] {
            let error = sqlx::query(&query).execute(&f.pool).await.unwrap_err();
            assert_eq!(
                error.as_database_error().unwrap().code().as_deref(),
                Some("23514"),
                "{table}"
            );
        }
    }
    assert_eq!(f.counts().await, before);
}

#[tokio::test]
async fn native_source_and_command_binding_reject_substitution() {
    let f = Fixture::new().await;
    let (ids, grant, _, _, revision) = f.ready().await;
    let before = f.counts().await;
    let mut input = preflight(ids.run_id(), &grant, revision).facts().clone();
    input.source = NativeEvidenceSource::PveApi;
    assert_eq!(
        f.store
            .record_native_evidence(
                ids.clone_id(),
                grant.attempt_id(),
                revision,
                &NativeEvidence::new(input).unwrap()
            )
            .await,
        Err(NativeStoreError::Validation)
    );
    assert_eq!(f.counts().await, before);
    sqlx::query("UPDATE rust_controller.commands SET payload_digest=$2 WHERE operation_id=$1")
        .bind(ids.clone_id().as_uuid())
        .bind("a".repeat(64))
        .execute(&f.pool)
        .await
        .unwrap();
    assert!(matches!(
        f.store.load_native_operation(ids.clone_id()).await,
        Err(NativeStoreError::Validation)
    ));
    assert_eq!(
        f.scheduler()
            .start_native_bound(
                &grant,
                &digest(&NativeOperationPlan::new(NativeStep::Clone, vm()))
            )
            .await,
        Err(NativeStoreError::Validation)
    );
    assert_eq!(f.counts().await, before);
}

#[tokio::test]
async fn owned_configure_and_running_start_satisfy_without_dispatch() {
    let f = Fixture::new().await;
    let (ids, _) = f.satisfied_clone().await;
    let clone = f.store.load_native_operation(ids.clone_id()).await.unwrap();
    for (operation, running) in [(ids.configure_id(), false), (ids.start_id(), true)] {
        let grant = f.start_step(operation).await;
        let snapshot = f.store.load_native_operation(operation).await.unwrap();
        let facts = owned_evidence(&snapshot, &clone, true, running);
        let event = f
            .store
            .record_native_evidence(operation, grant.attempt_id(), snapshot.revision(), &facts)
            .await
            .unwrap();
        assert_eq!(
            f.scheduler()
                .decide_native(&grant, event, snapshot.revision() + 1)
                .await
                .unwrap(),
            ExecutionState::Satisfied
        );
        let snapshot = f.store.load_native_operation(operation).await.unwrap();
        assert!(snapshot.dispatch().is_none());
        assert!(snapshot.receipt().is_none());
        assert_eq!(
            snapshot.decision().unwrap().evaluation().reason,
            NativeReason::AlreadySatisfied
        );
    }
    assert_eq!(f.counts().await.dispatches, 1);
    assert_eq!(f.counts().await.receipts, 1);
    assert_eq!(f.counts().await.decisions, 3);
}

#[tokio::test]
async fn configure_response_loss_only_reconciles_after_unknown() {
    let f = Fixture::new().await;
    let (ids, request) = f.satisfied_clone().await;
    let clone = f.store.load_native_operation(ids.clone_id()).await.unwrap();
    let grant = f.start_step(ids.configure_id()).await;
    let snapshot = f
        .store
        .load_native_operation(ids.configure_id())
        .await
        .unwrap();
    let facts = owned_evidence(&snapshot, &clone, false, false);
    let bound = clone
        .evidence()
        .unwrap()
        .1
        .facts()
        .target_config
        .as_ref()
        .unwrap()
        .result
        .as_ref()
        .unwrap();
    let current = facts
        .facts()
        .target_config
        .as_ref()
        .unwrap()
        .result
        .as_ref()
        .unwrap();
    let configure =
        ConfigureRequest::new(vm(), &request, bound, current, chrono::Utc::now()).unwrap();
    let event = f
        .store
        .record_native_evidence(
            ids.configure_id(),
            grant.attempt_id(),
            snapshot.revision(),
            &facts,
        )
        .await
        .unwrap();
    f.scheduler()
        .begin_native_dispatch(
            &grant,
            snapshot.revision() + 1,
            event,
            &digest(&configure),
            uuid::Uuid::now_v7(),
        )
        .await
        .unwrap();
    let snapshot = f
        .store
        .load_native_operation(ids.configure_id())
        .await
        .unwrap();
    let facts = owned_evidence(&snapshot, &clone, true, false);
    let event = f
        .store
        .record_native_evidence(
            ids.configure_id(),
            grant.attempt_id(),
            snapshot.revision(),
            &facts,
        )
        .await
        .unwrap();
    assert_eq!(
        f.scheduler()
            .decide_native(&grant, event, snapshot.revision() + 1)
            .await
            .unwrap(),
        ExecutionState::Unknown
    );
    let snapshot = f
        .store
        .load_native_operation(ids.configure_id())
        .await
        .unwrap();
    let facts = owned_evidence(&snapshot, &clone, true, false);
    let event = f
        .store
        .record_native_evidence(
            ids.configure_id(),
            grant.attempt_id(),
            snapshot.revision(),
            &facts,
        )
        .await
        .unwrap();
    assert_eq!(
        f.other_scheduler()
            .reconcile_native_unknown(
                ids.configure_id(),
                grant.attempt_id(),
                event,
                snapshot.revision() + 1,
                &digest(snapshot.plan())
            )
            .await
            .unwrap(),
        ExecutionState::Satisfied
    );
    assert!(
        f.store
            .load_native_operation(ids.configure_id())
            .await
            .unwrap()
            .receipt()
            .is_none()
    );
    assert_eq!(f.counts().await.dispatches, 2);
    assert_eq!(f.counts().await.receipts, 1);
}

#[tokio::test]
async fn clone_without_persisted_receipt_cannot_satisfy_with_coherent_task_claims() {
    let f = Fixture::new().await;
    let (ids, grant, request, event, revision) = f.ready().await;
    f.scheduler()
        .begin_native_dispatch(
            &grant,
            revision,
            event,
            &request.request_digest(),
            request.request_marker(),
        )
        .await
        .unwrap();
    let snapshot = f.store.load_native_operation(ids.clone_id()).await.unwrap();
    let mut input = clone_outcome(&snapshot, &request, TaskState::CompleteSuccess)
        .facts()
        .clone();
    input.receipt = Some(NativeReceipt {
        binding: input.binding.clone(),
        accepted_at: chrono::Utc::now(),
        receipt: MutationReceipt::Task(clone_upid()),
    });
    input.collected_at = chrono::Utc::now();
    input.evaluated_at = input.collected_at;
    let event = f
        .store
        .record_native_evidence(
            ids.clone_id(),
            grant.attempt_id(),
            snapshot.revision(),
            &NativeEvidence::new(input).unwrap(),
        )
        .await
        .unwrap();
    assert_ne!(
        f.scheduler()
            .decide_native(&grant, event, snapshot.revision() + 1)
            .await
            .unwrap(),
        ExecutionState::Satisfied
    );
    assert_eq!(f.counts().await.receipts, 0);
    assert_eq!(f.counts().await.dispatches, 1);
}

#[tokio::test]
async fn native_reaper_waits_for_run_before_locking_operation() {
    let f = Fixture::new().await;
    let (ids, _, _, _, _) = f.ready().await;
    f.expire(ids.clone_id()).await;
    let mut guard = f.pool.begin().await.unwrap();
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1,0))")
        .bind(format!("native:run:{}", ids.run_id().as_uuid()))
        .execute(&mut *guard)
        .await
        .unwrap();
    let scheduler = f.other_scheduler();
    let mut reap = tokio::spawn(async move { scheduler.reap_expired().await });
    assert!(
        tokio::time::timeout(std::time::Duration::from_millis(200), &mut reap)
            .await
            .is_err(),
        "native reaper ignored the run lock"
    );
    let mut observer = f.pool.begin().await.unwrap();
    sqlx::query("SET LOCAL lock_timeout='500ms'")
        .execute(&mut *observer)
        .await
        .unwrap();
    sqlx::query(
        "SELECT operation_id FROM rust_controller.operations WHERE operation_id=$1 FOR UPDATE",
    )
    .bind(ids.clone_id().as_uuid())
    .fetch_one(&mut *observer)
    .await
    .unwrap();
    observer.commit().await.unwrap();
    guard.commit().await.unwrap();
    assert_eq!(reap.await.unwrap().unwrap().marked_unknown(), 1);
}

#[tokio::test]
async fn receipt_is_exact_durable_and_idempotent_after_expiry() {
    let f = Fixture::new().await;
    let (ids, grant, request, event, revision) = f.ready().await;
    let scheduler = f.scheduler();
    let permit = scheduler
        .begin_native_dispatch(
            &grant,
            revision,
            event,
            &request.request_digest(),
            request.request_marker(),
        )
        .await
        .unwrap();
    f.expire(ids.clone_id()).await;
    let receipt = MutationReceipt::Task(clone_upid());
    assert!(
        scheduler
            .record_native_receipt(&permit, &MutationReceipt::SynchronousAccepted)
            .await
            .is_err()
    );
    let wrong = MutationReceipt::Task(
        Upid::parse("UPID:pve-test:00000001:00000002:00000003:qmclone:9010:proof@pve:").unwrap(),
    );
    assert!(
        scheduler
            .record_native_receipt(&permit, &wrong)
            .await
            .is_err()
    );
    scheduler
        .record_native_receipt(&permit, &receipt)
        .await
        .unwrap();
    let first = f
        .store
        .load_native_operation(ids.clone_id())
        .await
        .unwrap()
        .receipt()
        .unwrap()
        .clone();
    f.other_scheduler()
        .record_native_receipt(&permit, &receipt)
        .await
        .unwrap();
    assert_eq!(
        *f.other
            .load_native_operation(ids.clone_id())
            .await
            .unwrap()
            .receipt()
            .unwrap(),
        first
    );
    assert_eq!(f.counts().await.receipts, 1);
    assert_eq!(f.counts().await.decisions, 0);
    assert!(!format!("{permit:?}").contains(&grant.lease_token().to_string()));
}

#[tokio::test]
async fn waiting_keeps_attempt_and_lease_then_satisfies_from_fresh_proof() {
    let f = Fixture::new().await;
    let (ids, grant, request, event, revision) = f.ready().await;
    let scheduler = f.scheduler();
    let permit = scheduler
        .begin_native_dispatch(
            &grant,
            revision,
            event,
            &request.request_digest(),
            request.request_marker(),
        )
        .await
        .unwrap();
    scheduler
        .record_native_receipt(&permit, &MutationReceipt::Task(clone_upid()))
        .await
        .unwrap();
    for _ in 0..2 {
        let snap = f.store.load_native_operation(ids.clone_id()).await.unwrap();
        let evidence = clone_outcome(&snap, &request, TaskState::Running);
        let event = f
            .store
            .record_native_evidence(
                ids.clone_id(),
                grant.attempt_id(),
                snap.revision(),
                &evidence,
            )
            .await
            .unwrap();
        assert_eq!(
            scheduler
                .decide_native(&grant, event, snap.revision() + 1)
                .await
                .unwrap(),
            ExecutionState::Waiting
        );
        let states:(String,Option<chrono::DateTime<chrono::Utc>>,i64)=sqlx::query_as("SELECT state,completed_at,(SELECT count(*) FROM rust_controller.worker_leases) FROM rust_controller.attempts WHERE attempt_id=$1").bind(grant.attempt_id().as_uuid()).fetch_one(&f.pool).await.unwrap();
        assert_eq!(states, ("waiting".into(), None, 1));
        assert_eq!(f.counts().await.decisions, 0);
        scheduler.heartbeat(&grant).await.unwrap();
        scheduler.continuation(&grant).await.unwrap();
    }
    let snap = f.store.load_native_operation(ids.clone_id()).await.unwrap();
    let evidence = clone_outcome(&snap, &request, TaskState::CompleteSuccess);
    let event = f
        .store
        .record_native_evidence(
            ids.clone_id(),
            grant.attempt_id(),
            snap.revision(),
            &evidence,
        )
        .await
        .unwrap();
    assert_eq!(
        scheduler
            .decide_native(&grant, event, snap.revision() + 1)
            .await
            .unwrap(),
        ExecutionState::Satisfied
    );
    assert_eq!(f.counts().await.decisions, 1);
    assert!(
        scheduler
            .claim_native_bound(
                ids.configure_id(),
                &digest(&NativeOperationPlan::new(NativeStep::Configure, vm())),
                1
            )
            .await
            .unwrap()
            .is_some()
    );
}

#[tokio::test]
async fn cancellation_before_claim_is_durable_and_idempotent() {
    let f = Fixture::new().await;
    let ids = f
        .store
        .enqueue_native_vm(RunId::new(), &vm())
        .await
        .unwrap();
    f.scheduler().cancel_native_run(ids.run_id()).await.unwrap();
    f.other_scheduler()
        .cancel_native_run(ids.run_id())
        .await
        .unwrap();
    let counts = f.counts().await;
    assert_eq!(counts.events, 1);
    assert_eq!(counts.outbox, 1);
    assert_eq!(counts.decisions, 0);
    assert_eq!(counts.dispatches, 0);
    let event: (Option<uuid::Uuid>, serde_json::Value) =
        sqlx::query_as("SELECT attempt_id,payload FROM rust_controller.journal_events")
            .fetch_one(&f.pool)
            .await
            .unwrap();
    assert_eq!(
        event,
        (
            None,
            json!({"action":"cancel_native_run","contract_version":1})
        )
    );
    for operation in [ids.clone_id(), ids.configure_id(), ids.start_id()] {
        let snapshot = f.store.load_native_operation(operation).await.unwrap();
        assert!(snapshot.cancelled());
        assert_eq!(snapshot.state(), ExecutionState::Pending);
        assert_eq!(
            f.scheduler()
                .claim_native_bound(operation, &digest(snapshot.plan()), 1)
                .await,
            Err(NativeStoreError::Cancelled)
        );
    }
    assert_eq!(f.counts().await, counts);
}

#[tokio::test]
async fn concurrent_cancel_and_dispatch_never_allow_downstream_send() {
    let f = Fixture::new().await;
    let (ids, grant, request, event, revision) = f.ready().await;
    let a = f.scheduler();
    let b = f.other_scheduler();
    let hash = request.request_digest();
    let (cancel, send) = tokio::join!(
        a.cancel_native_run(ids.run_id()),
        b.begin_native_dispatch(&grant, revision, event, &hash, request.request_marker())
    );
    cancel.unwrap();
    assert_eq!(f.counts().await.dispatches, i64::from(send.is_ok()));
    let snap = f.store.load_native_operation(ids.clone_id()).await.unwrap();
    assert_eq!(snap.state(), ExecutionState::Cancelling);
    assert!(snap.cancelled());
    assert_eq!(
        f.scheduler()
            .claim_native_bound(
                ids.configure_id(),
                &digest(&NativeOperationPlan::new(NativeStep::Configure, vm())),
                1
            )
            .await,
        Err(NativeStoreError::Cancelled)
    );
    f.expire(ids.clone_id()).await;
    assert_eq!(
        f.scheduler().reap_expired().await.unwrap().marked_unknown(),
        1
    );
    assert_eq!(
        f.store
            .load_native_operation(ids.clone_id())
            .await
            .unwrap()
            .state(),
        ExecutionState::Unknown
    );
    assert_eq!(f.counts().await.events, f.counts().await.outbox);
}

#[tokio::test]
async fn unknown_reconciliation_uses_current_generation_original_dispatch_and_receipt() {
    let f = Fixture::new().await;
    let (ids, grant, request, event, revision) = f.ready().await;
    let old = f.scheduler();
    let permit = old
        .begin_native_dispatch(
            &grant,
            revision,
            event,
            &request.request_digest(),
            request.request_marker(),
        )
        .await
        .unwrap();
    old.record_native_receipt(&permit, &MutationReceipt::Task(clone_upid()))
        .await
        .unwrap();
    old.cancel_native_run(ids.run_id()).await.unwrap();
    f.expire(ids.clone_id()).await;
    old.reap_expired().await.unwrap();
    old.transition_authority(postgres_store::ExecutorKind::Rust, "new-reader")
        .await
        .unwrap();
    let current = postgres_store::Scheduler::new(
        f.other.clone(),
        postgres_store::ExecutorKind::Rust,
        2,
        "new-reader",
    )
    .unwrap();
    let snap = f.store.load_native_operation(ids.clone_id()).await.unwrap();
    let evidence = clone_outcome(&snap, &request, TaskState::CompleteSuccess);
    let event = f
        .store
        .record_native_evidence(
            ids.clone_id(),
            grant.attempt_id(),
            snap.revision(),
            &evidence,
        )
        .await
        .unwrap();
    assert!(
        old.reconcile_native_unknown(
            ids.clone_id(),
            grant.attempt_id(),
            event,
            snap.revision() + 1,
            &digest(snap.plan())
        )
        .await
        .is_err()
    );
    assert_eq!(
        current
            .reconcile_native_unknown(
                ids.clone_id(),
                grant.attempt_id(),
                event,
                snap.revision() + 1,
                &digest(snap.plan())
            )
            .await
            .unwrap(),
        ExecutionState::Satisfied
    );
    let snap = f.store.load_native_operation(ids.clone_id()).await.unwrap();
    assert!(snap.cancelled());
    assert_eq!(snap.decision().unwrap().generation(), 2);
    assert_eq!(snap.dispatch().unwrap().generation(), 1);
    assert_eq!(f.counts().await.dispatches, 1);
    assert_eq!(
        current
            .claim_native_bound(
                ids.configure_id(),
                &digest(&NativeOperationPlan::new(NativeStep::Configure, vm())),
                1
            )
            .await,
        Err(NativeStoreError::Cancelled)
    );
}

#[tokio::test]
async fn typed_digest_and_evidence_anchor_are_independently_fenced() {
    let f = Fixture::new().await;
    let (ids, grant, request, event, revision) = f.ready().await;
    let before = f.counts().await;
    assert_eq!(
        f.scheduler()
            .begin_native_dispatch(
                &grant,
                revision,
                event,
                &"a".repeat(64),
                request.request_marker()
            )
            .await
            .unwrap_err(),
        NativeStoreError::FenceMismatch
    );
    assert_eq!(f.counts().await, before);
    let snapshot = f.store.load_native_operation(ids.clone_id()).await.unwrap();
    let original = snapshot.evidence().unwrap().1.clone();
    assert_eq!(
        f.store
            .record_native_evidence(ids.clone_id(), grant.attempt_id(), revision - 1, &original)
            .await
            .unwrap(),
        event
    );
    let next = preflight(ids.run_id(), &grant, revision);
    f.store
        .record_native_evidence(ids.clone_id(), grant.attempt_id(), revision, &next)
        .await
        .unwrap();
    // The old event keeps its original anchor; current CAS is a separate input.
    assert!(
        f.scheduler()
            .begin_native_dispatch(
                &grant,
                revision,
                event,
                &request.request_digest(),
                request.request_marker()
            )
            .await
            .is_err()
    );
    f.scheduler()
        .begin_native_dispatch(
            &grant,
            revision + 1,
            event,
            &request.request_digest(),
            request.request_marker(),
        )
        .await
        .unwrap();
    assert_eq!(f.counts().await.dispatches, 1);
}

#[tokio::test]
async fn migration_upgrade_and_all_native_tables_are_immutable() {
    let f = Fixture::new().await;
    // Reapplication must preserve the foundation rows and all workflow values.
    f.store.migrate().await.unwrap();
    let ids = f
        .store
        .enqueue_native_vm(RunId::new(), &vm())
        .await
        .unwrap();
    for table in [
        "native_vm_reservations",
        "native_operation_plans",
        "native_dispatches",
        "native_receipts",
        "native_decisions",
        "native_run_cancellations",
    ] {
        assert!(
            sqlx::query(&format!("TRUNCATE rust_controller.{table} CASCADE"))
                .execute(&f.pool)
                .await
                .is_err()
        );
    }
    assert!(
        sqlx::query("UPDATE rust_controller.native_vm_reservations SET mac='02:00:00:00:00:09'")
            .execute(&f.pool)
            .await
            .is_err()
    );
    assert!(
        sqlx::query("DELETE FROM rust_controller.native_operation_plans WHERE operation_id=$1")
            .bind(ids.start_id().as_uuid())
            .execute(&f.pool)
            .await
            .is_err()
    );
    assert_eq!(f.counts().await, Counts::intake());
}

#[tokio::test]
async fn intake_same_digest_is_idempotent() {
    let f = Fixture::new().await;
    let run = RunId::new();
    let plan = vm();
    let (a, b) = tokio::join!(
        f.store.enqueue_native_vm(run, &plan),
        f.other.enqueue_native_vm(run, &plan)
    );
    assert_eq!(a.unwrap(), b.unwrap());
    assert_eq!(f.counts().await, Counts::intake());
}

#[tokio::test]
async fn intake_changed_digest_conflicts() {
    let f = Fixture::new().await;
    let run = RunId::new();
    f.store.enqueue_native_vm(run, &vm()).await.unwrap();
    let mut wire = serde_json::to_value(vm()).unwrap();
    wire["cores"] = json!(4);
    let changed = serde_json::from_value(wire).unwrap();
    assert_eq!(
        f.other.enqueue_native_vm(run, &changed).await,
        Err(NativeStoreError::Conflict)
    );
    assert_eq!(f.counts().await, Counts::intake());
}

#[tokio::test]
async fn concurrent_identity_reservation_has_one_winner() {
    for collision in ["target_vmid", "uuid", "mac"] {
        let f = Fixture::new().await;
        let first = vm();
        let mut second = serde_json::to_value(&first).unwrap();
        second["target_vmid"] = json!(9011);
        second["uuid"] = json!("7f2504e0-4f89-41d3-9a0c-0305e82c3301");
        second["mac"] = json!("02:00:00:00:90:11");
        second[collision] = serde_json::to_value(&first).unwrap()[collision].clone();
        let second = serde_json::from_value(second).unwrap();
        let (a, b) = tokio::join!(
            f.store.enqueue_native_vm(RunId::new(), &first),
            f.other.enqueue_native_vm(RunId::new(), &second)
        );
        assert_eq!(usize::from(a.is_ok()) + usize::from(b.is_ok()), 1);
        assert_eq!(a.err().or(b.err()), Some(NativeStoreError::Conflict));
        assert_eq!(f.counts().await, Counts::intake());
    }
}

#[tokio::test]
async fn dispatch_is_single_use() {
    let f = Fixture::new().await;
    let (ids, grant, request, event, revision) = f.ready().await;
    let a = f.scheduler();
    let b = f.other_scheduler();
    let hash = request.request_digest();
    let (a, b) = tokio::join!(
        a.begin_native_dispatch(&grant, revision, event, &hash, request.request_marker()),
        b.begin_native_dispatch(&grant, revision, event, &hash, request.request_marker())
    );
    assert_eq!(usize::from(a.is_ok()) + usize::from(b.is_ok()), 1);
    let snap = f.store.load_native_operation(ids.clone_id()).await.unwrap();
    assert!(snap.dispatch().is_some());
    assert_eq!(f.counts().await.dispatches, 1);
    assert_eq!(snap.revision(), revision + 1);
    assert_eq!(f.counts().await.events, f.counts().await.outbox);
}

#[tokio::test]
async fn cancelled_or_stale_grant_cannot_dispatch() {
    for cancel in [true, false] {
        let f = Fixture::new().await;
        let (ids, grant, request, event, revision) = f.ready().await;
        let scheduler = f.scheduler();
        if cancel {
            scheduler.cancel_native_run(ids.run_id()).await.unwrap();
        } else {
            scheduler
                .transition_authority(postgres_store::ExecutorKind::Rust, "new-generation")
                .await
                .unwrap();
        }
        let before = f.counts().await;
        assert!(
            scheduler
                .begin_native_dispatch(
                    &grant,
                    revision,
                    event,
                    &request.request_digest(),
                    request.request_marker()
                )
                .await
                .is_err()
        );
        assert_eq!(f.counts().await, before);
        assert_eq!(before.dispatches, 0);
    }
}

#[tokio::test]
async fn generic_scheduler_cannot_execute_native() {
    let f = Fixture::new().await;
    let ids = f
        .store
        .enqueue_native_vm(RunId::new(), &vm())
        .await
        .unwrap();
    let scheduler = f.scheduler();
    assert!(
        scheduler
            .claim_next(WorkflowKind::NativePveVmBoot, 1)
            .await
            .unwrap()
            .is_none()
    );
    assert!(
        scheduler
            .claim_next_bound(
                WorkflowKind::NativePveVmBoot,
                1,
                1,
                &digest(&NativeOperationPlan::new(NativeStep::Clone, vm()))
            )
            .await
            .unwrap()
            .is_none()
    );
    let grant = scheduler
        .claim_native_bound(
            ids.clone_id(),
            &digest(&NativeOperationPlan::new(NativeStep::Clone, vm())),
            1,
        )
        .await
        .unwrap()
        .unwrap();
    let before = f.counts().await;
    assert!(scheduler.start(&grant).await.is_err());
    assert!(
        scheduler
            .start_bound(
                &grant,
                WorkflowKind::NativePveVmBoot,
                1,
                &digest(&NativeOperationPlan::new(NativeStep::Clone, vm()))
            )
            .await
            .is_err()
    );
    assert!(
        scheduler
            .finalize(&grant, ExecutionState::Satisfied)
            .await
            .is_err()
    );
    assert!(scheduler.request_cancel(ids.clone_id()).await.is_err());
    assert_eq!(f.counts().await, before);
}

#[tokio::test]
async fn crash_after_dispatch_never_grants_second_send() {
    let f = Fixture::new().await;
    let (ids, grant, request, event, revision) = f.ready().await;
    let scheduler = f.scheduler();
    scheduler
        .begin_native_dispatch(
            &grant,
            revision,
            event,
            &request.request_digest(),
            request.request_marker(),
        )
        .await
        .unwrap();
    let recovered = f.other.load_native_operation(ids.clone_id()).await.unwrap();
    assert!(recovered.dispatch().is_some());
    assert!(recovered.receipt().is_none());
    assert!(
        f.other_scheduler()
            .begin_native_dispatch(
                &grant,
                recovered.revision(),
                event,
                &request.request_digest(),
                request.request_marker()
            )
            .await
            .is_err()
    );
    assert_eq!(f.counts().await.dispatches, 1);
}

#[tokio::test]
async fn public_append_remains_evidence_only() {
    let f = Fixture::new().await;
    let (ids, grant, _, _, revision) = f.ready().await;
    let before = f.counts().await;
    for kind in [
        EventKind::DecisionRecorded,
        EventKind::AttemptStarted,
        EventKind::ExecutionStateChanged(ExecutionState::Satisfied),
    ] {
        let payload = json!({"state":"satisfied"});
        let event = JournalEvent::new(
            EventId::new(),
            ids.clone_id(),
            Some(grant.attempt_id()),
            revision + 1,
            "forged",
            payload_digest(&payload).unwrap(),
            kind,
            payload,
            chrono::Utc::now(),
        )
        .unwrap();
        assert!(f.store.append_event(revision, &event).await.is_err());
    }
    assert_eq!(f.counts().await, before);
}

#[tokio::test]
async fn native_decision_checks_revision_and_generation() {
    let f = Fixture::new().await;
    let (ids, grant, _, _, revision) = f.ready().await;
    let mut input = preflight(ids.run_id(), &grant, revision).facts().clone();
    input.node = None;
    let event = f
        .store
        .record_native_evidence(
            ids.clone_id(),
            grant.attempt_id(),
            revision,
            &NativeEvidence::new(input).unwrap(),
        )
        .await
        .unwrap();
    let before = f.counts().await;
    let a = f.scheduler();
    let b = f.other_scheduler();
    let (a, b) = tokio::join!(
        a.decide_native(&grant, event, revision + 1),
        b.decide_native(&grant, event, revision + 1)
    );
    assert_eq!(usize::from(a.is_ok()) + usize::from(b.is_ok()), 1);
    assert_eq!(a.ok().or(b.ok()), Some(ExecutionState::Unknown));
    assert_eq!(f.counts().await.decisions, before.decisions + 1);
    assert_eq!(f.counts().await.events, before.events + 2);
    assert_eq!(f.counts().await.events, f.counts().await.outbox);
}

#[tokio::test]
async fn late_success_preserves_newer_conflict() {
    let f = Fixture::new().await;
    let (ids, grant, request, event, revision) = f.ready().await;
    let scheduler = f.scheduler();
    let permit = scheduler
        .begin_native_dispatch(
            &grant,
            revision,
            event,
            &request.request_digest(),
            request.request_marker(),
        )
        .await
        .unwrap();
    scheduler
        .record_native_receipt(&permit, &MutationReceipt::Task(clone_upid()))
        .await
        .unwrap();
    let snapshot = f.store.load_native_operation(ids.clone_id()).await.unwrap();
    let revision = snapshot.revision();
    let mut input = clone_outcome(&snapshot, &request, TaskState::CompleteSuccess)
        .facts()
        .clone();
    let mut config = serde_json::to_value(
        input
            .target_config
            .as_ref()
            .unwrap()
            .result
            .as_ref()
            .unwrap(),
    )
    .unwrap();
    config["fake_clone_provenance"]["request_digest"] = json!("a".repeat(64));
    input.target_config.as_mut().unwrap().result = Ok(serde_json::from_value(config).unwrap());
    input.collected_at = chrono::Utc::now();
    input.evaluated_at = input.collected_at;
    let event = f
        .store
        .record_native_evidence(
            ids.clone_id(),
            grant.attempt_id(),
            revision,
            &NativeEvidence::new(input).unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        f.scheduler()
            .decide_native(&grant, event, revision + 1)
            .await
            .unwrap(),
        ExecutionState::Conflicted
    );
    let snap = f.store.load_native_operation(ids.clone_id()).await.unwrap();
    let facts = clone_outcome(&snap, &request, TaskState::CompleteSuccess);
    let late = f
        .store
        .record_native_evidence(ids.clone_id(), grant.attempt_id(), snap.revision(), &facts)
        .await
        .unwrap();
    assert!(
        f.scheduler()
            .decide_native(&grant, late, snap.revision() + 1)
            .await
            .is_err()
    );
    assert_eq!(
        f.store
            .load_native_operation(ids.clone_id())
            .await
            .unwrap()
            .state(),
        ExecutionState::Conflicted
    );
    assert_eq!(f.counts().await.decisions, 1);
}
#[tokio::test]
async fn historical_ownership_uses_decision_evidence_after_later_typed_facts() {
    let f = support::Fixture::new().await;
    let pending = f
        .store
        .enqueue_native_vm(controller_domain::RunId::new(), &support::vm())
        .await
        .unwrap();
    assert!(
        f.store
            .load_native_clone_ownership(pending.clone_id())
            .await
            .is_err()
    );
    // The same reservation cannot be reused for another run; use the pending run.
    let grant = f.start_step(pending.clone_id()).await;
    let snap = f
        .store
        .load_native_operation(pending.clone_id())
        .await
        .unwrap();
    let facts = support::preflight(pending.run_id(), &grant, snap.revision());
    let event = f
        .store
        .record_native_evidence(
            pending.clone_id(),
            grant.attempt_id(),
            snap.revision(),
            &facts,
        )
        .await
        .unwrap();
    let request = pve_port::CloneRequest::new(support::vm(), pending.clone_id());
    let scheduler = f.scheduler();
    let permit = scheduler
        .begin_native_dispatch(
            &grant,
            snap.revision() + 1,
            event,
            &request.request_digest(),
            request.request_marker(),
        )
        .await
        .unwrap();
    scheduler
        .record_native_receipt(
            &permit,
            &pve_port::MutationReceipt::Task(support::clone_upid()),
        )
        .await
        .unwrap();
    let snap = f
        .store
        .load_native_operation(pending.clone_id())
        .await
        .unwrap();
    let facts = support::clone_outcome(&snap, &request, pve_port::TaskState::CompleteSuccess);
    let event = f
        .store
        .record_native_evidence(
            pending.clone_id(),
            grant.attempt_id(),
            snap.revision(),
            &facts,
        )
        .await
        .unwrap();
    scheduler
        .decide_native(&grant, event, snap.revision() + 1)
        .await
        .unwrap();
    let original = f
        .store
        .load_native_clone_ownership(pending.configure_id())
        .await
        .unwrap();
    let snap = f
        .store
        .load_native_operation(pending.clone_id())
        .await
        .unwrap();
    let later = support::clone_outcome(&snap, &request, pve_port::TaskState::Running);
    f.store
        .record_native_evidence(
            pending.clone_id(),
            grant.attempt_id(),
            snap.revision(),
            &later,
        )
        .await
        .unwrap();
    assert_eq!(
        f.store
            .load_native_clone_ownership(pending.start_id())
            .await
            .unwrap(),
        original
    );
    assert_eq!(original.proof(), &facts);
    assert!(
        f.store
            .load_native_clone_ownership(controller_domain::OperationId::new())
            .await
            .is_err()
    );
}

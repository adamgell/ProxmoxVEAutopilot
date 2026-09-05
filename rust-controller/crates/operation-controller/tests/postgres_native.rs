use controller_domain::{ExecutionState, RunId};
use operation_controller::{NativeController, NativeProgress};
use pve_port::*;
#[path = "../proof_support/mod.rs"]
mod support;
use support::*;

// Catches omitted orchestration, duplicate submissions, and loss of stable intake identity.
#[tokio::test]
async fn full_slice_duplicate_intake_has_three_exact_sends() {
    let f = Fixture::new().await;
    let run = RunId::new();
    let ids = f.store.enqueue_native_vm(run, &plan()).await.unwrap();
    assert_eq!(ids, f.store.enqueue_native_vm(run, &plan()).await.unwrap());
    let mut controller = f.controller("first");
    for id in [ids.clone_id(), ids.configure_id(), ids.start_id()] {
        assert_eq!(
            controller.run_once(id).await.unwrap(),
            NativeProgress::Decided(ExecutionState::Satisfied)
        );
        assert_eq!(
            controller.run_once(id).await.unwrap(),
            NativeProgress::Decided(ExecutionState::Satisfied)
        );
    }
    f.assert_success().await;
    f.cleanup().unwrap();
}

// Catches adoption of live work on restart and repeated sends during owned Waiting.
#[tokio::test]
async fn delayed_completion_retains_grant_but_restart_cannot_adopt() {
    let f = Fixture::new().await;
    let ids = f.enqueue().await;
    f.fake
        .enqueue_outcome(NativeStep::Clone, FakeMutationOutcome::AcceptedTaskDelayed)
        .unwrap();
    let mut owner = f.controller("owner");
    assert_eq!(
        owner.run_once(ids.clone_id()).await.unwrap(),
        NativeProgress::Waiting
    );
    let before = f.store.load_native_operation(ids.clone_id()).await.unwrap();
    assert_eq!(
        owner.run_once(ids.clone_id()).await.unwrap(),
        NativeProgress::Waiting
    );
    let mut restarted = f.controller("owner");
    f.fake.complete_pending().unwrap();
    assert_eq!(
        restarted.run_once(ids.clone_id()).await.unwrap(),
        NativeProgress::Waiting
    );
    assert_eq!(
        owner.run_once(ids.clone_id()).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Satisfied)
    );
    let after = f.store.load_native_operation(ids.clone_id()).await.unwrap();
    assert_eq!(before.attempt_id(), after.attempt_id());
    assert_eq!(f.fake.recorded_requests().len(), 1);
}

#[tokio::test]
async fn competing_workers_never_double_send() {
    let f = Fixture::new().await;
    let ids = f.enqueue().await;
    let mut first = f.controller("first");
    let mut second = f.controller("second");
    for id in [ids.clone_id(), ids.configure_id(), ids.start_id()] {
        let (a, b) = tokio::join!(first.run_once(id), second.run_once(id));
        assert!(a.is_ok() && b.is_ok());
    }
    f.assert_success().await;
}

#[tokio::test]
async fn clone_response_loss_stays_unknown_and_never_advances() {
    let f = Fixture::new().await;
    let ids = f.enqueue().await;
    f.fake
        .enqueue_outcome(NativeStep::Clone, FakeMutationOutcome::AppliedResponseLost)
        .unwrap();
    let mut c = f.controller("first");
    assert_eq!(
        c.run_once(ids.clone_id()).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Unknown)
    );
    assert_eq!(
        c.reconcile_once(ids.clone_id()).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Unknown)
    );
    assert_eq!(
        c.run_once(ids.configure_id()).await.unwrap(),
        NativeProgress::Idle
    );
    assert_eq!(
        c.run_once(ids.start_id()).await.unwrap(),
        NativeProgress::Idle
    );
    assert_eq!(f.fake.recorded_requests().len(), 1);
}

#[tokio::test]
async fn configure_response_loss_requires_fresh_reconciliation_without_replay() {
    let f = Fixture::new().await;
    let ids = f.enqueue().await;
    let mut c = f.controller("first");
    c.run_once(ids.clone_id()).await.unwrap();
    f.fake
        .enqueue_outcome(
            NativeStep::Configure,
            FakeMutationOutcome::AppliedResponseLost,
        )
        .unwrap();
    assert_eq!(
        c.run_once(ids.configure_id()).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Unknown)
    );
    assert!(
        f.store
            .load_native_operation(ids.configure_id())
            .await
            .unwrap()
            .receipt()
            .is_none()
    );
    assert_eq!(
        c.reconcile_once(ids.configure_id()).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Satisfied)
    );
    c.run_once(ids.start_id()).await.unwrap();
    f.assert_success().await;
}

#[tokio::test]
async fn cancelled_clone_is_observed_but_run_never_advances() {
    let f = Fixture::new().await;
    let ids = f.enqueue().await;
    f.fake
        .enqueue_outcome(NativeStep::Clone, FakeMutationOutcome::AcceptedTaskDelayed)
        .unwrap();
    let mut c = f.controller("first");
    c.run_once(ids.clone_id()).await.unwrap();
    f.scheduler("first")
        .cancel_native_run(ids.run_id())
        .await
        .unwrap();
    c.run_once(ids.clone_id()).await.unwrap();
    f.fake.complete_pending().unwrap();
    assert_eq!(
        c.reconcile_once(ids.clone_id()).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Satisfied)
    );
    assert_eq!(
        c.run_once(ids.configure_id()).await.unwrap(),
        NativeProgress::Idle
    );
    assert!(
        f.store
            .load_native_operation(ids.clone_id())
            .await
            .unwrap()
            .cancelled()
    );
    assert_eq!(f.fake.recorded_requests().len(), 1);
}

// Catches send without fresh source facts and failure to bound retries.
#[tokio::test]
async fn stale_then_fresh_config_retries_before_single_dispatch() {
    let f = Fixture::new().await;
    let ids = f.enqueue().await;
    let mut value = serde_json::to_value(source()).unwrap();
    value["observed_at"] = serde_json::json!(chrono::Utc::now() - chrono::Duration::seconds(60));
    f.fake.enqueue_config_read(
        plan().source_vmid(),
        FakeConfigRead::Snapshot(serde_json::from_value(value).unwrap()),
    );
    let mut c = f.controller("first");
    assert_eq!(
        c.run_once(ids.clone_id()).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Satisfied)
    );
    assert_eq!(f.fake.recorded_requests().len(), 1);
}

#[tokio::test]
async fn three_stale_config_reads_exhaust_bound_without_fourth_read_or_send() {
    let f = Fixture::new().await;
    let ids = f.enqueue().await;
    let mut value = serde_json::to_value(source()).unwrap();
    value["observed_at"] = serde_json::json!(chrono::Utc::now() - chrono::Duration::seconds(60));
    for _ in 0..3 {
        f.fake.enqueue_config_read(
            plan().source_vmid(),
            FakeConfigRead::Snapshot(serde_json::from_value(value.clone()).unwrap()),
        );
    }
    let mut c = f.controller("first");
    assert_eq!(
        c.run_once(ids.clone_id()).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Unknown)
    );
    assert!(f.fake.recorded_requests().is_empty());
}

#[tokio::test]
async fn unauthorized_and_malformed_preflight_never_send() {
    for (error, state) in [
        (PveReadError::Unauthorized, ExecutionState::Blocked),
        (PveReadError::InvalidResponse, ExecutionState::Unknown),
    ] {
        let f = Fixture::new().await;
        let ids = f.enqueue().await;
        for _ in 0..6 {
            f.fake
                .enqueue_config_read(plan().source_vmid(), FakeConfigRead::Error(error.clone()));
        }
        let mut c = f.controller("first");
        assert_eq!(
            c.run_once(ids.clone_id()).await.unwrap(),
            NativeProgress::Decided(state)
        );
        assert!(f.fake.recorded_requests().is_empty());
    }
}

#[tokio::test]
async fn crash_after_dispatch_before_submission_is_unknown_without_replay() {
    let f = Fixture::new().await;
    let ids = f.enqueue().await;
    let pause = f.fake.pause_next_submission();
    let mut c = f.controller("first");
    let clone_id = ids.clone_id();
    let task = tokio::spawn(async move { c.run_once(clone_id).await });
    pause.entered().await;
    let snap = f.store.load_native_operation(ids.clone_id()).await.unwrap();
    assert!(snap.dispatch().is_some());
    assert!(snap.receipt().is_none());
    task.abort();
    let _ = task.await;
    assert!(f.fake.recorded_requests().is_empty());
    let mut restart = f.controller("first");
    assert_eq!(
        restart.run_once(snap.operation_id()).await.unwrap(),
        NativeProgress::Waiting
    );
    expire(&f, snap.operation_id(), false).await;
    assert_eq!(
        restart.run_once(snap.operation_id()).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Unknown)
    );
    assert_eq!(
        restart.reconcile_once(snap.operation_id()).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Unknown)
    );
    assert!(f.fake.recorded_requests().is_empty());
}

#[tokio::test]
async fn crash_after_receipt_reloads_original_acceptance_and_never_replays() {
    let f = Fixture::new().await;
    let ids = f.enqueue().await;
    let submission = f.fake.pause_next_submission();
    let mut c = f.controller("first");
    let clone_id = ids.clone_id();
    let task = tokio::spawn(async move { c.run_once(clone_id).await });
    submission.entered().await;
    let observation = f.fake.pause_next_config_read(plan().source_vmid());
    submission.release();
    observation.entered().await;
    let receipt = f
        .store
        .load_native_operation(clone_id)
        .await
        .unwrap()
        .receipt()
        .unwrap()
        .clone();
    task.abort();
    let _ = task.await;
    let mut restart = f.controller("second");
    assert_eq!(
        restart.run_once(clone_id).await.unwrap(),
        NativeProgress::Waiting
    );
    expire(&f, clone_id, false).await;
    assert_eq!(
        restart.run_once(clone_id).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Satisfied)
    );
    let snap = f.store.load_native_operation(clone_id).await.unwrap();
    assert_eq!(
        snap.evidence().unwrap().1.facts().receipt.as_ref(),
        Some(&receipt)
    );
    assert_eq!(f.fake.recorded_requests().len(), 1);
}

#[tokio::test]
async fn delayed_start_after_deadline_reconciles_without_new_attempt() {
    let f = Fixture::new().await;
    let ids = f.enqueue().await;
    let mut c = f.controller("first");
    c.run_once(ids.clone_id()).await.unwrap();
    c.run_once(ids.configure_id()).await.unwrap();
    f.fake
        .enqueue_outcome(NativeStep::Start, FakeMutationOutcome::AcceptedTaskDelayed)
        .unwrap();
    assert_eq!(
        c.run_once(ids.start_id()).await.unwrap(),
        NativeProgress::Waiting
    );
    let before = f.store.load_native_operation(ids.start_id()).await.unwrap();
    expire(&f, ids.start_id(), true).await;
    assert_eq!(
        c.run_once(ids.start_id()).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Unknown)
    );
    assert_no_live_work(&f, ids.start_id()).await;
    f.fake.complete_pending().unwrap();
    assert_eq!(
        c.reconcile_once(ids.start_id()).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Satisfied)
    );
    assert_eq!(
        f.store
            .load_native_operation(ids.start_id())
            .await
            .unwrap()
            .attempt_id(),
        before.attempt_id()
    );
    f.assert_success().await;
}

#[tokio::test]
async fn unknown_clone_with_running_task_retains_facts_without_active_waiting() {
    let f = Fixture::new().await;
    let ids = f.enqueue().await;
    let mut c = f.controller("first");
    f.fake
        .enqueue_outcome(NativeStep::Clone, FakeMutationOutcome::AcceptedTaskDelayed)
        .unwrap();
    c.run_once(ids.clone_id()).await.unwrap();
    let attempt = f
        .store
        .load_native_operation(ids.clone_id())
        .await
        .unwrap()
        .attempt_id();
    expire(&f, ids.clone_id(), true).await;
    assert_eq!(
        c.run_once(ids.clone_id()).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Unknown)
    );
    let before = f.store.load_native_operation(ids.clone_id()).await.unwrap();
    assert_no_live_work(&f, ids.clone_id()).await;
    assert_eq!(
        c.reconcile_once(ids.clone_id()).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Unknown)
    );
    let after = f.store.load_native_operation(ids.clone_id()).await.unwrap();
    assert!(after.revision() > before.revision());
    assert_eq!(after.attempt_id(), attempt);
    assert_eq!(after.attempt_state(), Some(ExecutionState::Unknown));
    assert_eq!(
        after.dispatch().unwrap().dispatched_at(),
        before.dispatch().unwrap().dispatched_at()
    );
    assert_eq!(
        after.decision().map(|d| d.revision()),
        before.decision().map(|d| d.revision())
    );
    assert_no_live_work(&f, ids.clone_id()).await;
    f.fake.complete_pending().unwrap();
    assert_eq!(
        c.reconcile_once(ids.clone_id()).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Satisfied)
    );
    assert_eq!(f.fake.recorded_requests().len(), 1);
}

// A recovered clone may outlive the original infrastructure snapshot freshness.
#[tokio::test]
async fn recovered_clone_continues_configure_and_start_with_aged_infrastructure() {
    let f = Fixture::new().await;
    let ids = f.enqueue().await;
    f.fake
        .enqueue_outcome(NativeStep::Clone, FakeMutationOutcome::AcceptedTaskDelayed)
        .unwrap();
    let mut first = f.controller("first");
    assert_eq!(
        first.run_once(ids.clone_id()).await.unwrap(),
        NativeProgress::Waiting
    );
    expire(&f, ids.clone_id(), true).await;
    assert_eq!(
        first.run_once(ids.clone_id()).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Unknown)
    );
    drop(first);
    // Model the elapsed seed age without sleeping through the freshness horizon.
    let p = plan();
    let aged = chrono::Utc::now() - chrono::Duration::seconds(60);
    f.fake.set_node_status(
        NodeStatus::from_wire(p.node().clone(), serde_json::json!({"uptime":123}), aged).unwrap(),
    );
    f.fake.set_storage_status(
        StorageStatus::from_wire(
            p.node().clone(),
            p.storage().clone(),
            serde_json::json!({"active":1,"enabled":1,"content":"images","avail":34359738368_u64}),
            aged,
        )
        .unwrap(),
    );
    f.fake.set_bridges(
        BridgeInventory::from_wire(
            p.node().clone(),
            serde_json::json!([{"type":"bridge","iface":"vmbr0","active":1}]),
            aged,
        )
        .unwrap(),
    );
    f.fake.complete_pending().unwrap();
    let mut recovered = f.controller("recovered");
    assert_eq!(
        recovered.reconcile_once(ids.clone_id()).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Satisfied)
    );
    assert_no_live_work(&f, ids.clone_id()).await;
    for id in [ids.configure_id(), ids.start_id()] {
        assert_eq!(
            recovered.run_once(id).await.unwrap(),
            NativeProgress::Decided(ExecutionState::Satisfied)
        );
    }
    f.assert_success().await;
    f.cleanup().unwrap();
}
async fn assert_no_live_work(f: &Fixture, id: controller_domain::OperationId) {
    let (leases,attempts,dispatches):(i64,i64,i64)=sqlx::query_as("SELECT (SELECT count(*) FROM rust_controller.worker_leases WHERE operation_id=$1),(SELECT count(*) FROM rust_controller.attempts WHERE operation_id=$1),(SELECT count(*) FROM rust_controller.native_dispatches WHERE operation_id=$1)").bind(id.as_uuid()).fetch_one(&f.pool).await.unwrap();
    assert_eq!((leases, attempts, dispatches), (0, 1, 1));
}

#[tokio::test]
async fn authority_flip_and_expiry_before_dispatch_prevent_send() {
    for flip in [false, true] {
        let f = Fixture::new().await;
        let ids = f.enqueue().await;
        let pause = f.fake.pause_next_config_read(plan().source_vmid());
        let mut c = f.controller("first");
        let id = ids.clone_id();
        let task = tokio::spawn(async move { c.run_once(id).await });
        pause.entered().await;
        if flip {
            f.scheduler("admin")
                .transition_authority(postgres_store::ExecutorKind::Rust, "local-proof-flip")
                .await
                .unwrap();
        } else {
            expire(&f, id, false).await;
        }
        pause.release();
        assert!(task.await.unwrap().is_err());
        assert!(
            f.store
                .load_native_operation(id)
                .await
                .unwrap()
                .dispatch()
                .is_none()
        );
        assert!(f.fake.recorded_requests().is_empty());
    }
}

// The real fence cannot atomically cover fake acceptance after the last check.
#[tokio::test]
async fn authority_flip_in_last_check_send_window_preserves_receipt_for_current_reconciler() {
    let f = Fixture::new().await;
    let ids = f.enqueue().await;
    let pause = f.fake.pause_next_submission();
    let mut c = f.controller("first");
    let id = ids.clone_id();
    let task = tokio::spawn(async move { c.run_once(id).await });
    pause.entered().await;
    f.scheduler("admin")
        .transition_authority(postgres_store::ExecutorKind::Rust, "local-proof-flip")
        .await
        .unwrap();
    pause.release();
    assert!(task.await.unwrap().is_err());
    let snap = f.store.load_native_operation(id).await.unwrap();
    assert!(snap.receipt().is_some());
    assert_eq!(
        snap.evidence().unwrap().1.facts().receipt.as_ref(),
        snap.receipt()
    );
    assert_ne!(snap.state(), ExecutionState::Satisfied);
    assert_eq!(f.fake.recorded_requests().len(), 1);
    expire(&f, id, false).await;
    let current = postgres_store::Scheduler::new(
        f.store.clone(),
        postgres_store::ExecutorKind::Rust,
        2,
        "new",
    )
    .unwrap();
    let mut c = NativeController::new(f.store.clone(), current, f.fake.clone());
    assert_eq!(
        c.run_once(id).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Satisfied)
    );
    assert_eq!(f.fake.recorded_requests().len(), 1);
}

#[tokio::test]
async fn cancellation_before_start_sends_no_new_step() {
    let f = Fixture::new().await;
    let ids = f.enqueue().await;
    let mut c = f.controller("first");
    c.run_once(ids.clone_id()).await.unwrap();
    c.run_once(ids.configure_id()).await.unwrap();
    f.scheduler("admin")
        .cancel_native_run(ids.run_id())
        .await
        .unwrap();
    assert_eq!(
        c.run_once(ids.start_id()).await.unwrap(),
        NativeProgress::Idle
    );
    assert_eq!(f.fake.recorded_requests().len(), 2);
}

async fn expire(f: &Fixture, id: controller_domain::OperationId, deadline: bool) {
    if deadline {
        sqlx::query("UPDATE rust_controller.worker_leases SET acquired_at=clock_timestamp()-interval '3 seconds', heartbeat_at=clock_timestamp()-interval '2 seconds',deadline_at=clock_timestamp()-interval '1 second',lease_expires_at=clock_timestamp()-interval '1 second' WHERE operation_id=$1").bind(id.as_uuid()).execute(&f.pool).await.unwrap();
        sqlx::query("UPDATE rust_controller.attempts SET started_at=clock_timestamp()-interval '3 seconds',deadline_at=clock_timestamp()-interval '1 second' WHERE operation_id=$1").bind(id.as_uuid()).execute(&f.pool).await.unwrap();
    } else {
        sqlx::query("UPDATE rust_controller.worker_leases SET acquired_at=clock_timestamp()-interval '3 seconds',heartbeat_at=clock_timestamp()-interval '2 seconds',lease_expires_at=clock_timestamp()-interval '1 second' WHERE operation_id=$1").bind(id.as_uuid()).execute(&f.pool).await.unwrap();
    }
}

#[tokio::test]
async fn committed_dispatch_continuation_rechecks_cancel_authority_and_expiry() {
    for fault in ["cancel", "authority", "expiry", "timeout"] {
        let f = Fixture::new().await;
        let ids = f.enqueue().await;
        let pause = f
            .fake
            .pause_controller_checkpoint(FakeControllerCheckpoint::DispatchCommitted);
        let mut c = f.controller("first");
        let id = ids.clone_id();
        let task = tokio::spawn(async move { c.run_once(id).await });
        pause.entered().await;
        assert!(
            f.store
                .load_native_operation(id)
                .await
                .unwrap()
                .dispatch()
                .is_some()
        );
        match fault {
            "cancel" => f
                .scheduler("admin")
                .cancel_native_run(ids.run_id())
                .await
                .unwrap(),
            "authority" => {
                f.scheduler("admin")
                    .transition_authority(postgres_store::ExecutorKind::Rust, "local-checkpoint")
                    .await
                    .unwrap();
            }
            "expiry" => expire(&f, id, false).await,
            _ => {}
        }
        if fault != "timeout" {
            pause.release();
        }
        let result = tokio::time::timeout(std::time::Duration::from_secs(5), task)
            .await
            .unwrap()
            .unwrap();
        if matches!(fault, "cancel" | "timeout") {
            assert_eq!(
                result.unwrap(),
                NativeProgress::Decided(ExecutionState::Unknown)
            );
        } else {
            assert!(result.is_err());
        }
        assert!(f.fake.recorded_requests().is_empty());
        assert!(
            f.store
                .load_native_operation(id)
                .await
                .unwrap()
                .receipt()
                .is_none()
        );
    }
}

#[tokio::test]
async fn mutation_errors_are_journaled_as_fixed_classifications() {
    let f = Fixture::new().await;
    let ids = f.enqueue().await;
    f.fake
        .enqueue_outcome(
            NativeStep::Clone,
            FakeMutationOutcome::Rejected(PveWriteError::Unauthorized),
        )
        .unwrap();
    let mut c = f.controller("first");
    assert_eq!(
        c.run_once(ids.clone_id()).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Unknown)
    );
    let count:i64=sqlx::query_scalar("SELECT count(*) FROM rust_controller.journal_events WHERE payload->>'mutation_error'='unauthorized'").fetch_one(&f.pool).await.unwrap();
    assert_eq!(count, 1);
    assert_eq!(f.fake.recorded_requests().len(), 1);
}

#[tokio::test]
async fn occupied_foreign_vmid_is_preserved_without_send() {
    let f = Fixture::new().await;
    let ids = f.enqueue().await;
    let foreign = foreign_config();
    f.fake
        .insert_vm(foreign.clone(), PowerState::Stopped)
        .unwrap();
    let mut c = f.controller("first");
    assert_eq!(
        c.run_once(ids.clone_id()).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Conflicted)
    );
    assert!(f.fake.recorded_requests().is_empty());
    assert_config_preserved(&f, &foreign).await;
}

#[tokio::test]
async fn changed_owned_identity_disk_or_missing_marker_never_configures_foreign_vm() {
    for field in ["uuid", "mac", "boot_disk", "fake_clone_provenance"] {
        let f = Fixture::new().await;
        let ids = f.enqueue().await;
        let mut c = f.controller("first");
        c.run_once(ids.clone_id()).await.unwrap();
        let current = f
            .fake
            .native_vm_config(plan().node(), plan().target_vmid())
            .await
            .unwrap();
        let mut wire = serde_json::to_value(current).unwrap();
        match field {
            "uuid" => wire[field] = serde_json::json!("5f2504e0-4f89-41d3-9a0c-0305e82c3301"),
            "mac" => wire[field] = serde_json::json!("02:00:00:00:88:88"),
            "boot_disk" => wire[field]["volume"] = serde_json::json!("vm-9010-disk-9"),
            _ => wire[field] = serde_json::Value::Null,
        }
        let changed: NativeVmConfig = serde_json::from_value(wire).unwrap();
        f.fake
            .replace_vm(changed.clone(), PowerState::Stopped)
            .unwrap();
        let result = c.run_once(ids.configure_id()).await.unwrap();
        assert!(
            matches!(
                result,
                NativeProgress::Decided(ExecutionState::Conflicted | ExecutionState::Unknown)
            ),
            "{field}: {result:?}"
        );
        assert_eq!(f.fake.recorded_requests().len(), 1);
        assert_config_preserved(&f, &changed).await;
    }
}

#[tokio::test]
async fn lock_or_digest_change_in_send_window_is_observed_without_resend() {
    for field in ["locked", "digest"] {
        let f = Fixture::new().await;
        let ids = f.enqueue().await;
        let mut c = f.controller("first");
        c.run_once(ids.clone_id()).await.unwrap();
        let pause = f.fake.pause_next_submission();
        let id = ids.configure_id();
        let task = tokio::spawn(async move { c.run_once(id).await });
        pause.entered().await;
        let mut wire = serde_json::to_value(
            f.fake
                .native_vm_config(plan().node(), plan().target_vmid())
                .await
                .unwrap(),
        )
        .unwrap();
        wire[field] = if field == "locked" {
            serde_json::json!(true)
        } else {
            serde_json::json!("foreign-digest")
        };
        let changed: NativeVmConfig = serde_json::from_value(wire).unwrap();
        f.fake
            .replace_vm(changed.clone(), PowerState::Stopped)
            .unwrap();
        pause.release();
        let result = task.await.unwrap().unwrap();
        assert!(matches!(
            result,
            NativeProgress::Decided(ExecutionState::Unknown | ExecutionState::Conflicted)
        ));
        let mut restart = f.controller("second");
        restart.run_once(id).await.unwrap();
        assert_eq!(f.fake.recorded_requests().len(), 2);
        assert_config_preserved(&f, &changed).await;
    }
}

#[tokio::test]
async fn complete_identity_coverage_is_bounded_and_fails_closed() {
    let f = Fixture::new().await;
    let ids = f.enqueue().await;
    // Thirty-three distinct resources exceed the single collection's explicit bound.
    for number in 9100..9132 {
        let mut wire = serde_json::to_value(source()).unwrap();
        wire["vmid"] = serde_json::json!(number);
        wire["boot_disk"]["volume"] = serde_json::json!(format!("vm-{number}-disk-0"));
        f.fake
            .insert_vm(serde_json::from_value(wire).unwrap(), PowerState::Stopped)
            .unwrap();
    }
    let mut c = f.controller("first");
    assert_eq!(
        c.run_once(ids.clone_id()).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Unknown)
    );
    assert!(f.fake.recorded_requests().is_empty());
    let snap = f.store.load_native_operation(ids.clone_id()).await.unwrap();
    assert_eq!(
        snap.decision().unwrap().evaluation().reason,
        NativeReason::IncompleteIdentityCoverage
    );
}

#[tokio::test]
async fn late_success_evidence_is_retained_after_conflicting_decision() {
    let f = Fixture::new().await;
    let ids = f.enqueue().await;
    let mut c = f.controller("first");
    c.run_once(ids.clone_id()).await.unwrap();
    c.run_once(ids.configure_id()).await.unwrap();
    f.fake
        .enqueue_outcome(NativeStep::Start, FakeMutationOutcome::AcceptedTaskDelayed)
        .unwrap();
    c.run_once(ids.start_id()).await.unwrap();
    let original = f
        .fake
        .native_vm_config(plan().node(), plan().target_vmid())
        .await
        .unwrap();
    let mut wire = serde_json::to_value(&original).unwrap();
    wire["uuid"] = serde_json::json!("5f2504e0-4f89-41d3-9a0c-0305e82c3301");
    f.fake
        .replace_vm(serde_json::from_value(wire).unwrap(), PowerState::Stopped)
        .unwrap();
    assert_eq!(
        c.run_once(ids.start_id()).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Conflicted)
    );
    let before = f.store.load_native_operation(ids.start_id()).await.unwrap();
    f.fake.replace_vm(original, PowerState::Stopped).unwrap();
    f.fake.complete_pending().unwrap();
    assert_eq!(
        c.reconcile_once(ids.start_id()).await.unwrap(),
        NativeProgress::Decided(ExecutionState::Conflicted)
    );
    let after = f.store.load_native_operation(ids.start_id()).await.unwrap();
    assert!(after.revision() > before.revision());
    assert_eq!(
        after.decision().unwrap().revision(),
        before.decision().unwrap().revision()
    );
    assert_eq!(
        after
            .evidence()
            .unwrap()
            .1
            .facts()
            .task
            .as_ref()
            .unwrap()
            .result
            .as_ref()
            .unwrap()
            .state(),
        TaskState::CompleteSuccess
    );
    assert_eq!(f.fake.recorded_requests().len(), 3);
}

fn foreign_config() -> NativeVmConfig {
    let mut wire = serde_json::to_value(source()).unwrap();
    wire["vmid"] = serde_json::json!(9010);
    wire["name"] = serde_json::json!("native-proof-9010");
    wire["template"] = serde_json::json!(false);
    wire["boot_disk"]["volume"] = serde_json::json!("vm-9010-disk-0");
    serde_json::from_value(wire).unwrap()
}
async fn assert_config_preserved(f: &Fixture, config: &NativeVmConfig) {
    let current = f
        .fake
        .native_vm_config(config.node(), config.vmid())
        .await
        .unwrap();
    let mut wire = serde_json::to_value(config).unwrap();
    wire["observed_at"] = serde_json::json!(current.observed_at());
    assert_eq!(serde_json::to_value(current).unwrap(), wire);
}

#[tokio::test]
async fn blocked_database_does_not_make_run_once_unbounded() {
    let f = Fixture::new().await;
    let ids = f.enqueue().await;
    let mut lock = f.pool.begin().await.unwrap();
    sqlx::query("LOCK TABLE rust_controller.orchestration_authority IN ACCESS EXCLUSIVE MODE")
        .execute(&mut *lock)
        .await
        .unwrap();
    let mut c = f.controller("first");
    let result = tokio::time::timeout(
        std::time::Duration::from_secs(25),
        c.run_once(ids.clone_id()),
    )
    .await;
    lock.rollback().await.unwrap();
    assert!(
        result
            .expect("run_once must return within its own bounded call")
            .is_err()
    );
    assert!(f.fake.recorded_requests().is_empty());
    assert_eq!(
        f.store
            .load_native_operation(ids.clone_id())
            .await
            .unwrap()
            .state(),
        ExecutionState::Pending
    );
}

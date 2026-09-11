mod support;
use ansible_adapter::{AdapterEvent, AdapterRegistry, AdapterRunner, CompletionReason};
use controller_domain::ExecutionState;
use scheduler::{ExecutorKind, Scheduler};
use std::{path::PathBuf, time::Duration};
use support::{Fixture, plan};
use tokio::sync::watch;

fn registry() -> AdapterRegistry {
    AdapterRegistry::local(&PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..")).unwrap()
}

#[tokio::test]
async fn preexisting_cancel_cannot_mutate_a_different_plan_operation() {
    // Break caught: cancel shortcut bypassing the command fingerprint binding.
    let fixture = Fixture::new().await;
    let grant = fixture.grant(&plan(0)).await;
    let operation = grant.operation_id();
    let (_cancel, receiver) = watch::channel(true);
    let report = AdapterRunner::new(fixture.scheduler.clone())
        .run(registry().validate(&plan(1)).unwrap(), grant, receiver)
        .await
        .unwrap();
    assert!(!report.persisted);
    assert_eq!(
        fixture
            .store
            .load_operation(operation)
            .await
            .unwrap()
            .unwrap()
            .state(),
        ExecutionState::Leased
    );
    assert!(!report.events.contains(&AdapterEvent::Started));
}

#[tokio::test]
async fn approved_native_playbook_exits_zero_and_fenced_attempt_is_satisfied() {
    // Break caught: isolated Ansible cannot actually run the unchanged baseline.
    let fixture = Fixture::new().await;
    let plan = plan(0);
    let grant = fixture.grant(&plan).await;
    let operation_id = grant.operation_id();
    let (_cancel, receiver) = watch::channel(false);
    let report = AdapterRunner::new(fixture.scheduler.clone())
        .run(registry().validate(&plan).unwrap(), grant, receiver)
        .await
        .unwrap();
    assert_eq!(report.reason, CompletionReason::Exited, "{report:?}");
    assert_eq!(report.state, ExecutionState::Satisfied, "{report:?}");
    assert!(report.persisted);
    assert!(
        report
            .events
            .contains(&AdapterEvent::Exited { code: Some(0) })
    );
    assert_eq!(
        fixture
            .store
            .load_operation(operation_id)
            .await
            .unwrap()
            .unwrap()
            .state(),
        ExecutionState::Satisfied
    );
    let payloads: Vec<serde_json::Value> = sqlx::query_scalar(
        "SELECT payload FROM rust_controller.journal_events WHERE operation_id = $1",
    )
    .bind(operation_id.as_uuid())
    .fetch_all(&fixture.pool)
    .await
    .unwrap();
    assert!(!format!("{report:?}{payloads:?}").contains("secret-canary"));
}

#[tokio::test]
async fn native_cancellation_finishes_unknown_within_ten_seconds() {
    // Break caught: control cancellation failing to kill the native process group.
    let fixture = Fixture::new().await;
    let plan = plan(20);
    let grant = fixture.grant(&plan).await;
    let (cancel, receiver) = watch::channel(false);
    let run = tokio::spawn(AdapterRunner::new(fixture.scheduler.clone()).run(
        registry().validate(&plan).unwrap(),
        grant,
        receiver,
    ));
    tokio::time::sleep(Duration::from_secs(2)).await;
    let began = std::time::Instant::now();
    cancel.send(true).unwrap();
    let report = tokio::time::timeout(Duration::from_secs(10), run)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert_eq!(report.reason, CompletionReason::Cancelled);
    assert_eq!(report.state, ExecutionState::Unknown);
    assert!(report.persisted);
    assert!(began.elapsed() < Duration::from_secs(10));
}

#[tokio::test]
async fn native_authority_flip_leaves_recovery_to_current_authority_reaper() {
    // Break caught: stale worker finalizes after authority changes mid-process.
    let fixture = Fixture::new().await;
    let plan = plan(20);
    let grant = fixture.grant(&plan).await;
    let operation = grant.operation_id();
    let (_cancel, receiver) = watch::channel(false);
    let run = tokio::spawn(AdapterRunner::new(fixture.scheduler.clone()).run(
        registry().validate(&plan).unwrap(),
        grant,
        receiver,
    ));
    tokio::time::sleep(Duration::from_secs(2)).await;
    fixture
        .scheduler
        .transition_authority(ExecutorKind::Python, "local-test-authority-flip")
        .await
        .unwrap();
    let report = tokio::time::timeout(Duration::from_secs(10), run)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert_eq!(report.reason, CompletionReason::AuthorityLost);
    assert_eq!(report.state, ExecutionState::Unknown);
    assert!(!report.persisted);
    assert_eq!(
        fixture
            .store
            .load_operation(operation)
            .await
            .unwrap()
            .unwrap()
            .state(),
        ExecutionState::Running
    );
    // Expire using only this disposable test DB; the stale runner has no such API.
    sqlx::query("UPDATE rust_controller.worker_leases SET acquired_at=clock_timestamp()-interval '3 seconds', heartbeat_at=clock_timestamp()-interval '2 seconds', lease_expires_at=clock_timestamp()-interval '1 second' WHERE operation_id=$1").bind(operation.as_uuid()).execute(&fixture.pool).await.unwrap();
    let current = Scheduler::new(
        fixture.store.clone(),
        ExecutorKind::Python,
        2,
        "current-reaper",
    )
    .unwrap();
    assert_eq!(current.reap_expired().await.unwrap().marked_unknown(), 1);
    assert_eq!(
        fixture
            .store
            .load_operation(operation)
            .await
            .unwrap()
            .unwrap()
            .state(),
        ExecutionState::Unknown
    );
}

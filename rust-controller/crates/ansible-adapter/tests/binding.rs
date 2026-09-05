mod support;
use controller_domain::{ExecutionState, WorkflowKind};
use scheduler::SchedulerError;
use support::{Fixture, plan};

#[tokio::test]
async fn atomic_start_rejects_other_plan_and_preserves_unstarted_attempt() {
    // Break caught: valid plan for A being launched under operation B's lease.
    let fixture = Fixture::new().await;
    let original = plan(0);
    let grant = fixture.grant(&original).await;
    let other = plan(1);
    for (kind, version, fingerprint) in [
        (
            WorkflowKind::SyntheticLongSleep,
            1,
            other.fingerprint().unwrap(),
        ),
        (
            WorkflowKind::SyntheticLongSleep,
            2,
            original.fingerprint().unwrap(),
        ),
        (WorkflowKind::OsDeploy, 1, original.fingerprint().unwrap()),
    ] {
        assert!(matches!(
            fixture
                .scheduler
                .start_bound(&grant, kind, version, fingerprint.as_hex())
                .await,
            Err(SchedulerError::PlanBindingMismatch)
        ));
    }
    assert_eq!(
        fixture
            .store
            .load_operation(grant.operation_id())
            .await
            .unwrap()
            .unwrap()
            .state(),
        ExecutionState::Leased
    );
    let starts: i64 = sqlx::query_scalar("SELECT count(*) FROM rust_controller.journal_events WHERE operation_id = $1 AND semantic_key LIKE 'scheduler:running:%'")
        .bind(grant.operation_id().as_uuid()).fetch_one(&fixture.pool).await.unwrap();
    assert_eq!(starts, 0);
    assert_eq!(
        fixture
            .scheduler
            .start_bound(
                &grant,
                WorkflowKind::SyntheticLongSleep,
                1,
                original.fingerprint().unwrap().as_hex()
            )
            .await
            .unwrap(),
        ExecutionState::Running
    );
    assert!(
        fixture
            .scheduler
            .start_bound(
                &grant,
                WorkflowKind::SyntheticLongSleep,
                1,
                original.fingerprint().unwrap().as_hex()
            )
            .await
            .is_err()
    );
}

#[tokio::test]
async fn cancellation_checks_lease_owner_before_changing_operation() {
    // Break caught: old/different worker cancelling another worker's live attempt.
    let fixture = Fixture::new().await;
    let grant = fixture.grant(&plan(0)).await;
    let foreign = scheduler::Scheduler::new(
        fixture.store.clone(),
        scheduler::ExecutorKind::Rust,
        1,
        "foreign-worker",
    )
    .unwrap();
    assert!(foreign.request_cancel_bound(&grant).await.is_err());
    assert_eq!(
        fixture
            .store
            .load_operation(grant.operation_id())
            .await
            .unwrap()
            .unwrap()
            .state(),
        ExecutionState::Leased
    );
    assert_eq!(
        fixture
            .scheduler
            .request_cancel_bound(&grant)
            .await
            .unwrap(),
        ExecutionState::Cancelling
    );
    assert!(matches!(
        fixture
            .scheduler
            .start_bound(
                &grant,
                WorkflowKind::SyntheticLongSleep,
                1,
                plan(1).fingerprint().unwrap().as_hex()
            )
            .await,
        Err(SchedulerError::PlanBindingMismatch)
    ));
    assert!(matches!(
        fixture
            .scheduler
            .start_bound(
                &grant,
                WorkflowKind::SyntheticLongSleep,
                1,
                plan(0).fingerprint().unwrap().as_hex()
            )
            .await,
        Err(SchedulerError::CancellationRequested)
    ));
}

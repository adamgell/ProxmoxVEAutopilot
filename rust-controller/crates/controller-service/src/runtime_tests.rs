use super::*;
use axum::{
    body::{Body, to_bytes},
    http::Request,
};
use tower::ServiceExt;
mod support {
    include!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../ansible-adapter/tests/support/mod.rs"
    ));
}

async fn response_after(
    result: std::result::Result<AdapterResult, tokio::task::JoinError>,
    store: PgStore,
) -> (u16, serde_json::Value) {
    let mut progress = Progress::default();
    record_completion(&mut progress, result);
    // A later successful database sweep must not erase unexpected failure.
    progress.succeeded();
    let state = HealthState {
        store,
        progress: Arc::new(RwLock::new(progress)),
        mode: "adapter",
        generation: 1,
        adapter_versions: vec![SYNTHETIC_LONG_SLEEP_V1.adapter_identity],
    };
    ready_response(state).await
}

async fn ready_response(state: HealthState) -> (u16, serde_json::Value) {
    let response = crate::health::router(state)
        .oneshot(Request::get("/readyz").body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = response.status().as_u16();
    let bytes = to_bytes(response.into_body(), 16384).await.unwrap();
    (status, serde_json::from_slice(&bytes).unwrap())
}

#[tokio::test]
async fn sweep_validation_fault_survives_successful_reads_and_stops_later_claims() {
    use controller_domain::{CommandEnvelope, OperationId, RunId, SemanticOperationKey};
    let fixture = support::Fixture::new().await;
    let plan = support::plan(0);
    for index in 0..2 {
        let command = CommandEnvelope::new(
            format!("sweep-validation-{index}"),
            SemanticOperationKey::new(
                WorkflowKind::SyntheticLongSleep,
                RunId::new(),
                format!("sweep-validation-{index}"),
                1,
            )
            .unwrap(),
            plan.fingerprint().unwrap().as_hex(),
        )
        .unwrap();
        fixture
            .store
            .append_command(OperationId::new(), &command)
            .await
            .unwrap();
    }
    let root = tempfile::tempdir().unwrap();
    let playbook = root
        .path()
        .join(SYNTHETIC_LONG_SLEEP_V1.playbook_relative_path);
    std::fs::create_dir_all(playbook.parent().unwrap()).unwrap();
    std::fs::copy(
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../..")
            .join(SYNTHETIC_LONG_SLEEP_V1.playbook_relative_path),
        &playbook,
    )
    .unwrap();
    let registry = AdapterRegistry::local(root.path()).unwrap();
    registry.validate(&plan).unwrap(); // The same successful startup validation.
    std::fs::remove_file(playbook).unwrap(); // Break before post-claim validation.
    let state = HealthState {
        store: fixture.store.clone(),
        progress: Arc::new(RwLock::new(Progress::default())),
        mode: "adapter",
        generation: 1,
        adapter_versions: vec![SYNTHETIC_LONG_SLEEP_V1.adapter_identity],
    };
    let work = AdapterWork {
        registry,
        plan,
        scheduler: fixture.scheduler.clone(),
        cap: 1,
    };
    let (stop, receiver) = watch::channel(false);
    let worker = tokio::spawn(sweeps(
        state.clone(),
        fixture.pool.clone(),
        Some(work),
        receiver,
    ));
    tokio::time::timeout(Duration::from_secs(6), async {
        while state.progress.read().await.successful_sweeps == 0 {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .unwrap();
    // The next sweep has valid DB reads and a cap-full queue; it must not heal
    // the earlier local contract failure merely because it cannot claim.
    let (status, body) = ready_response(state.clone()).await;
    let previous_sweeps = state.progress.read().await.successful_sweeps;
    // Free real capacity through the current-authority reaper. The test only
    // advances the disposable lease clock; production remains a 30-second lease.
    sqlx::query("UPDATE rust_controller.worker_leases SET lease_expires_at=clock_timestamp()-interval '1 second'")
        .execute(&fixture.pool).await.unwrap();
    tokio::time::timeout(Duration::from_secs(6), async {
        while state.progress.read().await.successful_sweeps < previous_sweeps + 2 {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .unwrap();
    stop.send(true).unwrap();
    worker.await.unwrap();
    let attempts: i64 = sqlx::query_scalar("SELECT count(*) FROM rust_controller.attempts")
        .fetch_one(&fixture.pool)
        .await
        .unwrap();
    let pending: i64 =
        sqlx::query_scalar("SELECT count(*) FROM rust_controller.operations WHERE state='pending'")
            .fetch_one(&fixture.pool)
            .await
            .unwrap();
    let started: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM rust_controller.journal_events WHERE event_kind='attempt_started'",
    )
    .fetch_one(&fixture.pool)
    .await
    .unwrap();
    assert_eq!(
        status, 503,
        "an empty successful sweep erased a validation fault"
    );
    assert_eq!(body["operational_failure"], "adapter_preparation");
    assert!(
        !body
            .to_string()
            .contains(&root.path().to_string_lossy().to_string())
    );
    assert_eq!(
        (attempts, pending, started),
        (1, 2, 0),
        "faulted worker must not claim after capacity is freed"
    );
    assert_eq!(ready_response(state).await.0, 503);
}

#[tokio::test]
async fn real_preparation_error_stays_unready_after_later_successful_sweep() {
    let fixture = support::Fixture::new().await;
    let plan = support::plan(0);
    let grant = fixture.grant(&plan).await;
    let root = tempfile::tempdir().unwrap();
    let playbook = root
        .path()
        .join(SYNTHETIC_LONG_SLEEP_V1.playbook_relative_path);
    std::fs::create_dir_all(playbook.parent().unwrap()).unwrap();
    std::fs::copy(
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../..")
            .join(SYNTHETIC_LONG_SLEEP_V1.playbook_relative_path),
        &playbook,
    )
    .unwrap();
    let invocation = AdapterRegistry::local(root.path())
        .unwrap()
        .validate(&plan)
        .unwrap();
    std::fs::remove_file(playbook).unwrap();
    let (_cancel, receiver) = watch::channel(false);
    let result = tokio::spawn(
        AdapterRunner::new(fixture.scheduler.clone()).run(invocation, grant, receiver),
    )
    .await;
    assert!(matches!(
        result,
        Ok(Err(AdapterError::Io | AdapterError::TrustedPath))
    ));
    let started: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM rust_controller.journal_events WHERE event_kind='attempt_started'",
    )
    .fetch_one(&fixture.pool)
    .await
    .unwrap();
    assert_eq!(
        started, 0,
        "preparation failed before any durable process start"
    );
    let (status, body) = response_after(result, fixture.store.clone()).await;
    assert_eq!(status, 503);
    assert_eq!(body["operational_failure"], "adapter_preparation");
    assert!(
        !body
            .to_string()
            .contains(&root.path().to_string_lossy().to_string())
    );
}

#[tokio::test]
async fn panic_and_abort_join_failures_become_sanitized_operational_failure() {
    let fixture = support::Fixture::new().await;
    let panic: std::result::Result<AdapterResult, _> =
        tokio::spawn(async { panic!("secret-canary") }).await;
    let (status, body) = response_after(panic, fixture.store.clone()).await;
    assert_eq!(status, 503);
    assert_eq!(body["operational_failure"], "worker_join");
    assert!(!body.to_string().contains("secret-canary"));
    let task = tokio::spawn(std::future::pending::<AdapterResult>());
    task.abort();
    let (status, body) = response_after(task.await, fixture.store.clone()).await;
    assert_eq!(status, 503);
    assert_eq!(body["operational_failure"], "worker_join");
}

#[tokio::test]
async fn normal_cancellation_and_authority_loss_remain_execution_outcomes() {
    let fixture = support::Fixture::new().await;
    for reason in [
        ansible_adapter::CompletionReason::Cancelled,
        ansible_adapter::CompletionReason::AuthorityLost,
        ansible_adapter::CompletionReason::TimedOut,
    ] {
        let report = AdapterReport {
            reason,
            state: controller_domain::ExecutionState::Unknown,
            persisted: false,
            events: vec![],
        };
        let (status, body) = response_after(Ok(Ok(report)), fixture.store.clone()).await;
        assert_eq!(status, 200);
        assert!(body["operational_failure"].is_null());
    }
    for (reason, failure) in [
        (
            ansible_adapter::CompletionReason::SpawnFailed,
            "adapter_spawn",
        ),
        (
            ansible_adapter::CompletionReason::ProcessError,
            "adapter_process",
        ),
    ] {
        let report = AdapterReport {
            reason,
            state: controller_domain::ExecutionState::Unknown,
            persisted: true,
            events: vec![],
        };
        let (status, body) = response_after(Ok(Ok(report)), fixture.store.clone()).await;
        assert_eq!(status, 503);
        assert_eq!(body["operational_failure"], failure);
    }
}

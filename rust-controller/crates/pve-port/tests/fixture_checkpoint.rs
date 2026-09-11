#![cfg(feature = "fixture-ipc")]

use pve_port::{FakeControllerCheckpoint, NativeFakePve, fixture_ipc::ControllerFixturePort};
use std::{sync::Arc, time::Duration};

#[tokio::test]
async fn missing_checkpoint_daemon_is_typed_unavailable() {
    use pve_port::fixture_ipc::CheckpointError;
    use pve_port::fixture_support::{CheckpointBinding, CheckpointPoint, FixtureCheckpointClient};
    let directory =
        std::path::Path::new("/tmp").join(format!("checkpoint-{}", uuid::Uuid::now_v7()));
    let client =
        FixtureCheckpointClient::new(directory.join("missing.sock"), Duration::from_millis(50))
            .unwrap();
    let binding = CheckpointBinding {
        generation: uuid::Uuid::now_v7(),
        owner: uuid::Uuid::now_v7(),
        operation: uuid::Uuid::now_v7(),
        point: CheckpointPoint::DispatchCommitted,
    };
    assert_eq!(
        client.controller_checkpoint(binding).await,
        Err(CheckpointError::Unavailable)
    );
}

#[tokio::test]
async fn trait_object_checkpoint_preserves_native_pause_and_release() {
    let native = Arc::new(NativeFakePve::new());
    let pause = native.pause_controller_checkpoint(FakeControllerCheckpoint::DispatchCommitted);
    let fixture: Arc<dyn ControllerFixturePort> = native;
    let worker = tokio::spawn(async move {
        fixture
            .controller_checkpoint(FakeControllerCheckpoint::DispatchCommitted)
            .await
            .expect("native checkpoint succeeds after release");
    });
    tokio::time::timeout(Duration::from_secs(2), pause.entered())
        .await
        .expect("checkpoint must enter the native barrier");
    assert!(!worker.is_finished(), "checkpoint must await release");
    pause.release();
    tokio::time::timeout(Duration::from_secs(2), worker)
        .await
        .expect("released checkpoint must finish")
        .expect("worker must not panic");
}

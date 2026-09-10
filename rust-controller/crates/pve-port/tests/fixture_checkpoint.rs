#![cfg(feature = "fixture-ipc")]

use pve_port::{FakeControllerCheckpoint, NativeFakePve, fixture_ipc::ControllerFixturePort};
use std::{sync::Arc, time::Duration};

#[tokio::test]
async fn trait_object_checkpoint_preserves_native_pause_and_release() {
    let native = Arc::new(NativeFakePve::new());
    let pause = native.pause_controller_checkpoint(FakeControllerCheckpoint::DispatchCommitted);
    let fixture: Arc<dyn ControllerFixturePort> = native;
    let worker = tokio::spawn(async move {
        fixture
            .controller_checkpoint(FakeControllerCheckpoint::DispatchCommitted)
            .await;
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

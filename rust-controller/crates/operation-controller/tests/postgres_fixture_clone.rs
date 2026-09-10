//! PostgreSQL dispatch/receipt durability composed with the owned IPC fixture.
//! Preflight uses the existing native scenario; this does not prove controller IPC collection.
#![cfg(feature = "fixture-ipc")]
#[allow(dead_code)]
#[path = "../../postgres-store/tests/osdeploy_execution_support/mod.rs"]
mod osdeploy_execution_support;
#[allow(dead_code)]
#[path = "../../postgres-store/tests/osdeploy_support/mod.rs"]
mod osdeploy_support;

use osdeploy_execution_support::Scenario;
use pve_port::{fixture_ipc::*, fixture_support::*, *};
use sqlx::ConnectOptions;
use std::{fs, os::unix::fs::PermissionsExt, sync::Arc, time::Duration};

/// Fresh controller admission must not convert an unseeded IPC world into
/// vacancy or borrow the native Scenario's Ready authorization.
#[tokio::test]
async fn fresh_controller_ipc_collection_without_seed_cannot_dispatch() {
    let s = Scenario::new(300, true).await;
    let operation = s.ids.operation(osdeploy_adapter::OsDeployStage::Clone);
    let fixture_id = controller_domain::RunId::new().as_uuid();
    let directory = std::path::PathBuf::from("/tmp").join(format!("pgentry-{fixture_id}"));
    fs::create_dir(&directory).unwrap();
    fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
    let daemon_path = directory.clone();
    let daemon = std::thread::spawn(move || run(&daemon_path, Duration::from_secs(4)).unwrap());
    let supervisor =
        FixtureCheckpointClient::new(directory.join("supervisor.sock"), Duration::from_secs(1))
            .unwrap();
    let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
    let state = loop {
        if let Ok(reply) = supervisor.request(CheckpointRequest::Status).await {
            break reply.state;
        }
        assert!(tokio::time::Instant::now() < deadline);
        tokio::time::sleep(Duration::from_millis(5)).await;
    };
    let port = FixtureProvisioningPort::new(
        directory.join("client.sock"),
        Duration::from_secs(1),
        FixtureProvisioningIdentity {
            fixture_id,
            operation: operation.as_uuid(),
            request_sha256: "a".repeat(64),
            node: "node-a".into(),
            source_vmid: 900,
            target_vmid: 101,
        },
    )
    .unwrap()
    .with_checkpoint(
        FixtureCheckpointClient::new(directory.join("client.sock"), Duration::from_secs(1))
            .unwrap(),
        CheckpointBinding {
            generation: state.generation,
            owner: fixture_id,
            operation: operation.as_uuid(),
            point: CheckpointPoint::DispatchCommitted,
        },
    )
    .unwrap();
    let controller = operation_controller::OsDeployController::new_fixture(
        s.db.store.clone(),
        s.db.scheduler(),
        Arc::new(port),
        1,
    )
    .unwrap();
    controller.open_send_admission().await.unwrap();
    let progress = controller.run_osdeploy_once(operation).await.unwrap();
    assert_eq!(
        progress,
        postgres_store::OsDeployProgress::Decided(controller_domain::ExecutionState::Unknown)
    );
    let snapshot = s.db.other.load_osdeploy_operation(operation).await.unwrap();
    assert!(snapshot.dispatch().is_none());
    assert!(snapshot.receipt().is_none());
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
    let reader =
        FixtureReadClient::new(directory.join("client.sock"), Duration::from_secs(1)).unwrap();
    assert!(
        reader
            .accepted_effect(operation.as_uuid(), &"a".repeat(64))
            .await
            .unwrap()
            .is_none()
    );
    assert_eq!(
        supervisor
            .request(CheckpointRequest::Status)
            .await
            .unwrap()
            .state
            .phase,
        state.phase
    );
    controller
        .close_and_drain(Duration::from_secs(1))
        .await
        .unwrap();
    daemon.join().unwrap();
    fs::remove_dir_all(directory).unwrap();
}

#[tokio::test]
async fn committed_dispatch_captures_ipc_clone_receipt_in_independent_store() {
    let s = Scenario::new(300, true).await;
    let ready = s.ready(osdeploy_adapter::OsDeployStage::Clone).await;
    let ProvisioningMutationRequestV1::Clone(request) = &ready.request else {
        panic!("expected Clone request")
    };
    let fixture_id = controller_domain::RunId::new().as_uuid();
    let envelope = FixtureCloneRequest::new(fixture_id, request.clone()).unwrap();
    let directory = std::path::PathBuf::from("/tmp").join(format!("pgfc-{fixture_id}"));
    fs::create_dir(&directory).unwrap();
    fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
    let after = VmState {
        disk_bytes: 4096,
        pe_configured: false,
    };
    fs::write(
        directory.join("clone.json"),
        FixtureCloneSeed::new(&envelope, after.clone())
            .unwrap()
            .encode()
            .unwrap(),
    )
    .unwrap();
    let daemon_path = directory.clone();
    let daemon = std::thread::spawn(move || run(&daemon_path, Duration::from_secs(10)).unwrap());
    let supervisor =
        FixtureCheckpointClient::new(directory.join("supervisor.sock"), Duration::from_secs(2))
            .unwrap();
    let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
    let state = loop {
        if let Ok(reply) = supervisor.request(CheckpointRequest::Status).await {
            break reply.state;
        }
        assert!(tokio::time::Instant::now() < deadline);
        tokio::time::sleep(Duration::from_millis(5)).await;
    };
    let binding = CheckpointBinding {
        generation: state.generation,
        owner: fixture_id,
        operation: ready.grant.operation_id().as_uuid(),
        point: CheckpointPoint::DispatchCommitted,
    };
    assert!(
        supervisor
            .request(CheckpointRequest::Arm {
                binding,
                timeout_ms: 2000
            })
            .await
            .unwrap()
            .ok
    );
    let vm = request.clone_request().vm();
    let port = Arc::new(
        FixtureProvisioningPort::new(
            directory.join("client.sock"),
            Duration::from_secs(2),
            FixtureProvisioningIdentity {
                fixture_id,
                operation: binding.operation,
                request_sha256: envelope.request_sha256(),
                node: vm.node().as_str().into(),
                source_vmid: vm.source_vmid().get(),
                target_vmid: vm.target_vmid().get(),
            },
        )
        .unwrap()
        .with_checkpoint(
            FixtureCheckpointClient::new(directory.join("client.sock"), Duration::from_secs(2))
                .unwrap(),
            binding,
        )
        .unwrap(),
    );
    let scheduler = s.db.scheduler();
    let (permit, capture) = scheduler
        .begin_osdeploy_pve_dispatch(&ready.grant, ready.revision, ready.event, &ready.request)
        .await
        .unwrap();
    let committed =
        s.db.other
            .load_osdeploy_operation(ready.grant.operation_id())
            .await
            .unwrap();
    assert!(committed.dispatch().is_some());
    assert!(committed.receipt().is_none());
    let checkpoint_port = port.clone();
    let barrier = tokio::spawn(async move {
        checkpoint_port
            .controller_checkpoint(FakeControllerCheckpoint::DispatchCommitted)
            .await
    });
    let deadline = tokio::time::Instant::now() + Duration::from_secs(1);
    while supervisor
        .request(CheckpointRequest::Status)
        .await
        .unwrap()
        .state
        .phase
        != CheckpointPhase::Entered
    {
        assert!(tokio::time::Instant::now() < deadline);
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    let reader =
        FixtureReadClient::new(directory.join("client.sock"), Duration::from_secs(2)).unwrap();
    assert!(
        reader
            .accepted_effect(binding.operation, &envelope.request_sha256())
            .await
            .unwrap()
            .is_none()
    );
    assert!(!barrier.is_finished());
    assert!(
        supervisor
            .request(CheckpointRequest::Release { binding })
            .await
            .unwrap()
            .ok
    );
    barrier.await.unwrap().unwrap();
    let receipt = permit.submit_fake_once(port.as_ref()).await.unwrap();
    scheduler
        .record_osdeploy_pve_receipt(&capture, &receipt)
        .await
        .unwrap();
    let effect = reader
        .accepted_effect(binding.operation, &envelope.request_sha256())
        .await
        .unwrap()
        .unwrap();
    let original = envelope.decode_receipt(effect.receipt().unwrap()).unwrap();
    assert_eq!(original.submission_sequence(), 1);
    assert_eq!(
        reader.world(vm.target_vmid().get()).await.unwrap(),
        Some(after)
    );
    let reloaded =
        s.db.other
            .load_osdeploy_operation(ready.grant.operation_id())
            .await
            .unwrap();
    assert!(reloaded.dispatch().is_some());
    assert!(reloaded.receipt().is_some());
    assert_eq!(reloaded.receipt().unwrap().receipt(), original.receipt());
    assert_eq!(
        s.db.store
            .load_osdeploy_operation(ready.grant.operation_id())
            .await
            .unwrap()
            .receipt(),
        reloaded.receipt()
    );
    fs::write(directory.join("request.json"), envelope.encode().unwrap()).unwrap();
    fs::write(
        directory.join("operation.json"),
        serde_json::to_vec(&ready.grant.operation_id()).unwrap(),
    )
    .unwrap();
    let attempts_before: i64 =
        sqlx::query_scalar("SELECT count(*) FROM rust_controller.attempts WHERE operation_id=$1")
            .bind(binding.operation)
            .fetch_one(&s.db.pool)
            .await
            .unwrap();
    let mut child = tokio::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "independent_recovery_reader",
            "--ignored",
            "--nocapture",
        ])
        .env("PVA_OWNED_RECOVERY_DIRECTORY", &directory)
        .env(
            "PVA_OWNED_RECOVERY_DSN",
            s.db.pool.connect_options().to_url_lossy().as_str(),
        )
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::inherit())
        .stderr(std::process::Stdio::inherit())
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    let status = tokio::time::timeout(Duration::from_secs(3), child.wait())
        .await
        .expect("independent recovery reader exceeded bound")
        .unwrap();
    assert!(status.success(), "independent recovery reader failed");
    let attempts_after: i64 =
        sqlx::query_scalar("SELECT count(*) FROM rust_controller.attempts WHERE operation_id=$1")
            .bind(binding.operation)
            .fetch_one(&s.db.pool)
            .await
            .unwrap();
    assert_eq!(attempts_after, attempts_before);
    assert!(
        FixtureMutationClient::new(directory.join("client.sock"), Duration::from_secs(2))
            .unwrap()
            .clone_vm(&envelope)
            .await
            .is_err()
    );
    daemon.join().unwrap();
}

/// A new address space reconstructs only from durable inputs. This proves receipt
/// recovery reads, not scheduler takeover or a dispatching worker's crash boundary.
#[tokio::test]
#[ignore = "subprocess entry point; invoked by the owned PostgreSQL proof"]
async fn independent_recovery_reader() {
    let directory = std::env::var_os("PVA_OWNED_RECOVERY_DIRECTORY")
        .expect("owned recovery directory required");
    let directory = std::path::PathBuf::from(directory);
    let envelope =
        FixtureCloneRequest::decode(&fs::read(directory.join("request.json")).unwrap()).unwrap();
    let operation: controller_domain::OperationId =
        serde_json::from_slice(&fs::read(directory.join("operation.json")).unwrap()).unwrap();
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(1)
        .acquire_timeout(Duration::from_secs(1))
        .connect(&std::env::var("PVA_OWNED_RECOVERY_DSN").unwrap())
        .await
        .expect("owned recovery database unavailable");
    let store = postgres_store::PgStore::new(pool);
    let snapshot = store.load_osdeploy_operation(operation).await.unwrap();
    assert!(snapshot.dispatch().is_some());
    let reader =
        FixtureReadClient::new(directory.join("client.sock"), Duration::from_secs(1)).unwrap();
    let effect = reader
        .accepted_effect(operation.as_uuid(), &envelope.request_sha256())
        .await
        .unwrap()
        .expect("accepted effect missing");
    let receipt = envelope.decode_receipt(effect.receipt().unwrap()).unwrap();
    assert_eq!(receipt.submission_sequence(), 1);
    assert_eq!(snapshot.receipt().unwrap().receipt(), receipt.receipt());
    assert!(
        reader
            .accepted_effect(operation.as_uuid(), &"0".repeat(64))
            .await
            .is_err()
    );
}

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
use std::{fs, os::unix::fs::PermissionsExt, sync::Arc, time::Duration};

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
    let daemon = std::thread::spawn(move || run(&daemon_path, Duration::from_secs(5)).unwrap());
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
    assert!(
        FixtureMutationClient::new(directory.join("client.sock"), Duration::from_secs(2))
            .unwrap()
            .clone_vm(&envelope)
            .await
            .is_err()
    );
    daemon.join().unwrap();
}

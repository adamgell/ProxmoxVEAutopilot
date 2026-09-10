#![cfg(feature = "fixture-ipc")]

mod provisioning_support;
use provisioning_support::*;
use pve_port::{fixture_ipc::FixtureCloneRequest, *};
use serde_json::{Value, json};
use uuid::Uuid;

fn request() -> FixtureCloneRequest {
    FixtureCloneRequest::new(
        Uuid::from_u128(1),
        CloneProvisioningRequestV1::new(
            binding(ProvisioningActionV1::Clone, 10, 0),
            plan(ProvisioningActionV1::Clone),
            clone_request(),
            before(source(), false),
            time(),
            30,
        )
        .unwrap(),
    )
    .unwrap()
}

#[test]
fn late_authorization_candidate_requires_exact_dispatch_and_entered_owner() {
    use pve_port::fixture_support::*;
    let request = request();
    let vm = request.request().clone_request().vm();
    let identity = FixtureReadIdentity {
        fixture_id: request.fixture_id(),
        operation: request.request().binding().operation_id().as_uuid(),
        node: vm.node().as_str().into(),
        source_vmid: vm.source_vmid().get(),
        target_vmid: vm.target_vmid().get(),
    };
    let binding = CheckpointBinding {
        generation: Uuid::from_u128(12),
        owner: Uuid::from_u128(13),
        operation: identity.operation,
        point: CheckpointPoint::DispatchCommitted,
    };
    let barrier = CheckpointState {
        generation: binding.generation,
        binding: Some(binding),
        phase: CheckpointPhase::Entered,
    };
    let mut candidate = LateCloneAuthorizationV1 {
        version: 1,
        binding,
        identity: identity.clone(),
        request_sha256: request.request_sha256(),
        request: request.encode().unwrap(),
        after: pve_port::fixture_support::VmState {
            disk_bytes: 4096,
            pe_configured: false,
        },
    };
    let valid = |candidate: &LateCloneAuthorizationV1, state: &CheckpointState| {
        candidate.validate_candidate(&identity, binding, state, &request)
    };
    assert!(valid(&candidate, &barrier).is_ok());
    for phase in [
        CheckpointPhase::Idle,
        CheckpointPhase::Armed,
        CheckpointPhase::Released,
        CheckpointPhase::Expired,
    ] {
        let mut state = barrier.clone();
        state.phase = phase;
        assert!(valid(&candidate, &state).is_err());
    }
    candidate.request_sha256 = "0".repeat(64);
    assert!(valid(&candidate, &barrier).is_err());
    candidate.request_sha256 = request.request_sha256();
    candidate.binding.owner = Uuid::from_u128(14);
    assert!(valid(&candidate, &barrier).is_err());
    candidate.binding = binding;
    candidate.identity.target_vmid += 1;
    assert!(valid(&candidate, &barrier).is_err());
    candidate.identity = identity;
    candidate.version = 2;
    assert!(
        candidate
            .validate_candidate(&candidate.identity, binding, &barrier, &request)
            .is_err()
    );
    let mut state = barrier;
    state.generation = Uuid::from_u128(99);
    candidate.version = 1;
    assert!(
        candidate
            .validate_candidate(&candidate.identity, binding, &state, &request)
            .is_err()
    );
}

#[tokio::test]
async fn bound_stable_read_adapter_reports_unavailable_late_transport() {
    use pve_port::fixture_support::*;
    let request = request();
    let vm = request.request().clone_request().vm();
    let identity = FixtureReadIdentity {
        fixture_id: request.fixture_id(),
        operation: request.request().binding().operation_id().as_uuid(),
        node: vm.node().as_str().into(),
        source_vmid: vm.source_vmid().get(),
        target_vmid: vm.target_vmid().get(),
    };
    let binding = CheckpointBinding {
        generation: Uuid::now_v7(),
        owner: Uuid::now_v7(),
        operation: identity.operation,
        point: CheckpointPoint::DispatchCommitted,
    };
    let socket = std::path::PathBuf::from("/nonexistent-late-mutation.sock");
    let port = FixtureProvisioningPort::new_late(
        socket.clone(),
        std::time::Duration::from_millis(100),
        identity,
    )
    .unwrap()
    .with_checkpoint(
        FixtureCheckpointClient::new(socket, std::time::Duration::from_millis(100)).unwrap(),
        binding,
    )
    .unwrap();
    assert_eq!(
        port.submit_provisioning(&ProvisioningMutationRequestV1::Clone(
            request.request().clone()
        ))
        .await,
        Err(PveWriteError::OutcomeUnknown)
    );
}

fn upid() -> Upid {
    Upid::parse("UPID:pve-test:00000001:00000001:00000001:qmclone:900:fake@pve:").unwrap()
}

fn modify(bytes: &[u8], f: impl FnOnce(&mut Value)) -> Vec<u8> {
    let mut v: Value = serde_json::from_slice(bytes).unwrap();
    f(&mut v);
    serde_json::to_vec(&v).unwrap()
}

#[test]
fn request_and_original_task_receipt_round_trip_without_identity_loss() {
    let original = request();
    let restored = FixtureCloneRequest::decode(&original.encode().unwrap()).unwrap();
    assert_eq!(restored, original);
    let bytes = restored.encode_receipt(7, upid()).unwrap();
    let receipt = original.decode_receipt(&bytes).unwrap();
    assert_eq!(receipt.submission_sequence(), 7);
    assert_eq!(receipt.receipt(), &MutationReceipt::Task(upid()));
    assert_eq!(restored.request().binding(), original.request().binding());
}

#[test]
fn request_rejects_unknown_fields_versions_nil_fixture_and_invalid_typed_clone() {
    let bytes = request().encode().unwrap();
    for bad in [
        modify(&bytes, |v| v["version"] = json!(2)),
        modify(&bytes, |v| v["fixture_id"] = json!(Uuid::nil())),
        modify(&bytes, |v| v["effect"] = json!({})),
        modify(&bytes, |v| {
            v["request"]["plan"]["action"] = json!("start_pe")
        }),
    ] {
        assert!(FixtureCloneRequest::decode(&bad).is_err());
    }
    assert!(FixtureCloneRequest::decode(&[]).is_err());
    assert!(FixtureCloneRequest::decode(&vec![b' '; 65_537]).is_err());
}

#[test]
fn receipt_rejects_mismatched_fixture_request_sequence_and_extra_fields() {
    let original = request();
    let bytes = original.encode_receipt(7, upid()).unwrap();
    for bad in [
        modify(&bytes, |v| v["version"] = json!(2)),
        modify(&bytes, |v| v["fixture_id"] = json!(Uuid::from_u128(2))),
        modify(&bytes, |v| v["request_sha256"] = json!("0".repeat(64))),
        modify(&bytes, |v| v["submission_sequence"] = json!(0)),
        modify(&bytes, |v| v["effect"] = json!(true)),
        modify(&bytes, |v| v["receipt"] = json!("synchronous_accepted")),
    ] {
        assert!(original.decode_receipt(&bad).is_err());
    }
    assert!(original.decode_receipt(&vec![b' '; 65_537]).is_err());
    assert!(original.encode_receipt(0, upid()).is_err());
    let changed = FixtureCloneRequest::decode(&modify(&original.encode().unwrap(), |v| {
        v["request"]["clone"]["request_marker"] = json!(Uuid::from_u128(77));
    }))
    .unwrap();
    assert!(changed.decode_receipt(&bytes).is_err());
}

#[test]
fn clone_task_receipt_requires_source_node_worker_and_synthetic_user() {
    let original = request();
    let bytes = original.encode_receipt(1, upid()).unwrap();
    for bad in [
        "UPID:other:00000001:00000001:00000001:qmclone:900:fake@pve:",
        "UPID:pve-test:00000001:00000001:00000001:qmstart:900:fake@pve:",
        "UPID:pve-test:00000001:00000001:00000001:qmclone:101:fake@pve:",
        "UPID:pve-test:00000001:00000001:00000001:qmclone:900:root@pam:",
    ] {
        assert!(
            original
                .encode_receipt(1, Upid::parse(bad).unwrap())
                .is_err()
        );
        let bad = modify(&bytes, |v| v["receipt"] = json!({"task":bad}));
        assert!(original.decode_receipt(&bad).is_err());
    }
}

#[tokio::test]
async fn daemon_clone_commits_receipt_and_rejects_duplicate_after_restart() {
    use pve_port::fixture_support::*;
    use std::{fs, os::unix::fs::PermissionsExt, time::Duration};
    struct Directory(std::path::PathBuf);
    impl Directory {
        fn path(&self) -> &std::path::Path {
            &self.0
        }
    }
    let directory =
        Directory(std::path::PathBuf::from("/tmp").join(format!("fc-{}", Uuid::now_v7())));
    fs::create_dir(directory.path()).unwrap();
    fs::set_permissions(directory.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let original = request();
    let seed = FixtureCloneSeed::new(
        &original,
        VmState {
            disk_bytes: 4096,
            pe_configured: false,
        },
    )
    .unwrap();
    fs::write(directory.path().join("clone.json"), seed.encode().unwrap()).unwrap();
    for restart in [false, true] {
        if restart {
            fs::remove_file(directory.path().join("client.sock")).unwrap();
            fs::remove_file(directory.path().join("supervisor.sock")).unwrap();
        }
        let path = directory.path().to_owned();
        let daemon = std::thread::spawn(move || run(&path, Duration::from_secs(1)).unwrap());
        let socket = directory.path().join("client.sock");
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        while !socket.exists() {
            assert!(std::time::Instant::now() < deadline);
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        let client = FixtureMutationClient::new(socket.clone(), Duration::from_secs(1)).unwrap();
        if !restart {
            use tokio::io::{AsyncReadExt, AsyncWriteExt};
            let mut stream = tokio::net::UnixStream::connect(&socket).await.unwrap();
            let payload = br#"{"command":"clone","request":{},"extra":true}"#;
            stream.write_u32(payload.len() as u32).await.unwrap();
            stream.write_all(payload).await.unwrap();
            let size = stream.read_u32().await.unwrap();
            let mut bytes = vec![0; size as usize];
            stream.read_exact(&mut bytes).await.unwrap();
            let reply: Reply = serde_json::from_slice(&bytes).unwrap();
            assert!(!reply.ok);
            assert_eq!(reply.attempts, 0);
        }
        let result = client.clone_vm(&original).await;
        if restart {
            assert!(result.is_err());
        } else {
            assert_eq!(result.unwrap().submission_sequence(), 1);
        }
        let reader = FixtureReadClient::new(socket, Duration::from_secs(1)).unwrap();
        let effect = reader
            .accepted_effect(
                original.request().binding().operation_id().as_uuid(),
                &original.request_sha256(),
            )
            .await
            .unwrap()
            .unwrap();
        assert_eq!(
            original
                .decode_receipt(effect.receipt().unwrap())
                .unwrap()
                .submission_sequence(),
            1
        );
        assert!(client.clone_vm(&original).await.is_err());
        let stale =
            FixtureCloneRequest::new(Uuid::from_u128(99), original.request().clone()).unwrap();
        assert!(client.clone_vm(&stale).await.is_err());
        daemon.join().unwrap();
    }
}

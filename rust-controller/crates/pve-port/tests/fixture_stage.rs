#![cfg(feature = "fixture-ipc")]
mod provisioning_support;
use provisioning_support::chain;
use pve_port::{fixture_ipc::*, *};

#[tokio::test]
async fn start_validation_rejects_adapter_identity_before_socket_io() {
    use pve_port::fixture_support::*;
    use serde_json::json;
    use uuid::Uuid;
    let request = FixtureStageRequest::new(Uuid::now_v7(), chain()[3].request.clone()).unwrap();
    let identity = FixtureStageIdentity {
        operation: request.request().binding().operation_id().as_uuid(),
        attempt: request.request().binding().attempt_id().as_uuid(),
        generation: Uuid::now_v7(),
        owner: Uuid::now_v7(),
        stage: FixtureLedgerStage::StartPe,
        request_sha256: request.request_sha256(),
    };
    let stable = json!({"fixture_id":request.fixture_id(),"operation":identity.operation,"node":"pve-test","source_vmid":900,"target_vmid":101});
    for (field, value) in [
        ("fixture_id", json!(Uuid::now_v7())),
        ("operation", json!(Uuid::now_v7())),
        ("node", json!("other-node")),
        ("source_vmid", json!(901)),
        ("target_vmid", json!(102)),
    ] {
        let mut wrong = stable.clone();
        wrong[field] = value;
        let adapter = FixtureProvisioningPort::new_late(
            "/nonexistent-validation.sock".into(),
            std::time::Duration::from_millis(10),
            serde_json::from_value(wrong).unwrap(),
        )
        .unwrap();
        assert_eq!(
            adapter
                .validate_start_pe(&identity, &request, b"{}")
                .await
                .unwrap_err(),
            PveReadError::InvalidResponse,
            "{field}"
        );
    }
}

#[tokio::test]
async fn controller_request_checkpoint_rejects_unbound_and_unsupported_stages() {
    use pve_port::fixture_support::*;
    let episodes = chain();
    let port = FixtureProvisioningPort::new_late(
        "/nonexistent-stage-checkpoint.sock".into(),
        std::time::Duration::from_millis(10),
        FixtureReadIdentity {
            fixture_id: uuid::Uuid::now_v7(),
            operation: uuid::Uuid::now_v7(),
            node: "fixture-node".into(),
            source_vmid: 100,
            target_vmid: 101,
        },
    )
    .unwrap();
    let capability: &dyn ControllerFixturePort = &port;
    for episode in &episodes {
        assert_eq!(
            capability.provisioning_checkpoint(&episode.request).await,
            Err(CheckpointError::Rejected)
        );
    }
}

#[tokio::test]
async fn stage_transport_rejects_without_attempts_or_effects_across_restart() {
    use pve_port::fixture_support::*;
    use std::{fs, os::unix::fs::PermissionsExt, time::Duration};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let directory = std::path::PathBuf::from("/tmp").join(format!("fs-{}", uuid::Uuid::now_v7()));
    fs::create_dir(&directory).unwrap();
    fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
    for restart in [false, true] {
        if restart {
            fs::remove_file(directory.join("client.sock")).unwrap();
            fs::remove_file(directory.join("supervisor.sock")).unwrap();
        }
        let path = directory.clone();
        let daemon = std::thread::spawn(move || run(&path, Duration::from_secs(1)).unwrap());
        let socket = directory.join("client.sock");
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        while !socket.exists() {
            assert!(std::time::Instant::now() < deadline);
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        let client = FixtureMutationClient::new(socket.clone(), Duration::from_secs(1)).unwrap();
        for episode in chain().iter().take(3) {
            let request =
                FixtureStageRequest::new(uuid::Uuid::now_v7(), episode.request.clone()).unwrap();
            let binding = CheckpointBinding {
                generation: uuid::Uuid::now_v7(),
                owner: uuid::Uuid::now_v7(),
                operation: episode.request.binding().operation_id().as_uuid(),
                point: CheckpointPoint::DispatchCommitted,
            };
            assert_eq!(
                client
                    .stage_late(binding, &request)
                    .await
                    .unwrap_err()
                    .kind(),
                std::io::ErrorKind::InvalidData
            );
        }
        let mut stream = tokio::net::UnixStream::connect(&socket).await.unwrap();
        let payload = br#"{"command":"status"}"#;
        stream.write_u32(payload.len() as u32).await.unwrap();
        stream.write_all(payload).await.unwrap();
        let size = stream.read_u32().await.unwrap();
        let mut bytes = vec![0; size as usize];
        stream.read_exact(&mut bytes).await.unwrap();
        let reply: Reply = serde_json::from_slice(&bytes).unwrap();
        assert!(reply.ok);
        assert_eq!(reply.attempts, 0);
        assert_eq!(reply.effects, 0);
        daemon.join().unwrap();
    }
}

#[test]
fn first_five_stage_contracts_bind_exact_request_and_receipt_kind() {
    let episodes = chain();
    let fixture = controller_domain::RunId::new().as_uuid();
    let mut receipts: Vec<Vec<u8>> = Vec::new();
    for (i, episode) in episodes.iter().enumerate() {
        let envelope = FixtureStageRequest::new(fixture, episode.request.clone());
        if i > 4 {
            assert!(envelope.is_err());
            continue;
        }
        let envelope = envelope.unwrap();
        assert_eq!(
            FixtureStageRequest::decode(&envelope.encode().unwrap()).unwrap(),
            envelope
        );
        let receipt = match i {
            0 => MutationReceipt::Task(
                Upid::parse("UPID:pve-test:00000001:00000001:00000001:qmclone:900:fake@pve:")
                    .unwrap(),
            ),
            1 => MutationReceipt::Task(
                Upid::parse("UPID:pve-test:00000001:00000001:00000001:resize:101:fake@pve:")
                    .unwrap(),
            ),
            2 => MutationReceipt::SynchronousAccepted,
            3 => MutationReceipt::Task(
                Upid::parse("UPID:pve-test:00000001:00000001:00000001:qmstart:101:fake@pve:")
                    .unwrap(),
            ),
            _ => MutationReceipt::Task(
                Upid::parse("UPID:pve-test:00000001:00000001:00000001:qmstop:101:fake@pve:")
                    .unwrap(),
            ),
        };
        assert!(envelope.encode_receipt(0, receipt.clone()).is_err());
        let bytes = envelope.encode_receipt(1, receipt.clone()).unwrap();
        let decoded = envelope.decode_receipt(&bytes).unwrap();
        let mut altered: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        altered["request_sha256"] = serde_json::json!("0".repeat(64));
        assert!(
            envelope
                .decode_receipt(&serde_json::to_vec(&altered).unwrap())
                .is_err()
        );
        let mut wire: serde_json::Value =
            serde_json::from_slice(&envelope.encode().unwrap()).unwrap();
        wire["version"] = serde_json::json!(1);
        assert!(FixtureStageRequest::decode(&serde_json::to_vec(&wire).unwrap()).is_err());
        assert_eq!(decoded.receipt(), &receipt);
        assert_eq!(decoded.submission_sequence(), 1);
        for prior in &receipts {
            assert!(envelope.decode_receipt(prior).is_err());
        }
        let foreign = FixtureStageRequest::new(
            controller_domain::RunId::new().as_uuid(),
            episode.request.clone(),
        )
        .unwrap();
        assert!(foreign.decode_receipt(&bytes).is_err());
        receipts.push(bytes);
        for wrong in [
            "UPID:pve-test:00000001:00000001:00000001:resize:900:fake@pve:",
            "UPID:other:00000001:00000001:00000001:resize:101:fake@pve:",
            "UPID:pve-test:00000001:00000001:00000001:resize:101:other@pve:",
            "UPID:pve-test:00000001:00000001:00000001:qmdestroy:101:fake@pve:",
        ] {
            assert!(
                envelope
                    .encode_receipt(1, MutationReceipt::Task(Upid::parse(wrong).unwrap()))
                    .is_err()
            );
        }
        if i != 2 {
            assert!(
                envelope
                    .encode_receipt(1, MutationReceipt::SynchronousAccepted)
                    .is_err()
            );
        }
        assert!(FixtureStageRequest::new(uuid::Uuid::nil(), episode.request.clone()).is_err());
    }
}

#[test]
fn stop_contract_requires_exact_start_history_and_stop_task_identity() {
    use pve_port::fixture_support::*;
    let episodes = chain();
    let fixture = uuid::Uuid::now_v7();
    let start = FixtureStageRequest::new(fixture, episodes[3].request.clone()).unwrap();
    let stop = FixtureStageRequest::new(fixture, episodes[4].request.clone()).unwrap();
    let receipt = start
        .encode_receipt(
            4,
            MutationReceipt::Task(
                Upid::parse("UPID:pve-test:00000001:00000001:00000001:qmstart:101:fake@pve:")
                    .unwrap(),
            ),
        )
        .unwrap();
    stop.validate_ensure_stopped_physical_predecessor(&start, &receipt)
        .unwrap();
    for field in ["operation_id", "attempt_id"] {
        let mut altered = serde_json::to_value(&episodes[3].request).unwrap();
        altered["request"]["binding"][field] = serde_json::json!(uuid::Uuid::now_v7());
        let altered =
            FixtureStageRequest::new(fixture, serde_json::from_value(altered).unwrap()).unwrap();
        let altered_receipt = altered
            .encode_receipt(
                4,
                MutationReceipt::Task(
                    Upid::parse("UPID:pve-test:00000001:00000001:00000001:qmstart:101:fake@pve:")
                        .unwrap(),
                ),
            )
            .unwrap();
        assert!(
            stop.validate_ensure_stopped_physical_predecessor(&altered, &altered_receipt)
                .is_err(),
            "{field}"
        );
    }
    for predecessor in [
        FixtureStageRequest::new(uuid::Uuid::now_v7(), episodes[3].request.clone()).unwrap(),
        FixtureStageRequest::new(fixture, episodes[2].request.clone()).unwrap(),
        stop.clone(),
    ] {
        assert!(
            stop.validate_ensure_stopped_physical_predecessor(&predecessor, &receipt)
                .is_err()
        );
    }
    assert!(
        start
            .validate_ensure_stopped_physical_predecessor(&start, &receipt)
            .is_err()
    );
    for (field, value) in [
        ("fixture_id", serde_json::json!(uuid::Uuid::now_v7())),
        ("request_sha256", serde_json::json!("f".repeat(64))),
        ("submission_sequence", serde_json::json!(0)),
        ("receipt", serde_json::json!("synchronous_accepted")),
    ] {
        let mut altered: serde_json::Value = serde_json::from_slice(&receipt).unwrap();
        altered[field] = value;
        assert!(
            stop.validate_ensure_stopped_physical_predecessor(
                &start,
                &serde_json::to_vec(&altered).unwrap()
            )
            .is_err(),
            "{field}"
        );
    }
    for wrong in [
        "UPID:pve-test:00000001:00000001:00000001:qmstart:101:fake@pve:",
        "UPID:pve-test:00000001:00000001:00000001:qmstop:900:fake@pve:",
        "UPID:other:00000001:00000001:00000001:qmstop:101:fake@pve:",
        "UPID:pve-test:00000001:00000001:00000001:qmstop:101:root@pam:",
    ] {
        assert!(
            stop.encode_receipt(5, MutationReceipt::Task(Upid::parse(wrong).unwrap()))
                .is_err()
        );
    }
    let identity = FixtureStageIdentity {
        operation: stop.request().binding().operation_id().as_uuid(),
        attempt: stop.request().binding().attempt_id().as_uuid(),
        generation: uuid::Uuid::now_v7(),
        owner: uuid::Uuid::now_v7(),
        stage: FixtureLedgerStage::PeEnsureStopped,
        request_sha256: stop.request_sha256(),
    };
    identity.validate_request(&stop).unwrap();
    for (field, value) in [
        ("stage", serde_json::json!("start_pe")),
        ("operation", serde_json::json!(uuid::Uuid::now_v7())),
        ("attempt", serde_json::json!(uuid::Uuid::now_v7())),
        ("generation", serde_json::json!(uuid::Uuid::nil())),
        ("owner", serde_json::json!(uuid::Uuid::nil())),
        ("request_sha256", serde_json::json!("a".repeat(64))),
    ] {
        let mut altered = serde_json::to_value(&identity).unwrap();
        altered[field] = value;
        let altered: FixtureStageIdentity = serde_json::from_value(altered).unwrap();
        assert!(altered.validate_request(&stop).is_err(), "{field}");
    }
}

#[test]
fn start_pe_requires_exact_configure_predecessor_and_target_start_receipt() {
    let episodes = chain();
    let fixture = uuid::Uuid::now_v7();
    let configure = FixtureStageRequest::new(fixture, episodes[2].request.clone()).unwrap();
    let start = FixtureStageRequest::new(fixture, episodes[3].request.clone()).unwrap();
    let receipt = configure
        .encode_receipt(3, MutationReceipt::SynchronousAccepted)
        .unwrap();
    start
        .validate_start_pe_predecessor(&configure, &receipt)
        .unwrap();
    let foreign =
        FixtureStageRequest::new(uuid::Uuid::now_v7(), episodes[2].request.clone()).unwrap();
    assert!(
        start
            .validate_start_pe_predecessor(&foreign, &receipt)
            .is_err()
    );
    let resize = FixtureStageRequest::new(fixture, episodes[1].request.clone()).unwrap();
    assert!(
        start
            .validate_start_pe_predecessor(&resize, &receipt)
            .is_err()
    );
    let mut altered: serde_json::Value = serde_json::from_slice(&receipt).unwrap();
    altered["request_sha256"] = serde_json::json!("f".repeat(64));
    assert!(
        start
            .validate_start_pe_predecessor(&configure, &serde_json::to_vec(&altered).unwrap())
            .is_err()
    );
    for upid in [
        "UPID:pve-test:00000001:00000001:00000001:qmstart:900:fake@pve:",
        "UPID:pve-test:00000001:00000001:00000001:resize:101:fake@pve:",
    ] {
        assert!(
            start
                .encode_receipt(4, MutationReceipt::Task(Upid::parse(upid).unwrap()))
                .is_err()
        );
    }
}

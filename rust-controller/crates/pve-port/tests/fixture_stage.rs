#![cfg(feature = "fixture-ipc")]
mod provisioning_support;
use provisioning_support::chain;
use pve_port::{fixture_ipc::*, *};

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
fn first_four_stage_contracts_bind_exact_request_and_receipt_kind() {
    let episodes = chain();
    let fixture = controller_domain::RunId::new().as_uuid();
    let mut receipts: Vec<Vec<u8>> = Vec::new();
    for (i, episode) in episodes.iter().enumerate() {
        let envelope = FixtureStageRequest::new(fixture, episode.request.clone());
        if i > 3 {
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
            _ => MutationReceipt::Task(
                Upid::parse("UPID:pve-test:00000001:00000001:00000001:qmstart:101:fake@pve:")
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
            "UPID:pve-test:00000001:00000001:00000001:qmstop:101:fake@pve:",
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

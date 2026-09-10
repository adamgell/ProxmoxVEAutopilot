#![cfg(feature = "fixture-ipc")]
mod provisioning_support;
use pve_port::{
    fixture_ipc::{FixtureCloneRequest, FixtureStageRequest},
    fixture_support::*,
    *,
};
use serde_json::{Value, json};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::Path,
    time::{Duration, Instant},
};
use uuid::Uuid;

async fn wire(socket: &Path, value: Value) -> Value {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let mut stream = tokio::net::UnixStream::connect(socket).await.unwrap();
    let bytes = serde_json::to_vec(&value).unwrap();
    stream.write_u32(bytes.len() as u32).await.unwrap();
    stream.write_all(&bytes).await.unwrap();
    let size = stream.read_u32().await.unwrap();
    assert!(size < 200_000);
    let mut bytes = vec![0; size as usize];
    stream.read_exact(&mut bytes).await.unwrap();
    serde_json::from_slice(&bytes).unwrap()
}

async fn authorize(
    control: &FixtureCheckpointClient,
    worker: &FixtureCheckpointClient,
    identity: &FixtureStageIdentity,
    request: &FixtureStageRequest,
) {
    assert!(
        control
            .stage_request(StageCheckpointRequest::Arm {
                identity: identity.clone(),
                timeout_ms: 5000
            })
            .await
            .unwrap()
            .ok
    );
    assert!(
        worker
            .stage_request(StageCheckpointRequest::Enter {
                identity: identity.clone()
            })
            .await
            .unwrap()
            .ok
    );
    assert!(
        control
            .stage_request(StageCheckpointRequest::AuthorizeRelease {
                identity: identity.clone(),
                request: request.encode().unwrap(),
                committed_request: request.encode().unwrap(),
                after: VmState {
                    disk_bytes: request
                        .request()
                        .plan()
                        .expected()
                        .effective_capacity_bytes(),
                    pe_configured: false
                }
            })
            .await
            .unwrap()
            .ok
    );
}

#[tokio::test]
async fn legacy_clone_bridge_requires_original_receipt_across_daemon_restarts() {
    let directory = std::path::PathBuf::from("/tmp").join(format!("lr-ipc-{}", Uuid::now_v7()));
    fs::create_dir(&directory).unwrap();
    fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
    let episodes = provisioning_support::chain();
    let fixture = Uuid::now_v7();
    let ProvisioningMutationRequestV1::Clone(clone) = &episodes[0].request else {
        panic!()
    };
    let legacy = FixtureCloneRequest::new(fixture, clone.clone()).unwrap();
    let resize = FixtureStageRequest::new(fixture, episodes[1].request.clone()).unwrap();
    let seed = FixtureCloneSeed::new(
        &legacy,
        VmState {
            disk_bytes: 80 * provisioning_support::GIB,
            pe_configured: false,
        },
    )
    .unwrap();
    fs::write(directory.join("clone.json"), seed.encode().unwrap()).unwrap();
    let mut original_receipt = Vec::new();
    let mut resize_receipt: Vec<u8> = Vec::new();
    let mut resize_identity = None;
    for cycle in 0..3 {
        if cycle > 0 {
            fs::remove_file(directory.join("client.sock")).unwrap();
            fs::remove_file(directory.join("supervisor.sock")).unwrap();
        }
        let path = directory.clone();
        let daemon = std::thread::spawn(move || run(&path, Duration::from_secs(10)).unwrap());
        let socket = directory.join("client.sock");
        let control_socket = directory.join("supervisor.sock");
        let deadline = Instant::now() + Duration::from_secs(2);
        while !control_socket.exists() {
            assert!(Instant::now() < deadline);
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        let control =
            FixtureCheckpointClient::new(control_socket.clone(), Duration::from_secs(1)).unwrap();
        let worker = FixtureCheckpointClient::new(socket.clone(), Duration::from_secs(1)).unwrap();
        let mutation = FixtureMutationClient::new(socket.clone(), Duration::from_secs(1)).unwrap();
        let reader = FixtureReadClient::new(socket.clone(), Duration::from_secs(1)).unwrap();
        let identity = FixtureStageIdentity {
            operation: resize.request().binding().operation_id().as_uuid(),
            stage: FixtureLedgerStage::DiskCapacity,
            attempt: resize.request().binding().attempt_id().as_uuid(),
            generation: control
                .stage_request(StageCheckpointRequest::Status)
                .await
                .unwrap()
                .generation,
            owner: Uuid::now_v7(),
            request_sha256: resize.request_sha256(),
        };
        if cycle == 0 {
            authorize(&control, &worker, &identity, &resize).await;
            // Structurally valid receipt bytes cannot establish acceptance.
            let fabricated = legacy
                .encode_receipt(
                    1,
                    Upid::parse("UPID:pve-test:00000001:00000001:00000001:qmclone:900:fake@pve:")
                        .unwrap(),
                )
                .unwrap();
            assert!(
                mutation
                    .stage_late_after_legacy_clone(identity.clone(), &resize, &legacy, &fabricated)
                    .await
                    .is_err()
            );
            assert_eq!(reader.status().await.unwrap().attempts, 0);
            assert!(
                worker
                    .stage_request(StageCheckpointRequest::Poll {
                        identity: identity.clone()
                    })
                    .await
                    .unwrap()
                    .ok
            );
            mutation.clone_vm(&legacy).await.unwrap();
            original_receipt = reader
                .accepted_effect(
                    clone.binding().operation_id().as_uuid(),
                    &legacy.request_sha256(),
                )
                .await
                .unwrap()
                .unwrap()
                .receipt()
                .unwrap()
                .to_vec();
        } else {
            assert_eq!(
                reader
                    .accepted_effect(
                        clone.binding().operation_id().as_uuid(),
                        &legacy.request_sha256()
                    )
                    .await
                    .unwrap()
                    .unwrap()
                    .receipt(),
                Some(original_receipt.as_slice())
            );
            authorize(&control, &worker, &identity, &resize).await;
            if cycle == 1 {
                let changed = legacy
                    .encode_receipt(
                        2,
                        Upid::parse(
                            "UPID:pve-test:00000002:00000001:00000001:qmclone:900:fake@pve:",
                        )
                        .unwrap(),
                    )
                    .unwrap();
                assert!(
                    mutation
                        .stage_late_after_legacy_clone(identity.clone(), &resize, &legacy, &changed)
                        .await
                        .is_err()
                );
                let wrong_fixture =
                    FixtureCloneRequest::new(Uuid::now_v7(), clone.clone()).unwrap();
                let wrong_receipt = wrong_fixture
                    .encode_receipt(
                        1,
                        Upid::parse(
                            "UPID:pve-test:00000001:00000001:00000001:qmclone:900:fake@pve:",
                        )
                        .unwrap(),
                    )
                    .unwrap();
                assert!(
                    mutation
                        .stage_late_after_legacy_clone(
                            identity.clone(),
                            &resize,
                            &wrong_fixture,
                            &wrong_receipt
                        )
                        .await
                        .is_err()
                );
                for field in ["owner", "generation", "attempt", "request_sha256"] {
                    let mut wrong = serde_json::to_value(&identity).unwrap();
                    wrong[field] = if field == "request_sha256" {
                        json!("a".repeat(64))
                    } else {
                        json!(Uuid::now_v7())
                    };
                    let wrong = serde_json::from_value(wrong).unwrap();
                    assert!(
                        mutation
                            .stage_late_after_legacy_clone(
                                wrong,
                                &resize,
                                &legacy,
                                &original_receipt
                            )
                            .await
                            .is_err()
                    );
                }
                // Relabeling the accepted v1 Clone as v2 does not create v2 provenance.
                let relabeled =
                    FixtureStageRequest::new(fixture, episodes[0].request.clone()).unwrap();
                let reassigned = FixtureStageIdentity {
                    operation: clone.binding().operation_id().as_uuid(),
                    stage: FixtureLedgerStage::Clone,
                    attempt: clone.binding().attempt_id().as_uuid(),
                    generation: identity.generation,
                    owner: identity.owner,
                    request_sha256: relabeled.request_sha256(),
                };
                assert!(
                    mutation
                        .stage_late_with_predecessor(
                            identity.clone(),
                            &resize,
                            &reassigned,
                            &relabeled
                        )
                        .await
                        .is_err()
                );
                assert_eq!(reader.status().await.unwrap().attempts, 1);
                assert!(
                    worker
                        .stage_request(StageCheckpointRequest::Poll {
                            identity: identity.clone()
                        })
                        .await
                        .unwrap()
                        .ok
                );
                let accepted = mutation
                    .stage_late_after_legacy_clone(
                        identity.clone(),
                        &resize,
                        &legacy,
                        &original_receipt,
                    )
                    .await
                    .unwrap();
                assert_eq!(accepted.submission_sequence(), 2);
                let MutationReceipt::Task(upid) = accepted.receipt() else {
                    panic!()
                };
                assert_eq!(upid.worker_type(), "resize");
                let effect = wire(
                    &socket,
                    json!({"command":"accepted_stage_effect","identity":identity}),
                )
                .await;
                resize_receipt =
                    serde_json::from_value(effect["accepted_effect"]["receipt"].clone()).unwrap();
                resize_identity = Some(identity.clone());
            } else {
                let effect = wire(
                    &socket,
                    json!({"command":"accepted_stage_effect","identity":resize_identity}),
                )
                .await;
                let recovered: Vec<u8> =
                    serde_json::from_value(effect["accepted_effect"]["receipt"].clone()).unwrap();
                assert_eq!(recovered, resize_receipt);
                assert!(
                    mutation
                        .stage_late_after_legacy_clone(
                            resize_identity.clone().unwrap(),
                            &resize,
                            &legacy,
                            &original_receipt
                        )
                        .await
                        .is_err()
                );
            }
            assert!(
                mutation
                    .stage_late_after_legacy_clone(identity, &resize, &legacy, &original_receipt)
                    .await
                    .is_err()
            );
            let status = reader.status().await.unwrap();
            assert_eq!((status.attempts, status.effects), (2, 2));
        }
        assert!(
            wire(&control_socket, json!({"command":"shutdown"})).await["ok"]
                .as_bool()
                .unwrap()
        );
        daemon.join().unwrap();
    }
}

#![cfg(feature = "fixture-ipc")]
mod provisioning_support;
use pve_port::{fixture_ipc::FixtureStageRequest, fixture_support::*};
use std::{path::Path, time::Duration};
use uuid::Uuid;

async fn send(socket: &Path, value: serde_json::Value) -> serde_json::Value {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let mut stream = tokio::net::UnixStream::connect(socket).await.unwrap();
    let bytes = serde_json::to_vec(&value).unwrap();
    stream.write_u32(bytes.len() as u32).await.unwrap();
    stream.write_all(&bytes).await.unwrap();
    let size = stream.read_u32().await.unwrap();
    let mut bytes = vec![0; size as usize];
    stream.read_exact(&mut bytes).await.unwrap();
    serde_json::from_slice(&bytes).unwrap()
}
async fn checkpoint(socket: &Path, request: StageCheckpointRequest) -> StageCheckpointReply {
    serde_json::from_value(
        send(
            socket,
            serde_json::json!({"command":"stage_checkpoint", "request":request}),
        )
        .await,
    )
    .unwrap()
}

#[tokio::test]
async fn stage_consumers_bind_all_fields_and_restart_without_mutation() {
    use std::os::unix::fs::PermissionsExt;
    let directory = std::path::PathBuf::from("/tmp").join(format!("sc-{}", Uuid::now_v7()));
    std::fs::create_dir(&directory).unwrap();
    std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(0o700)).unwrap();
    let mut previous = None;
    for episode in provisioning_support::chain().iter().take(3) {
        if previous.is_some() {
            std::fs::remove_file(directory.join("client.sock")).unwrap();
            std::fs::remove_file(directory.join("supervisor.sock")).unwrap();
        }
        let path = directory.clone();
        let daemon = std::thread::spawn(move || run(&path, Duration::from_secs(5)).unwrap());
        let client = directory.join("client.sock");
        let supervisor = directory.join("supervisor.sock");
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        while !supervisor.exists() {
            assert!(std::time::Instant::now() < deadline);
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        let status = checkpoint(&supervisor, StageCheckpointRequest::Status).await;
        if let Some(identity) = previous.take() {
            assert!(
                !checkpoint(&client, StageCheckpointRequest::Poll { identity })
                    .await
                    .ok
            );
        }
        let request = FixtureStageRequest::new(Uuid::now_v7(), episode.request.clone()).unwrap();
        let stage = match episode.request.plan().action() {
            pve_port::ProvisioningActionV1::Clone => FixtureLedgerStage::Clone,
            pve_port::ProvisioningActionV1::EnsureCapacity => FixtureLedgerStage::DiskCapacity,
            _ => FixtureLedgerStage::ConfigurePe,
        };
        let identity = FixtureStageIdentity {
            operation: episode.request.binding().operation_id().as_uuid(),
            stage,
            attempt: episode.request.binding().attempt_id().as_uuid(),
            generation: status.generation,
            owner: Uuid::now_v7(),
            request_sha256: request.request_sha256(),
        };
        assert!(
            checkpoint(
                &supervisor,
                StageCheckpointRequest::Arm {
                    identity: identity.clone(),
                    timeout_ms: 5000
                }
            )
            .await
            .ok
        );
        for field in [
            "operation",
            "stage",
            "attempt",
            "generation",
            "owner",
            "request_sha256",
        ] {
            let mut wrong = serde_json::to_value(&identity).unwrap();
            wrong[field] = match field {
                "stage" => serde_json::json!(if stage == FixtureLedgerStage::Clone {
                    "disk_capacity"
                } else {
                    "clone"
                }),
                "request_sha256" => serde_json::json!("a".repeat(64)),
                _ => serde_json::json!(Uuid::now_v7()),
            };
            let wrong: FixtureStageIdentity = serde_json::from_value(wrong).unwrap();
            assert!(
                !checkpoint(&client, StageCheckpointRequest::Enter { identity: wrong })
                    .await
                    .ok
            );
        }
        assert!(
            checkpoint(
                &client,
                StageCheckpointRequest::Enter {
                    identity: identity.clone()
                }
            )
            .await
            .ok
        );
        let authorization = StageCheckpointRequest::AuthorizeRelease {
            identity: identity.clone(),
            request: request.encode().unwrap(),
            committed_request: request.encode().unwrap(),
            after: VmState {
                disk_bytes: 1024,
                pe_configured: false,
            },
        };
        assert!(!checkpoint(&client, authorization.clone()).await.ok);
        let mut wrong_request = request.encode().unwrap();
        wrong_request.push(b' ');
        assert!(
            !checkpoint(
                &supervisor,
                StageCheckpointRequest::AuthorizeRelease {
                    identity: identity.clone(),
                    request: wrong_request,
                    committed_request: request.encode().unwrap(),
                    after: VmState {
                        disk_bytes: 1024,
                        pe_configured: false
                    },
                }
            )
            .await
            .ok
        );
        assert!(checkpoint(&supervisor, authorization.clone()).await.ok);
        assert!(!checkpoint(&supervisor, authorization).await.ok);
        let mutation = FixtureMutationClient::new(client.clone(), Duration::from_secs(1)).unwrap();
        assert!(
            mutation
                .stage_late_bound(identity.clone(), &request)
                .await
                .is_err()
        );
        let effect = send(
            &client,
            serde_json::json!({"command":"accepted_stage_effect","identity":identity}),
        )
        .await;
        assert_eq!(effect["ok"], true);
        assert_eq!(effect["attempts"], 0);
        assert_eq!(effect["effects"], 0);
        assert!(effect["accepted_effect"].is_null());
        assert!(
            checkpoint(
                &client,
                StageCheckpointRequest::Poll {
                    identity: identity.clone()
                }
            )
            .await
            .ok
        );
        previous = Some(identity);
        send(&supervisor, serde_json::json!({"command":"shutdown"})).await;
        daemon.join().unwrap();
    }
}

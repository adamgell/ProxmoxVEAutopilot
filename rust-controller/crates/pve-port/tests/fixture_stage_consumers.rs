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

/// The parent owns and reaps this process. A marker is written only after the
/// daemon confirms that this particular generation/owner entered the barrier.
#[tokio::test]
#[ignore = "spawned by the bounded stop refusal proof"]
async fn fixture_stop_worker_child() {
    let directory = std::path::PathBuf::from(std::env::var_os("STOP_WORKER_DIR").unwrap());
    let identity: FixtureStageIdentity =
        serde_json::from_str(&std::env::var("STOP_WORKER_IDENTITY").unwrap()).unwrap();
    let reply = checkpoint(
        &directory.join("client.sock"),
        StageCheckpointRequest::Enter { identity },
    )
    .await;
    assert!(reply.ok);
    std::fs::write(directory.join("worker-entered"), b"entered").unwrap();
    std::future::pending::<()>().await;
}

struct StopWorker(std::process::Child);
impl Drop for StopWorker {
    fn drop(&mut self) {
        if self.0.try_wait().ok().flatten().is_none() {
            let _ = self.0.kill();
        }
        let _ = self.0.wait();
    }
}

async fn kill_entered_stop_worker(directory: &Path, identity: &FixtureStageIdentity) {
    let marker = directory.join("worker-entered");
    if marker.exists() {
        std::fs::remove_file(&marker).unwrap();
    }
    let mut worker = StopWorker(
        std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--ignored", "--exact", "fixture_stop_worker_child"])
            .env("STOP_WORKER_DIR", directory)
            .env(
                "STOP_WORKER_IDENTITY",
                serde_json::to_string(identity).unwrap(),
            )
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .unwrap(),
    );
    let deadline = std::time::Instant::now() + Duration::from_secs(3);
    while !marker.exists() {
        assert!(
            worker.0.try_wait().unwrap().is_none(),
            "stop worker exited before Enter acknowledgement"
        );
        assert!(
            std::time::Instant::now() < deadline,
            "stop worker startup exceeded bound"
        );
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    assert!(worker.0.try_wait().unwrap().is_none());
    worker.0.kill().unwrap();
    assert!(!worker.0.wait().unwrap().success());
}

#[tokio::test]
async fn stop_identity_cannot_acquire_disk_only_release_or_effect_across_daemon_restart() {
    use std::os::unix::fs::PermissionsExt;
    let directory = std::path::PathBuf::from("/tmp").join(format!("stop-gate-{}", Uuid::now_v7()));
    std::fs::create_dir(&directory).unwrap();
    std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(0o700)).unwrap();
    let episodes = provisioning_support::chain();
    let fixture = Uuid::now_v7();
    let stop = FixtureStageRequest::new(fixture, episodes[4].request.clone()).unwrap();
    let start = FixtureStageRequest::new(fixture, episodes[3].request.clone()).unwrap();
    let mut dead_worker_identity: Option<FixtureStageIdentity> = None;
    for restart in [false, true] {
        if restart {
            std::fs::remove_file(directory.join("client.sock")).unwrap();
            std::fs::remove_file(directory.join("supervisor.sock")).unwrap();
        }
        let path = directory.clone();
        let daemon = std::thread::spawn(move || run(&path, Duration::from_secs(1)).unwrap());
        let client = directory.join("client.sock");
        let supervisor = directory.join("supervisor.sock");
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        while !supervisor.exists() {
            assert!(std::time::Instant::now() < deadline);
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        let generation = checkpoint(&supervisor, StageCheckpointRequest::Status)
            .await
            .generation;
        if let Some(stale) = &dead_worker_identity {
            assert_ne!(generation, stale.generation);
            assert!(
                !checkpoint(
                    &client,
                    StageCheckpointRequest::Enter {
                        identity: stale.clone()
                    }
                )
                .await
                .ok
            );
        }
        let identity = FixtureStageIdentity {
            operation: stop.request().binding().operation_id().as_uuid(),
            attempt: stop.request().binding().attempt_id().as_uuid(),
            generation,
            owner: Uuid::now_v7(),
            stage: FixtureLedgerStage::PeEnsureStopped,
            request_sha256: stop.request_sha256(),
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
        kill_entered_stop_worker(&directory, &identity).await;
        dead_worker_identity = Some(identity.clone());
        let release_reply = checkpoint(
            &supervisor,
            StageCheckpointRequest::AuthorizeRelease {
                identity: identity.clone(),
                request: stop.encode().unwrap(),
                committed_request: stop.encode().unwrap(),
                after: VmState {
                    disk_bytes: stop.request().plan().expected().effective_capacity_bytes(),
                    pe_configured: true,
                },
            },
        )
        .await;
        assert!(!release_reply.ok);
        assert_eq!(release_reply.phase, CheckpointPhase::Entered);
        assert_eq!(
            release_reply.refusal,
            Some(
                pve_port::fixture_support::StageCheckpointRefusal::StopReleaseAuthorityUnavailable
            )
        );
        assert_eq!(
            checkpoint(&supervisor, StageCheckpointRequest::Status)
                .await
                .refusal,
            None,
            "refusals describe the request, not persisted execution state"
        );
        let predecessor = FixtureStageIdentity {
            operation: start.request().binding().operation_id().as_uuid(),
            attempt: start.request().binding().attempt_id().as_uuid(),
            generation,
            owner: Uuid::now_v7(),
            stage: FixtureLedgerStage::StartPe,
            request_sha256: start.request_sha256(),
        };
        let reply = send(&client, serde_json::json!({
            "command":"stage_late", "binding":identity,
            "request":serde_json::from_slice::<serde_json::Value>(&stop.encode().unwrap()).unwrap(),
            "predecessor":[predecessor, serde_json::from_slice::<serde_json::Value>(&start.encode().unwrap()).unwrap()]
        })).await;
        assert_eq!(reply["ok"], false);
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis() as u64;
        let admission = StageCheckpointRequest::AdmitStop {
            identity: identity.clone(),
            request: stop.encode().unwrap(),
            committed_request: stop.encode().unwrap(),
            predecessor: predecessor.clone(),
            predecessor_request: start.encode().unwrap(),
            predecessor_receipt: start
                .encode_receipt(
                    4,
                    pve_port::MutationReceipt::Task(
                        pve_port::Upid::parse(
                            "UPID:pve-test:00000001:00000001:00000001:qmstart:101:fake@pve:",
                        )
                        .unwrap(),
                    ),
                )
                .unwrap(),
            authority: pve_port::fixture_support::FixtureStopAuthorityV1 {
                version: 1,
                grace_operation: Uuid::now_v7(),
                decision_event: Uuid::now_v7(),
                evidence_fence: stop.request().binding().evidence_fence(),
                grace_due_unix_ms: now - 2,
                decision_unix_ms: now - 1,
                lease_checked_unix_ms: now,
                lease_expires_unix_ms: now + 5000,
                original_deadline_unix_ms: now + 10000,
            },
        };
        assert!(
            !checkpoint(&client, admission.clone()).await.ok,
            "workers cannot supply supervisor admission"
        );
        let refused_admission = checkpoint(&supervisor, admission).await;
        assert!(
            !refused_admission.ok,
            "structural receipt cannot substitute for durable accepted StartPe history"
        );
        assert!(refused_admission.stop_admission.is_none());
        assert!(
            checkpoint(&supervisor, StageCheckpointRequest::Status)
                .await
                .stop_admission
                .is_none()
        );
        let status = send(&client, serde_json::json!({"command":"status"})).await;
        assert_eq!(status["attempts"], 0);
        assert_eq!(status["effects"], 0);
        daemon.join().unwrap();
    }
}

#[tokio::test]
async fn durable_clone_resize_configure_requires_exact_accepted_predecessor() {
    use std::os::unix::fs::PermissionsExt;
    let directory = std::path::PathBuf::from("/tmp").join(format!("se-{}", Uuid::now_v7()));
    std::fs::create_dir(&directory).unwrap();
    std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(0o700)).unwrap();
    let fixture = Uuid::now_v7();
    let episodes = provisioning_support::chain();
    let mut prior: Option<(FixtureStageIdentity, FixtureStageRequest)> = None;
    let mut accepted: Vec<(FixtureStageIdentity, serde_json::Value)> = Vec::new();
    let mut submitted = Vec::new();
    for (index, episode) in episodes.iter().take(3).enumerate() {
        if index > 0 {
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
        let generation = checkpoint(&supervisor, StageCheckpointRequest::Status)
            .await
            .generation;
        let request = FixtureStageRequest::new(fixture, episode.request.clone()).unwrap();
        let identity = FixtureStageIdentity {
            operation: episode.request.binding().operation_id().as_uuid(),
            stage: if index == 0 {
                FixtureLedgerStage::Clone
            } else if index == 1 {
                FixtureLedgerStage::DiskCapacity
            } else {
                FixtureLedgerStage::ConfigurePe
            },
            attempt: episode.request.binding().attempt_id().as_uuid(),
            generation,
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
        let disk_bytes = if index == 0 {
            request
                .request()
                .plan()
                .expected()
                .template_capacity_bytes()
        } else {
            request
                .request()
                .plan()
                .expected()
                .effective_capacity_bytes()
        };
        assert!(
            checkpoint(
                &supervisor,
                StageCheckpointRequest::AuthorizeRelease {
                    identity: identity.clone(),
                    request: request.encode().unwrap(),
                    committed_request: request.encode().unwrap(),
                    after: VmState {
                        disk_bytes,
                        pe_configured: index == 2
                    }
                }
            )
            .await
            .ok
        );
        let mut message = serde_json::json!({"command":"stage_late", "binding":identity, "request":serde_json::from_slice::<serde_json::Value>(&request.encode().unwrap()).unwrap()});
        if let Some((prior_identity, prior_request)) = &prior {
            // Omitting the durable predecessor must neither submit nor consume release.
            assert_eq!(send(&client, message.clone()).await["ok"], false);
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
            let prior_wire =
                serde_json::from_slice::<serde_json::Value>(&prior_request.encode().unwrap())
                    .unwrap();
            let mut wrong = prior_identity.clone();
            wrong.owner = Uuid::now_v7();
            message["predecessor"] = serde_json::json!([wrong, prior_wire]);
            assert_eq!(send(&client, message.clone()).await["ok"], false);
            message["predecessor"] = serde_json::json!([prior_identity, prior_wire]);
        }
        let reply = send(&client, message.clone()).await;
        submitted.push(message.clone());
        let receipt = request
            .decode_receipt(&serde_json::to_vec(&reply).unwrap())
            .unwrap_or_else(|error| panic!("stage {index}: {error:?}, {reply}"));
        assert_eq!(receipt.submission_sequence(), (index + 1) as u64);
        if index == 2 {
            assert_eq!(
                receipt.receipt(),
                &pve_port::MutationReceipt::SynchronousAccepted
            );
            // ConfigurePe cannot acquire an asynchronous task identity, even
            // when the receipt envelope otherwise matches its exact request.
            let mut wrong_kind = reply.clone();
            wrong_kind["receipt"] = accepted[1].1["receipt"].clone();
            assert!(
                request
                    .decode_receipt(&serde_json::to_vec(&wrong_kind).unwrap())
                    .is_err()
            );
        }
        assert_eq!(send(&client, message).await["ok"], false);
        assert!(
            !checkpoint(
                &client,
                StageCheckpointRequest::Poll {
                    identity: identity.clone()
                }
            )
            .await
            .ok
        );
        accepted.push((identity.clone(), reply));
        for (original, receipt) in &accepted {
            let recovery = send(
                &client,
                serde_json::json!({"command":"accepted_stage_effect", "identity":original}),
            )
            .await;
            assert_eq!(recovery["ok"], true);
            assert_eq!(recovery["attempts"], index + 1);
            assert_eq!(recovery["effects"], index + 1);
            let bytes: Vec<u8> =
                serde_json::from_value(recovery["accepted_effect"]["receipt"].clone()).unwrap();
            assert_eq!(
                serde_json::from_slice::<serde_json::Value>(&bytes).unwrap(),
                *receipt
            );
        }
        prior = Some((identity, request));
        send(&supervisor, serde_json::json!({"command":"shutdown"})).await;
        daemon.join().unwrap();
    }
    // A fresh daemon reloads all three committed effects; every read uses a new
    // connection, without retaining the submitting worker's transport state.
    std::fs::remove_file(directory.join("client.sock")).unwrap();
    std::fs::remove_file(directory.join("supervisor.sock")).unwrap();
    let path = directory.clone();
    let daemon = std::thread::spawn(move || run(&path, Duration::from_secs(5)).unwrap());
    let client = directory.join("client.sock");
    let supervisor = directory.join("supervisor.sock");
    let deadline = std::time::Instant::now() + Duration::from_secs(2);
    while !supervisor.exists() {
        assert!(std::time::Instant::now() < deadline);
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    let generation = checkpoint(&supervisor, StageCheckpointRequest::Status)
        .await
        .generation;
    for ((identity, receipt), submission) in accepted.iter().zip(&submitted) {
        assert_ne!(generation, identity.generation);
        let recovered = send(
            &client,
            serde_json::json!({"command":"accepted_stage_effect", "identity":identity}),
        )
        .await;
        assert_eq!(recovered["ok"], true);
        assert_eq!(recovered["attempts"], 3);
        assert_eq!(recovered["effects"], 3);
        let bytes: Vec<u8> =
            serde_json::from_value(recovered["accepted_effect"]["receipt"].clone()).unwrap();
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&bytes).unwrap(),
            *receipt
        );
        assert_eq!(send(&client, submission.clone()).await["ok"], false);
        for field in [
            "operation",
            "stage",
            "attempt",
            "generation",
            "owner",
            "request_sha256",
        ] {
            let mut wrong = serde_json::to_value(identity).unwrap();
            wrong[field] = if field == "stage" {
                serde_json::json!("unsupported_stage")
            } else if field == "request_sha256" {
                serde_json::json!("f".repeat(64))
            } else {
                serde_json::json!(Uuid::now_v7())
            };
            let response = send(
                &client,
                serde_json::json!({"command":"accepted_stage_effect", "identity":wrong}),
            )
            .await;
            assert!(response["ok"] == false || response["accepted_effect"].is_null());
            assert_eq!(response["attempts"], 3);
            assert_eq!(response["effects"], 3);
        }
    }
    let resize_request = FixtureStageRequest::new(fixture, episodes[1].request.clone()).unwrap();
    let mut capacity_rebound = submitted[1].clone();
    let capacity = &mut capacity_rebound["request"]["request"]["request"]["plan"]["expected"]["effective_capacity_bytes"];
    assert_eq!(capacity.as_u64(), Some(120 * 1_073_741_824));
    *capacity = serde_json::json!(121_u64 * 1_073_741_824);
    assert_eq!(send(&client, capacity_rebound).await["ok"], false);
    let resize_receipt = &accepted[1].1;
    let bytes = serde_json::to_vec(resize_receipt).unwrap();
    assert_eq!(
        resize_request
            .decode_receipt(&bytes)
            .unwrap()
            .submission_sequence(),
        2
    );
    assert!(
        resize_request
            .decode_receipt(&bytes[..bytes.len() / 2])
            .is_err()
    );
    let mut missing = resize_receipt.clone();
    missing.as_object_mut().unwrap().remove("receipt");
    assert!(
        resize_request
            .decode_receipt(&serde_json::to_vec(&missing).unwrap())
            .is_err()
    );
    let mut rebound = resize_receipt.clone();
    rebound["request_sha256"] = serde_json::json!("e".repeat(64));
    assert!(
        resize_request
            .decode_receipt(&serde_json::to_vec(&rebound).unwrap())
            .is_err()
    );
    let status = send(&client, serde_json::json!({"command":"status"})).await;
    assert_eq!(status["attempts"], 3);
    assert_eq!(status["effects"], 3);
    // StartPe has an exact typed contract but cannot manufacture power state
    // from the existing disk-capacity/PE-configured world representation.
    let start = FixtureStageRequest::new(fixture, episodes[3].request.clone()).unwrap();
    let start_identity = FixtureStageIdentity {
        operation: start.request().binding().operation_id().as_uuid(),
        stage: FixtureLedgerStage::StartPe,
        attempt: start.request().binding().attempt_id().as_uuid(),
        generation,
        owner: Uuid::now_v7(),
        request_sha256: start.request_sha256(),
    };
    start_identity.validate_request(&start).unwrap();
    assert!(
        checkpoint(
            &supervisor,
            StageCheckpointRequest::Arm {
                identity: start_identity.clone(),
                timeout_ms: 5000
            }
        )
        .await
        .ok
    );
    assert!(
        checkpoint(
            &client,
            StageCheckpointRequest::Enter {
                identity: start_identity.clone()
            }
        )
        .await
        .ok
    );
    assert!(
        checkpoint(
            &supervisor,
            StageCheckpointRequest::AuthorizeRelease {
                identity: start_identity.clone(),
                request: start.encode().unwrap(),
                committed_request: start.encode().unwrap(),
                after: VmState {
                    disk_bytes: 120 * provisioning_support::GIB,
                    pe_configured: true
                }
            }
        )
        .await
        .ok
    );
    let configure = FixtureStageRequest::new(fixture, episodes[2].request.clone()).unwrap();
    let mutation = FixtureMutationClient::new(client.clone(), Duration::from_secs(1)).unwrap();
    for _ in 0..2 {
        assert!(
            mutation
                .stage_late_with_predecessor(
                    start_identity.clone(),
                    &start,
                    &accepted[2].0,
                    &configure
                )
                .await
                .is_err()
        );
        assert!(
            checkpoint(
                &client,
                StageCheckpointRequest::Poll {
                    identity: start_identity.clone()
                }
            )
            .await
            .ok
        );
    }
    let unchanged = send(&client, serde_json::json!({"command":"status"})).await;
    assert_eq!(unchanged["attempts"], 3);
    assert_eq!(unchanged["effects"], 3);
    let absent = send(
        &client,
        serde_json::json!({"command":"accepted_stage_effect","identity":start_identity}),
    )
    .await;
    assert!(absent["accepted_effect"].is_null());
    send(&supervisor, serde_json::json!({"command":"shutdown"})).await;
    daemon.join().unwrap();
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

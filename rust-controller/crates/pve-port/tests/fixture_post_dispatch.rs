#![cfg(feature = "fixture-ipc")]
#[allow(dead_code)]
mod provisioning_seed_support;
use provisioning_seed_support::support as provisioning_support;
use provisioning_seed_support::support::*;
use pve_port::{fixture_ipc::FixtureCloneRequest, fixture_support::*, *};
use serde_json::{Value, json};
use uuid::Uuid;

async fn wire(socket: &std::path::Path, value: Value) -> std::io::Result<Value> {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let mut stream = tokio::net::UnixStream::connect(socket).await?;
    let bytes = serde_json::to_vec(&value)?;
    stream.write_u32(bytes.len() as u32).await?;
    stream.write_all(&bytes).await?;
    let length = stream.read_u32().await? as usize;
    assert!(length < 200_000);
    let mut bytes = vec![0; length];
    stream.read_exact(&mut bytes).await?;
    Ok(serde_json::from_slice(&bytes)?)
}

fn retime(value: &mut Value, at: u64) {
    match value {
        Value::Object(map) => {
            for (key, value) in map {
                if key == "observed_unix_ms" {
                    *value = json!(at);
                } else if key == "observed_at" {
                    *value = serde_json::to_value(
                        chrono::DateTime::from_timestamp_millis(at as i64).unwrap(),
                    )
                    .unwrap();
                } else {
                    retime(value, at);
                }
            }
        }
        Value::Array(values) => {
            for value in values {
                retime(value, at);
            }
        }
        _ => {}
    }
}

#[tokio::test]
async fn supervisor_publication_requires_accepted_effect_and_invalidates_on_restart() {
    use std::{
        fs,
        os::unix::fs::PermissionsExt,
        time::{Duration, Instant},
    };
    let directory = std::path::PathBuf::from("/tmp").join(format!("fpd-{}", Uuid::now_v7()));
    fs::create_dir(&directory).unwrap();
    fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
    let (observation, request, _, _) = sample();
    let seed = FixtureCloneSeed::new(
        &request,
        VmState {
            disk_bytes: 4096,
            pe_configured: false,
        },
    )
    .unwrap();
    fs::write(directory.join("clone.json"), seed.encode().unwrap()).unwrap();
    let request_json: Value = serde_json::from_slice(&request.encode().unwrap()).unwrap();
    let mut document = serde_json::to_value(observation).unwrap();
    let mut receipt = Vec::new();
    for restart in [false, true] {
        if restart {
            fs::remove_file(directory.join("client.sock")).unwrap();
            fs::remove_file(directory.join("supervisor.sock")).unwrap();
        }
        let path = directory.clone();
        let daemon = std::thread::spawn(move || run(&path, Duration::from_secs(5)).unwrap());
        let socket = directory.join("client.sock");
        let control = directory.join("supervisor.sock");
        let deadline = Instant::now() + Duration::from_secs(2);
        while !control.exists() {
            assert!(Instant::now() < deadline);
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        let reader = FixtureReadClient::new(socket.clone(), Duration::from_secs(1)).unwrap();
        let identity: FixtureProvisioningIdentity =
            serde_json::from_value(document["provisioning"]["identity"].clone()).unwrap();
        let port =
            FixtureProvisioningPort::new(socket.clone(), Duration::from_secs(1), identity).unwrap();
        let node = request.request().clone_request().vm().node();
        let target = request.request().clone_request().vm().target_vmid();
        let port = if restart {
            port.with_dispatched_request(request.clone()).unwrap()
        } else {
            port
        };
        let publish = |observation: &Value| json!({"command":"publish_post_dispatch","request":request_json,"observation":observation});
        assert!(wire(&control, publish(&document)).await.is_err());
        if !restart {
            port.submit_provisioning(&ProvisioningMutationRequestV1::Clone(
                request.request().clone(),
            ))
            .await
            .unwrap();
            assert_eq!(
                port.provisioning_vm_config(node, target).await,
                Err(PveReadError::TransportUnavailable)
            );
            receipt = reader
                .accepted_effect(
                    request.request().binding().operation_id().as_uuid(),
                    &request.request_sha256(),
                )
                .await
                .unwrap()
                .unwrap()
                .receipt()
                .unwrap()
                .to_vec();
            assert!(
                reader
                    .post_dispatch(&request, &receipt)
                    .await
                    .unwrap()
                    .is_none()
            );
            // Stale startup observations cannot become post-dispatch evidence.
            assert!(wire(&control, publish(&document)).await.is_err());
            retime(&mut document, chrono::Utc::now().timestamp_millis() as u64);
            assert!(wire(&socket, publish(&document)).await.is_err());
            let mut wrong = document.clone();
            wrong["task"]["identity"]["upid"] =
                json!("UPID:pve-test:00000002:00000001:00000001:qmclone:900:fake@pve:");
            assert!(wire(&control, publish(&wrong)).await.is_err());
            let published = wire(&control, publish(&document)).await.unwrap();
            assert!(!published.is_null());
            let fetched = reader
                .post_dispatch(&request, &receipt)
                .await
                .unwrap()
                .unwrap();
            assert_eq!(serde_json::to_value(fetched.observation).unwrap(), document);
            let MutationReceipt::Task(upid) =
                request.decode_receipt(&receipt).unwrap().receipt().clone()
            else {
                panic!("expected task");
            };
            assert!(port.task_status(node, &upid).await.is_ok());
            assert_eq!(
                port.cluster_vms().await,
                Err(PveReadError::TransportUnavailable)
            );
            assert!(port.provisioning_vm_config(node, target).await.is_ok());
            assert!(port.vm_status(node, target).await.is_ok());
            let wrong =
                Upid::parse("UPID:pve-test:00000002:00000001:00000001:qmclone:900:fake@pve:")
                    .unwrap();
            assert_eq!(
                port.task_status(node, &wrong).await,
                Err(PveReadError::InvalidResponse)
            );
            assert!(wire(&control, publish(&document)).await.is_err());
            assert!(fs::read_dir(&directory).unwrap().any(|file| {
                file.unwrap()
                    .file_name()
                    .to_string_lossy()
                    .starts_with("post-dispatch-")
            }));
        } else {
            assert_eq!(
                port.cluster_vms().await,
                Err(PveReadError::TransportUnavailable)
            );
            assert_eq!(
                port.provisioning_vm_config(node, target).await,
                Err(PveReadError::TransportUnavailable)
            );
            assert!(
                reader
                    .post_dispatch(&request, &receipt)
                    .await
                    .unwrap()
                    .is_none()
            );
        }
        let status = reader.status().await.unwrap();
        assert_eq!((status.attempts, status.effects), (1, 1));
        wire(&control, json!({"command":"shutdown"})).await.unwrap();
        daemon.join().unwrap();
    }
}

fn sample() -> (FixturePostDispatchV1, FixtureCloneRequest, Vec<u8>, u64) {
    let request = FixtureCloneRequest::new(
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
    .unwrap();
    let vm = request.request().clone_request().vm();
    let upid =
        Upid::parse("UPID:pve-test:00000001:00000001:00000001:qmclone:900:fake@pve:").unwrap();
    let receipt = request.encode_receipt(1, upid.clone()).unwrap();
    let at = time().timestamp_millis() as u64;
    let mut provisioning = provisioning_seed_support::target_present();
    provisioning.identity.operation = request.request().binding().operation_id().as_uuid();
    provisioning.identity.request_sha256 = request.request_sha256();
    let observation = FixturePostDispatchV1 {
        version: 1,
        task: FixtureTaskObservation {
            version: 1,
            identity: FixtureTaskIdentity {
                fixture_id: request.fixture_id(),
                operation: provisioning.identity.operation,
                node: vm.node().as_str().into(),
                request_sha256: request.request_sha256(),
                upid: upid.as_str().into(),
            },
            observed_unix_ms: at,
            result: FixtureTaskState::Succeeded {},
        },
        provisioning,
        inventory: FixtureCloneReads {
            version: 1,
            fixture_id: request.fixture_id(),
            node: vm.node().as_str().into(),
            node_status: SeedRead::Error {
                observed_unix_ms: at,
                error: SeedReadError::Unavailable,
            },
            storage: SeedRead::Error {
                observed_unix_ms: at,
                error: SeedReadError::Unavailable,
            },
            bridges: SeedRead::Error {
                observed_unix_ms: at,
                error: SeedReadError::Unavailable,
            },
            cluster_inventory: SeedRead::Error {
                observed_unix_ms: at,
                error: SeedReadError::Unavailable,
            },
        },
    };
    (observation, request, receipt, at)
}

#[test]
fn stage_resize_observations_require_exact_digest_and_resize_receipt() {
    use pve_port::fixture_ipc::FixtureStageRequest;
    let episodes = provisioning_support::chain();
    let (mut observation, legacy, _, at) = sample();
    let request =
        FixtureStageRequest::new(legacy.fixture_id(), episodes[1].request.clone()).unwrap();
    observation.provisioning.identity.operation =
        request.request().binding().operation_id().as_uuid();
    observation.provisioning.identity.request_sha256 = request.request_sha256();
    observation.task.identity.operation = observation.provisioning.identity.operation;
    observation.task.identity.request_sha256 = request.request_sha256();
    let upid =
        Upid::parse("UPID:pve-test:00000001:00000001:00000001:resize:101:fake@pve:").unwrap();
    observation.task.identity.upid = upid.as_str().into();
    let mut timed = serde_json::to_value(&observation).unwrap();
    retime(&mut timed, at);
    observation = serde_json::from_value(timed).unwrap();
    let receipt = request
        .encode_receipt(1, MutationReceipt::Task(upid))
        .unwrap();
    let bytes = serde_json::to_vec(&observation).unwrap();
    FixturePostDispatchV1::decode_stage(&bytes, &request, &receipt, at, at).unwrap();
    observation.task.identity.upid =
        "UPID:pve-test:00000001:00000001:00000001:qmclone:900:fake@pve:".into();
    assert!(
        FixturePostDispatchV1::decode_stage(
            &serde_json::to_vec(&observation).unwrap(),
            &request,
            &receipt,
            at,
            at
        )
        .is_err()
    );
    observation.task.identity.upid =
        "UPID:pve-test:00000001:00000001:00000001:resize:101:fake@pve:".into();
    observation.provisioning.identity.request_sha256 = legacy.request_sha256();
    assert!(
        FixturePostDispatchV1::decode_stage(
            &serde_json::to_vec(&observation).unwrap(),
            &request,
            &receipt,
            at,
            at
        )
        .is_err()
    );
    assert!(
        FixturePostDispatchV1::decode_stage(&bytes, &request, &receipt, at + 1, at + 1).is_err()
    );
    let configure =
        FixtureStageRequest::new(legacy.fixture_id(), episodes[2].request.clone()).unwrap();
    let synchronous = configure
        .encode_receipt(1, MutationReceipt::SynchronousAccepted)
        .unwrap();
    assert!(FixturePostDispatchV1::decode_stage(&bytes, &configure, &synchronous, at, at).is_err());
}

#[tokio::test]
async fn stage_publication_binds_owner_attempt_and_restart_for_clone_and_resize() {
    use pve_port::fixture_ipc::FixtureStageRequest;
    use std::{
        fs,
        os::unix::fs::PermissionsExt,
        time::{Duration, Instant},
    };
    let directory = std::path::PathBuf::from("/tmp").join(format!("stage-pd-{}", Uuid::now_v7()));
    fs::create_dir(&directory).unwrap();
    fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
    let episodes = provisioning_support::chain();
    let fixture = Uuid::from_u128(1);
    let mut prior: Option<(FixtureStageIdentity, FixtureStageRequest)> = None;
    let mut publications: Vec<(FixtureStageIdentity, FixtureStageRequest, Vec<u8>, Value)> =
        Vec::new();
    for restart in [false, true] {
        if restart {
            fs::remove_file(directory.join("client.sock")).unwrap();
            fs::remove_file(directory.join("supervisor.sock")).unwrap();
        }
        let path = directory.clone();
        let daemon = std::thread::spawn(move || run(&path, Duration::from_secs(5)).unwrap());
        let client = directory.join("client.sock");
        let control = directory.join("supervisor.sock");
        let deadline = Instant::now() + Duration::from_secs(2);
        while !control.exists() {
            assert!(Instant::now() < deadline);
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        let reader = FixtureReadClient::new(client.clone(), Duration::from_secs(1)).unwrap();
        if restart {
            for (identity, request, receipt, publish) in &publications {
                assert!(
                    reader
                        .stage_post_dispatch(identity, request, receipt)
                        .await
                        .unwrap()
                        .is_none()
                );
                assert!(wire(&control, publish.clone()).await.is_err());
            }
            let (identity, request, receipt, _) = &publications[1];
            let (previous_identity, previous_request, previous_receipt, _) = &publications[0];
            let restored = FixtureProvisioningPort::new_late(
                client.clone(),
                Duration::from_secs(1),
                FixtureReadIdentity {
                    fixture_id: fixture,
                    operation: identity.operation,
                    node: "pve-test".into(),
                    source_vmid: 900,
                    target_vmid: 101,
                },
            )
            .unwrap()
            .with_resize_stage(
                FixtureCheckpointClient::new(client.clone(), Duration::from_secs(1)).unwrap(),
                identity.clone(),
                request.clone(),
                previous_identity.clone(),
                previous_request.clone(),
                previous_receipt.clone(),
            )
            .unwrap()
            .with_resize_receipt(receipt.clone())
            .unwrap();
            assert!(
                restored
                    .provisioning_vm_config(
                        &NodeName::parse("pve-test").unwrap(),
                        Vmid::new(101).unwrap()
                    )
                    .await
                    .is_err()
            );
            assert_eq!(
                restored.submit_provisioning(request.request()).await,
                Err(PveWriteError::Rejected)
            );
            use pve_port::fixture_ipc::ControllerFixturePort;
            assert!(
                restored
                    .provisioning_checkpoint(request.request())
                    .await
                    .is_err()
            );
            assert_eq!(reader.status().await.unwrap().attempts, 2);
            // Cancel a real entered stage wait before release. The client owns
            // no submission path, and cancellation must leave both effects intact.
            let control_client =
                FixtureCheckpointClient::new(control.clone(), Duration::from_secs(1)).unwrap();
            let worker =
                FixtureCheckpointClient::new(client.clone(), Duration::from_secs(1)).unwrap();
            let fresh_generation = control_client
                .stage_request(StageCheckpointRequest::Status)
                .await
                .unwrap()
                .generation;
            let mut fresh = identity.clone();
            fresh.generation = fresh_generation;
            assert!(
                control_client
                    .stage_request(StageCheckpointRequest::Arm {
                        identity: fresh.clone(),
                        timeout_ms: 1000
                    })
                    .await
                    .unwrap()
                    .ok
            );
            {
                let wait = worker.stage_checkpoint(&fresh);
                let entered = async {
                    let deadline = Instant::now() + Duration::from_secs(1);
                    loop {
                        if control_client
                            .stage_request(StageCheckpointRequest::Status)
                            .await
                            .unwrap()
                            .phase
                            == CheckpointPhase::Entered
                        {
                            break;
                        }
                        assert!(Instant::now() < deadline);
                        tokio::time::sleep(Duration::from_millis(5)).await;
                    }
                };
                tokio::select! {
                    result = wait => panic!("stage returned before release: {result:?}"),
                    () = entered => {},
                }
            }
            let unchanged = reader.status().await.unwrap();
            assert_eq!((unchanged.attempts, unchanged.effects), (2, 2));
        } else {
            let state = wire(
                &control,
                json!({"command":"stage_checkpoint","request":{"action":"status"}}),
            )
            .await
            .unwrap();
            let generation: Uuid = serde_json::from_value(state["generation"].clone()).unwrap();
            for (index, episode) in episodes.iter().take(2).enumerate() {
                let request = FixtureStageRequest::new(fixture, episode.request.clone()).unwrap();
                let identity = FixtureStageIdentity {
                    operation: request.request().binding().operation_id().as_uuid(),
                    stage: if index == 0 {
                        FixtureLedgerStage::Clone
                    } else {
                        FixtureLedgerStage::DiskCapacity
                    },
                    attempt: request.request().binding().attempt_id().as_uuid(),
                    generation,
                    owner: Uuid::now_v7(),
                    request_sha256: request.request_sha256(),
                };
                let adapter = if index == 1 {
                    let (previous_identity, previous_request, previous_receipt, _) =
                        &publications[0];
                    Some(
                        FixtureProvisioningPort::new_late(
                            client.clone(),
                            Duration::from_secs(1),
                            FixtureReadIdentity {
                                fixture_id: fixture,
                                operation: identity.operation,
                                node: "pve-test".into(),
                                source_vmid: 900,
                                target_vmid: 101,
                            },
                        )
                        .unwrap()
                        .with_resize_stage(
                            FixtureCheckpointClient::new(client.clone(), Duration::from_secs(1))
                                .unwrap(),
                            identity.clone(),
                            request.clone(),
                            previous_identity.clone(),
                            previous_request.clone(),
                            previous_receipt.clone(),
                        )
                        .unwrap(),
                    )
                } else {
                    None
                };
                if let Some(adapter) = &adapter {
                    assert!(
                        adapter
                            .provisioning_vm_config(
                                &NodeName::parse("pve-test").unwrap(),
                                Vmid::new(101).unwrap()
                            )
                            .await
                            .is_ok()
                    );
                    assert_eq!(
                        adapter.submit_provisioning(&episodes[2].request).await,
                        Err(PveWriteError::Rejected)
                    );
                    use pve_port::fixture_ipc::ControllerFixturePort;
                    assert_eq!(
                        adapter.provisioning_checkpoint(&episodes[2].request).await,
                        Err(pve_port::fixture_ipc::CheckpointError::Rejected)
                    );
                    assert_eq!(reader.status().await.unwrap().attempts, 1);
                }
                for (socket, command) in [
                    (
                        &control,
                        StageCheckpointRequest::Arm {
                            identity: identity.clone(),
                            timeout_ms: 5000,
                        },
                    ),
                    (
                        &client,
                        StageCheckpointRequest::Enter {
                            identity: identity.clone(),
                        },
                    ),
                    (
                        &control,
                        StageCheckpointRequest::AuthorizeRelease {
                            identity: identity.clone(),
                            request: request.encode().unwrap(),
                            committed_request: request.encode().unwrap(),
                            after: VmState {
                                disk_bytes: if index == 0 {
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
                                },
                                pe_configured: false,
                            },
                        },
                    ),
                ] {
                    if let Some(adapter) = &adapter {
                        use pve_port::fixture_ipc::ControllerFixturePort;
                        if matches!(command, StageCheckpointRequest::AuthorizeRelease { .. }) {
                            continue;
                        }
                        if matches!(command, StageCheckpointRequest::Enter { .. }) {
                            let release = async {
                                let deadline = Instant::now() + Duration::from_secs(1);
                                loop {
                                    let status = wire(&control, json!({"command":"stage_checkpoint","request":{"action":"status"}})).await.unwrap();
                                    if status["phase"] == "entered" {
                                        break;
                                    }
                                    assert!(Instant::now() < deadline);
                                    tokio::time::sleep(Duration::from_millis(5)).await;
                                }
                                assert_eq!(reader.status().await.unwrap().attempts, 1);
                                let release = StageCheckpointRequest::AuthorizeRelease {
                                    identity: identity.clone(),
                                    request: request.encode().unwrap(),
                                    committed_request: request.encode().unwrap(),
                                    after: VmState {
                                        disk_bytes: request
                                            .request()
                                            .plan()
                                            .expected()
                                            .effective_capacity_bytes(),
                                        pe_configured: false,
                                    },
                                };
                                assert_eq!(
                                    wire(
                                        &control,
                                        json!({"command":"stage_checkpoint","request":release})
                                    )
                                    .await
                                    .unwrap()["ok"],
                                    true
                                );
                            };
                            let (checkpoint, ()) = tokio::join!(
                                adapter.provisioning_checkpoint(request.request()),
                                release
                            );
                            checkpoint.unwrap();
                            continue;
                        }
                    }
                    assert_eq!(
                        wire(
                            socket,
                            json!({"command":"stage_checkpoint","request":command})
                        )
                        .await
                        .unwrap()["ok"],
                        true
                    );
                }
                let value: Value = serde_json::from_slice(&request.encode().unwrap()).unwrap();
                let predecessor = prior.as_ref().map(|(identity, request)| {
                    json!([
                        identity,
                        serde_json::from_slice::<Value>(&request.encode().unwrap()).unwrap()
                    ])
                });
                let receipt = if let Some(adapter) = &adapter {
                    let returned = adapter
                        .submit_provisioning(request.request())
                        .await
                        .unwrap();
                    assert_eq!(
                        adapter.submit_provisioning(request.request()).await,
                        Err(PveWriteError::Rejected)
                    );
                    assert!(
                        adapter
                            .provisioning_vm_config(
                                &NodeName::parse("pve-test").unwrap(),
                                Vmid::new(101).unwrap()
                            )
                            .await
                            .is_err()
                    );
                    let effect = wire(
                        &client,
                        json!({"command":"accepted_stage_effect","identity":identity}),
                    )
                    .await
                    .unwrap();
                    let receipt: Vec<u8> =
                        serde_json::from_value(effect["accepted_effect"]["receipt"].clone())
                            .unwrap();
                    assert_eq!(
                        request.decode_receipt(&receipt).unwrap().receipt(),
                        &returned
                    );
                    receipt
                } else {
                    let receipt = wire(&client, json!({"command":"stage_late","binding":identity,"request":value,"predecessor":predecessor})).await.unwrap();
                    serde_json::to_vec(&receipt).unwrap()
                };
                let parsed = request.decode_receipt(&receipt).unwrap();
                let MutationReceipt::Task(upid) = parsed.receipt() else {
                    panic!("task receipt required")
                };
                let (mut observation, _, _, _) = sample();
                observation.provisioning.identity.operation = identity.operation;
                observation.provisioning.identity.request_sha256 = identity.request_sha256.clone();
                observation.task.identity.operation = identity.operation;
                observation.task.identity.request_sha256 = identity.request_sha256.clone();
                observation.task.identity.upid = upid.as_str().into();
                let mut observation = serde_json::to_value(observation).unwrap();
                retime(
                    &mut observation,
                    chrono::Utc::now().timestamp_millis() as u64,
                );
                let publish = json!({"command":"publish_stage_post_dispatch","identity":identity,"request":value,"observation":observation});
                assert!(
                    wire(&control, publish.clone()).await.unwrap()["daemon_generation"].is_string()
                );
                assert!(
                    reader
                        .stage_post_dispatch(&identity, &request, &receipt)
                        .await
                        .unwrap()
                        .is_some()
                );
                if let Some(adapter) = &adapter {
                    assert_eq!(
                        adapter
                            .task_status(&NodeName::parse("pve-test").unwrap(), upid)
                            .await
                            .unwrap()
                            .state(),
                        TaskState::CompleteSuccess
                    );
                    assert_eq!(reader.status().await.unwrap().attempts, 2);
                }
                assert!(wire(&control, publish.clone()).await.is_err());
                for field in ["owner", "generation", "attempt"] {
                    let mut wrong = serde_json::to_value(&identity).unwrap();
                    wrong[field] = json!(Uuid::now_v7());
                    let wrong: FixtureStageIdentity = serde_json::from_value(wrong).unwrap();
                    assert!(
                        reader
                            .stage_post_dispatch(&wrong, &request, &receipt)
                            .await
                            .is_err()
                    );
                }
                publications.push((identity.clone(), request.clone(), receipt, publish));
                prior = Some((identity, request));
            }
        }
        let _ = wire(&control, json!({"command":"shutdown"})).await;
        daemon.join().unwrap();
    }
}

#[test]
fn typed_observations_preserve_task_states_absence_errors_and_partial_coverage() {
    let (mut value, request, receipt, at) = sample();
    for state in [
        FixtureTaskState::Absent {},
        FixtureTaskState::Running {},
        FixtureTaskState::Succeeded {},
        FixtureTaskState::Failed {
            reason: "fixture failure".into(),
        },
    ] {
        value.task.result = state.clone();
        value.provisioning.target_config = SeedRead::Observed {
            observed_unix_ms: at,
            value: SeedConfig::Absent {},
        };
        value.provisioning.target_coverage = SeedRead::Observed {
            observed_unix_ms: at,
            value: ProvisioningCoverageV1::Partial,
        };
        let decoded = FixturePostDispatchV1::decode(
            &serde_json::to_vec(&value).unwrap(),
            &request,
            &receipt,
            at,
            at + 10,
        )
        .unwrap();
        assert_eq!(decoded, value);
    }
}

#[test]
fn exact_receipt_request_and_vm_identity_cannot_be_substituted() {
    let (value, request, receipt, at) = sample();
    let wire = serde_json::to_value(value).unwrap();
    for (pointer, bad) in [
        ("/version", json!(2)),
        ("/task/version", json!(2)),
        ("/task/identity/fixture_id", json!(Uuid::from_u128(3))),
        ("/task/identity/operation", json!(Uuid::from_u128(3))),
        ("/task/identity/request_sha256", json!("0".repeat(64))),
        ("/task/identity/node", json!("other-node")),
        (
            "/task/identity/upid",
            json!("UPID:pve-test:00000002:00000001:00000001:qmclone:900:fake@pve:"),
        ),
        ("/provisioning/identity/source_vmid", json!(800)),
        ("/provisioning/identity/target_vmid", json!(801)),
        (
            "/provisioning/identity/request_sha256",
            json!("0".repeat(64)),
        ),
        ("/inventory/fixture_id", json!(Uuid::from_u128(3))),
        ("/inventory/node", json!("other-node")),
    ] {
        let mut bad_wire = wire.clone();
        *bad_wire.pointer_mut(pointer).unwrap() = bad;
        assert!(
            FixturePostDispatchV1::decode(
                &serde_json::to_vec(&bad_wire).unwrap(),
                &request,
                &receipt,
                at,
                at + 10
            )
            .is_err(),
            "{pointer}"
        );
    }
    let other_receipt = request
        .encode_receipt(
            2,
            Upid::parse("UPID:pve-test:00000002:00000001:00000001:qmclone:900:fake@pve:").unwrap(),
        )
        .unwrap();
    assert!(
        FixturePostDispatchV1::decode(
            &serde_json::to_vec(&wire).unwrap(),
            &request,
            &other_receipt,
            at,
            at + 10
        )
        .is_err()
    );
}

#[test]
fn every_observation_is_bounded_by_independent_acceptance_and_collection_times() {
    let (value, request, receipt, at) = sample();
    let wire = serde_json::to_value(value).unwrap();
    for pointer in [
        "/task/observed_unix_ms",
        "/provisioning/source_config/observed_unix_ms",
        "/provisioning/target_config/observed_unix_ms",
        "/provisioning/source_power/observed_unix_ms",
        "/provisioning/target_power/observed_unix_ms",
        "/provisioning/source_coverage/observed_unix_ms",
        "/provisioning/target_coverage/observed_unix_ms",
        "/provisioning/deployment_media/observed_unix_ms",
        "/provisioning/driver_media/observed_unix_ms",
        "/inventory/node_status/observed_unix_ms",
        "/inventory/storage/observed_unix_ms",
        "/inventory/bridges/observed_unix_ms",
        "/inventory/cluster_inventory/observed_unix_ms",
    ] {
        for bad_time in [0, at - 1, at + 11, u64::MAX] {
            let mut bad_wire = wire.clone();
            *bad_wire.pointer_mut(pointer).unwrap() = json!(bad_time);
            assert!(
                FixturePostDispatchV1::decode(
                    &serde_json::to_vec(&bad_wire).unwrap(),
                    &request,
                    &receipt,
                    at,
                    at + 10
                )
                .is_err(),
                "{pointer} {bad_time}"
            );
        }
    }
    for (accepted, now) in [(0, at), (at + 1, at), (at, u64::MAX)] {
        assert!(
            FixturePostDispatchV1::decode(
                &serde_json::to_vec(&wire).unwrap(),
                &request,
                &receipt,
                accepted,
                now
            )
            .is_err()
        );
    }
}

#[test]
fn strict_wire_rejects_missing_extra_duplicate_and_oversized_fields() {
    let (value, request, receipt, at) = sample();
    let wire = serde_json::to_value(value).unwrap();
    for field in ["version", "task", "provisioning", "inventory"] {
        let mut bad_wire = wire.clone();
        bad_wire.as_object_mut().unwrap().remove(field);
        assert!(
            FixturePostDispatchV1::decode(
                &serde_json::to_vec(&bad_wire).unwrap(),
                &request,
                &receipt,
                at,
                at + 10
            )
            .is_err()
        );
    }
    let mut bad_wire = wire.clone();
    bad_wire["success"] = Value::Bool(true);
    assert!(
        FixturePostDispatchV1::decode(
            &serde_json::to_vec(&bad_wire).unwrap(),
            &request,
            &receipt,
            at,
            at + 10
        )
        .is_err()
    );
    let duplicate = serde_json::to_string(&wire)
        .unwrap()
        .replacen('{', "{\"version\":1,", 1);
    assert!(
        FixturePostDispatchV1::decode(duplicate.as_bytes(), &request, &receipt, at, at + 10)
            .is_err()
    );
    assert!(
        FixturePostDispatchV1::decode(&vec![b' '; 131_073], &request, &receipt, at, at + 10)
            .is_err()
    );
}

#![cfg(feature = "fixture-ipc")]
#[allow(dead_code)]
mod provisioning_seed_support;
mod start_full_process;
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

fn start_adapter(
    socket: std::path::PathBuf,
    identity: &FixtureStageIdentity,
    request: &pve_port::fixture_ipc::FixtureStageRequest,
) -> FixtureProvisioningPort {
    let vm = request.request().plan().expected().vm();
    FixtureProvisioningPort::new_late(
        socket,
        std::time::Duration::from_secs(1),
        FixtureReadIdentity {
            fixture_id: request.fixture_id(),
            operation: identity.operation,
            node: vm.node().to_string(),
            source_vmid: vm.source_vmid().get(),
            target_vmid: vm.target_vmid().get(),
        },
    )
    .unwrap()
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

fn synchronous_sample(request: &pve_port::fixture_ipc::FixtureStageRequest, at: u64) -> Value {
    let (mut sample, _, _, _) = sample();
    sample.provisioning.identity.operation = request.request().binding().operation_id().as_uuid();
    sample.provisioning.identity.request_sha256 = request.request_sha256();
    let mut document =
        json!({"version":1,"provisioning":sample.provisioning,"inventory":sample.inventory});
    document["provisioning"]["target_power"] = json!({"state":"observed","observed_unix_ms":at,"value":{"power":"stopped","locked":false}});
    retime(&mut document, at);
    document
}

#[test]
fn synchronous_contract_requires_configure_receipt_and_rejects_task_fields() {
    use pve_port::fixture_ipc::FixtureStageRequest;
    let episodes = provisioning_support::chain();
    let request =
        FixtureStageRequest::new(Uuid::from_u128(1), episodes[2].request.clone()).unwrap();
    let receipt = request
        .encode_receipt(3, MutationReceipt::SynchronousAccepted)
        .unwrap();
    let at = time().timestamp_millis() as u64;
    let document = synchronous_sample(&request, at);
    let bytes = serde_json::to_vec(&document).unwrap();
    FixtureSynchronousPostDispatchV1::decode(&bytes, &request, &receipt, at, at).unwrap();
    for field in ["task", "upid"] {
        let mut wrong = document.clone();
        wrong[field] = json!(null);
        assert!(
            FixtureSynchronousPostDispatchV1::decode(
                &serde_json::to_vec(&wrong).unwrap(),
                &request,
                &receipt,
                at,
                at
            )
            .is_err()
        );
    }
    assert!(
        FixtureSynchronousPostDispatchV1::decode(&bytes, &request, &receipt, at + 1, at + 1)
            .is_err()
    );
    let resize =
        FixtureStageRequest::new(request.fixture_id(), episodes[1].request.clone()).unwrap();
    let resize_receipt = resize
        .encode_receipt(
            2,
            MutationReceipt::Task(
                Upid::parse("UPID:pve-test:00000002:00000001:00000001:resize:101:fake@pve:")
                    .unwrap(),
            ),
        )
        .unwrap();
    assert!(
        FixtureSynchronousPostDispatchV1::decode(&bytes, &resize, &resize_receipt, at, at).is_err()
    );
    let mut wrong = document;
    wrong["provisioning"]["identity"]["request_sha256"] = json!("a".repeat(64));
    assert!(
        FixtureSynchronousPostDispatchV1::decode(
            &serde_json::to_vec(&wrong).unwrap(),
            &request,
            &receipt,
            at,
            at
        )
        .is_err()
    );
}

#[tokio::test]
async fn synchronous_configure_publication_requires_resize_and_invalidates_on_restart() {
    full_start_pe_case(false).await;
}

#[tokio::test]
async fn full_start_pe_worker_death_preserves_exact_publication_and_ledger() {
    full_start_pe_case(true).await;
}

async fn full_start_pe_case(process_death: bool) {
    use pve_port::fixture_ipc::FixtureStageRequest;
    use std::{
        fs,
        os::unix::fs::PermissionsExt,
        time::{Duration, Instant},
    };
    let directory = std::path::PathBuf::from("/tmp").join(format!("sync-pd-{}", Uuid::now_v7()));
    fs::create_dir(&directory).unwrap();
    fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
    let episodes = provisioning_support::chain();
    let mut accepted: Vec<(FixtureStageIdentity, FixtureStageRequest, Vec<u8>)> = Vec::new();
    let mut final_publication = None;
    let mut start_effect: Option<(FixtureStageIdentity, Value)> = None;
    let mut start_publication = None;
    let mut start_publish_command = None;
    let mut start_readback = None;
    let mut full_publication = None;
    let mut full_command: Option<Value> = None;
    let mut process_durable_bytes: Option<(std::path::PathBuf, Vec<u8>, Vec<u8>)> = None;
    for restart in [false, true] {
        if restart {
            fs::remove_file(directory.join("client.sock")).unwrap();
            fs::remove_file(directory.join("supervisor.sock")).unwrap();
        }
        let path = directory.clone();
        let mut process = process_death.then(|| start_full_process::OwnedChild::daemon(&directory));
        let daemon = (!process_death)
            .then(|| std::thread::spawn(move || run(&path, Duration::from_secs(5)).unwrap()));
        let client = directory.join("client.sock");
        let control = directory.join("supervisor.sock");
        let deadline = Instant::now() + Duration::from_secs(2);
        while !control.exists() {
            assert!(Instant::now() < deadline);
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        let reader = FixtureReadClient::new(client.clone(), Duration::from_secs(1)).unwrap();
        if restart && process_death {
            let (sidecar, bytes, ledger) = process_durable_bytes.as_ref().unwrap();
            assert_eq!(&fs::read(sidecar).unwrap(), bytes);
            assert_eq!(&fs::read(directory.join("fixture.log")).unwrap(), ledger);
        }
        if restart {
            let (identity, request, receipt) = &accepted[2];
            assert!(
                reader
                    .synchronous_stage_post_dispatch(identity, request, receipt)
                    .await
                    .unwrap()
                    .is_none()
            );
            assert!(
                wire(&control, final_publication.clone().unwrap())
                    .await
                    .is_err()
            );
            assert_eq!(reader.status().await.unwrap().effects, 4);
            let (identity, expected) = start_effect.as_ref().unwrap();
            let recovered = wire(
                &client,
                json!({"command":"accepted_stage_effect","identity":identity}),
            )
            .await
            .unwrap();
            assert_eq!(&recovered["accepted_effect"], expected);
            let request = pve_port::fixture_ipc::FixtureStageRequest::new(
                Uuid::from_u128(1),
                episodes[3].request.clone(),
            )
            .unwrap();
            let bytes = serde_json::from_value(expected["receipt"].clone()).unwrap();
            let restored = FixtureStartPeRestoration::restore(
                client.clone(),
                Duration::from_secs(1),
                identity.clone(),
                request,
                bytes,
            )
            .unwrap();
            assert_eq!(
                serde_json::to_value(restored.observe().await.unwrap().unwrap()).unwrap(),
                start_readback.clone().unwrap()
            );
            let replay = wire(&client, json!({"command":"start_pe_observation","identity":identity,"request":serde_json::from_slice::<Value>(&pve_port::fixture_ipc::FixtureStageRequest::new(Uuid::from_u128(1), episodes[3].request.clone()).unwrap().encode().unwrap()).unwrap()})).await.unwrap();
            assert_eq!(Some(replay), start_publication);
            let mut read = full_command.clone().unwrap();
            let restored_request = pve_port::fixture_ipc::FixtureStageRequest::new(
                Uuid::from_u128(1),
                episodes[3].request.clone(),
            )
            .unwrap();
            let receipt: Vec<u8> = serde_json::from_value(expected["receipt"].clone()).unwrap();
            if process_death {
                let replay = start_full_process::worker(
                    &directory,
                    identity,
                    &restored_request,
                    &receipt,
                    false,
                )
                .await;
                assert_eq!(replay, full_publication.clone().unwrap());
            }
            let validated = start_adapter(client.clone(), identity, &restored_request)
                .validate_start_pe(identity, &restored_request, &receipt)
                .await
                .unwrap()
                .unwrap();
            assert_eq!(
                serde_json::to_value(validated.publication()).unwrap(),
                full_publication.clone().unwrap()
            );
            assert_eq!(
                serde_json::to_value(restored.observe_full().await.unwrap().unwrap()).unwrap(),
                full_publication.clone().unwrap()
            );
            read["command"] = json!("start_pe_full");
            read.as_object_mut().unwrap().remove("observation");
            assert_eq!(
                wire(&client, read.clone()).await.unwrap(),
                full_publication.clone().unwrap()
            );
            let path = directory.join(format!(
                "start-full-{}-{}.json",
                identity.operation, identity.request_sha256
            ));
            let persisted = fs::read(&path).unwrap();
            for malformed in [persisted[..persisted.len() / 2].to_vec(), b"{}".to_vec()] {
                fs::write(&path, malformed).unwrap();
                assert!(wire(&client, read.clone()).await.is_err());
            }
            fs::write(&path, &persisted).unwrap();
            assert_eq!(
                wire(&client, read.clone()).await.unwrap(),
                full_publication.clone().unwrap()
            );
            for field in ["owner", "generation", "attempt"] {
                let mut wrong = read.clone();
                wrong["identity"][field] = json!(Uuid::now_v7());
                assert!(wire(&client, wrong).await.is_err());
            }
            assert_eq!(reader.status().await.unwrap().effects, 4);
            assert!(wire(&control, full_command.clone().unwrap()).await.is_err());
            assert!(
                wire(&control, start_publish_command.clone().unwrap())
                    .await
                    .is_err()
            );
        } else {
            let supervisor =
                FixtureCheckpointClient::new(control.clone(), Duration::from_secs(1)).unwrap();
            let worker =
                FixtureCheckpointClient::new(client.clone(), Duration::from_secs(1)).unwrap();
            let generation = supervisor
                .stage_request(StageCheckpointRequest::Status)
                .await
                .unwrap()
                .generation;
            for (index, episode) in episodes.iter().take(3).enumerate() {
                let request =
                    FixtureStageRequest::new(Uuid::from_u128(1), episode.request.clone()).unwrap();
                let identity = FixtureStageIdentity {
                    operation: request.request().binding().operation_id().as_uuid(),
                    stage: [
                        FixtureLedgerStage::Clone,
                        FixtureLedgerStage::DiskCapacity,
                        FixtureLedgerStage::ConfigurePe,
                    ][index],
                    attempt: request.request().binding().attempt_id().as_uuid(),
                    generation,
                    owner: Uuid::now_v7(),
                    request_sha256: request.request_sha256(),
                };
                assert!(
                    supervisor
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
                    supervisor
                        .stage_request(StageCheckpointRequest::AuthorizeRelease {
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
                                pe_configured: index == 2
                            }
                        })
                        .await
                        .unwrap()
                        .ok
                );
                let mut command = json!({"command":"stage_late","binding":identity,"request":serde_json::from_slice::<Value>(&request.encode().unwrap()).unwrap()});
                if index > 0 {
                    assert_eq!(wire(&client, command.clone()).await.unwrap()["ok"], false);
                    let (prior_identity, prior_request, _) = &accepted[index - 1];
                    command["predecessor"] = json!([
                        prior_identity,
                        serde_json::from_slice::<Value>(&prior_request.encode().unwrap()).unwrap()
                    ]);
                }
                let bytes =
                    serde_json::to_vec(&wire(&client, command.clone()).await.unwrap()).unwrap();
                let receipt = request.decode_receipt(&bytes).unwrap();
                assert_eq!(wire(&client, command).await.unwrap()["ok"], false);
                if index == 2 {
                    assert_eq!(receipt.receipt(), &MutationReceipt::SynchronousAccepted);
                    let document =
                        synchronous_sample(&request, chrono::Utc::now().timestamp_millis() as u64);
                    let publish = json!({"command":"publish_synchronous_stage","identity":identity,"request":serde_json::from_slice::<Value>(&request.encode().unwrap()).unwrap(),"observation":document});
                    assert!(wire(&client, publish.clone()).await.is_err());
                    let before = std::fs::read(directory.join("fixture.log")).unwrap();
                    for field in ["owner", "generation", "attempt", "operation"] {
                        let mut wrong = publish.clone();
                        wrong["identity"][field] = json!(Uuid::now_v7());
                        assert!(wire(&control, wrong).await.is_err());
                    }
                    let mut stale = publish.clone();
                    stale["observation"]["provisioning"]["target_power"]["observed_unix_ms"] =
                        json!(1);
                    assert!(wire(&control, stale).await.is_err());
                    assert_eq!(
                        before,
                        std::fs::read(directory.join("fixture.log")).unwrap()
                    );
                    assert!(
                        wire(&control, publish.clone()).await.unwrap()["daemon_generation"]
                            .is_string()
                    );
                    let observation = reader
                        .synchronous_stage_post_dispatch(&identity, &request, &bytes)
                        .await
                        .unwrap()
                        .unwrap();
                    let value = serde_json::to_value(observation.observation).unwrap();
                    assert!(value.get("task").is_none());
                    assert!(value.get("upid").is_none());
                    assert!(wire(&control, publish.clone()).await.is_err());
                    for field in ["owner", "generation", "attempt"] {
                        let mut wrong = serde_json::to_value(&identity).unwrap();
                        wrong[field] = json!(Uuid::now_v7());
                        assert!(
                            reader
                                .synchronous_stage_post_dispatch(
                                    &serde_json::from_value(wrong).unwrap(),
                                    &request,
                                    &bytes
                                )
                                .await
                                .is_err()
                        );
                    }
                    final_publication = Some(publish);
                }
                accepted.push((identity, request, bytes));
            }
            let status = reader.status().await.unwrap();
            assert_eq!((status.attempts, status.effects), (3, 3));
        }
        // Only fresh power-aware authority can admit the synthetic transition;
        // replay and generic release cannot, and admission is not observation.
        let supervisor =
            FixtureCheckpointClient::new(control.clone(), Duration::from_secs(1)).unwrap();
        let worker = FixtureCheckpointClient::new(client.clone(), Duration::from_secs(1)).unwrap();
        let generation = supervisor
            .stage_request(StageCheckpointRequest::Status)
            .await
            .unwrap()
            .generation;
        let start =
            FixtureStageRequest::new(Uuid::from_u128(1), episodes[3].request.clone()).unwrap();
        let identity = FixtureStageIdentity {
            operation: start.request().binding().operation_id().as_uuid(),
            stage: FixtureLedgerStage::StartPe,
            attempt: start.request().binding().attempt_id().as_uuid(),
            generation,
            owner: Uuid::now_v7(),
            request_sha256: start.request_sha256(),
        };
        assert!(
            supervisor
                .stage_request(StageCheckpointRequest::Arm {
                    identity: identity.clone(),
                    timeout_ms: 5000
                })
                .await
                .unwrap()
                .ok
        );
        let start_port = if process_death && !restart {
            Some(std::sync::Arc::new(
                start_adapter(client.clone(), &identity, &start)
                    .with_late_start_after_configure(
                        FixtureCheckpointClient::new(client.clone(), Duration::from_secs(2))
                            .unwrap(),
                        generation,
                        identity.owner,
                        accepted[2].0.clone(),
                        accepted[2].1.clone(),
                    )
                    .unwrap(),
            ))
        } else {
            None
        };
        let checkpoint_worker = if let Some(port) = &start_port {
            use pve_port::fixture_ipc::ControllerFixturePort;
            assert!(
                port.provisioning_checkpoint(accepted[2].1.request())
                    .await
                    .is_err()
            );
            assert!(port.submit_provisioning(start.request()).await.is_err());
            assert!(port.validate_bound_start_pe().await.unwrap().is_none());
            let provenance = port.shared_history_provenance().unwrap();
            assert_eq!(provenance.operation(), identity.operation);
            assert_eq!(provenance.generation(), generation);
            let port = port.clone();
            let request = start.request().clone();
            let child = tokio::spawn(async move { port.provisioning_checkpoint(&request).await });
            tokio::time::timeout(Duration::from_secs(1), async {
                loop {
                    if supervisor
                        .stage_request(StageCheckpointRequest::Status)
                        .await
                        .unwrap()
                        .phase
                        == CheckpointPhase::Entered
                    {
                        break;
                    }
                    tokio::time::sleep(Duration::from_millis(5)).await;
                }
            })
            .await
            .unwrap();
            Some(child)
        } else {
            assert!(
                worker
                    .stage_request(StageCheckpointRequest::Enter {
                        identity: identity.clone()
                    })
                    .await
                    .unwrap()
                    .ok
            );
            None
        };
        let exact_effect = wire(
            &client,
            json!({"command":"accepted_stage_effect","identity":accepted[2].0}),
        )
        .await
        .unwrap();
        let exact_receipt =
            serde_json::from_value(exact_effect["accepted_effect"]["receipt"].clone()).unwrap();
        let start_power = StageCheckpointRequest::AuthorizeStartPe {
            identity: identity.clone(),
            request: start.encode().unwrap(),
            committed_request: start.encode().unwrap(),
            power: StartPePowerAuthorizationV1 {
                version: 1,
                predecessor: accepted[2].0.clone(),
                predecessor_request: accepted[2].1.encode().unwrap(),
                predecessor_receipt: exact_receipt,
                power: SeedPower {
                    power: pve_port::PowerState::Stopped,
                    locked: false,
                },
            },
        };
        let authorization_ledger = fs::read(directory.join("fixture.log")).unwrap();
        assert!(!worker.stage_request(start_power.clone()).await.unwrap().ok);
        for (pointer, value) in [
            ("/power/power/power", json!("running")),
            ("/power/power/locked", json!(true)),
            ("/power/predecessor/owner", json!(Uuid::now_v7())),
            ("/power/predecessor/generation", json!(Uuid::now_v7())),
            ("/power/predecessor/attempt", json!(Uuid::now_v7())),
            ("/power/predecessor_receipt", json!([1, 2, 3])),
            (
                "/power/predecessor_receipt",
                json!(
                    serde_json::to_vec_pretty(
                        &serde_json::from_slice::<Value>(&accepted[2].2).unwrap()
                    )
                    .unwrap()
                ),
            ),
        ] {
            let mut wrong = serde_json::to_value(&start_power).unwrap();
            *wrong.pointer_mut(pointer).unwrap() = value;
            let request = serde_json::from_value(wrong).unwrap();
            assert!(!supervisor.stage_request(request).await.unwrap().ok);
        }
        assert_eq!(
            supervisor
                .stage_request(start_power.clone())
                .await
                .unwrap()
                .ok,
            !restart
        );
        assert_eq!(
            authorization_ledger,
            fs::read(directory.join("fixture.log")).unwrap()
        );
        if !restart {
            assert!(
                !supervisor.stage_request(start_power).await.unwrap().ok,
                "duplicate release must fail"
            );
        } else {
            assert!(
                supervisor
                    .stage_request(StageCheckpointRequest::AuthorizeRelease {
                        identity: identity.clone(),
                        request: start.encode().unwrap(),
                        committed_request: start.encode().unwrap(),
                        after: VmState {
                            disk_bytes: 120 * provisioning_support::GIB,
                            pe_configured: true
                        },
                    })
                    .await
                    .unwrap()
                    .ok
            );
        }
        let mutation = FixtureMutationClient::new(client.clone(), Duration::from_secs(1)).unwrap();
        if !restart {
            let receipt = if let Some(port) = &start_port {
                checkpoint_worker.unwrap().await.unwrap().unwrap();
                let actual = port.submit_provisioning(start.request()).await.unwrap();
                assert!(port.submit_provisioning(start.request()).await.is_err());
                assert!(port.validate_bound_start_pe().await.unwrap().is_none());
                let vm = start.request().plan().expected().vm();
                assert!(
                    port.provisioning_vm_config(vm.node(), vm.target_vmid())
                        .await
                        .is_err()
                );
                let effect = wire(
                    &client,
                    json!({"command":"accepted_stage_effect","identity":identity}),
                )
                .await
                .unwrap();
                let bytes: Vec<u8> =
                    serde_json::from_value(effect["accepted_effect"]["receipt"].clone()).unwrap();
                let decoded = start.decode_receipt(&bytes).unwrap();
                assert_eq!(decoded.receipt(), &actual);
                decoded
            } else {
                mutation
                    .stage_late_with_predecessor(
                        identity.clone(),
                        &start,
                        &accepted[2].0,
                        &accepted[2].1,
                    )
                    .await
                    .unwrap()
            };
            assert!(
                matches!(receipt.receipt(), MutationReceipt::Task(upid) if upid.as_str().contains(":qmstart:101:"))
            );
            let effect = wire(
                &client,
                json!({"command":"accepted_stage_effect","identity":identity}),
            )
            .await
            .unwrap();
            assert_eq!(
                effect["accepted_effect"]["start_transition"]["after"],
                "running"
            );
            start_effect = Some((identity.clone(), effect["accepted_effect"].clone()));
            let (mut observation, _, _, _) = sample();
            observation.provisioning.identity.operation = identity.operation;
            observation.provisioning.identity.request_sha256 = identity.request_sha256.clone();
            observation.task.identity.operation = identity.operation;
            observation.task.identity.request_sha256 = identity.request_sha256.clone();
            let MutationReceipt::Task(upid) = receipt.receipt() else {
                panic!("StartPe requires a task receipt");
            };
            observation.task.identity.upid = upid.as_str().to_owned();
            let at = chrono::Utc::now().timestamp_millis() as u64;
            let mut observation = serde_json::to_value(observation).unwrap();
            observation["provisioning"]["target_power"] = json!({"state":"observed","observed_unix_ms":at,"value":{"power":"running","locked":false}});
            retime(&mut observation, at);
            let original: Vec<u8> =
                serde_json::from_value(effect["accepted_effect"]["receipt"].clone()).unwrap();
            FixturePostDispatchV1::decode_stage(
                &serde_json::to_vec(&observation).unwrap(),
                &start,
                &original,
                at,
                at,
            )
            .unwrap();
            let publication = json!({"command":"publish_stage_post_dispatch","identity":identity,"request":serde_json::from_slice::<Value>(&start.encode().unwrap()).unwrap(),"observation":observation});
            assert!(
                wire(&control, publication).await.is_err(),
                "atomic acceptance cannot publish unproven running/task evidence"
            );
            let committed = fs::read(directory.join("fixture.log")).unwrap();
            assert_eq!(
                committed[authorization_ledger.len()..]
                    .iter()
                    .filter(|byte| **byte == b'\n')
                    .count(),
                1
            );
            let torn =
                std::path::PathBuf::from("/tmp").join(format!("start-torn-{}", Uuid::now_v7()));
            fs::create_dir(&torn).unwrap();
            fs::set_permissions(&torn, fs::Permissions::from_mode(0o700)).unwrap();
            fs::write(torn.join("fixture.log"), &committed[..committed.len() - 1]).unwrap();
            assert!(
                run(&torn, Duration::from_millis(100)).is_err(),
                "torn atomic IPC acceptance must fail recovery"
            );
            fs::remove_file(torn.join("fixture.log")).unwrap();
            fs::remove_dir(torn).unwrap();
            tokio::time::sleep(Duration::from_millis(5)).await;
            let at = chrono::Utc::now().timestamp_millis() as u64;
            let evidence = json!({"version":1,"task":{"version":1,"identity":{"fixture_id":start.fixture_id(),"node":"pve-test","operation":identity.operation,"request_sha256":identity.request_sha256,"upid":upid.as_str()},"observed_unix_ms":at,"result":{"state":"succeeded"}},"power":{"state":"observed","observed_unix_ms":at,"value":{"power":"running","locked":false}}});
            let publish = json!({"command":"publish_start_pe_observation","identity":identity,"request":serde_json::from_slice::<Value>(&start.encode().unwrap()).unwrap(),"observation":evidence});
            let restored = FixtureStartPeRestoration::restore(
                client.clone(),
                Duration::from_secs(1),
                identity.clone(),
                start.clone(),
                original.clone(),
            )
            .unwrap();
            assert!(restored.observe().await.unwrap().is_none());
            assert!(restored.observe_full().await.unwrap().is_none());
            assert!(
                start_adapter(client.clone(), &identity, &start)
                    .validate_start_pe(&identity, &start, &original)
                    .await
                    .unwrap()
                    .is_none()
            );
            assert!(wire(&client, publish.clone()).await.is_err());
            let before_observation = fs::read(directory.join("fixture.log")).unwrap();
            for (pointer, value) in [
                ("/identity/owner", json!(Uuid::now_v7())),
                (
                    "/observation/task/identity/operation",
                    json!(Uuid::now_v7()),
                ),
                (
                    "/observation/task/identity/upid",
                    json!("UPID:pve-test:000000FF:00000001:00000001:qmstart:101:fake@pve:"),
                ),
                ("/observation/task/result/state", json!("running")),
                ("/observation/task/observed_unix_ms", json!(1)),
                ("/observation/power/observed_unix_ms", json!(1)),
                ("/observation/power/value/power", json!("stopped")),
                ("/observation/power/value/locked", json!(true)),
            ] {
                let mut wrong = publish.clone();
                *wrong.pointer_mut(pointer).unwrap() = value;
                assert!(wire(&control, wrong).await.is_err());
                assert_eq!(
                    before_observation,
                    fs::read(directory.join("fixture.log")).unwrap()
                );
            }
            let observed = wire(&control, publish.clone()).await.unwrap();
            assert_eq!(observed["power"]["state"], "running");
            assert!(restored.observe_full().await.unwrap().is_none());
            let at = chrono::Utc::now().timestamp_millis() as u64;
            let ProvisioningMutationRequestV1::Start(start_request) = start.request() else {
                panic!()
            };
            let members:Vec<_> = [source(), start_request.expected_before().config().clone()].into_iter().enumerate().map(|(index, config)| json!({"identity":{"kind":"pve_digest","value":config.digest()},"config":config,"power":if index==0 {"stopped"} else {"running"},"coverage":"complete"})).collect();
            use sha2::{Digest, Sha256};
            let media = |storage: &str, iso: &str| {
                ProvisioningMediaInventoryV1::new(
                    node(),
                    StorageName::parse(storage).unwrap(),
                    vec![iso.into()],
                    ProvisioningCoverageV1::Complete,
                    chrono::DateTime::from_timestamp_millis(at as i64).unwrap(),
                )
                .unwrap()
            };
            let mut full = json!({"version":1,"inventory":{"version":2,"fixture_id":start.fixture_id(),"identity":identity,"receipt_sha256":format!("{:x}",Sha256::digest(&original)),"observed_unix_ms":at,"coverage":"complete","members":members},"durable":observed,"deployment_media":media("local","local:iso/deployment.iso"),"driver_media":media("drivers","drivers:iso/virtio.iso"),"node_status":{"online":true,"uptime_seconds":1},"storage":[{"name":"local-lvm","active":true,"enabled":true,"available_bytes":1000000000u64,"content":["images"]}],"bridges":[{"name":"vmbr0","active":true}]});
            retime(&mut full, at);
            full["durable"] = observed.clone();
            let command = json!({"command":"publish_start_pe_full","identity":identity,"request":serde_json::from_slice::<Value>(&start.encode().unwrap()).unwrap(),"observation":full});
            for (pointer, value) in [
                ("/observation/inventory/coverage", json!("partial")),
                ("/observation/durable/task_upid", json!("forged")),
                ("/observation/node_status/online", json!(false)),
                ("/observation/storage", json!([])),
                ("/observation/bridges", json!([])),
                ("/observation/inventory/members/1/power", json!("stopped")),
                ("/observation/driver_media/iso_volids", json!([])),
            ] {
                let mut wrong = command.clone();
                *wrong.pointer_mut(pointer).unwrap() = value;
                assert!(wire(&control, wrong).await.is_err());
            }
            assert!(wire(&client, command.clone()).await.is_err());
            full_publication = Some(wire(&control, command.clone()).await.unwrap());
            if let Some(port) = &start_port {
                let publication = port.validate_bound_start_pe().await.unwrap().unwrap();
                for member in &publication.observation.inventory.members {
                    let config = port
                        .provisioning_vm_config(member.config.node(), member.config.vmid())
                        .await
                        .unwrap();
                    assert_eq!(config, member.config);
                    let power = port
                        .vm_status(member.config.node(), member.config.vmid())
                        .await
                        .unwrap();
                    assert_eq!(power.power(), member.power);
                    assert_eq!(
                        power.observed_at().timestamp_millis() as u64,
                        publication.observation.inventory.observed_unix_ms
                    );
                }
                for media in [
                    &publication.observation.deployment_media,
                    &publication.observation.driver_media,
                ] {
                    assert_eq!(
                        port.provisioning_media(media.node(), media.storage())
                            .await
                            .unwrap(),
                        *media
                    );
                }
                assert!(
                    port.cluster_vms().await.is_err(),
                    "unmapped inventory must remain closed"
                );
                assert_eq!(
                    serde_json::to_value(publication).unwrap(),
                    full_publication.clone().unwrap()
                );
                use pve_port::fixture_ipc::ControllerFixturePort;
                let fresh = || {
                    start_adapter(client.clone(), &identity, &start)
                        .with_late_start_after_configure(
                            FixtureCheckpointClient::new(client.clone(), Duration::from_secs(2))
                                .unwrap(),
                            generation,
                            identity.owner,
                            accepted[2].0.clone(),
                            accepted[2].1.clone(),
                        )
                        .unwrap()
                };
                assert!(
                    fresh()
                        .with_late_start_receipt(start.request(), b"{}".to_vec())
                        .is_err()
                );
                assert!(
                    fresh()
                        .with_late_start_receipt(accepted[2].1.request(), original.clone())
                        .is_err()
                );
                // The accepted journal receipt is restored into a fresh adapter;
                // no checkpoint or mutation supplies a replacement acceptance.
                let ledger_before = fs::read(directory.join("fixture.log")).unwrap();
                // Equal JSON claims do not substitute for the original durable
                // response bytes. Structural restoration grants no observation.
                let mut respelled = original.clone();
                respelled.push(b' ');
                let untrusted = fresh()
                    .with_late_start_receipt(start.request(), respelled)
                    .unwrap();
                assert!(untrusted.validate_bound_start_pe().await.is_err());
                let restored = fresh()
                    .with_late_start_receipt(start.request(), original.clone())
                    .unwrap();
                assert_eq!(
                    serde_json::to_value(
                        restored.validate_bound_start_pe().await.unwrap().unwrap()
                    )
                    .unwrap(),
                    full_publication.clone().unwrap()
                );
                assert!(
                    restored
                        .provisioning_checkpoint(start.request())
                        .await
                        .is_err()
                );
                assert!(restored.submit_provisioning(start.request()).await.is_err());
                assert_eq!(
                    fs::read(directory.join("fixture.log")).unwrap(),
                    ledger_before
                );
                assert!(
                    restored
                        .with_late_start_receipt(start.request(), original.clone())
                        .is_err()
                );
            }
            if process_death {
                let ledger = fs::read(directory.join("fixture.log")).unwrap();
                let path = directory.join(format!(
                    "start-full-{}-{}.json",
                    identity.operation, identity.request_sha256
                ));
                let sidecar = fs::read(&path).unwrap();
                let recovered =
                    start_full_process::worker(&directory, &identity, &start, &original, true)
                        .await;
                assert_eq!(recovered, full_publication.clone().unwrap());
                assert_eq!(fs::read(&path).unwrap(), sidecar);
                assert_eq!(fs::read(directory.join("fixture.log")).unwrap(), ledger);
            }
            let validated = start_adapter(client.clone(), &identity, &start)
                .validate_start_pe(&identity, &start, &original)
                .await
                .unwrap()
                .unwrap();
            assert_eq!(
                serde_json::to_value(validated.publication()).unwrap(),
                full_publication.clone().unwrap()
            );
            assert_eq!(
                serde_json::to_value(restored.observe_full().await.unwrap().unwrap()).unwrap(),
                full_publication.clone().unwrap()
            );
            assert!(wire(&control, command.clone()).await.is_err());
            full_command = Some(command);
            let readback = restored.observe().await.unwrap().unwrap();
            assert!(matches!(
                readback.task.result,
                FixtureTaskState::Succeeded {}
            ));
            assert!(matches!(
                readback.power,
                SeedRead::Observed {
                    value: SeedPower {
                        power: PowerState::Running,
                        locked: false
                    },
                    ..
                }
            ));
            start_readback = Some(serde_json::to_value(readback).unwrap());
            for field in ["operation", "attempt"] {
                let mut wrong = serde_json::to_value(&identity).unwrap();
                wrong[field] = json!(Uuid::now_v7());
                assert!(
                    FixtureStartPeRestoration::restore(
                        client.clone(),
                        Duration::from_secs(1),
                        serde_json::from_value(wrong).unwrap(),
                        start.clone(),
                        original.clone()
                    )
                    .is_err()
                );
            }
            let reencoded =
                serde_json::to_vec_pretty(&serde_json::from_slice::<Value>(&original).unwrap())
                    .unwrap();
            let wrong_receipt = FixtureStartPeRestoration::restore(
                client.clone(),
                Duration::from_secs(1),
                identity.clone(),
                start.clone(),
                reencoded,
            )
            .unwrap();
            assert!(wrong_receipt.observe().await.is_err());
            for (pointer, replacement) in [
                ("/power/state", json!("stopped")),
                ("/power/vmid", json!(102)),
                ("/power/binding/generation", json!(Uuid::now_v7())),
                ("/power/binding/attempt", json!(Uuid::now_v7())),
                ("/power/receipt_sha256", json!("f".repeat(64))),
                (
                    "/task_upid",
                    json!("UPID:pve-test:000000FF:00000001:00000001:qmstart:101:fake@pve:"),
                ),
                ("/task_observed_unix_ms", json!(1)),
                ("/power/observed_unix_ms", json!(1)),
            ] {
                use tokio::io::{AsyncReadExt, AsyncWriteExt};
                let mut forged = observed.clone();
                *forged.pointer_mut(pointer).unwrap() = replacement;
                let socket = directory.join("forged.sock");
                let listener = tokio::net::UnixListener::bind(&socket).unwrap();
                let server = tokio::spawn(async move {
                    let (mut stream, _) = listener.accept().await.unwrap();
                    let size = stream.read_u32().await.unwrap() as usize;
                    let mut request = vec![0; size];
                    stream.read_exact(&mut request).await.unwrap();
                    assert_eq!(
                        serde_json::from_slice::<Value>(&request).unwrap()["command"],
                        "start_pe_observation"
                    );
                    let bytes = serde_json::to_vec(&forged).unwrap();
                    stream.write_u32(bytes.len() as u32).await.unwrap();
                    stream.write_all(&bytes).await.unwrap();
                });
                let restored = FixtureStartPeRestoration::restore(
                    socket.clone(),
                    Duration::from_secs(1),
                    identity.clone(),
                    start.clone(),
                    original.clone(),
                )
                .unwrap();
                assert_eq!(
                    restored.observe().await.unwrap_err().kind(),
                    std::io::ErrorKind::InvalidData
                );
                server.await.unwrap();
                fs::remove_file(socket).unwrap();
            }
            for (pointer, replacement) in [
                (
                    "/observation/inventory/identity/owner",
                    json!(Uuid::now_v7()),
                ),
                (
                    "/observation/inventory/identity/generation",
                    json!(Uuid::now_v7()),
                ),
                (
                    "/observation/inventory/identity/attempt",
                    json!(Uuid::now_v7()),
                ),
                (
                    "/observation/inventory/receipt_sha256",
                    json!("a".repeat(64)),
                ),
                ("/observation/inventory/coverage", json!("partial")),
                ("/observation/durable/task_upid", json!("forged")),
                ("/observation/storage", json!([])),
                ("/observation/bridges", json!([])),
                ("/observation/inventory/members/1/power", json!("stopped")),
            ] {
                use tokio::io::{AsyncReadExt, AsyncWriteExt};
                let mut forged = full_publication.clone().unwrap();
                *forged.pointer_mut(pointer).unwrap() = replacement;
                // Recompute corruption checksum: deep validation must still
                // reject the forged evidence, not merely a stale checksum.
                let typed: FixtureStartPeFullV1 =
                    serde_json::from_value(forged["observation"].clone()).unwrap();
                forged["sha256"] = json!(format!(
                    "{:x}",
                    Sha256::digest(
                        serde_json::to_vec(&(forged["published_unix_ms"].as_u64().unwrap(), typed))
                            .unwrap()
                    )
                ));
                let socket = directory.join("forged-full.sock");
                let listener = tokio::net::UnixListener::bind(&socket).unwrap();
                let compact = observed.clone();
                let server = tokio::spawn(async move {
                    for (command, response) in
                        [("start_pe_observation", compact), ("start_pe_full", forged)]
                    {
                        let (mut stream, _) = listener.accept().await.unwrap();
                        let size = stream.read_u32().await.unwrap() as usize;
                        let mut bytes = vec![0; size];
                        stream.read_exact(&mut bytes).await.unwrap();
                        assert_eq!(
                            serde_json::from_slice::<Value>(&bytes).unwrap()["command"],
                            command
                        );
                        let reply = serde_json::to_vec(&response).unwrap();
                        stream.write_u32(reply.len() as u32).await.unwrap();
                        stream.write_all(&reply).await.unwrap();
                    }
                });
                let reader = start_adapter(socket.clone(), &identity, &start);
                assert_eq!(
                    reader
                        .validate_start_pe(&identity, &start, &original)
                        .await
                        .unwrap_err(),
                    PveReadError::InvalidResponse,
                    "{pointer}"
                );
                server.await.unwrap();
                fs::remove_file(socket).unwrap();
            }
            for length in [0, 131_073] {
                use tokio::io::{AsyncReadExt, AsyncWriteExt};
                let socket = directory.join("oversized-full.sock");
                let listener = tokio::net::UnixListener::bind(&socket).unwrap();
                let compact = observed.clone();
                let server = tokio::spawn(async move {
                    for round in 0..2 {
                        let (mut stream, _) = listener.accept().await.unwrap();
                        let size = stream.read_u32().await.unwrap() as usize;
                        let mut bytes = vec![0; size];
                        stream.read_exact(&mut bytes).await.unwrap();
                        if round == 0 {
                            let reply = serde_json::to_vec(&compact).unwrap();
                            stream.write_u32(reply.len() as u32).await.unwrap();
                            stream.write_all(&reply).await.unwrap();
                        } else {
                            stream.write_u32(length).await.unwrap();
                        }
                    }
                });
                let reader = FixtureStartPeRestoration::restore(
                    socket.clone(),
                    Duration::from_secs(1),
                    identity.clone(),
                    start.clone(),
                    original.clone(),
                )
                .unwrap();
                assert_eq!(
                    reader.observe_full().await.unwrap_err().kind(),
                    std::io::ErrorKind::InvalidData
                );
                server.await.unwrap();
                fs::remove_file(socket).unwrap();
            }
            for field in ["owner", "generation"] {
                let mut wrong = serde_json::to_value(&identity).unwrap();
                wrong[field] = json!(Uuid::now_v7());
                let restored = FixtureStartPeRestoration::restore(
                    client.clone(),
                    Duration::from_secs(1),
                    serde_json::from_value(wrong).unwrap(),
                    start.clone(),
                    original.clone(),
                )
                .unwrap();
                assert!(restored.observe().await.is_err());
            }
            assert!(wire(&control, publish.clone()).await.is_err());
            start_publish_command = Some(publish);
            start_publication = Some(observed);
            let committed = fs::read(directory.join("fixture.log")).unwrap();
            assert_eq!(
                committed[before_observation.len()..]
                    .iter()
                    .filter(|byte| **byte == b'\n')
                    .count(),
                1
            );
            let torn =
                std::path::PathBuf::from("/tmp").join(format!("obs-torn-{}", Uuid::now_v7()));
            fs::create_dir(&torn).unwrap();
            fs::set_permissions(&torn, fs::Permissions::from_mode(0o700)).unwrap();
            fs::write(torn.join("fixture.log"), &committed[..committed.len() - 1]).unwrap();
            assert!(run(&torn, Duration::from_millis(100)).is_err());
            fs::remove_file(torn.join("fixture.log")).unwrap();
            fs::remove_dir(torn).unwrap();
        }
        let before = fs::read(directory.join("fixture.log")).unwrap();
        for _ in 0..2 {
            assert!(
                mutation
                    .stage_late_with_predecessor(
                        identity.clone(),
                        &start,
                        &accepted[2].0,
                        &accepted[2].1
                    )
                    .await
                    .is_err()
            );
            assert_eq!(
                worker
                    .stage_request(StageCheckpointRequest::Poll {
                        identity: identity.clone()
                    })
                    .await
                    .unwrap()
                    .ok,
                restart
            );
            assert_eq!(before, fs::read(directory.join("fixture.log")).unwrap());
            let status = reader.status().await.unwrap();
            assert_eq!((status.attempts, status.effects), (4, 4));
        }
        if process_death && !restart {
            let sidecar = directory.join(format!(
                "start-full-{}-{}.json",
                identity.operation, identity.request_sha256
            ));
            process_durable_bytes = Some((
                sidecar.clone(),
                fs::read(sidecar).unwrap(),
                fs::read(directory.join("fixture.log")).unwrap(),
            ));
            process.as_mut().unwrap().kill();
        } else {
            wire(&control, json!({"command":"shutdown"})).await.unwrap();
            if let Some(mut process) = process {
                process.wait();
            }
            if let Some(daemon) = daemon {
                daemon.join().unwrap();
            }
        }
        let ledger = std::fs::read_to_string(directory.join("fixture.log")).unwrap();
        assert_eq!(
            ledger
                .lines()
                .filter(
                    |line| serde_json::from_str::<Value>(&line[9..line.len() - 65])
                        .unwrap()
                        .get("power_version")
                        .is_some()
                )
                .count(),
            1
        );
    }
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

//! PostgreSQL dispatch/receipt durability composed with the owned IPC fixture.
//! Includes fresh controller IPC collection and separate pre-admitted composition proofs.
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

fn retime_outcome(value: &mut serde_json::Value, at: u64) {
    use serde_json::{Value, json};
    match value {
        Value::Object(map) => {
            for (key, value) in map {
                if key == "observed_unix_ms" {
                    *value = json!(at);
                } else if key == "observed_at" {
                    *value = json!(chrono::DateTime::from_timestamp_millis(at as i64).unwrap());
                } else {
                    retime_outcome(value, at);
                }
            }
        }
        Value::Array(values) => {
            for value in values {
                retime_outcome(value, at);
            }
        }
        _ => {}
    }
}

/// Supervisor-authored physical observations, bound to the actual accepted
/// request. The real evaluator must establish ownership and Clone postcondition.
fn clone_outcome_document(
    reads: &FixtureProvisioningReadsV2,
    inventory: &FixtureCloneReads,
    source: &ProvisioningVmConfigV1,
    envelope: &FixtureCloneRequest,
    upid: &Upid,
) -> serde_json::Value {
    use serde_json::json;
    let request = envelope.request().clone_request();
    let vm = request.vm();
    let mut target = serde_json::to_value(source).unwrap();
    target["vmid"] = json!(vm.target_vmid());
    target["name"] = json!(vm.name());
    target["template"] = json!(false);
    target["uuid"] = json!("44444444-4444-4444-8444-444444444491");
    target["mac"] = json!("02:00:00:00:09:01");
    target["digest"] = json!("b".repeat(64));
    target["primary_disk"]["storage"] = json!(vm.storage());
    target["primary_disk"]["volume"] = json!("vm-901-disk-0");
    target["fake_clone_provenance"] = json!({
        "operation_id": request.operation_id(), "request_marker": request.request_marker(),
        "source_vmid": vm.source_vmid(), "target_vmid": vm.target_vmid(),
        "request_digest": request.request_digest()
    });
    let mut provisioning = serde_json::to_value(reads).unwrap();
    provisioning["version"] = json!(1);
    provisioning["identity"]["request_sha256"] = json!(envelope.request_sha256());
    provisioning["target_power"] = serde_json::to_value(SeedRead::Observed {
        observed_unix_ms: 1,
        value: SeedPower {
            power: PowerState::Stopped,
            locked: false,
        },
    })
    .unwrap();
    // Preserve the exact serde representation rather than assuming the SeedRead tag.
    provisioning["target_config"] = serde_json::to_value(SeedRead::Observed {
        observed_unix_ms: 1,
        value: SeedConfig::Present {
            config: Box::new(serde_json::from_value(target.clone()).unwrap()),
        },
    })
    .unwrap();
    let mut inventory = serde_json::to_value(inventory).unwrap();
    let SeedRead::Observed {
        value: original, ..
    } = &reads.source_coverage
    else {
        panic!("coverage")
    };
    let target_identity = SeedIdentity {
        status: SeedRead::Observed {
            observed_unix_ms: 1,
            value: PowerState::Stopped,
        },
        coverage: SeedRead::Observed {
            observed_unix_ms: 1,
            value: *original,
        },
        node: vm.node().to_string(),
        vmid: vm.target_vmid().get(),
        name: vm.name().to_string(),
        template: false,
        config_sha256: "b".repeat(64),
        uuid: "44444444-4444-4444-8444-444444444491".parse().unwrap(),
        mac: "02:00:00:00:09:01".into(),
        primary_storage: vm.storage().to_string(),
        primary_volume: "vm-901-disk-0".into(),
    };
    // Existing source inventory remains independently present.
    let mut identities = inventory["cluster_inventory"]["value"]
        .as_array()
        .unwrap()
        .clone();
    identities.push(serde_json::to_value(target_identity).unwrap());
    inventory["cluster_inventory"]["value"] = json!(identities);
    let mut result = json!({"version":1,"provisioning":provisioning,"inventory":inventory,
        "task":FixtureTaskObservation { version:1, identity:FixtureTaskIdentity {
            fixture_id:envelope.fixture_id(), operation:request.operation_id().as_uuid(),
            request_sha256:envelope.request_sha256(), node:vm.node().to_string(), upid:upid.to_string()
        }, observed_unix_ms:1, result:FixtureTaskState::Succeeded {} }});
    retime_outcome(&mut result, chrono::Utc::now().timestamp_millis() as u64);
    result
}

async fn publish_clone_outcome(
    socket: &std::path::Path,
    request: &FixtureCloneRequest,
    observation: serde_json::Value,
) -> serde_json::Value {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let request: serde_json::Value = serde_json::from_slice(&request.encode().unwrap()).unwrap();
    let bytes = serde_json::to_vec(&serde_json::json!({"command":"publish_post_dispatch", "request":request, "observation":observation})).unwrap();
    tokio::time::timeout(Duration::from_secs(1), async {
        let mut stream = tokio::net::UnixStream::connect(socket).await.unwrap();
        stream.write_u32(bytes.len() as u32).await.unwrap();
        stream.write_all(&bytes).await.unwrap();
        let length = stream.read_u32().await.unwrap() as usize;
        assert!(length < 200_000);
        let mut result = vec![0; length];
        stream.read_exact(&mut result).await.unwrap();
        serde_json::from_slice(&result).unwrap()
    })
    .await
    .unwrap()
}

/// A fresh controller collects IPC facts and creates its own attempt and request.
/// Only the supervisor observes the committed request and releases its exact digest.
#[tokio::test]
async fn fresh_controller_late_clone_records_exact_durable_receipt() {
    fresh_controller_clone(false).await;
}

#[tokio::test]
async fn fresh_controller_clone_reaches_satisfied_from_supervisor_publication() {
    fresh_controller_clone(true).await;
}

async fn fresh_controller_clone(publish_outcome: bool) {
    let s = Scenario::new(300, true).await;
    let operation = s.ids.operation(osdeploy_adapter::OsDeployStage::Clone);
    let fixture_id = controller_domain::RunId::new().as_uuid();
    let identity = FixtureReadIdentity {
        fixture_id,
        operation: operation.as_uuid(),
        node: "node-a".into(),
        source_vmid: 900,
        target_vmid: 901,
    };
    let directory = std::path::PathBuf::from("/tmp").join(format!("pglate-{fixture_id}"));
    fs::create_dir(&directory).unwrap();
    fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
    let now = chrono::Utc::now().timestamp_millis() as u64;
    fn observed<T>(at: u64, value: T) -> SeedRead<T> {
        SeedRead::Observed {
            observed_unix_ms: at,
            value,
        }
    }
    let node = NodeName::parse("node-a").unwrap();
    let source = s
        .fake
        .provisioning_vm_config(&node, Vmid::new(900).unwrap())
        .await
        .unwrap();
    let fingerprint = source.template_fingerprint().unwrap();
    let mut source_wire = serde_json::to_value(&source).unwrap();
    source_wire["digest"] = serde_json::json!("a".repeat(64));
    let source: ProvisioningVmConfigV1 = serde_json::from_value(source_wire).unwrap();
    assert_eq!(source.template_fingerprint().unwrap(), fingerprint);
    let reads = FixtureProvisioningReadsV2 {
        version: 2,
        identity: identity.clone(),
        source_config: observed(
            source.observed_at().timestamp_millis() as u64,
            SeedConfig::Present {
                config: Box::new(source.clone()),
            },
        ),
        target_config: observed(now, SeedConfig::Absent {}),
        source_power: observed(
            now,
            SeedPower {
                power: PowerState::Stopped,
                locked: false,
            },
        ),
        target_power: SeedRead::Error {
            observed_unix_ms: now,
            error: SeedReadError::Unavailable,
        },
        source_coverage: observed(now, ProvisioningCoverageV1::Complete),
        target_coverage: observed(now, ProvisioningCoverageV1::Complete),
        deployment_media: {
            let value = s
                .fake
                .provisioning_media(&node, &StorageName::parse("media-store").unwrap())
                .await
                .unwrap();
            observed(value.observed_at().timestamp_millis() as u64, value)
        },
        driver_media: {
            let value = s
                .fake
                .provisioning_media(&node, &StorageName::parse("drivers").unwrap())
                .await
                .unwrap();
            observed(value.observed_at().timestamp_millis() as u64, value)
        },
    };
    fs::write(
        directory.join("provisioning_reads_v2.json"),
        serde_json::to_vec(&reads).unwrap(),
    )
    .unwrap();
    let infrastructure = FixtureCloneReads {
        version: 1,
        fixture_id,
        node: "node-a".into(),
        node_status: observed(
            now,
            SeedNode {
                online: true,
                uptime_seconds: 100,
            },
        ),
        storage: observed(
            now,
            vec![SeedStorage {
                name: "disk-store".into(),
                active: true,
                enabled: true,
                available_bytes: 999999999999,
                content: vec!["images".into()],
            }],
        ),
        bridges: observed(
            now,
            vec![SeedBridge {
                name: "vmbr0".into(),
                active: true,
            }],
        ),
        cluster_inventory: observed(
            now,
            vec![SeedIdentity {
                status: observed(now, PowerState::Stopped),
                coverage: observed(now, ProvisioningCoverageV1::Complete),
                node: "node-a".into(),
                vmid: 900,
                name: "blank-template".into(),
                template: true,
                config_sha256: "a".repeat(64),
                uuid: "33333333-3333-4333-8333-333333333390".parse().unwrap(),
                mac: "02:00:00:00:09:00".into(),
                primary_storage: "disk-store".into(),
                primary_volume: "vm-900-disk-0".into(),
            }],
        ),
    };
    fs::write(
        directory.join("clone_reads.json"),
        serde_json::to_vec(&infrastructure).unwrap(),
    )
    .unwrap();
    let daemon_path = directory.clone();
    let daemon = std::thread::spawn(move || run(&daemon_path, Duration::from_secs(5)).unwrap());
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
    let binding = CheckpointBinding {
        generation: state.generation,
        owner: fixture_id,
        operation: identity.operation,
        point: CheckpointPoint::DispatchCommitted,
    };
    assert!(
        supervisor
            .request(CheckpointRequest::ArmLate {
                binding,
                timeout_ms: 3000,
                identity: identity.clone()
            })
            .await
            .unwrap()
            .ok
    );
    let port = FixtureProvisioningPort::new_late(
        directory.join("client.sock"),
        Duration::from_secs(2),
        identity.clone(),
    )
    .unwrap()
    .with_checkpoint(
        FixtureCheckpointClient::new(directory.join("client.sock"), Duration::from_secs(2))
            .unwrap(),
        binding,
    )
    .unwrap();
    let controller = Arc::new(
        operation_controller::OsDeployController::new_fixture(
            s.db.store.clone(),
            s.db.scheduler(),
            Arc::new(port),
            1,
        )
        .unwrap(),
    );
    controller.open_send_admission().await.unwrap();
    let worker = tokio::spawn({
        let controller = controller.clone();
        async move { controller.run_osdeploy_once(operation).await }
    });
    let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
    let entered = loop {
        let state = supervisor
            .request(CheckpointRequest::Status)
            .await
            .unwrap()
            .state;
        if state.phase == CheckpointPhase::Entered {
            break state;
        }
        if worker.is_finished() {
            let rows: Vec<(serde_json::Value,)> = sqlx::query_as("SELECT payload->'detail' FROM rust_controller.journal_events WHERE payload->>'action'='pve_evaluated'").fetch_all(&s.db.pool).await.unwrap();
            panic!(
                "controller ended before dispatch checkpoint: {:?}; {:?}",
                worker.await,
                rows
            );
        }
        assert!(tokio::time::Instant::now() < deadline);
        tokio::time::sleep(Duration::from_millis(5)).await;
    };
    let committed = s.db.other.load_osdeploy_operation(operation).await.unwrap();
    let dispatch = committed
        .dispatch()
        .expect("independently visible durable dispatch");
    assert!(committed.receipt().is_none());
    let ProvisioningMutationRequestV1::Clone(request) = dispatch.request() else {
        panic!("expected Clone")
    };
    let envelope = FixtureCloneRequest::new(fixture_id, request.clone()).unwrap();
    assert_eq!(
        dispatch.request_sha256(),
        dispatch.request().request_digest().unwrap()
    );
    let reader =
        FixtureReadClient::new(directory.join("client.sock"), Duration::from_secs(1)).unwrap();
    assert!(
        reader
            .accepted_effect(identity.operation, &envelope.request_sha256())
            .await
            .unwrap()
            .is_none()
    );
    let proposal = LateCloneAuthorizationV1 {
        version: 1,
        binding,
        identity: identity.clone(),
        request_sha256: envelope.request_sha256(),
        request: envelope.encode().unwrap(),
        after: VmState {
            disk_bytes: 85899345920,
            pe_configured: false,
        },
    };
    proposal
        .validate_candidate(&identity, binding, &entered, &envelope)
        .unwrap();
    let mut mismatched = proposal.clone();
    mismatched.request_sha256 = "0".repeat(64);
    assert!(
        !supervisor
            .request(CheckpointRequest::AuthorizeRelease {
                committed_request: envelope.encode().unwrap(),
                proposal: mismatched,
            })
            .await
            .unwrap()
            .ok
    );
    assert!(!worker.is_finished());
    assert!(
        reader
            .accepted_effect(identity.operation, &envelope.request_sha256())
            .await
            .unwrap()
            .is_none()
    );
    // Hold only the isolated fixture journal insert. Receipt time is allocated
    // before that insert; the observed lock wait below is our deterministic
    // supervisor barrier before the controller starts its outcome collection.
    let mut publication_barrier = if publish_outcome {
        let mut tx = s.db.pool.begin().await.unwrap();
        sqlx::query("LOCK TABLE rust_controller.journal_events IN SHARE MODE")
            .execute(&mut *tx)
            .await
            .unwrap();
        Some(tx)
    } else {
        None
    };
    assert!(
        supervisor
            .request(CheckpointRequest::AuthorizeRelease {
                committed_request: envelope.encode().unwrap(),
                proposal
            })
            .await
            .unwrap()
            .ok
    );
    if let Some(tx) = publication_barrier.take() {
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                let waiting: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE 'INSERT INTO rust_controller.journal_events%osdeploy:receipt%')")
                    .fetch_one(&s.db.pool).await.unwrap();
                if waiting { break; }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        }).await.expect("controller receipt insert did not reach publication barrier");
        let effect = reader
            .accepted_effect(identity.operation, &envelope.request_sha256())
            .await
            .unwrap()
            .unwrap();
        let receipt = envelope.decode_receipt(effect.receipt().unwrap()).unwrap();
        let MutationReceipt::Task(upid) = receipt.receipt() else {
            panic!("Clone task expected")
        };
        let document = clone_outcome_document(&reads, &infrastructure, &source, &envelope, upid);
        let response =
            publish_clone_outcome(&directory.join("supervisor.sock"), &envelope, document).await;
        assert!(!response.is_null());
        assert!(!worker.is_finished());
        tx.commit().await.unwrap();
    }
    let result = worker.await.unwrap().unwrap();
    // Startup facts alone stay Unknown. Only the supervisor's fresh physical
    // observations allow the real controller evaluator to establish Satisfied.
    assert_eq!(
        result,
        postgres_store::OsDeployProgress::Decided(if publish_outcome {
            controller_domain::ExecutionState::Satisfied
        } else {
            controller_domain::ExecutionState::Unknown
        })
    );
    let durable = s.db.other.load_osdeploy_operation(operation).await.unwrap();
    assert_eq!(durable.attempt_id(), committed.attempt_id());
    let attempts: i64 =
        sqlx::query_scalar("SELECT count(*) FROM rust_controller.attempts WHERE operation_id=$1")
            .bind(operation.as_uuid())
            .fetch_one(&s.db.pool)
            .await
            .unwrap();
    assert_eq!(attempts, 1);
    let effect = reader
        .accepted_effect(identity.operation, &envelope.request_sha256())
        .await
        .unwrap()
        .unwrap();
    let receipt = envelope.decode_receipt(effect.receipt().unwrap()).unwrap();
    assert_eq!(receipt.submission_sequence(), 1);
    assert_eq!(durable.receipt().unwrap().receipt(), receipt.receipt());
    assert_eq!(
        durable.dispatch().unwrap().request_sha256(),
        dispatch.request_sha256()
    );
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
    let mutation =
        FixtureMutationClient::new(directory.join("client.sock"), Duration::from_secs(1)).unwrap();
    assert!(mutation.clone_vm_late(binding, &envelope).await.is_err());
    controller
        .close_and_drain(Duration::from_secs(1))
        .await
        .unwrap();
    daemon.join().unwrap();
    fs::remove_dir_all(directory).unwrap();
}

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

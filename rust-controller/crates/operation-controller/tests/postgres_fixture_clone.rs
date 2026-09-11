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
mod fixture_prefix_process;

#[tokio::test]
#[ignore = "owned prefix worker subprocess entry point"]
async fn fixture_prefix_worker() {
    fixture_prefix_process::run().await;
}

#[tokio::test]
#[ignore = "owned prefix recovery subprocess entry point"]
async fn fixture_prefix_recovery_worker() {
    fixture_prefix_process::run_recovery().await;
}

#[tokio::test]
#[ignore = "owned StartPe SQL reload subprocess entry point"]
async fn fixture_start_response_reload_worker() {
    let dsn = std::env::var("FIXTURE_START_RESPONSE_DSN").unwrap();
    let operation: controller_domain::OperationId =
        serde_json::from_str(&std::env::var("FIXTURE_START_RESPONSE_OPERATION").unwrap()).unwrap();
    let output =
        std::path::PathBuf::from(std::env::var_os("FIXTURE_START_RESPONSE_OUTPUT").unwrap());
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(1)
        .connect(&dsn)
        .await
        .unwrap();
    let store = postgres_store::PgStore::new(pool.clone());
    let channel =
        std::path::PathBuf::from(std::env::var_os("FIXTURE_START_RESPONSE_CHANNEL").unwrap());
    let binding: CheckpointBinding =
        serde_json::from_str(&std::env::var("FIXTURE_START_RESPONSE_BINDING").unwrap()).unwrap();
    let client = FixtureCheckpointClient::new(channel.clone(), Duration::from_secs(2)).unwrap();
    let route = client.shared_history_provenance(&binding).unwrap();
    let snapshot = store.load_osdeploy_operation(operation).await.unwrap();
    let stored = store
        .load_fixture_start_pe_response_for_route(operation, &route)
        .await
        .unwrap()
        .unwrap();
    let bytes = stored.original_receipt();
    let wrong_client = FixtureCheckpointClient::new(
        channel.with_file_name("wrong-channel.sock"),
        Duration::from_secs(2),
    )
    .unwrap();
    let wrong_route = wrong_client.shared_history_provenance(&binding).unwrap();
    assert!(
        store
            .load_fixture_start_pe_response_for_route(operation, &wrong_route)
            .await
            .is_err()
    );
    fs::write(output, serde_json::to_vec(&serde_json::json!({"pid":std::process::id(),"revision":snapshot.revision(),"accepted_at":snapshot.receipt().unwrap().accepted_at(),"response":bytes,"route_sha256":route.sha256()})).unwrap()).unwrap();
}

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

async fn await_database_observation_time(pool: &sqlx::PgPool) {
    // PostgreSQL runs in an isolated Linux VM; do not stamp observations before
    // its independently allocated receipt time when host/guest clocks differ.
    let database_now: chrono::DateTime<chrono::Utc> =
        sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(pool)
            .await
            .unwrap();
    tokio::time::timeout(Duration::from_secs(2), async {
        while chrono::Utc::now().timestamp_millis() <= database_now.timestamp_millis() {
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .expect("fixture host clock did not catch database receipt clock");
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
    fresh_controller_clone(false, false, None).await;
}

#[tokio::test]
async fn fresh_controller_clone_reaches_satisfied_from_supervisor_publication() {
    fresh_controller_clone(true, false, None).await;
}

#[tokio::test]
async fn fresh_controller_clone_then_disk_capacity_then_configure_pe_reaches_satisfied() {
    fresh_controller_clone(true, true, None).await;
}

#[tokio::test]
async fn configure_worker_death_before_publication_preserves_prefix_and_uncertainty() {
    fresh_controller_clone(true, true, Some(ConfigureCrash::BeforePublication)).await;
}

#[tokio::test]
async fn configure_worker_death_after_publication_preserves_prefix_and_uncertainty() {
    fresh_controller_clone(true, true, Some(ConfigureCrash::AfterPublication)).await;
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum ConfigureCrash {
    StartAtWrite,
    BeforePublication,
    AfterPublication,
    AfterReceipt,
}

#[tokio::test]
async fn start_pe_controller_death_during_write_rolls_back_original_response() {
    fresh_controller_clone(true, true, Some(ConfigureCrash::StartAtWrite)).await;
}

#[tokio::test]
async fn configure_worker_death_after_receipt_reconciles_exact_prefix_to_satisfied() {
    fresh_controller_clone(true, true, Some(ConfigureCrash::AfterReceipt)).await;
}

async fn fresh_controller_clone(
    publish_outcome: bool,
    resize_after: bool,
    crash_configure: Option<ConfigureCrash>,
) {
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
    let daemon_lifetime = if crash_configure == Some(ConfigureCrash::StartAtWrite) {
        15
    } else if crash_configure == Some(ConfigureCrash::AfterReceipt) {
        45
    } else {
        10
    };
    let daemon = std::thread::spawn(move || {
        run(&daemon_path, Duration::from_secs(daemon_lifetime)).unwrap()
    });
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
    let worker = fixture_prefix_process::spawn(
        &directory,
        s.db.pool.connect_options().to_url_lossy().to_string(),
        identity.clone(),
        fixture_prefix_process::Setup::Clone(binding),
    );
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
        await_database_observation_time(&s.db.pool).await;
        let document = clone_outcome_document(&reads, &infrastructure, &source, &envelope, upid);
        let response =
            publish_clone_outcome(&directory.join("supervisor.sock"), &envelope, document).await;
        assert!(!response.is_null());
        assert!(!worker.is_finished());
        tx.commit().await.unwrap();
    }
    let result = worker.await.unwrap().unwrap();
    if publish_outcome
        && result
            != postgres_store::OsDeployProgress::Decided(
                controller_domain::ExecutionState::Satisfied,
            )
    {
        let rows: Vec<(serde_json::Value,)> = sqlx::query_as("SELECT payload FROM rust_controller.journal_events WHERE operation_id=$1 AND payload->>'action'='pve_evaluated'").bind(operation.as_uuid()).fetch_all(&s.db.pool).await.unwrap();
        eprintln!("Clone evaluation diagnostic: {rows:?}");
    }
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
    if resize_after {
        fresh_resize_after_clone(
            &s,
            &directory,
            fixture_id,
            &envelope,
            effect.receipt().unwrap(),
            &reads,
            &infrastructure,
            &source,
            crash_configure,
        )
        .await;
    }
    daemon.join().unwrap();
    if crash_configure.is_some_and(|c| c != ConfigureCrash::StartAtWrite) {
        // The original daemon thread has terminated. Reopen its durable ledger
        // in a fresh daemon and independently verify the full prefix survived.
        fs::remove_file(directory.join("client.sock")).unwrap();
        fs::remove_file(directory.join("supervisor.sock")).unwrap();
        let daemon_path = directory.clone();
        let recovered =
            std::thread::spawn(move || run(&daemon_path, Duration::from_secs(2)).unwrap());
        let restored_reader =
            FixtureReadClient::new(directory.join("client.sock"), Duration::from_secs(1)).unwrap();
        let status = tokio::time::timeout(Duration::from_secs(1), async {
            loop {
                if let Ok(status) = restored_reader.status().await {
                    break status;
                }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
        assert_eq!((status.attempts, status.effects), (3, 3));
        recovered.join().unwrap();
        eprintln!("owned_prefix_recovery_ledger {}", directory.display());
        return;
    }
    fs::remove_dir_all(directory).unwrap();
}

#[allow(clippy::too_many_arguments)]
async fn fresh_resize_after_clone(
    s: &Scenario,
    directory: &std::path::Path,
    fixture_id: sqlx::types::Uuid,
    predecessor: &FixtureCloneRequest,
    original_receipt: &[u8],
    reads: &FixtureProvisioningReadsV2,
    infrastructure: &FixtureCloneReads,
    source: &ProvisioningVmConfigV1,
    crash_configure: Option<ConfigureCrash>,
) {
    use serde_json::json;
    let operation = s
        .ids
        .operation(osdeploy_adapter::OsDeployStage::DiskCapacity);
    let supervisor =
        FixtureCheckpointClient::new(directory.join("supervisor.sock"), Duration::from_secs(2))
            .unwrap();
    let generation = supervisor
        .stage_request(StageCheckpointRequest::Status)
        .await
        .unwrap()
        .generation;
    let owner = controller_domain::RunId::new().as_uuid();
    let port = FixtureProvisioningPort::new_late(
        directory.join("client.sock"),
        Duration::from_secs(2),
        FixtureReadIdentity {
            fixture_id,
            operation: operation.as_uuid(),
            node: "node-a".into(),
            source_vmid: 900,
            target_vmid: 901,
        },
    )
    .unwrap()
    .with_late_resize_after_legacy_clone(
        FixtureCheckpointClient::new(directory.join("client.sock"), Duration::from_secs(2))
            .unwrap(),
        generation,
        owner,
        predecessor.clone(),
        original_receipt.to_vec(),
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
    let worker = fixture_prefix_process::spawn(
        directory,
        s.db.pool.connect_options().to_url_lossy().to_string(),
        FixtureReadIdentity {
            fixture_id,
            operation: operation.as_uuid(),
            node: "node-a".into(),
            source_vmid: 900,
            target_vmid: 901,
        },
        fixture_prefix_process::Setup::Resize {
            generation,
            owner,
            predecessor: predecessor.encode().unwrap(),
            receipt: original_receipt.to_vec(),
        },
    );
    let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
    let committed = loop {
        let operation = s.db.other.load_osdeploy_operation(operation).await.unwrap();
        if operation.dispatch().is_some() {
            break operation;
        }
        assert!(
            !worker.is_finished(),
            "resize ended before generated dispatch: {:?}",
            worker.await
        );
        assert!(tokio::time::Instant::now() < deadline);
        tokio::time::sleep(Duration::from_millis(5)).await;
    };
    let dispatch = committed.dispatch().unwrap();
    let ProvisioningMutationRequestV1::GrowDisk(grow) = dispatch.request() else {
        panic!("GrowDisk required")
    };
    assert!(
        predecessor
            .request()
            .binding()
            .same_operation_attempt(grow.predecessor_binding())
    );
    assert_eq!(grow.predecessor_plan(), predecessor.request().plan());
    let request = FixtureStageRequest::new(fixture_id, dispatch.request().clone()).unwrap();
    let identity = FixtureStageIdentity {
        operation: operation.as_uuid(),
        stage: FixtureLedgerStage::DiskCapacity,
        attempt: committed.attempt_id().unwrap().as_uuid(),
        generation,
        owner,
        request_sha256: request.request_sha256(),
    };
    identity.validate_request(&request).unwrap();
    assert!(
        supervisor
            .stage_request(StageCheckpointRequest::Arm {
                identity: identity.clone(),
                timeout_ms: 3000
            })
            .await
            .unwrap()
            .ok
    );
    loop {
        let state = supervisor
            .stage_request(StageCheckpointRequest::Status)
            .await
            .unwrap();
        if state.phase == CheckpointPhase::Entered {
            assert_eq!(state.identity, Some(identity.clone()));
            break;
        }
        assert!(tokio::time::Instant::now() < deadline);
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    let reader =
        FixtureReadClient::new(directory.join("client.sock"), Duration::from_secs(1)).unwrap();
    assert_eq!(reader.status().await.unwrap().effects, 1);
    assert!(committed.receipt().is_none());
    let mut tx = s.db.pool.begin().await.unwrap();
    sqlx::query("LOCK TABLE rust_controller.journal_events IN SHARE MODE")
        .execute(&mut *tx)
        .await
        .unwrap();
    assert!(
        supervisor
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
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            let waiting: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE 'INSERT INTO rust_controller.journal_events%osdeploy:receipt%')").fetch_one(&s.db.pool).await.unwrap();
            if waiting { break; }
            assert!(!worker.is_finished(), "resize ended before receipt barrier");
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    }).await.unwrap();
    async fn exchange(socket: &std::path::Path, value: serde_json::Value) -> serde_json::Value {
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
    let effect = exchange(
        &directory.join("client.sock"),
        json!({"command":"accepted_stage_effect","identity":identity}),
    )
    .await;
    let receipt_bytes: Vec<u8> =
        serde_json::from_value(effect["accepted_effect"]["receipt"].clone()).unwrap();
    let receipt = request.decode_receipt(&receipt_bytes).unwrap();
    let MutationReceipt::Task(upid) = receipt.receipt() else {
        panic!("resize task")
    };
    assert_eq!(upid.worker_type(), "resize");
    let original = predecessor.decode_receipt(original_receipt).unwrap();
    let MutationReceipt::Task(clone_upid) = original.receipt() else {
        panic!()
    };
    let mut observation =
        clone_outcome_document(reads, infrastructure, source, predecessor, clone_upid);
    observation["provisioning"]["identity"]["operation"] = json!(operation.as_uuid());
    observation["provisioning"]["identity"]["request_sha256"] = json!(request.request_sha256());
    observation["provisioning"]["target_config"]["value"]["config"]["primary_disk"]["capacity_bytes"] = json!(
        request
            .request()
            .plan()
            .expected()
            .effective_capacity_bytes()
    );
    observation["provisioning"]["target_config"]["value"]["config"]["digest"] =
        json!("c".repeat(64));
    for vm in observation["inventory"]["cluster_inventory"]["value"]
        .as_array_mut()
        .unwrap()
    {
        if vm["vmid"] == 901 {
            vm["config_sha256"] = json!("c".repeat(64));
        }
    }
    observation["task"]["identity"]["operation"] = json!(operation.as_uuid());
    observation["task"]["identity"]["request_sha256"] = json!(request.request_sha256());
    observation["task"]["identity"]["upid"] = json!(upid.to_string());
    await_database_observation_time(&s.db.pool).await;
    retime_outcome(
        &mut observation,
        chrono::Utc::now().timestamp_millis() as u64,
    );
    let publication = exchange(&directory.join("supervisor.sock"), json!({"command":"publish_stage_post_dispatch","identity":identity,"request":serde_json::from_slice::<serde_json::Value>(&request.encode().unwrap()).unwrap(),"observation":observation})).await;
    assert!(publication["daemon_generation"].is_string());
    tx.commit().await.unwrap();
    assert_eq!(
        worker.await.unwrap().unwrap(),
        postgres_store::OsDeployProgress::Decided(controller_domain::ExecutionState::Satisfied)
    );
    let durable = s.db.other.load_osdeploy_operation(operation).await.unwrap();
    assert_eq!(durable.dispatch().unwrap().request(), dispatch.request());
    assert_eq!(durable.receipt().unwrap().receipt(), receipt.receipt());
    let status = reader.status().await.unwrap();
    assert_eq!((status.attempts, status.effects), (2, 2));
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
    controller
        .close_and_drain(Duration::from_secs(1))
        .await
        .unwrap();
    fresh_configure_after_resize(
        s,
        directory,
        fixture_id,
        &identity,
        &request,
        &receipt_bytes,
        &observation,
        crash_configure,
    )
    .await;
}

#[allow(clippy::too_many_arguments)]
async fn fresh_configure_after_resize(
    s: &Scenario,
    directory: &std::path::Path,
    fixture_id: sqlx::types::Uuid,
    predecessor_identity: &FixtureStageIdentity,
    predecessor: &FixtureStageRequest,
    original_receipt: &[u8],
    previous_observation: &serde_json::Value,
    crash_configure: Option<ConfigureCrash>,
) {
    use serde_json::json;
    let operation = s
        .ids
        .operation(osdeploy_adapter::OsDeployStage::ConfigurePe);
    let supervisor =
        FixtureCheckpointClient::new(directory.join("supervisor.sock"), Duration::from_secs(2))
            .unwrap();
    let generation = supervisor
        .stage_request(StageCheckpointRequest::Status)
        .await
        .unwrap()
        .generation;
    let owner = controller_domain::RunId::new().as_uuid();
    let port = FixtureProvisioningPort::new_late(
        directory.join("client.sock"),
        Duration::from_secs(2),
        FixtureReadIdentity {
            fixture_id,
            operation: operation.as_uuid(),
            node: "node-a".into(),
            source_vmid: 900,
            target_vmid: 901,
        },
    )
    .unwrap()
    .with_late_configure_after_resize(
        FixtureCheckpointClient::new(directory.join("client.sock"), Duration::from_secs(2))
            .unwrap(),
        generation,
        owner,
        predecessor_identity.clone(),
        predecessor.clone(),
        original_receipt.to_vec(),
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
    let worker = fixture_prefix_process::spawn(
        directory,
        s.db.pool.connect_options().to_url_lossy().to_string(),
        FixtureReadIdentity {
            fixture_id,
            operation: operation.as_uuid(),
            node: "node-a".into(),
            source_vmid: 900,
            target_vmid: 901,
        },
        fixture_prefix_process::Setup::Configure {
            generation,
            owner,
            predecessor_identity: predecessor_identity.clone(),
            predecessor: predecessor.encode().unwrap(),
            receipt: original_receipt.to_vec(),
        },
    );
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    let committed = loop {
        let operation = s.db.other.load_osdeploy_operation(operation).await.unwrap();
        if operation.dispatch().is_some() {
            break operation;
        }
        assert!(
            !worker.is_finished(),
            "resize ended before generated dispatch: {:?}",
            worker.await
        );
        assert!(tokio::time::Instant::now() < deadline);
        tokio::time::sleep(Duration::from_millis(5)).await;
    };
    let dispatch = committed.dispatch().unwrap();
    let ProvisioningMutationRequestV1::Configure(grow) = dispatch.request() else {
        panic!("ConfigurePe required")
    };
    assert!(
        predecessor
            .request()
            .binding()
            .same_operation_attempt(grow.predecessor_binding())
    );
    assert_eq!(grow.predecessor_plan(), predecessor.request().plan());
    let request = FixtureStageRequest::new(fixture_id, dispatch.request().clone()).unwrap();
    let identity = FixtureStageIdentity {
        operation: operation.as_uuid(),
        stage: FixtureLedgerStage::ConfigurePe,
        attempt: committed.attempt_id().unwrap().as_uuid(),
        generation,
        owner,
        request_sha256: request.request_sha256(),
    };
    identity.validate_request(&request).unwrap();
    assert!(
        supervisor
            .stage_request(StageCheckpointRequest::Arm {
                identity: identity.clone(),
                timeout_ms: 3000
            })
            .await
            .unwrap()
            .ok
    );
    loop {
        let state = supervisor
            .stage_request(StageCheckpointRequest::Status)
            .await
            .unwrap();
        if state.phase == CheckpointPhase::Entered {
            assert_eq!(state.identity, Some(identity.clone()));
            break;
        }
        assert!(
            !worker.is_finished(),
            "configure checkpoint stopped: {:?}",
            worker.await
        );
        assert!(tokio::time::Instant::now() < deadline);
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    let reader =
        FixtureReadClient::new(directory.join("client.sock"), Duration::from_secs(1)).unwrap();
    assert_eq!(reader.status().await.unwrap().effects, 2);
    assert!(committed.receipt().is_none());
    let outcome_hold = if crash_configure == Some(ConfigureCrash::AfterReceipt) {
        sqlx::raw_sql("CREATE FUNCTION rust_controller.fixture_hold_outcome() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.operation_id::text = TG_ARGV[0] AND NEW.payload->>'action' = 'pve_evaluated' THEN PERFORM pg_advisory_xact_lock(91722311); END IF; RETURN NEW; END $$;")
            .execute(&s.db.pool).await.unwrap();
        sqlx::raw_sql(&format!("CREATE TRIGGER fixture_hold_outcome BEFORE INSERT ON rust_controller.journal_events FOR EACH ROW EXECUTE FUNCTION rust_controller.fixture_hold_outcome('{}')", operation.as_uuid())).execute(&s.db.pool).await.unwrap();
        let mut hold = s.db.pool.begin().await.unwrap();
        sqlx::query("SELECT pg_advisory_xact_lock(91722311)")
            .execute(&mut *hold)
            .await
            .unwrap();
        Some(hold)
    } else {
        None
    };
    let mut tx = s.db.pool.begin().await.unwrap();
    sqlx::query("LOCK TABLE rust_controller.journal_events IN SHARE MODE")
        .execute(&mut *tx)
        .await
        .unwrap();
    assert!(
        supervisor
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
                    pe_configured: true
                }
            })
            .await
            .unwrap()
            .ok
    );
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            let waiting: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE 'INSERT INTO rust_controller.journal_events%osdeploy:receipt%')").fetch_one(&s.db.pool).await.unwrap();
            if waiting { break; }
            assert!(!worker.is_finished(), "resize ended before receipt barrier");
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    }).await.unwrap();
    async fn exchange(socket: &std::path::Path, value: serde_json::Value) -> serde_json::Value {
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
    let effect = exchange(
        &directory.join("client.sock"),
        json!({"command":"accepted_stage_effect","identity":identity}),
    )
    .await;
    let receipt_bytes: Vec<u8> =
        serde_json::from_value(effect["accepted_effect"]["receipt"].clone()).unwrap();
    let receipt = request.decode_receipt(&receipt_bytes).unwrap();
    assert_eq!(receipt.receipt(), &MutationReceipt::SynchronousAccepted);
    let mut observation = previous_observation.clone();
    observation.as_object_mut().unwrap().remove("task");
    observation["provisioning"]["identity"]["operation"] = json!(operation.as_uuid());
    observation["provisioning"]["identity"]["request_sha256"] = json!(request.request_sha256());
    let e = request.request().plan().expected();
    let p = e.vm();
    let v = &mut observation["provisioning"]["target_config"]["value"]["config"];
    v["cores"] = json!(p.cores());
    v["memory_mib"] = json!(p.memory_mib());
    v["cpu"] = json!("host");
    v["balloon_mib"] = json!(0);
    v["firmware"] = json!("seabios");
    v["qga_enabled"] = json!(true);
    v["qga_channel"] = json!("virtio");
    v["uuid"] = json!(p.uuid());
    v["system_serial"] = json!(e.system_serial());
    v["mac"] = json!(p.mac());
    v["bridge"] = json!(p.bridge());
    v["primary_disk"]["serial"] = json!(e.disk_serial());
    v["deployment_iso"] = json!({"state":"iso","volid":e.deployment_iso_volid()});
    v["driver_iso"] = json!({"state":"iso","volid":e.driver_iso_volid()});
    v["boot_profile"] = json!("pe_media");
    v["digest"] = json!("d".repeat(64));
    for vm in observation["inventory"]["cluster_inventory"]["value"]
        .as_array_mut()
        .unwrap()
    {
        if vm["vmid"] == 901 {
            vm["config_sha256"] = json!("d".repeat(64));
            vm["uuid"] = json!(p.uuid());
            vm["mac"] = json!(p.mac());
        }
    }
    await_database_observation_time(&s.db.pool).await;
    retime_outcome(
        &mut observation,
        chrono::Utc::now().timestamp_millis() as u64,
    );
    if !matches!(
        crash_configure,
        Some(ConfigureCrash::BeforePublication | ConfigureCrash::AfterReceipt)
    ) {
        let publication = exchange(&directory.join("supervisor.sock"), json!({"command":"publish_synchronous_stage","identity":identity,"request":serde_json::from_slice::<serde_json::Value>(&request.encode().unwrap()).unwrap(),"observation":observation})).await;
        assert!(publication["daemon_generation"].is_string());
    }
    if crash_configure == Some(ConfigureCrash::AfterReceipt) {
        tx.commit().await.unwrap();
        let persisted = tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                let snapshot = s.db.other.load_osdeploy_operation(operation).await.unwrap();
                let waiting: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND wait_event='advisory')").fetch_one(&s.db.pool).await.unwrap();
                if snapshot.receipt().is_some() && waiting { break snapshot; }
                assert!(!worker.is_finished(), "worker ended before durable receipt death gate");
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        }).await.unwrap();
        assert_eq!(
            persisted.state(),
            controller_domain::ExecutionState::Running
        );
        assert_eq!(persisted.receipt().unwrap().receipt(), receipt.receipt());
        assert_eq!(persisted.dispatch(), committed.dispatch());
        fs::write(
            directory.join(format!("worker-{}.kill", operation.as_uuid())),
            b"kill after independently observed durable receipt",
        )
        .unwrap();
        assert!(worker.await.unwrap().is_err());
        outcome_hold.unwrap().rollback().await.unwrap();
        sqlx::raw_sql("DROP TRIGGER fixture_hold_outcome ON rust_controller.journal_events; DROP FUNCTION rust_controller.fixture_hold_outcome();").execute(&s.db.pool).await.unwrap();
        let publish_for_recovery = async {
            let ready = directory.join(format!("worker-{}.await-publication", operation.as_uuid()));
            tokio::time::timeout(Duration::from_secs(40), async {
                while !ready.exists() {
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
            })
            .await
            .unwrap();
            // New supervisor collection after lease expiry: independently verify
            // the still-live daemon's physical state before first publication.
            assert_eq!(
                reader.world(901).await.unwrap(),
                Some(VmState {
                    disk_bytes: request
                        .request()
                        .plan()
                        .expected()
                        .effective_capacity_bytes(),
                    pe_configured: true
                })
            );
            let current = exchange(
                &directory.join("client.sock"),
                json!({"command":"accepted_stage_effect","identity":identity}),
            )
            .await;
            let current_receipt: Vec<u8> =
                serde_json::from_value(current["accepted_effect"]["receipt"].clone()).unwrap();
            assert_eq!(current_receipt, receipt_bytes);
            await_database_observation_time(&s.db.pool).await;
            retime_outcome(
                &mut observation,
                chrono::Utc::now().timestamp_millis() as u64,
            );
            let published = exchange(&directory.join("supervisor.sock"), json!({"command":"publish_synchronous_stage","identity":identity,"request":serde_json::from_slice::<serde_json::Value>(&request.encode().unwrap()).unwrap(),"observation":observation})).await;
            assert!(published["daemon_generation"].is_string());
            fs::write(
                directory.join(format!("worker-{}.publication-ready", operation.as_uuid())),
                b"fresh supervisor observation published",
            )
            .unwrap();
        };
        let ((), ()) = tokio::join!(
            fixture_prefix_process::recover(
                directory,
                s.db.pool.connect_options().to_url_lossy().to_string(),
                operation
            ),
            publish_for_recovery
        );
        let recovered = s.db.other.load_osdeploy_operation(operation).await.unwrap();
        assert_eq!(
            recovered.state(),
            controller_domain::ExecutionState::Satisfied
        );
        assert_eq!(recovered.attempt_id(), persisted.attempt_id());
        assert_eq!(recovered.dispatch(), persisted.dispatch());
        assert_eq!(recovered.receipt(), persisted.receipt());
        for stage in [
            osdeploy_adapter::OsDeployStage::Clone,
            osdeploy_adapter::OsDeployStage::DiskCapacity,
        ] {
            assert_eq!(
                s.db.other
                    .load_osdeploy_operation(s.ids.operation(stage))
                    .await
                    .unwrap()
                    .state(),
                controller_domain::ExecutionState::Satisfied
            );
        }
        let final_status = reader.status().await.unwrap();
        assert_eq!((final_status.attempts, final_status.effects), (3, 3));
        return;
    }
    if crash_configure.is_some_and(|c| c != ConfigureCrash::StartAtWrite) {
        // The independent effect read and PostgreSQL lock wait establish the
        // window. The killed process has no opportunity to persist its receipt.
        assert!(
            s.db.other
                .load_osdeploy_operation(operation)
                .await
                .unwrap()
                .receipt()
                .is_none()
        );
        fs::write(
            directory.join(format!("worker-{}.kill", operation.as_uuid())),
            b"kill owned worker",
        )
        .unwrap();
        assert!(worker.await.unwrap().is_err());
        tx.rollback().await.unwrap();
        let status = reader.status().await.unwrap();
        assert_eq!((status.attempts, status.effects), (3, 3));
        fixture_prefix_process::recover(
            directory,
            s.db.pool.connect_options().to_url_lossy().to_string(),
            operation,
        )
        .await;
        for stage in [
            osdeploy_adapter::OsDeployStage::Clone,
            osdeploy_adapter::OsDeployStage::DiskCapacity,
        ] {
            assert_eq!(
                s.db.other
                    .load_osdeploy_operation(s.ids.operation(stage))
                    .await
                    .unwrap()
                    .state(),
                controller_domain::ExecutionState::Satisfied
            );
        }
        assert_eq!(
            s.db.other
                .load_osdeploy_operation(operation)
                .await
                .unwrap()
                .state(),
            controller_domain::ExecutionState::Unknown
        );
        return;
    }
    tx.commit().await.unwrap();
    assert_eq!(
        worker.await.unwrap().unwrap(),
        postgres_store::OsDeployProgress::Decided(controller_domain::ExecutionState::Satisfied)
    );
    let durable = s.db.other.load_osdeploy_operation(operation).await.unwrap();
    assert_eq!(durable.dispatch().unwrap().request(), dispatch.request());
    assert_eq!(durable.receipt().unwrap().receipt(), receipt.receipt());
    let status = reader.status().await.unwrap();
    assert_eq!((status.attempts, status.effects), (3, 3));
    let restored = FixtureProvisioningPort::new_late(
        directory.join("client.sock"),
        Duration::from_secs(2),
        FixtureReadIdentity {
            fixture_id,
            operation: operation.as_uuid(),
            node: "node-a".into(),
            source_vmid: 900,
            target_vmid: 901,
        },
    )
    .unwrap()
    .with_late_configure_after_resize(
        FixtureCheckpointClient::new(directory.join("client.sock"), Duration::from_secs(2))
            .unwrap(),
        generation,
        owner,
        predecessor_identity.clone(),
        predecessor.clone(),
        original_receipt.to_vec(),
    )
    .unwrap()
    .with_late_configure_receipt(dispatch.request(), receipt_bytes.clone())
    .unwrap();
    assert_eq!(
        restored.submit_provisioning(dispatch.request()).await,
        Err(PveWriteError::Rejected)
    );
    assert!(
        restored
            .provisioning_checkpoint(dispatch.request())
            .await
            .is_err()
    );
    let physical = restored
        .provisioning_vm_config(&NodeName::parse("node-a").unwrap(), Vmid::new(901).unwrap())
        .await
        .unwrap();
    assert_eq!(
        physical.boot_profile(),
        Some(ProvisioningBootProfile::PeMedia)
    );
    assert_eq!(reader.status().await.unwrap().effects, 3);
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
    controller
        .close_and_drain(Duration::from_secs(1))
        .await
        .unwrap();
    if crash_configure == Some(ConfigureCrash::StartAtWrite) {
        start_pe_controller_death(
            s,
            directory,
            fixture_id,
            &identity,
            &request,
            &receipt_bytes,
            &restored,
        )
        .await;
        return;
    }
    capture_start_pe_after_genuine_prefix(
        s,
        directory,
        fixture_id,
        &identity,
        &request,
        &receipt_bytes,
        &restored,
    )
    .await;
}

#[allow(clippy::too_many_arguments)]
async fn start_pe_controller_death(
    s: &Scenario,
    directory: &std::path::Path,
    fixture_id: sqlx::types::Uuid,
    predecessor_identity: &FixtureStageIdentity,
    predecessor: &FixtureStageRequest,
    predecessor_receipt: &[u8],
    observer: &FixtureProvisioningPort,
) {
    let op = s.ids.operation(osdeploy_adapter::OsDeployStage::StartPe);
    let supervisor =
        FixtureCheckpointClient::new(directory.join("supervisor.sock"), Duration::from_secs(2))
            .unwrap();
    let generation = supervisor
        .stage_request(StageCheckpointRequest::Status)
        .await
        .unwrap()
        .generation;
    let owner = sqlx::types::Uuid::now_v7();
    let mut hold = s.db.pool.begin().await.unwrap();
    sqlx::query("SELECT pg_advisory_xact_lock(68123002)")
        .execute(&mut *hold)
        .await
        .unwrap();
    sqlx::raw_sql("CREATE FUNCTION rust_controller.hold_start_child_response() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_advisory_xact_lock(68123002); RETURN NEW; END $$; CREATE TRIGGER hold_start_child_response AFTER INSERT ON rust_controller.fixture_start_pe_responses FOR EACH ROW EXECUTE FUNCTION rust_controller.hold_start_child_response();").execute(&s.db.pool).await.unwrap();
    let worker = fixture_prefix_process::spawn(
        directory,
        s.db.pool.connect_options().to_url_lossy().to_string(),
        FixtureReadIdentity {
            fixture_id,
            operation: op.as_uuid(),
            node: "node-a".into(),
            source_vmid: 900,
            target_vmid: 901,
        },
        fixture_prefix_process::Setup::Start {
            generation,
            owner,
            predecessor_identity: predecessor_identity.clone(),
            predecessor: predecessor.encode().unwrap(),
            receipt: predecessor_receipt.to_vec(),
        },
    );
    let committed = tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let snapshot = s.db.other.load_osdeploy_operation(op).await.unwrap();
            if snapshot.dispatch().is_some() {
                break snapshot;
            }
            assert!(
                !worker.is_finished(),
                "StartPe worker ended before dispatch"
            );
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .expect("StartPe controller did not reach dispatch");
    let request =
        FixtureStageRequest::new(fixture_id, committed.dispatch().unwrap().request().clone())
            .unwrap();
    let identity = FixtureStageIdentity {
        operation: op.as_uuid(),
        stage: FixtureLedgerStage::StartPe,
        attempt: committed.attempt_id().unwrap().as_uuid(),
        generation,
        owner,
        request_sha256: request.request_sha256(),
    };
    assert!(
        supervisor
            .stage_request(StageCheckpointRequest::Arm {
                identity: identity.clone(),
                timeout_ms: 3000
            })
            .await
            .unwrap()
            .ok
    );
    tokio::time::timeout(Duration::from_secs(2), async {
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
            assert!(!worker.is_finished());
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .unwrap();
    let power = observer
        .vm_status(&NodeName::parse("node-a").unwrap(), Vmid::new(901).unwrap())
        .await
        .unwrap();
    assert_eq!(power.power(), PowerState::Stopped);
    assert_eq!(power.locked(), Some(false));
    assert!(
        supervisor
            .stage_request(StageCheckpointRequest::AuthorizeStartPe {
                identity: identity.clone(),
                request: request.encode().unwrap(),
                committed_request: request.encode().unwrap(),
                power: StartPePowerAuthorizationV1 {
                    version: 1,
                    predecessor: predecessor_identity.clone(),
                    predecessor_request: predecessor.encode().unwrap(),
                    predecessor_receipt: predecessor_receipt.to_vec(),
                    power: SeedPower {
                        power: power.power(),
                        locked: power.locked().unwrap()
                    }
                },
            })
            .await
            .unwrap()
            .ok
    );
    let backend = tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let waiting: Option<i32> = sqlx::query_scalar("SELECT pid FROM pg_stat_activity WHERE datname=current_database() AND usename=current_user AND wait_event='advisory' AND query LIKE 'INSERT INTO rust_controller.fixture_start_pe_responses%'").fetch_optional(&s.db.pool).await.unwrap();
            if let Some(pid) = waiting { break pid; }
            assert!(!worker.is_finished(), "StartPe worker ended before original response write");
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    }).await.expect("StartPe original-response write barrier not reached");
    let ledger = fs::read(directory.join("fixture.log")).unwrap();
    fs::write(
        directory.join(format!("worker-{}.kill", op.as_uuid())),
        b"kill owned StartPe worker at response transaction",
    )
    .unwrap();
    assert!(worker.await.unwrap().is_err());
    hold.rollback().await.unwrap();
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let exists: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=$1 AND datname=current_database())").bind(backend).fetch_one(&s.db.pool).await.unwrap();
            if !exists { break; }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    }).await.expect("killed controller backend did not close");
    let restored = s.db.other.load_osdeploy_operation(op).await.unwrap();
    assert!(restored.receipt().is_none());
    assert_eq!(restored.state(), controller_domain::ExecutionState::Running);
    assert_eq!(restored.dispatch(), committed.dispatch());
    let rows: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM rust_controller.fixture_start_pe_responses WHERE operation_id=$1",
    )
    .bind(op.as_uuid())
    .fetch_one(&s.db.pool)
    .await
    .unwrap();
    assert_eq!(rows, 0);
    assert_eq!(fs::read(directory.join("fixture.log")).unwrap(), ledger);
    let reader =
        FixtureReadClient::new(directory.join("client.sock"), Duration::from_secs(2)).unwrap();
    let status = reader.status().await.unwrap();
    assert_eq!((status.attempts, status.effects), (4, 4));
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
}

#[allow(clippy::too_many_arguments)]
async fn capture_start_pe_after_genuine_prefix(
    s: &Scenario,
    directory: &std::path::Path,
    fixture_id: sqlx::types::Uuid,
    predecessor_identity: &FixtureStageIdentity,
    predecessor: &FixtureStageRequest,
    predecessor_receipt: &[u8],
    observer: &FixtureProvisioningPort,
) {
    let scheduler = s.db.scheduler().with_fixture_start_pe();
    let op = s.ids.operation(osdeploy_adapter::OsDeployStage::StartPe);
    let grant = scheduler
        .claim_osdeploy_bound(op, s.ids.workflow_sha256(), 1)
        .await
        .unwrap()
        .unwrap();
    scheduler
        .start_osdeploy_bound(&grant, s.ids.workflow_sha256())
        .await
        .unwrap();
    let snapshot = s.db.store.load_osdeploy_operation(op).await.unwrap();
    let context =
        s.db.store
            .load_osdeploy_pve_context(
                op,
                snapshot.revision(),
                ProvisioningEvaluationModeV1::Preflight,
            )
            .await
            .unwrap();
    let evidence = s.collect_with(&context, observer).await;
    let event =
        s.db.store
            .record_osdeploy_pve_evidence(op, grant.attempt_id(), snapshot.revision(), &evidence)
            .await
            .unwrap();
    let revision =
        s.db.store
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .revision();
    let request =
        s.db.store
            .prepare_osdeploy_pve_request(op, revision, event)
            .await
            .unwrap();
    let (permit, capture) = scheduler
        .begin_osdeploy_pve_dispatch(&grant, revision, event, &request)
        .await
        .unwrap();
    let supervisor =
        FixtureCheckpointClient::new(directory.join("supervisor.sock"), Duration::from_secs(2))
            .unwrap();
    let generation = supervisor
        .stage_request(StageCheckpointRequest::Status)
        .await
        .unwrap()
        .generation;
    let owner = sqlx::types::Uuid::now_v7();
    let envelope = FixtureStageRequest::new(fixture_id, request.clone()).unwrap();
    let identity = FixtureStageIdentity {
        operation: op.as_uuid(),
        stage: FixtureLedgerStage::StartPe,
        attempt: grant.attempt_id().as_uuid(),
        generation,
        owner,
        request_sha256: envelope.request_sha256(),
    };
    let build_start_port = || {
        FixtureProvisioningPort::new_late(
            directory.join("client.sock"),
            Duration::from_secs(2),
            FixtureReadIdentity {
                fixture_id,
                operation: op.as_uuid(),
                node: "node-a".into(),
                source_vmid: 900,
                target_vmid: 901,
            },
        )
        .unwrap()
        .with_late_start_after_configure(
            FixtureCheckpointClient::new(directory.join("client.sock"), Duration::from_secs(2))
                .unwrap(),
            generation,
            owner,
            predecessor_identity.clone(),
            predecessor.clone(),
        )
        .unwrap()
    };
    assert!(
        build_start_port()
            .with_late_start_preflight_receipt(b"{}".to_vec())
            .is_err()
    );
    assert!(
        build_start_port()
            .with_late_start_preflight_receipt(predecessor_receipt.to_vec())
            .unwrap()
            .with_late_start_preflight_receipt(predecessor_receipt.to_vec())
            .is_err()
    );
    let port = Arc::new(
        build_start_port()
            .with_late_start_preflight_receipt(predecessor_receipt.to_vec())
            .unwrap(),
    );
    let vm = request.plan().expected().vm();
    let mut respelled = b" \n".to_vec();
    respelled.extend_from_slice(predecessor_receipt);
    assert!(
        build_start_port()
            .with_late_start_preflight_receipt(respelled)
            .is_err()
    );
    assert_eq!(
        port.provisioning_vm_config(vm.node(), vm.target_vmid())
            .await
            .unwrap(),
        observer
            .provisioning_vm_config(vm.node(), vm.target_vmid())
            .await
            .unwrap()
    );
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
    let worker_port = port.clone();
    let worker_request = request.clone();
    let checkpoint =
        tokio::spawn(async move { worker_port.provisioning_checkpoint(&worker_request).await });
    tokio::time::timeout(Duration::from_secs(2), async {
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
    let power = observer
        .vm_status(&NodeName::parse("node-a").unwrap(), Vmid::new(901).unwrap())
        .await
        .unwrap();
    assert_eq!(power.power(), PowerState::Stopped);
    assert_eq!(power.locked(), Some(false));
    assert!(
        supervisor
            .stage_request(StageCheckpointRequest::AuthorizeStartPe {
                identity,
                request: envelope.encode().unwrap(),
                committed_request: envelope.encode().unwrap(),
                power: StartPePowerAuthorizationV1 {
                    version: 1,
                    predecessor: predecessor_identity.clone(),
                    predecessor_request: predecessor.encode().unwrap(),
                    predecessor_receipt: predecessor_receipt.to_vec(),
                    power: SeedPower {
                        power: power.power(),
                        locked: power.locked().unwrap()
                    }
                },
            })
            .await
            .unwrap()
            .ok
    );
    checkpoint.await.unwrap().unwrap();
    assert_eq!(
        port.provisioning_vm_config(vm.node(), vm.target_vmid())
            .await,
        Err(PveReadError::TransportUnavailable)
    );
    let semantic = permit.submit_fake_once(port.as_ref()).await.unwrap();
    let original = port.captured_start_pe_response().unwrap();
    let controller_port: &dyn pve_port::fixture_ipc::ControllerFixturePort = port.as_ref();
    let controller_capture = controller_port
        .original_start_pe_response()
        .unwrap()
        .unwrap();
    assert_eq!(
        controller_capture.original_receipt(),
        original.original_receipt()
    );
    assert_eq!(
        controller_capture.provenance_sha256(),
        original.provenance_sha256()
    );
    let input = capture.bind_fixture_start_pe_response(&original).unwrap();
    assert_eq!(input.semantic_receipt(), &semantic);
    let ledger = fs::read(directory.join("fixture.log")).unwrap();
    // Inject failure after the original envelope row is inserted, while still
    // inside the semantic receipt/journal transaction.
    sqlx::raw_sql("CREATE FUNCTION rust_controller.reject_original_fixture_response() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected original response failure'; END $$; CREATE TRIGGER reject_original_fixture_response AFTER INSERT ON rust_controller.fixture_start_pe_responses FOR EACH ROW EXECUTE FUNCTION rust_controller.reject_original_fixture_response();").execute(&s.db.pool).await.unwrap();
    assert!(
        scheduler
            .record_fixture_start_pe_receipt(&input)
            .await
            .is_err()
    );
    assert!(
        s.db.other
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .receipt()
            .is_none()
    );
    let count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM rust_controller.fixture_start_pe_responses WHERE operation_id=$1",
    )
    .bind(op.as_uuid())
    .fetch_one(&s.db.pool)
    .await
    .unwrap();
    assert_eq!(count, 0);
    sqlx::raw_sql("DROP TRIGGER reject_original_fixture_response ON rust_controller.fixture_start_pe_responses; DROP FUNCTION rust_controller.reject_original_fixture_response();").execute(&s.db.pool).await.unwrap();
    // Terminate only the identified PostgreSQL backend of this owned fixture's
    // blocked original-response INSERT. This proves connection-loss rollback,
    // not controller-process death (the closed capture remains in this process).
    let mut write_hold = s.db.pool.begin().await.unwrap();
    sqlx::query("SELECT pg_advisory_xact_lock(68123001)")
        .execute(&mut *write_hold)
        .await
        .unwrap();
    sqlx::raw_sql("CREATE FUNCTION rust_controller.hold_original_fixture_response() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_advisory_xact_lock(68123001); RETURN NEW; END $$; CREATE TRIGGER hold_original_fixture_response AFTER INSERT ON rust_controller.fixture_start_pe_responses FOR EACH ROW EXECUTE FUNCTION rust_controller.hold_original_fixture_response();").execute(&s.db.pool).await.unwrap();
    let terminate_exact_backend = async {
        let pid: i32 = tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                let pid: Option<i32> = sqlx::query_scalar("SELECT pid FROM pg_stat_activity WHERE datname=current_database() AND usename=current_user AND wait_event='advisory' AND query LIKE 'INSERT INTO rust_controller.fixture_start_pe_responses%' AND pid<>pg_backend_pid()")
                    .fetch_optional(&s.db.pool).await.unwrap();
                if let Some(pid) = pid { break pid; }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        }).await.expect("original-response backend did not reach write barrier");
        let killed: bool = sqlx::query_scalar("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE pid=$1 AND datname=current_database() AND usename=current_user AND wait_event='advisory' AND query LIKE 'INSERT INTO rust_controller.fixture_start_pe_responses%'")
            .bind(pid).fetch_one(&s.db.pool).await.unwrap();
        assert!(killed);
    };
    let (interrupted, ()) = tokio::join!(
        scheduler.record_fixture_start_pe_receipt(&input),
        terminate_exact_backend
    );
    assert!(interrupted.is_err());
    write_hold.rollback().await.unwrap();
    assert!(
        s.db.other
            .load_osdeploy_operation(op)
            .await
            .unwrap()
            .receipt()
            .is_none()
    );
    let retained: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM rust_controller.fixture_start_pe_responses WHERE operation_id=$1",
    )
    .bind(op.as_uuid())
    .fetch_one(&s.db.pool)
    .await
    .unwrap();
    assert_eq!(retained, 0);
    sqlx::raw_sql("DROP TRIGGER hold_original_fixture_response ON rust_controller.fixture_start_pe_responses; DROP FUNCTION rust_controller.hold_original_fixture_response();").execute(&s.db.pool).await.unwrap();
    let other = s.db.other_scheduler().with_fixture_start_pe();
    let (a, b) = tokio::join!(
        scheduler.record_fixture_start_pe_receipt(&input),
        other.record_fixture_start_pe_receipt(&input)
    );
    a.unwrap();
    b.unwrap();
    let first = s.db.other.load_osdeploy_operation(op).await.unwrap();
    assert_eq!(first.receipt().unwrap().receipt(), &semantic);
    let bytes: Vec<u8> = sqlx::query_scalar("SELECT response_envelope FROM rust_controller.fixture_start_pe_responses WHERE operation_id=$1").bind(op.as_uuid()).fetch_one(&s.db.pool).await.unwrap();
    assert_eq!(bytes, original.original_receipt());
    let stored =
        s.db.other
            .load_fixture_start_pe_response(op)
            .await
            .unwrap()
            .unwrap();
    assert_eq!(stored.identity(), original.identity());
    let route = port.shared_history_provenance().unwrap();
    assert_eq!(original.provenance_sha256(), route.sha256());
    for difference in 0..4 {
        let client = FixtureCheckpointClient::new(
            directory.join(if difference == 3 {
                "other.sock"
            } else {
                "client.sock"
            }),
            Duration::from_secs(2),
        )
        .unwrap();
        let wrong = client
            .shared_history_provenance(&pve_port::fixture_support::CheckpointBinding {
                operation: if difference == 0 {
                    sqlx::types::Uuid::now_v7()
                } else {
                    op.as_uuid()
                },
                generation: if difference == 1 {
                    sqlx::types::Uuid::now_v7()
                } else {
                    generation
                },
                owner: if difference == 2 {
                    sqlx::types::Uuid::now_v7()
                } else {
                    owner
                },
                point: pve_port::fixture_support::CheckpointPoint::DispatchCommitted,
            })
            .unwrap();
        assert!(
            s.db.other
                .load_fixture_start_pe_response_for_route(op, &wrong)
                .await
                .is_err(),
            "route difference {difference}"
        );
    }
    assert!(
        s.db.other
            .load_fixture_start_pe_response_for_route(op, &route)
            .await
            .unwrap()
            .is_some()
    );
    assert_eq!(
        stored.predecessor_identity(),
        original.predecessor_identity()
    );
    assert_eq!(
        stored.request().encode().unwrap(),
        original.request().encode().unwrap()
    );
    assert_eq!(
        stored.predecessor().encode().unwrap(),
        original.predecessor().encode().unwrap()
    );
    assert_eq!(stored.original_receipt(), original.original_receipt());
    assert!(
        s.db.other
            .load_fixture_start_pe_response(predecessor.request().binding().operation_id())
            .await
            .is_err()
    );
    // Fresh store reload preserves the original receipt and its first clock.
    let reopened = postgres_store::PgStore::new(s.db.pool.clone());
    scheduler
        .record_fixture_start_pe_receipt(&input)
        .await
        .unwrap();
    let reloaded = reopened.load_osdeploy_operation(op).await.unwrap();
    assert_eq!(reloaded.receipt(), first.receipt());
    assert_eq!(reloaded.revision(), first.revision());
    // Independent process reconstructs SQL state without either original
    // in-memory capability or a process-local adapter response slot.
    let output_path = directory.join("start-response-reloaded.json");
    let child = tokio::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "fixture_start_response_reload_worker",
            "--ignored",
            "--nocapture",
            "--test-threads=1",
        ])
        .env(
            "FIXTURE_START_RESPONSE_DSN",
            s.db.pool.connect_options().to_url_lossy().to_string(),
        )
        .env(
            "FIXTURE_START_RESPONSE_OPERATION",
            serde_json::to_string(&op).unwrap(),
        )
        .env("FIXTURE_START_RESPONSE_OUTPUT", &output_path)
        .env(
            "FIXTURE_START_RESPONSE_CHANNEL",
            directory.join("client.sock"),
        )
        .env(
            "FIXTURE_START_RESPONSE_BINDING",
            serde_json::to_string(&CheckpointBinding {
                operation: op.as_uuid(),
                generation,
                owner,
                point: CheckpointPoint::DispatchCommitted,
            })
            .unwrap(),
        )
        .kill_on_drop(true)
        .output();
    let child = tokio::time::timeout(Duration::from_secs(10), child)
        .await
        .unwrap()
        .unwrap();
    assert!(child.status.success(), "reload subprocess failed");
    let child_value: serde_json::Value =
        serde_json::from_slice(&fs::read(output_path).unwrap()).unwrap();
    assert_ne!(child_value["pid"], serde_json::json!(std::process::id()));
    assert_eq!(
        child_value["route_sha256"],
        serde_json::json!(route.sha256())
    );
    assert_eq!(child_value["revision"], serde_json::json!(first.revision()));
    assert_eq!(
        child_value["accepted_at"],
        serde_json::json!(first.receipt().unwrap().accepted_at())
    );
    assert_eq!(
        child_value["response"],
        serde_json::json!(original.original_receipt())
    );
    assert_eq!(fs::read(directory.join("fixture.log")).unwrap(), ledger);
    assert!(port.submit_provisioning(&request).await.is_err());
    assert!(s.fake.recorded_provisioning_submissions().is_empty());
    // Reconstruct the pre-0018 table shape only in this owned disposable DB.
    // Keep the genuine IPC/SQL response; never fabricate a historical response.
    sqlx::query(
        "ALTER TABLE rust_controller.fixture_start_pe_responses DROP COLUMN provenance_sha256",
    )
    .execute(&s.db.pool)
    .await
    .unwrap();
    s.db.store.migrate().await.unwrap();
    s.db.store.migrate().await.unwrap(); // Idempotent upgrade, still no backfill.
    let historical: Option<String> = sqlx::query_scalar("SELECT provenance_sha256 FROM rust_controller.fixture_start_pe_responses WHERE operation_id=$1")
        .bind(op.as_uuid()).fetch_one(&s.db.pool).await.unwrap();
    assert!(historical.is_none());
    assert!(
        s.db.other
            .load_fixture_start_pe_response_for_route(op, &route)
            .await
            .is_err()
    );
    assert!(
        scheduler
            .record_fixture_start_pe_receipt(&input)
            .await
            .is_err()
    );
    let unbound =
        s.db.other
            .load_fixture_start_pe_response(op)
            .await
            .unwrap()
            .unwrap();
    assert_eq!(unbound.original_receipt(), original.original_receipt());
    let unchanged = s.db.other.load_osdeploy_operation(op).await.unwrap();
    assert_eq!(unchanged.receipt(), first.receipt());
    assert_eq!(unchanged.revision(), first.revision());
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

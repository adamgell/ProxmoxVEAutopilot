#![cfg(feature = "fixture-ipc")]
#[allow(dead_code)]
mod provisioning_seed_support;
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
        let publish = |observation: &Value| json!({"command":"publish_post_dispatch","request":request_json,"observation":observation});
        assert!(wire(&control, publish(&document)).await.is_err());
        if !restart {
            let mutation =
                FixtureMutationClient::new(socket.clone(), Duration::from_secs(1)).unwrap();
            mutation.clone_vm(&request).await.unwrap();
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
            assert!(wire(&control, publish(&document)).await.is_err());
            assert!(fs::read_dir(&directory).unwrap().any(|file| {
                file.unwrap()
                    .file_name()
                    .to_string_lossy()
                    .starts_with("post-dispatch-")
            }));
        } else {
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

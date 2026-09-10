#![cfg(feature = "fixture-ipc")]
#[allow(dead_code)]
mod provisioning_seed_support;
use provisioning_seed_support::support::*;
use pve_port::{fixture_ipc::FixtureCloneRequest, fixture_support::*, *};
use serde_json::{Value, json};
use uuid::Uuid;

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

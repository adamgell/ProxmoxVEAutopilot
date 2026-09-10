#![cfg(feature = "fixture-ipc")]

mod provisioning_support;
use provisioning_support::*;
use pve_port::{fixture_ipc::FixtureCloneRequest, *};
use serde_json::{Value, json};
use uuid::Uuid;

fn request() -> FixtureCloneRequest {
    FixtureCloneRequest::new(
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
    .unwrap()
}

fn upid() -> Upid {
    Upid::parse("UPID:pve-test:00000001:00000001:00000001:qmclone:900:fake@pve:").unwrap()
}

fn modify(bytes: &[u8], f: impl FnOnce(&mut Value)) -> Vec<u8> {
    let mut v: Value = serde_json::from_slice(bytes).unwrap();
    f(&mut v);
    serde_json::to_vec(&v).unwrap()
}

#[test]
fn request_and_original_task_receipt_round_trip_without_identity_loss() {
    let original = request();
    let restored = FixtureCloneRequest::decode(&original.encode().unwrap()).unwrap();
    assert_eq!(restored, original);
    let bytes = restored.encode_receipt(7, upid()).unwrap();
    let receipt = original.decode_receipt(&bytes).unwrap();
    assert_eq!(receipt.submission_sequence(), 7);
    assert_eq!(receipt.receipt(), &MutationReceipt::Task(upid()));
    assert_eq!(restored.request().binding(), original.request().binding());
}

#[test]
fn request_rejects_unknown_fields_versions_nil_fixture_and_invalid_typed_clone() {
    let bytes = request().encode().unwrap();
    for bad in [
        modify(&bytes, |v| v["version"] = json!(2)),
        modify(&bytes, |v| v["fixture_id"] = json!(Uuid::nil())),
        modify(&bytes, |v| v["effect"] = json!({})),
        modify(&bytes, |v| {
            v["request"]["plan"]["action"] = json!("start_pe")
        }),
    ] {
        assert!(FixtureCloneRequest::decode(&bad).is_err());
    }
    assert!(FixtureCloneRequest::decode(&[]).is_err());
    assert!(FixtureCloneRequest::decode(&vec![b' '; 65_537]).is_err());
}

#[test]
fn receipt_rejects_mismatched_fixture_request_sequence_and_extra_fields() {
    let original = request();
    let bytes = original.encode_receipt(7, upid()).unwrap();
    for bad in [
        modify(&bytes, |v| v["version"] = json!(2)),
        modify(&bytes, |v| v["fixture_id"] = json!(Uuid::from_u128(2))),
        modify(&bytes, |v| v["request_sha256"] = json!("0".repeat(64))),
        modify(&bytes, |v| v["submission_sequence"] = json!(0)),
        modify(&bytes, |v| v["effect"] = json!(true)),
        modify(&bytes, |v| v["receipt"] = json!("synchronous_accepted")),
    ] {
        assert!(original.decode_receipt(&bad).is_err());
    }
    assert!(original.decode_receipt(&vec![b' '; 65_537]).is_err());
    assert!(original.encode_receipt(0, upid()).is_err());
    let changed = FixtureCloneRequest::decode(&modify(&original.encode().unwrap(), |v| {
        v["request"]["clone"]["request_marker"] = json!(Uuid::from_u128(77));
    }))
    .unwrap();
    assert!(changed.decode_receipt(&bytes).is_err());
}

#[test]
fn clone_task_receipt_requires_source_node_worker_and_synthetic_user() {
    let original = request();
    let bytes = original.encode_receipt(1, upid()).unwrap();
    for bad in [
        "UPID:other:00000001:00000001:00000001:qmclone:900:fake@pve:",
        "UPID:pve-test:00000001:00000001:00000001:qmstart:900:fake@pve:",
        "UPID:pve-test:00000001:00000001:00000001:qmclone:101:fake@pve:",
        "UPID:pve-test:00000001:00000001:00000001:qmclone:900:root@pam:",
    ] {
        assert!(
            original
                .encode_receipt(1, Upid::parse(bad).unwrap())
                .is_err()
        );
        let bad = modify(&bytes, |v| v["receipt"] = json!({"task":bad}));
        assert!(original.decode_receipt(&bad).is_err());
    }
}

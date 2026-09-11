#![cfg(feature = "fixture-ipc")]
mod provisioning_support;
use provisioning_support::*;
use pve_port::{fixture_ipc::*, fixture_support::*, *};
use serde_json::json;
use sha2::{Digest, Sha256};
use uuid::Uuid;

#[test]
fn inventory_v2_preserves_opaque_identity_and_rejects_rebinding_and_partial_facts() {
    let request = FixtureStageRequest::new(Uuid::now_v7(), chain()[3].request.clone()).unwrap();
    let identity = FixtureStageIdentity {
        operation: request.request().binding().operation_id().as_uuid(),
        attempt: request.request().binding().attempt_id().as_uuid(),
        generation: Uuid::now_v7(),
        owner: Uuid::now_v7(),
        stage: FixtureLedgerStage::StartPe,
        request_sha256: request.request_sha256(),
    };
    let receipt = request
        .encode_receipt(
            1,
            MutationReceipt::Task(
                Upid::parse("UPID:pve-test:00000001:00000001:00000001:qmstart:101:fake@pve:")
                    .unwrap(),
            ),
        )
        .unwrap();
    let ProvisioningMutationRequestV1::Start(start) = request.request() else {
        panic!()
    };
    let at = time().timestamp_millis() as u64;
    let members:Vec<_> = [source(), start.expected_before().config().clone()].into_iter().map(|config| json!({"identity":{"kind":"pve_digest","value":config.digest()},"config":config,"power":"stopped","coverage":"complete"})).collect();
    let document = json!({"version":2,"fixture_id":request.fixture_id(),"identity":identity,"receipt_sha256":format!("{:x}",Sha256::digest(&receipt)),"observed_unix_ms":at,"coverage":"complete","members":members});
    let decode = |value: &serde_json::Value, receipt: &[u8]| {
        FixtureStageInventoryV2::decode(
            &serde_json::to_vec(value).unwrap(),
            &identity,
            &request,
            receipt,
            at - 1,
            at,
        )
    };
    let decoded = decode(&document, &receipt).unwrap();
    assert_eq!(serde_json::to_value(decoded).unwrap(), document);
    for path in ["owner", "generation", "attempt", "operation"] {
        let mut bad = document.clone();
        bad["identity"][path] = json!(Uuid::now_v7());
        assert!(decode(&bad, &receipt).is_err());
    }
    for (field, value) in [
        ("version", json!(1)),
        ("coverage", json!("partial")),
        ("members", json!([])),
        ("observed_unix_ms", json!(at - 1)),
        ("extra", json!(true)),
    ] {
        let mut bad = document.clone();
        bad[field] = value;
        assert!(decode(&bad, &receipt).is_err());
    }
    let mut rewritten = receipt.clone();
    rewritten.push(b' ');
    assert!(decode(&document, &rewritten).is_err());
    let mut bad = document.clone();
    bad["members"][0]["identity"]["kind"] = json!("canonical_config_sha256_v1");
    assert!(decode(&bad, &receipt).is_err());
    for (pointer, value) in [
        ("/members/0/coverage", json!("partial")),
        ("/members/0/config/locked", json!(true)),
        (
            "/members/0/config/observed_at",
            json!("2026-09-05T11:59:59Z"),
        ),
        (
            "/members/0/config/uuid",
            document["members"][1]["config"]["uuid"].clone(),
        ),
        ("/identity/request_sha256", json!("a".repeat(64))),
        ("/fixture_id", json!(Uuid::now_v7())),
    ] {
        let mut bad = document.clone();
        *bad.pointer_mut(pointer).unwrap() = value;
        assert!(decode(&bad, &receipt).is_err(), "accepted {pointer}");
    }
    let mut missing = document.clone();
    missing["members"].as_array_mut().unwrap().remove(1);
    assert!(decode(&missing, &receipt).is_err());
    assert!(
        FixtureStageInventoryV2::decode(
            &vec![b' '; 65_537],
            &identity,
            &request,
            &receipt,
            at - 1,
            at
        )
        .is_err()
    );
    bad["members"][0]["identity"]["value"] = json!(format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(&source()).unwrap())
    ));
    assert!(decode(&bad, &receipt).is_ok());
    bad["members"][0]["identity"]["value"] = json!("a".repeat(64));
    assert!(decode(&bad, &receipt).is_err());
    let mut bad = document.clone();
    bad["members"][0]["identity"]["value"] = json!("other-digest");
    assert!(decode(&bad, &receipt).is_err());
    let mut bad = document.clone();
    bad["members"]
        .as_array_mut()
        .unwrap()
        .push(document["members"][0].clone());
    assert!(decode(&bad, &receipt).is_err());
}

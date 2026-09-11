use chrono::{DateTime, Utc};
use pve_port::{ClusterVisibility, GuestKind, PveReadError, VisiblePower};
use serde_json::{Value, json};

fn observed_at() -> DateTime<Utc> {
    "2026-09-05T12:00:00Z".parse().unwrap()
}

fn parse(rows: Value) -> Result<ClusterVisibility, PveReadError> {
    ClusterVisibility::from_wire(rows, observed_at())
}

#[test]
fn mixed_inventory_preserves_lxc_and_unknown_metadata() {
    let snapshot = ClusterVisibility::from_wire(
        json!([
            {"vmid":101,"node":"pve-test","type":"qemu","status":"running","template":0},
            {"vmid":102,"node":"pve-test","type":"lxc"}
        ]),
        Utc::now(),
    )
    .unwrap();
    assert_eq!(snapshot.coverage(), "unverified");
    assert_eq!(snapshot.records().len(), 2);
    assert_eq!(snapshot.records()[0].kind(), GuestKind::Qemu);
    assert_eq!(snapshot.records()[0].power(), VisiblePower::Running);
    assert_eq!(snapshot.records()[0].template(), Some(false));
    assert_eq!(snapshot.records()[1].kind(), GuestKind::Lxc);
    assert_eq!(snapshot.records()[1].power(), VisiblePower::Unknown);
    assert_eq!(snapshot.records()[1].template(), None);
    assert_eq!(snapshot.rejected_rows(), 0);
}

#[test]
fn empty_inventory_never_claims_verified_coverage() {
    let snapshot = parse(json!([])).unwrap();
    assert_eq!(snapshot.coverage(), "unverified");
    assert!(snapshot.records().is_empty());
    assert_eq!(snapshot.rejected_rows(), 0);
    assert_eq!(snapshot.observed_at(), observed_at());
}

#[test]
fn unknown_kind_is_preserved_without_retaining_raw_type() {
    let snapshot =
        parse(json!([{"vmid":101,"node":"pve-test","type":"future-guest-canary"}])).unwrap();
    assert_eq!(snapshot.records().len(), 1);
    assert_eq!(snapshot.records()[0].kind(), GuestKind::Unsupported);
    assert_eq!(snapshot.rejected_rows(), 0);
    assert!(
        !serde_json::to_string(&snapshot)
            .unwrap()
            .contains("future-guest-canary")
    );
}

#[test]
fn missing_status_is_unknown() {
    let snapshot = parse(json!([{"vmid":101,"node":"pve-test","type":"qemu"}])).unwrap();
    assert_eq!(snapshot.records()[0].power(), VisiblePower::Unknown);
}

#[test]
fn unknown_status_is_unknown_without_retaining_raw_status() {
    for status in ["unknown", "paused", "future-status-canary", ""] {
        let snapshot =
            parse(json!([{"vmid":101,"node":"pve-test","type":"qemu","status":status}])).unwrap();
        assert_eq!(snapshot.records()[0].power(), VisiblePower::Unknown);
        assert_eq!(snapshot.rejected_rows(), 0);
        assert!(
            !serde_json::to_string(&snapshot)
                .unwrap()
                .contains("future-status-canary")
        );
    }
}

#[test]
fn stopped_and_template_one_remain_explicit() {
    let snapshot = parse(
        json!([{"vmid":101,"node":"pve-test","type":"qemu","status":"stopped","template":1}]),
    )
    .unwrap();
    assert_eq!(snapshot.records()[0].power(), VisiblePower::Stopped);
    assert_eq!(snapshot.records()[0].template(), Some(true));
}

#[test]
fn invalid_vmids_are_rejected_and_counted() {
    for vmid in [
        json!(null),
        json!("101"),
        json!(-1),
        json!(99),
        json!(1_000_000_000_u64),
        json!(4_294_967_396_u64),
        json!(101.0),
        json!(true),
    ] {
        let snapshot = parse(json!([{"vmid":vmid,"node":"pve-test","type":"qemu"}])).unwrap();
        assert!(snapshot.records().is_empty(), "{vmid}");
        assert_eq!(snapshot.rejected_rows(), 1, "{vmid}");
    }
}

#[test]
fn invalid_nodes_are_rejected_and_counted() {
    for node in [
        json!(null),
        json!(101),
        json!(""),
        json!("../pve-test"),
        json!("bad node"),
        json!("a".repeat(65)),
    ] {
        let snapshot = parse(json!([{"vmid":101,"node":node,"type":"qemu"}])).unwrap();
        assert!(snapshot.records().is_empty(), "{node}");
        assert_eq!(snapshot.rejected_rows(), 1, "{node}");
    }
}

#[test]
fn missing_identity_and_non_object_rows_are_rejected_and_counted() {
    let snapshot = parse(json!([null, [], "raw-row-canary", {}, {"node":"pve-test","type":"qemu"}, {"vmid":101,"type":"qemu"}])).unwrap();
    assert!(snapshot.records().is_empty());
    assert_eq!(snapshot.rejected_rows(), 6);
}

#[test]
fn invalid_optional_template_is_rejected_and_counted() {
    for template in [
        json!(null),
        json!(false),
        json!(true),
        json!("0"),
        json!(0.0),
        json!(1.0),
        json!(-1),
        json!(2),
        json!([]),
        json!({}),
    ] {
        let snapshot =
            parse(json!([{"vmid":101,"node":"pve-test","type":"qemu","template":template}]))
                .unwrap();
        assert!(snapshot.records().is_empty(), "{template}");
        assert_eq!(snapshot.rejected_rows(), 1, "{template}");
    }
}

#[test]
fn present_non_string_status_is_rejected_and_counted() {
    for status in [json!(null), json!(0), json!(true), json!([]), json!({})] {
        let snapshot =
            parse(json!([{"vmid":101,"node":"pve-test","type":"qemu","status":status}])).unwrap();
        assert!(snapshot.records().is_empty(), "{status}");
        assert_eq!(snapshot.rejected_rows(), 1, "{status}");
    }
}

#[test]
fn missing_or_non_string_kind_is_rejected_without_defaulting_to_qemu() {
    let snapshot = parse(json!([
        {"vmid":101,"node":"pve-test"},
        {"vmid":102,"node":"pve-test","type":null},
        {"vmid":103,"node":"pve-test","type":7},
        {"vmid":104,"node":"pve-test","type":true}
    ]))
    .unwrap();
    assert!(snapshot.records().is_empty());
    assert_eq!(snapshot.rejected_rows(), 4);
}

#[test]
fn duplicate_valid_vmid_rejects_response_across_nodes_and_kinds() {
    assert_eq!(
        parse(json!([
            {"vmid":101,"node":"pve-one","type":"qemu"},
            {"vmid":101,"node":"pve-two","type":"lxc"}
        ]))
        .unwrap_err(),
        PveReadError::InvalidResponse
    );
}

#[test]
fn duplicate_identity_is_detected_even_when_metadata_is_rejected() {
    for malformed in [
        json!({"template":2}),
        json!({"status":null}),
        json!({"type":null}),
    ] {
        let mut invalid = json!({"vmid":101,"node":"pve-test","type":"qemu"});
        invalid
            .as_object_mut()
            .unwrap()
            .extend(malformed.as_object().unwrap().clone());
        let valid = json!({"vmid":101,"node":"pve-other","type":"lxc"});
        for rows in [
            json!([invalid, valid]),
            json!([valid, invalid]),
            json!([invalid, invalid]),
        ] {
            assert_eq!(parse(rows).unwrap_err(), PveReadError::InvalidResponse);
        }
    }
}

#[test]
fn non_array_response_is_rejected() {
    for value in [
        json!(null),
        json!({"data":[]}),
        json!("raw-body-canary"),
        json!(7),
        json!(true),
    ] {
        assert_eq!(parse(value).unwrap_err(), PveReadError::InvalidResponse);
    }
}

#[test]
fn row_limit_accepts_1024_and_rejects_1025_before_row_validation() {
    let rows: Vec<_> = (100..1124)
        .map(|vmid| json!({"vmid":vmid,"node":"pve-test","type":"qemu"}))
        .collect();
    let snapshot = parse(json!(rows)).unwrap();
    assert_eq!(snapshot.records().len(), 1024);
    assert_eq!(snapshot.rejected_rows(), 0);
    assert_eq!(snapshot.records()[0].vmid().get(), 100);
    assert_eq!(snapshot.records()[1023].vmid().get(), 1123);
    assert_eq!(
        parse(json!(vec![Value::Null; 1025])).unwrap_err(),
        PveReadError::InvalidResponse
    );
}

#[test]
fn sanitization_preserves_wire_order_timestamp_and_rejection_count() {
    let snapshot = parse(json!([
        {"vmid":999999999,"node":"pve-test","type":"qemu","status":"running","template":0,"name":"name-canary","secret":"field-canary","nested":{"raw":"nested-canary"}},
        {"vmid":103,"node":"pve-test","type":"qemu","status":null,"name":"rejected-canary"},
        {"vmid":100,"node":"pve-other","type":"lxc"}
    ])).unwrap();
    assert_eq!(snapshot.records()[0].vmid().get(), 999_999_999);
    assert_eq!(snapshot.records()[1].vmid().get(), 100);
    assert_eq!(snapshot.records()[1].node().as_str(), "pve-other");
    assert_eq!(snapshot.observed_at(), observed_at());
    assert_eq!(
        serde_json::to_value(&snapshot).unwrap(),
        json!({
            "observed_at":"2026-09-05T12:00:00Z",
            "coverage":"unverified",
            "rejected_rows":1,
            "records":[
                {"vmid":999999999,"node":"pve-test","kind":"qemu","power":"running","template":false},
                {"vmid":100,"node":"pve-other","kind":"lxc","power":"unknown","template":null}
            ]
        })
    );
    assert!(!format!("{snapshot:?}").contains("canary"));
}

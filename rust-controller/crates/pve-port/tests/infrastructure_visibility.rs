use chrono::{DateTime, Utc};
use pve_port::{InterfaceKind, NetworkVisibility, NodeName, NodeVisibility, PveReadError};
use serde_json::{Value, json};

fn node() -> NodeName {
    NodeName::parse("pve-test").unwrap()
}

fn observed_at() -> DateTime<Utc> {
    "2026-09-05T12:34:56Z".parse().unwrap()
}

fn parse_node(value: Value) -> Result<NodeVisibility, PveReadError> {
    NodeVisibility::from_wire(node(), value, observed_at())
}

fn parse_network(value: Value) -> Result<NetworkVisibility, PveReadError> {
    NetworkVisibility::from_wire(node(), value, observed_at())
}

#[test]
fn zero_uptime_is_visibility_not_online_authority() {
    let node = NodeName::parse("pve-test").unwrap();
    let report =
        NodeVisibility::from_wire(node, serde_json::json!({"uptime":0}), chrono::Utc::now())
            .unwrap();
    assert_eq!(report.uptime(), Some(0));
    assert_eq!(report.coverage(), "unverified");
}

#[test]
fn missing_uptime_is_unknown() {
    let report = parse_node(json!({})).unwrap();
    assert_eq!(report.uptime(), None);
    assert_eq!(report.coverage(), "unverified");
}

#[test]
fn positive_and_maximum_uptime_are_retained() {
    for uptime in [1, u64::MAX] {
        assert_eq!(
            parse_node(json!({"uptime":uptime})).unwrap().uptime(),
            Some(uptime)
        );
    }
}

#[test]
fn present_invalid_uptime_invalidates_report() {
    for uptime in [
        json!(-1),
        json!(0.0),
        json!(1.5),
        json!("0"),
        json!(null),
        json!(true),
        json!([]),
        json!({}),
    ] {
        assert_eq!(
            parse_node(json!({"uptime":uptime})).unwrap_err(),
            PveReadError::InvalidResponse,
            "{uptime}"
        );
    }
}

#[test]
fn nonobject_node_data_is_invalid() {
    for value in [json!(null), json!([]), json!(0), json!(true), json!("node")] {
        assert_eq!(
            parse_node(value).unwrap_err(),
            PveReadError::InvalidResponse
        );
    }
}

#[test]
fn matching_node_binding_is_retained() {
    let report = parse_node(json!({"node":"pve-test"})).unwrap();
    assert_eq!(report.node(), &node());
}

#[test]
fn invalid_or_mismatched_node_binding_invalidates_node_report() {
    for binding in [
        json!("other"),
        json!(""),
        json!("../pve-test"),
        json!("bad node"),
        json!("a".repeat(65)),
        json!(null),
        json!(1),
        json!(true),
        json!([]),
        json!({}),
    ] {
        assert_eq!(
            parse_node(json!({"node":binding})).unwrap_err(),
            PveReadError::InvalidResponse,
            "{binding}"
        );
    }
}

#[test]
fn node_timestamp_is_preserved() {
    assert_eq!(parse_node(json!({})).unwrap().observed_at(), observed_at());
}

#[test]
fn node_serialization_retains_only_sanitized_visibility() {
    let report = parse_node(json!({"node":"pve-test","uptime":0,"name":"name-canary","certificates":["cert-canary"],"cpu":0.8,"memory":{"total":123},"online":true,"execution_ready":true})).unwrap();
    assert_eq!(
        serde_json::to_value(&report).unwrap(),
        json!({"node":"pve-test","uptime":0,"observed_at":"2026-09-05T12:34:56Z","coverage":"unverified"})
    );
    let debug = format!("{report:?}");
    assert!(!debug.contains("canary"));
}

#[test]
fn ovs_and_missing_activity_remain_visible() {
    let node = NodeName::parse("pve-test").unwrap();
    let report = NetworkVisibility::from_wire(
        node,
        serde_json::json!([
            {"iface":"vmbr0","type":"bridge","active":0},
            {"iface":"ovs0","type":"OVSBridge"}
        ]),
        chrono::Utc::now(),
    )
    .unwrap();
    assert_eq!(report.records()[1].kind(), InterfaceKind::OvsBridge);
    assert_eq!(report.records()[1].active(), None);
    assert_eq!(report.rejected_rows(), 0);
    assert_eq!(report.coverage(), "unverified");
}

#[test]
fn interface_activity_preserves_zero_one_and_missing() {
    let report = parse_network(json!([
        {"iface":"off","type":"bridge","active":0},
        {"iface":"on","type":"bridge","active":1},
        {"iface":"unknown","type":"bridge"}
    ]))
    .unwrap();
    assert_eq!(
        report
            .records()
            .iter()
            .map(|row| row.active())
            .collect::<Vec<_>>(),
        [Some(false), Some(true), None]
    );
    assert_eq!(report.records()[0].kind(), InterfaceKind::LinuxBridge);
}

#[test]
fn unknown_interface_types_remain_visible_without_raw_metadata() {
    for kind in ["future-kind-canary", "", "Bridge", "ovsbridge"] {
        let report = parse_network(json!([{"iface":"eth0","type":kind}])).unwrap();
        assert_eq!(report.records()[0].kind(), InterfaceKind::Other);
        assert_eq!(report.rejected_rows(), 0);
        assert_eq!(
            serde_json::to_value(&report).unwrap()["records"][0],
            json!({"name":"eth0","kind":"other","active":null})
        );
        assert!(!format!("{report:?}").contains("future-kind-canary"));
    }
}

#[test]
fn opaque_interface_identity_accepts_exact_byte_and_character_boundaries() {
    for name in [
        "a".to_owned(),
        "a".repeat(64),
        "AZaz09._:-".to_owned(),
        "eth0:1".to_owned(),
    ] {
        let report = parse_network(json!([{"iface":name,"type":"bridge"}])).unwrap();
        assert_eq!(report.records()[0].name(), name);
        assert_eq!(report.rejected_rows(), 0);
    }
}

#[test]
fn invalid_interface_identity_is_counted() {
    for name in [
        json!(""),
        json!("a".repeat(65)),
        json!("eth 0"),
        json!("eth/0"),
        json!("eth\\0"),
        json!("é"),
        json!("eth\n0"),
        json!("eth\u{0}0"),
        json!("eth@0"),
        json!(null),
        json!(1),
        json!(true),
        json!([]),
        json!({}),
    ] {
        let report = parse_network(json!([{"iface":name,"type":"bridge"}])).unwrap();
        assert!(report.records().is_empty(), "{name}");
        assert_eq!(report.rejected_rows(), 1, "{name}");
    }
}

#[test]
fn malformed_rows_are_counted_without_losing_valid_rows() {
    let report = parse_network(
        json!([null, [], "row-canary", {}, {"type":"bridge"}, {"iface":"kept","type":"bridge"}]),
    )
    .unwrap();
    assert_eq!(report.rejected_rows(), 5);
    assert_eq!(report.records().len(), 1);
    assert_eq!(report.records()[0].name(), "kept");
}

#[test]
fn missing_or_nonstring_type_is_counted() {
    let report = parse_network(json!([{"iface":"missing"},{"iface":"null","type":null},{"iface":"number","type":0},{"iface":"bool","type":true},{"iface":"array","type":[]},{"iface":"object","type":{}}])).unwrap();
    assert_eq!(report.rejected_rows(), 6);
    assert!(report.records().is_empty());
}

#[test]
fn invalid_activity_is_counted() {
    for active in [
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
        let report =
            parse_network(json!([{"iface":"eth0","type":"bridge","active":active}])).unwrap();
        assert_eq!(report.rejected_rows(), 1, "{active}");
        assert!(report.records().is_empty(), "{active}");
    }
}

#[test]
fn matching_network_node_binding_is_retained() {
    let report =
        parse_network(json!([{"node":"pve-test","iface":"eth0","type":"bridge"}])).unwrap();
    assert_eq!(report.node(), &node());
    assert_eq!(report.records().len(), 1);
}

#[test]
fn invalid_or_crossnode_binding_invalidates_even_otherwise_malformed_rows() {
    for binding in [
        json!("other"),
        json!(""),
        json!("../pve-test"),
        json!("bad node"),
        json!("a".repeat(65)),
        json!(null),
        json!(1),
        json!(true),
        json!([]),
        json!({}),
    ] {
        for row in [
            json!({"node":binding,"iface":"eth0","type":"bridge"}),
            json!({"node":binding}),
            json!({"node":binding,"iface":"bad/iface","type":null}),
        ] {
            assert_eq!(
                parse_network(json!([{"iface":"valid","type":"bridge"},row])).unwrap_err(),
                PveReadError::InvalidResponse,
                "{row}"
            );
        }
    }
}

#[test]
fn duplicate_identity_invalidates_network_report() {
    assert_eq!(
        parse_network(
            json!([{"iface":"eth0","type":"bridge"},{"iface":"eth0","type":"OVSBridge"}])
        )
        .unwrap_err(),
        PveReadError::InvalidResponse
    );
}

#[test]
fn duplicate_identity_is_detected_before_metadata_rejection() {
    for invalid in [
        json!({"iface":"eth0"}),
        json!({"iface":"eth0","type":null}),
        json!({"iface":"eth0","type":"bridge","active":2}),
    ] {
        let valid = json!({"iface":"eth0","type":"bridge"});
        for rows in [
            json!([invalid, valid]),
            json!([valid, invalid]),
            json!([invalid, invalid]),
        ] {
            assert_eq!(
                parse_network(rows).unwrap_err(),
                PveReadError::InvalidResponse
            );
        }
    }
}

#[test]
fn nonarray_network_data_is_invalid() {
    for value in [json!(null), json!({}), json!(0), json!(true), json!("row")] {
        assert_eq!(
            parse_network(value).unwrap_err(),
            PveReadError::InvalidResponse
        );
    }
}

#[test]
fn network_row_limit_accepts_1024_and_rejects_1025() {
    let rows: Vec<_> = (0..1024)
        .map(|index| json!({"iface":format!("eth{index}"),"type":"bridge"}))
        .collect();
    let report = parse_network(json!(rows)).unwrap();
    assert_eq!(report.records().len(), 1024);
    assert_eq!(report.rejected_rows(), 0);
    assert_eq!(report.records()[1023].name(), "eth1023");
    assert_eq!(
        parse_network(json!(vec![Value::Null; 1025])).unwrap_err(),
        PveReadError::InvalidResponse
    );
}

#[test]
fn empty_network_remains_unverified() {
    let report = parse_network(json!([])).unwrap();
    assert!(report.records().is_empty());
    assert_eq!(report.rejected_rows(), 0);
    assert_eq!(report.coverage(), "unverified");
}

#[test]
fn network_timestamp_is_preserved() {
    assert_eq!(
        parse_network(json!([])).unwrap().observed_at(),
        observed_at()
    );
}

#[test]
fn network_serialization_preserves_order_and_only_sanitized_fields() {
    let report = parse_network(json!([
        {"iface":"z0","type":"OVSBridge","address":"address-canary","comments":"comment-canary","config":{"secret":"config-canary"}},
        null,
        {"iface":"a0","node":"pve-test","type":"bridge","active":1}
    ])).unwrap();
    assert_eq!(
        serde_json::to_value(&report).unwrap(),
        json!({"node":"pve-test","observed_at":"2026-09-05T12:34:56Z","coverage":"unverified","rejected_rows":1,"records":[{"name":"z0","kind":"ovs_bridge","active":null},{"name":"a0","kind":"linux_bridge","active":true}]})
    );
    assert!(!format!("{report:?}").contains("canary"));
}

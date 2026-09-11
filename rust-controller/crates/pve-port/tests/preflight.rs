mod support;

use chrono::{DateTime, Utc};
use pve_port::{
    BridgeInventory, BridgeName, ClusterVmInventory, NativeVmConfig, NodeName, NodeStatus,
    PowerState, PvePreflightReadPort, PveReadError, StorageName, StorageStatus, UnsupportedConfig,
    VmPowerStatus, Vmid,
};
use pve_port::{observe_target_absence, observe_target_absence_with_clock};
use serde::{Serialize, de::DeserializeOwned};
use serde_json::{Value, json};
use std::time::Duration;
use support::Server;

fn node() -> NodeName {
    NodeName::parse("pve-test").unwrap()
}
fn vmid() -> Vmid {
    Vmid::new(9000).unwrap()
}
fn now() -> DateTime<Utc> {
    "2026-09-04T12:00:00Z".parse().unwrap()
}
fn config() -> Value {
    json!({"digest":"synthetic-digest", "name":"native-source", "cores":2, "memory":2048,
        "scsi0":"local-lvm:vm-9000-disk-0,size=16G", "smbios1":"uuid=3f2504e0-4f89-41d3-9a0c-0305e82c3301",
        "net0":"virtio=02:00:00:00:90:00,bridge=vmbr0,firewall=0", "agent":"1", "boot":"order=scsi0", "template":1})
}
fn resource() -> Value {
    json!({"vmid":9000,"node":"pve-test","type":"qemu","name":"native-source","template":1,"status":"stopped"})
}

#[tokio::test]
async fn all_preflight_routes_return_bound_typed_facts() {
    let server = Server::json(json!({"uptime":42})).await;
    assert!(server.url.starts_with("http://127.0.0.1:"));
    let before = Utc::now();
    let fact = server.observer().node_status(&node()).await.unwrap();
    assert_eq!(fact.node(), &node());
    assert!(fact.online());
    assert_eq!(fact.uptime(), 42);
    assert!(fact.observed_at() >= before && fact.observed_at() <= Utc::now());
    assert!(
        server.requests.lock().unwrap()[0]
            .starts_with("GET /api2/json/nodes/pve-test/status HTTP/1.1\r\n")
    );
    server.finish().await;

    let server = Server::json(
        json!({"active":1,"enabled":1,"avail":17_179_869_184_u64,"content":"images,rootdir"}),
    )
    .await;
    let fact = server
        .observer()
        .storage_status(&node(), &StorageName::parse("local-lvm").unwrap())
        .await
        .unwrap();
    assert_eq!(fact.available_bytes(), 17_179_869_184);
    assert_eq!(fact.storage().as_str(), "local-lvm");
    assert!(
        server.requests.lock().unwrap()[0]
            .starts_with("GET /api2/json/nodes/pve-test/storage/local-lvm/status HTTP/1.1\r\n")
    );
    server.finish().await;

    let server = Server::json(json!([{"iface":"eno1","type":"eth","active":1},{"iface":"vmbr0","type":"bridge","active":1}])).await;
    let fact = server.observer().bridges(&node()).await.unwrap();
    assert_eq!(fact.bridges().len(), 1);
    assert!(fact.has_active(&BridgeName::parse("vmbr0").unwrap()));
    assert!(!fact.has_active(&BridgeName::parse("vmbr1").unwrap()));
    assert!(
        server.requests.lock().unwrap()[0]
            .starts_with("GET /api2/json/nodes/pve-test/network HTTP/1.1\r\n")
    );
    server.finish().await;

    let server = Server::json(json!([resource()])).await;
    let fact = server.observer().cluster_vms().await.unwrap();
    let vm = fact.find(vmid()).unwrap();
    assert_eq!(vm.node(), &node());
    assert_eq!(vm.name().as_str(), "native-source");
    assert_eq!(vm.power(), PowerState::Stopped);
    assert!(vm.is_template());
    assert!(
        server.requests.lock().unwrap()[0]
            .starts_with("GET /api2/json/cluster/resources?type=vm HTTP/1.1\r\n")
    );
    server.finish().await;

    let server = Server::json(config()).await;
    let fact = server
        .observer()
        .native_vm_config(&node(), vmid())
        .await
        .unwrap();
    assert_eq!(fact.node(), &node());
    assert_eq!(fact.vmid(), vmid());
    assert_eq!(fact.name().as_str(), "native-source");
    assert_eq!(fact.digest(), "synthetic-digest");
    assert_eq!(fact.cores(), 2);
    assert_eq!(fact.memory_mib(), 2048);
    assert_eq!(
        fact.uuid().to_string(),
        "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
    );
    assert_eq!(fact.mac().as_str(), "02:00:00:00:90:00");
    assert_eq!(fact.bridge().as_str(), "vmbr0");
    assert_eq!(fact.boot_disk().storage().as_str(), "local-lvm");
    assert_eq!(fact.boot_disk().volume(), "vm-9000-disk-0");
    assert!(fact.agent_enabled() && fact.boots_scsi0() && fact.is_template());
    assert!(!fact.locked());
    assert!(fact.unsupported().is_empty());
    assert!(
        server.requests.lock().unwrap()[0]
            .starts_with("GET /api2/json/nodes/pve-test/qemu/9000/config HTTP/1.1\r\n")
    );
    server.finish().await;

    let server = Server::json(json!({"vmid":9000,"status":"stopped","locked":1})).await;
    let fact = server.observer().vm_status(&node(), vmid()).await.unwrap();
    assert_eq!(fact.power(), PowerState::Stopped);
    assert_eq!(fact.locked(), Some(true));
    assert!(
        server.requests.lock().unwrap()[0]
            .starts_with("GET /api2/json/nodes/pve-test/qemu/9000/status/current HTTP/1.1\r\n")
    );
    server.finish().await;
}

#[test]
fn node_storage_and_bridge_wire_validation_fails_closed() {
    for data in [
        json!(null),
        json!({}),
        json!({"uptime":0}),
        json!({"uptime":-1}),
        json!({"uptime":"42"}),
        json!({"uptime":1,"node":"wrong-node"}),
    ] {
        assert_eq!(
            NodeStatus::from_wire(node(), data, now()).unwrap_err(),
            PveReadError::InvalidResponse
        );
    }
    let valid = json!({"active":1,"enabled":1,"avail":0,"content":"images"});
    assert!(
        StorageStatus::from_wire(
            node(),
            StorageName::parse("local-lvm").unwrap(),
            valid.clone(),
            now()
        )
        .is_ok()
    );
    for (key, value) in [
        ("active", json!(0)),
        ("enabled", json!(0)),
        ("active", json!(2)),
        ("enabled", json!(true)),
        ("avail", json!(-1)),
        ("content", json!("iso")),
        ("content", json!("images,unknown")),
        ("node", json!("wrong-node")),
        ("storage", json!("other")),
    ] {
        let mut data = valid.clone();
        data[key] = value;
        assert_eq!(
            StorageStatus::from_wire(
                node(),
                StorageName::parse("local-lvm").unwrap(),
                data,
                now()
            )
            .unwrap_err(),
            PveReadError::InvalidResponse
        );
    }
    for data in [
        json!([{"type":"bridge","iface":"../bad","active":1}]),
        json!([{"type":"bridge","iface":"vmbr0","active":2}]),
        json!([{"type":"bridge","iface":"vmbr0"}]),
        json!([{}, {"type":"bridge","iface":"vmbr0","active":1}]),
        json!([{"type":"bridge","iface":"vmbr0","active":1},{"type":"bridge","iface":"vmbr0","active":1}]),
    ] {
        assert_eq!(
            BridgeInventory::from_wire(node(), data, now()).unwrap_err(),
            PveReadError::InvalidResponse
        );
    }
    let inactive = BridgeInventory::from_wire(
        node(),
        json!([{"type":"bridge","iface":"vmbr0","active":0}]),
        now(),
    )
    .unwrap();
    assert!(!inactive.has_active(&BridgeName::parse("vmbr0").unwrap()));
}

#[test]
fn inventory_rejects_duplicate_vmids_and_malformed_identity() {
    assert_eq!(
        ClusterVmInventory::from_wire(json!([resource(), resource()]), now()).unwrap_err(),
        PveReadError::InvalidResponse
    );
    for (key, value) in [
        ("vmid", json!(0)),
        ("vmid", json!("9000")),
        ("node", json!("../bad")),
        ("type", json!("lxc")),
        ("name", json!("bad name")),
        ("template", json!(2)),
        ("status", json!("paused")),
    ] {
        let mut data = resource();
        data[key] = value;
        assert_eq!(
            ClusterVmInventory::from_wire(json!([data]), now()).unwrap_err(),
            PveReadError::InvalidResponse
        );
    }
    for key in ["vmid", "node", "type", "name", "template", "status"] {
        let mut data = resource();
        data.as_object_mut().unwrap().remove(key);
        assert!(ClusterVmInventory::from_wire(json!([data]), now()).is_err());
    }
}

#[test]
fn native_config_rejects_missing_or_malformed_required_facts() {
    for key in [
        "digest", "name", "cores", "memory", "scsi0", "smbios1", "net0", "boot", "agent",
    ] {
        let mut data = config();
        data.as_object_mut().unwrap().remove(key);
        assert_eq!(
            NativeVmConfig::from_wire(node(), vmid(), data, now()).unwrap_err(),
            PveReadError::InvalidResponse
        );
    }
    for (key, value) in [
        ("digest", json!("")),
        ("name", json!("bad/name")),
        ("cores", json!(0)),
        ("memory", json!(0)),
        ("smbios1", json!("uuid=bad")),
        (
            "smbios1",
            json!("uuid=00000000-0000-0000-0000-000000000000"),
        ),
        (
            "smbios1",
            json!(
                "uuid=3f2504e0-4f89-41d3-9a0c-0305e82c3301,uuid=3f2504e0-4f89-41d3-9a0c-0305e82c3301"
            ),
        ),
        ("net0", json!("virtio=bad,bridge=vmbr0")),
        ("net0", json!("virtio=02:00:00:00:90:00,bridge=../bad")),
        (
            "net0",
            json!("virtio=02:00:00:00:90:00,bridge=vmbr0,bridge=vmbr1"),
        ),
        ("net0", json!("virtio=02:00:00:00:90:00")),
        ("scsi0", json!("")),
        ("scsi0", json!("local-lvm:vm-9000-disk-0,size=16G,size=32G")),
        ("agent", json!("enabled=1,enabled=0")),
        ("boot", json!("order=scsi0,order=net0")),
        ("template", json!(3)),
        ("lock", json!("")),
        ("node", json!("wrong-node")),
        ("vmid", json!(9010)),
    ] {
        let mut data = config();
        data[key] = value;
        assert_eq!(
            NativeVmConfig::from_wire(node(), vmid(), data, now()).unwrap_err(),
            PveReadError::InvalidResponse,
            "field {key}"
        );
    }
}

#[test]
fn unsupported_configuration_is_typed_and_never_copies_raw_text() {
    for (key, value, reason) in [
        (
            "args",
            json!("private raw arbitrary QEMU arguments"),
            UnsupportedConfig::Args,
        ),
        (
            "net1",
            json!("virtio=02:00:00:00:90:01,bridge=vmbr0"),
            UnsupportedConfig::AdditionalNic,
        ),
        (
            "net01",
            json!("virtio=02:00:00:00:90:01,bridge=vmbr0"),
            UnsupportedConfig::AdditionalNic,
        ),
        (
            "ide2",
            json!("local:iso/private.iso,media=cdrom"),
            UnsupportedConfig::AdditionalDisk,
        ),
        (
            "scsi1",
            json!("local-lvm:vm-9000-disk-1"),
            UnsupportedConfig::AdditionalDisk,
        ),
        (
            "net0",
            json!("virtio=02:00:00:00:90:00,bridge=vmbr0,tag=30"),
            UnsupportedConfig::NicProperties,
        ),
        (
            "net0",
            json!("virtio=02:00:00:00:90:00,bridge=vmbr0,firewall=1"),
            UnsupportedConfig::NicProperties,
        ),
        (
            "boot",
            json!("order=net0;scsi0"),
            UnsupportedConfig::BootOrder,
        ),
        (
            "scsi0",
            json!("local-lvm:vm-9000-disk-0,media=cdrom"),
            UnsupportedConfig::DiskProperties,
        ),
        (
            "unknown",
            json!("private raw unsupported field"),
            UnsupportedConfig::UnknownField,
        ),
    ] {
        let mut data = config();
        data[key] = value;
        let fact = NativeVmConfig::from_wire(node(), vmid(), data, now()).unwrap();
        assert!(fact.unsupported().contains(&reason), "field {key}");
        let serialized = serde_json::to_string(&fact).unwrap();
        assert!(!serialized.contains("private"));
        assert!(!format!("{fact:?}").contains("private"));
    }
}

#[test]
fn lock_and_agent_state_are_preserved_and_normalized() {
    for agent in [json!(1), json!("1"), json!("enabled=1,type=virtio")] {
        let mut data = config();
        data["agent"] = agent;
        data["lock"] = json!("clone");
        let fact = NativeVmConfig::from_wire(node(), vmid(), data, now()).unwrap();
        assert!(fact.locked() && fact.is_template() && fact.agent_enabled());
    }
    for agent in [json!(0), json!("0"), json!("enabled=0,type=virtio")] {
        let mut data = config();
        data["agent"] = agent;
        assert!(
            !NativeVmConfig::from_wire(node(), vmid(), data, now())
                .unwrap()
                .agent_enabled()
        );
    }
}

#[test]
fn power_status_rejects_wrong_identity_and_unknown_states() {
    for data in [
        json!({"vmid":9001,"status":"running"}),
        json!({"vmid":9000,"node":"wrong-node","status":"running"}),
        json!({"status":"stopped"}),
        json!({"vmid":9000,"status":"paused"}),
        json!({"vmid":9000,"status":"running","locked":2}),
    ] {
        assert_eq!(
            VmPowerStatus::from_wire(node(), vmid(), data, now()).unwrap_err(),
            PveReadError::InvalidResponse
        );
    }
    for (locked, want) in [
        (None, None),
        (Some(json!(0)), Some(false)),
        (Some(json!(1)), Some(true)),
    ] {
        let mut data = json!({"vmid":9000,"status":"running"});
        if let Some(value) = locked {
            data["locked"] = value;
        }
        let fact = VmPowerStatus::from_wire(node(), vmid(), data, now()).unwrap();
        assert_eq!(fact.power(), PowerState::Running);
        assert_eq!(fact.locked(), want);
    }
}

#[test]
fn additional_nics_cannot_hide_malformed_identity_behind_unsupported_layout() {
    for value in [
        json!("virtio=not-a-mac,bridge=vmbr0"),
        json!("virtio=02:00:00:00:90:01,bridge=../bad"),
        json!(false),
        json!("virtio=02:00:00:00:90:01,bridge=vmbr0,bridge=vmbr1"),
    ] {
        let mut data = config();
        data["net1"] = value;
        assert_eq!(
            NativeVmConfig::from_wire(node(), vmid(), data, now()).unwrap_err(),
            PveReadError::InvalidResponse
        );
    }
}

#[tokio::test]
async fn target_absence_requires_inventory_exclusion_and_config_not_found() {
    for (inventory_status, inventory, config_status, config_body, want) in [
        (200, json!([]), 404, json!({}), Ok(true)),
        (200, json!([resource()]), 404, json!({}), Ok(false)),
        (200, json!([]), 200, config(), Ok(false)),
        (
            200,
            json!([]),
            401,
            json!({}),
            Err(PveReadError::Unauthorized),
        ),
        (200, json!([]), 409, json!({}), Err(PveReadError::Conflict)),
        (
            200,
            json!([]),
            500,
            json!({}),
            Err(PveReadError::TransportUnavailable),
        ),
        (
            401,
            json!([]),
            404,
            json!({}),
            Err(PveReadError::Unauthorized),
        ),
        (
            500,
            json!([]),
            404,
            json!({}),
            Err(PveReadError::TransportUnavailable),
        ),
        (404, json!([]), 404, json!({}), Err(PveReadError::NotFound)),
        (
            200,
            json!([resource(), resource()]),
            404,
            json!({}),
            Err(PveReadError::InvalidResponse),
        ),
        (
            200,
            json!([]),
            200,
            json!({}),
            Err(PveReadError::InvalidResponse),
        ),
    ] {
        let server = Server::script(vec![
            (
                inventory_status,
                json!({"data":inventory}).to_string(),
                String::new(),
                Duration::ZERO,
            ),
            (
                config_status,
                json!({"data":config_body}).to_string(),
                String::new(),
                Duration::ZERO,
            ),
        ])
        .await;
        let result = observe_target_absence_with_clock(&server.observer(), &node(), vmid(), || {
            let requests = server.requests.lock().unwrap();
            assert_eq!(requests.len(), 2, "evaluation clock must follow both reads");
            assert!(
                requests[0].starts_with("GET /api2/json/cluster/resources?type=vm HTTP/1.1\r\n")
            );
            assert!(
                requests[1]
                    .starts_with("GET /api2/json/nodes/pve-test/qemu/9000/config HTTP/1.1\r\n")
            );
            Utc::now()
        })
        .await;
        assert_eq!(result, want);
        server.finish().await;
    }
}

#[tokio::test]
async fn absence_does_not_accept_stale_or_future_inventory() {
    for offset in [-1, 31] {
        let server = Server::script(vec![
            (
                200,
                json!({"data":[]}).to_string(),
                String::new(),
                Duration::ZERO,
            ),
            (
                404,
                json!({"data":null}).to_string(),
                String::new(),
                Duration::ZERO,
            ),
        ])
        .await;
        assert_eq!(
            observe_target_absence_with_clock(&server.observer(), &node(), vmid(), || Utc::now()
                + chrono::Duration::seconds(offset))
            .await,
            Ok(false)
        );
        server.finish().await;
    }
    let server = Server::script(vec![
        (
            200,
            json!({"data":[]}).to_string(),
            String::new(),
            Duration::ZERO,
        ),
        (
            404,
            json!({"data":null}).to_string(),
            String::new(),
            Duration::ZERO,
        ),
    ])
    .await;
    assert_eq!(
        observe_target_absence(&server.observer(), &node(), vmid()).await,
        Ok(true)
    );
    server.finish().await;
}

fn round_trip<T: Serialize + DeserializeOwned + PartialEq + std::fmt::Debug>(fact: T) {
    let json = serde_json::to_value(&fact).unwrap();
    assert_eq!(serde_json::from_value::<T>(json.clone()).unwrap(), fact);
    let mut forged = json;
    forged["unrecognized_fact"] = json!(true);
    assert!(serde_json::from_value::<T>(forged).is_err());
}

#[test]
fn sanitized_snapshots_round_trip_through_validated_deserialization() {
    round_trip(NodeStatus::from_wire(node(), json!({"uptime":42}), now()).unwrap());
    round_trip(
        StorageStatus::from_wire(
            node(),
            StorageName::parse("local-lvm").unwrap(),
            json!({"active":1,"enabled":1,"avail":10,"content":"images"}),
            now(),
        )
        .unwrap(),
    );
    round_trip(
        BridgeInventory::from_wire(
            node(),
            json!([{"iface":"vmbr0","type":"bridge","active":1}]),
            now(),
        )
        .unwrap(),
    );
    round_trip(ClusterVmInventory::from_wire(json!([resource()]), now()).unwrap());
    round_trip(NativeVmConfig::from_wire(node(), vmid(), config(), now()).unwrap());
    round_trip(
        VmPowerStatus::from_wire(
            node(),
            vmid(),
            json!({"vmid":9000,"status":"running","locked":1}),
            now(),
        )
        .unwrap(),
    );
    let mut unsupported = config();
    unsupported["args"] = json!("private value");
    unsupported["boot"] = json!("order=net0");
    unsupported["lock"] = json!("clone");
    round_trip(NativeVmConfig::from_wire(node(), vmid(), unsupported, now()).unwrap());
}

#[test]
fn sanitized_decoding_rejects_forged_or_malformed_fact_fields() {
    let original =
        serde_json::to_value(NodeStatus::from_wire(node(), json!({"uptime":42}), now()).unwrap())
            .unwrap();
    for (key, value) in [
        ("uptime", json!(0)),
        ("online", json!(false)),
        ("node", json!("../bad")),
        ("observed_at", json!("bad time")),
    ] {
        let mut data = original.clone();
        data[key] = value;
        assert!(serde_json::from_value::<NodeStatus>(data).is_err());
    }
    let original =
        serde_json::to_value(NativeVmConfig::from_wire(node(), vmid(), config(), now()).unwrap())
            .unwrap();
    for (key, value) in [
        ("vmid", json!(0)),
        ("digest", json!("")),
        ("name", json!("bad name")),
        ("cores", json!(0)),
        ("memory_mib", json!(0)),
        ("uuid", json!("bad")),
        ("mac", json!("bad")),
        ("bridge", json!("../bad")),
        (
            "boot_disk",
            json!({"storage":"local-lvm","volume":"../bad"}),
        ),
        ("boots_scsi0", json!(false)),
        ("unsupported", json!(["private-raw-text"])),
    ] {
        let mut data = original.clone();
        data[key] = value;
        assert!(
            serde_json::from_value::<NativeVmConfig>(data).is_err(),
            "field {key}"
        );
    }
    let mut inventory =
        serde_json::to_value(ClusterVmInventory::from_wire(json!([resource()]), now()).unwrap())
            .unwrap();
    inventory["vms"]["9000"]["vmid"] = json!(9010);
    assert!(serde_json::from_value::<ClusterVmInventory>(inventory).is_err());
    assert!(
        serde_json::from_value::<BridgeInventory>(
            json!({"node":"pve-test","bridges":{"../bad":true},"observed_at":now()})
        )
        .is_err()
    );
    assert!(serde_json::from_value::<StorageStatus>(json!({"node":"pve-test","storage":"local-lvm","available_bytes":-1,"observed_at":now()})).is_err());
    assert!(serde_json::from_value::<VmPowerStatus>(json!({"node":"pve-test","vmid":9000,"power":"paused","locked":false,"observed_at":now()})).is_err());
}

#[test]
fn sanitized_maps_reject_duplicate_identity_keys() {
    assert!(serde_json::from_str::<BridgeInventory>(r#"{"node":"pve-test","bridges":{"vmbr0":true,"vmbr0":false},"observed_at":"2026-09-04T12:00:00Z"}"#).is_err());
    let resource =
        r#"{"vmid":9000,"node":"pve-test","name":"source","template":true,"power":"stopped"}"#;
    let wire = format!(
        r#"{{"vms":{{"9000":{resource},"9000":{resource}}},"observed_at":"2026-09-04T12:00:00Z"}}"#
    );
    assert!(serde_json::from_str::<ClusterVmInventory>(&wire).is_err());
}

#[test]
fn every_snapshot_enforces_thirty_second_freshness_and_rejects_future_facts() {
    for (offset, want) in [(-30_001, false), (-30_000, true), (0, true), (1, false)] {
        let observed = now() + chrono::Duration::milliseconds(offset);
        assert_eq!(
            NodeStatus::from_wire(node(), json!({"uptime":1}), observed)
                .unwrap()
                .is_fresh(now()),
            want
        );
        assert_eq!(
            StorageStatus::from_wire(
                node(),
                StorageName::parse("local-lvm").unwrap(),
                json!({"active":1,"enabled":1,"avail":0,"content":"images"}),
                observed
            )
            .unwrap()
            .is_fresh(now()),
            want
        );
        assert_eq!(
            BridgeInventory::from_wire(node(), json!([]), observed)
                .unwrap()
                .is_fresh(now()),
            want
        );
        assert_eq!(
            ClusterVmInventory::from_wire(json!([]), observed)
                .unwrap()
                .is_fresh(now()),
            want
        );
        assert_eq!(
            NativeVmConfig::from_wire(node(), vmid(), config(), observed)
                .unwrap()
                .is_fresh(now()),
            want
        );
        assert_eq!(
            VmPowerStatus::from_wire(
                node(),
                vmid(),
                json!({"vmid":9000,"status":"stopped"}),
                observed
            )
            .unwrap()
            .is_fresh(now()),
            want
        );
    }
}

#[tokio::test]
async fn status_decode_and_timeout_failures_are_fixed_and_never_leak_body() {
    for (status, want) in [
        (401, PveReadError::Unauthorized),
        (403, PveReadError::Unauthorized),
        (404, PveReadError::NotFound),
        (409, PveReadError::Conflict),
        (500, PveReadError::TransportUnavailable),
        (302, PveReadError::TransportUnavailable),
        (200, PveReadError::InvalidResponse),
    ] {
        let server = Server::start(
            status,
            "private-untrusted-response".into(),
            String::new(),
            Duration::ZERO,
        )
        .await;
        let error = server.observer().node_status(&node()).await.unwrap_err();
        assert_eq!(error, want);
        assert!(!error.to_string().contains("private"));
        assert_eq!(server.requests.lock().unwrap().len(), 1);
        server.finish().await;
    }
    let server = Server::start(
        200,
        "{\"data\":{\"uptime\":42}}".into(),
        String::new(),
        Duration::from_millis(300),
    )
    .await;
    assert_eq!(
        server.observer().node_status(&node()).await.unwrap_err(),
        PveReadError::TimedOut
    );
    assert_eq!(server.requests.lock().unwrap().len(), 1);
    server.finish().await;
}

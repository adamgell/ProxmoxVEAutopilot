#![cfg(feature = "fixture-ipc")]

use chrono::{TimeZone, Utc};
use pve_port::{ClusterVmInventory, PveReadError, fixture_support::SeedIdentity};
use serde_json::json;

#[test]
fn seeded_inventory_status_supplies_its_own_power_observation() {
    let seed: SeedIdentity = serde_json::from_value(json!({
        "node": "pve", "vmid": 900, "name": "template", "template": true,
        "config_sha256": "a".repeat(64),
        "uuid": "550e8400-e29b-41d4-a716-446655440000",
        "mac": "02:00:00:00:00:01", "primary_storage": "local-lvm",
        "primary_volume": "vm-900-disk-0",
        "status": {"state":"observed", "observed_unix_ms":1800000000000u64,"value":"running"}
    }))
    .unwrap();
    let at = Utc.timestamp_opt(1_800_000_000, 0).unwrap();
    let entry = json!({"type":"qemu", "node":seed.node, "vmid":seed.vmid,
        "name":seed.name, "template":u8::from(seed.template)});
    assert_eq!(
        ClusterVmInventory::from_wire(json!([entry.clone()]), at),
        Err(PveReadError::InvalidResponse)
    );

    // The seed now explicitly distinguishes these worlds.
    let mut stopped = entry.clone();
    stopped["status"] = json!("stopped");
    let mut running = entry.clone();
    running["status"] = json!("running");
    let stopped = ClusterVmInventory::from_wire(json!([stopped]), at).unwrap();
    let running = ClusterVmInventory::from_wire(json!([running]), at).unwrap();
    assert_ne!(stopped, running);
    assert_eq!(stopped.observed_at(), at);
    assert_eq!(running.observed_at(), at);
    let pve_port::fixture_support::SeedRead::Observed {
        observed_unix_ms,
        value,
    } = seed.status
    else {
        panic!("observed status expected")
    };
    assert_eq!(observed_unix_ms, at.timestamp_millis() as u64);
    assert_eq!(value, pve_port::PowerState::Running);
    let mut projected = entry;
    projected["status"] = serde_json::to_value(value).unwrap();
    assert_eq!(
        ClusterVmInventory::from_wire(json!([projected]), at).unwrap(),
        running
    );
}

#[test]
fn inventory_status_requires_explicit_valid_snapshot_bound_observation() {
    use pve_port::fixture_support::FixtureCloneReads;
    let id = uuid::Uuid::from_u128(1);
    let entry = json!({
        "node":"other-node", "vmid":777, "name":"unrelated", "template":false,
        "config_sha256":"a".repeat(64),"uuid":uuid::Uuid::from_u128(2),
        "mac":"02:00:00:00:00:02","primary_storage":"local-lvm","primary_volume":"vm-777-disk-0",
        "status":{"state":"observed","observed_unix_ms":126,"value":"running"}
    });
    let seed = json!({"version":1,"fixture_id":id,"node":"pve",
        "node_status":{"state":"error","observed_unix_ms":123,"error":"unavailable"},
        "storage":{"state":"error","observed_unix_ms":124,"error":"unavailable"},
        "bridges":{"state":"error","observed_unix_ms":125,"error":"unavailable"},
        "cluster_inventory":{"state":"observed","observed_unix_ms":126,"value":[entry]}});
    assert!(FixtureCloneReads::decode(&serde_json::to_vec(&seed).unwrap(), id).is_ok());
    let text = serde_json::to_string(&seed).unwrap();
    let duplicate = text.replace(
        "\"value\":\"running\"",
        "\"value\":\"running\",\"value\":\"stopped\"",
    );
    assert!(FixtureCloneReads::decode(duplicate.as_bytes(), id).is_err());
    for case in 0..5 {
        let mut bad = seed.clone();
        let entry = &mut bad["cluster_inventory"]["value"][0];
        match case {
            0 => {
                entry.as_object_mut().unwrap().remove("status");
            }
            1 => entry["status"]["observed_unix_ms"] = json!(127),
            2 => entry["status"]["value"] = json!("unknown"),
            3 => entry["status"]["extra"] = json!(true),
            _ => entry["status"]["observed_unix_ms"] = json!(0),
        }
        assert!(
            FixtureCloneReads::decode(&serde_json::to_vec(&bad).unwrap(), id).is_err(),
            "case {case}"
        );
    }
    let mut unavailable = seed;
    unavailable["cluster_inventory"]["value"][0]["status"] =
        json!({"state":"error","observed_unix_ms":126,"error":"unavailable"});
    assert!(FixtureCloneReads::decode(&serde_json::to_vec(&unavailable).unwrap(), id).is_ok());
}

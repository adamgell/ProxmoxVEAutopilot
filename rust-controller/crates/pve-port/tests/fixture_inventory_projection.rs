#![cfg(feature = "fixture-ipc")]

use chrono::{TimeZone, Utc};
use pve_port::{ClusterVmInventory, PveReadError, fixture_support::SeedIdentity};
use serde_json::json;

#[test]
fn seeded_identity_cannot_supply_cluster_power_observation() {
    let seed: SeedIdentity = serde_json::from_value(json!({
        "node": "pve", "vmid": 900, "name": "template", "template": true,
        "config_sha256": "a".repeat(64),
        "uuid": "550e8400-e29b-41d4-a716-446655440000",
        "mac": "02:00:00:00:00:01", "primary_storage": "local-lvm",
        "primary_volume": "vm-900-disk-0"
    }))
    .unwrap();
    let at = Utc.timestamp_opt(1_800_000_000, 0).unwrap();
    let entry = json!({"type":"qemu", "node":seed.node, "vmid":seed.vmid,
        "name":seed.name, "template":u8::from(seed.template)});
    assert_eq!(
        ClusterVmInventory::from_wire(json!([entry.clone()]), at),
        Err(PveReadError::InvalidResponse)
    );

    // Both worlds fit exactly the same seed identity. Choosing either status
    // therefore adds information that the supervisor did not supply.
    let mut stopped = entry.clone();
    stopped["status"] = json!("stopped");
    let mut running = entry;
    running["status"] = json!("running");
    let stopped = ClusterVmInventory::from_wire(json!([stopped]), at).unwrap();
    let running = ClusterVmInventory::from_wire(json!([running]), at).unwrap();
    assert_ne!(stopped, running);
    assert_eq!(stopped.observed_at(), at);
    assert_eq!(running.observed_at(), at);
}

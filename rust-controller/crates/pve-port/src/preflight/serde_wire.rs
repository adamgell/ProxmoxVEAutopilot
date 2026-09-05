//! Private persisted-fact decoding. Raw PVE JSON uses the separate `from_wire`
//! constructors; sanitized snapshots reload with ordinary serde deserialization.
//! Both entry points validate facts, and neither retains unrecognized text.

use super::*;
use serde::{
    Deserializer,
    de::{self, MapAccess, Visitor},
};
use std::{fmt, marker::PhantomData};

macro_rules! snapshot_wire {
    ($snapshot:ident, $wire:ident, { $($field:ident: $ty:ty),* $(,)? }) => {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct $wire { $($field: $ty),* }

        impl<'de> Deserialize<'de> for $snapshot {
            fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
                let wire = $wire::deserialize(deserializer)
                    .map_err(|_| de::Error::custom(PveReadError::InvalidResponse))?;
                Self::try_from(wire).map_err(de::Error::custom)
            }
        }
    };
}

// Standard BTreeMap decoding silently overwrites duplicate JSON keys. Evidence
// identity maps must instead reject that ambiguity before snapshot construction.
struct UniqueMap<K, V>(BTreeMap<K, V>);
impl<'de, K: Deserialize<'de> + Ord, V: Deserialize<'de>> Deserialize<'de> for UniqueMap<K, V> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct UniqueVisitor<K, V>(PhantomData<(K, V)>);
        impl<'de, K: Deserialize<'de> + Ord, V: Deserialize<'de>> Visitor<'de> for UniqueVisitor<K, V> {
            type Value = UniqueMap<K, V>;
            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("unique identity map")
            }
            fn visit_map<A: MapAccess<'de>>(self, mut access: A) -> Result<Self::Value, A::Error> {
                let mut map = BTreeMap::new();
                while let Some((key, value)) = access.next_entry()? {
                    if map.insert(key, value).is_some() {
                        return Err(de::Error::custom(PveReadError::InvalidResponse));
                    }
                }
                Ok(UniqueMap(map))
            }
        }
        deserializer.deserialize_map(UniqueVisitor(PhantomData))
    }
}

snapshot_wire!(NodeStatus, NodeStatusWire, { node: NodeName, online: bool, uptime: u64, observed_at: DateTime<Utc> });
impl TryFrom<NodeStatusWire> for NodeStatus {
    type Error = PveReadError;
    fn try_from(wire: NodeStatusWire) -> Result<Self, Self::Error> {
        if !wire.online || wire.uptime == 0 {
            return Err(PveReadError::InvalidResponse);
        }
        Ok(Self {
            node: wire.node,
            online: true,
            uptime: wire.uptime,
            observed_at: wire.observed_at,
        })
    }
}

snapshot_wire!(StorageStatus, StorageStatusWire, { node: NodeName, storage: StorageName, available_bytes: u64, observed_at: DateTime<Utc> });
impl TryFrom<StorageStatusWire> for StorageStatus {
    type Error = PveReadError;
    fn try_from(wire: StorageStatusWire) -> Result<Self, Self::Error> {
        // A StorageStatus is specifically active, enabled, image-capable storage.
        // The unsigned capacity and both identifier types have validated already.
        Ok(Self {
            node: wire.node,
            storage: wire.storage,
            available_bytes: wire.available_bytes,
            observed_at: wire.observed_at,
        })
    }
}

snapshot_wire!(BridgeInventory, BridgeInventoryWire, { node: NodeName, bridges: UniqueMap<BridgeName,bool>, observed_at: DateTime<Utc> });
impl TryFrom<BridgeInventoryWire> for BridgeInventory {
    type Error = PveReadError;
    fn try_from(wire: BridgeInventoryWire) -> Result<Self, Self::Error> {
        Ok(Self {
            node: wire.node,
            bridges: wire.bridges.0,
            observed_at: wire.observed_at,
        })
    }
}

snapshot_wire!(ClusterVm, ClusterVmWire, { vmid: Vmid, node: NodeName, name: NativeVmName, template: bool, power: PowerState });
impl TryFrom<ClusterVmWire> for ClusterVm {
    type Error = PveReadError;
    fn try_from(wire: ClusterVmWire) -> Result<Self, Self::Error> {
        Ok(Self {
            vmid: wire.vmid,
            node: wire.node,
            name: wire.name,
            template: wire.template,
            power: wire.power,
        })
    }
}

snapshot_wire!(ClusterVmInventory, ClusterVmInventoryWire, { vms: UniqueMap<Vmid,ClusterVm>, observed_at: DateTime<Utc> });
impl TryFrom<ClusterVmInventoryWire> for ClusterVmInventory {
    type Error = PveReadError;
    fn try_from(wire: ClusterVmInventoryWire) -> Result<Self, Self::Error> {
        if wire.vms.0.iter().any(|(vmid, vm)| vmid != &vm.vmid) {
            return Err(PveReadError::InvalidResponse);
        }
        Ok(Self {
            vms: wire.vms.0,
            observed_at: wire.observed_at,
        })
    }
}

snapshot_wire!(BootDisk, BootDiskWire, { storage: StorageName, volume: String });
impl TryFrom<BootDiskWire> for BootDisk {
    type Error = PveReadError;
    fn try_from(wire: BootDiskWire) -> Result<Self, Self::Error> {
        if !safe_atom(&wire.volume, 128) {
            return Err(PveReadError::InvalidResponse);
        }
        Ok(Self {
            storage: wire.storage,
            volume: wire.volume,
        })
    }
}

snapshot_wire!(NativeVmConfig, NativeVmConfigWire, {
    node: NodeName, vmid: Vmid, digest: String, name: NativeVmName, cores: u32, memory_mib: u64,
    boot_disk: BootDisk, uuid: VmUuid, mac: MacAddress, bridge: BridgeName, agent_enabled: bool,
    boots_scsi0: bool, template: bool, locked: bool, unsupported: BTreeSet<UnsupportedConfig>, observed_at: DateTime<Utc>
});
impl TryFrom<NativeVmConfigWire> for NativeVmConfig {
    type Error = PveReadError;
    fn try_from(wire: NativeVmConfigWire) -> Result<Self, Self::Error> {
        if !safe_atom(&wire.digest, 256)
            || wire.cores == 0
            || wire.memory_mib == 0
            || (wire.boots_scsi0 == wire.unsupported.contains(&UnsupportedConfig::BootOrder))
        {
            return Err(PveReadError::InvalidResponse);
        }
        Ok(Self {
            node: wire.node,
            vmid: wire.vmid,
            digest: wire.digest,
            name: wire.name,
            cores: wire.cores,
            memory_mib: wire.memory_mib,
            boot_disk: wire.boot_disk,
            uuid: wire.uuid,
            mac: wire.mac,
            bridge: wire.bridge,
            agent_enabled: wire.agent_enabled,
            boots_scsi0: wire.boots_scsi0,
            template: wire.template,
            locked: wire.locked,
            unsupported: wire.unsupported,
            observed_at: wire.observed_at,
        })
    }
}

snapshot_wire!(VmPowerStatus, VmPowerStatusWire, { node: NodeName, vmid: Vmid, power: PowerState, locked: Option<bool>, observed_at: DateTime<Utc> });
impl TryFrom<VmPowerStatusWire> for VmPowerStatus {
    type Error = PveReadError;
    fn try_from(wire: VmPowerStatusWire) -> Result<Self, Self::Error> {
        Ok(Self {
            node: wire.node,
            vmid: wire.vmid,
            power: wire.power,
            locked: wire.locked,
            observed_at: wire.observed_at,
        })
    }
}

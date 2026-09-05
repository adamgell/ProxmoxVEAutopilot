//! Synthetic native-v1 read contract. These fixtures are not a claim of live PVE
//! compatibility. `from_wire` validates raw PVE JSON; custom serde deserialization
//! validates persisted sanitized snapshots through private wire conversions.

mod serde_wire;

use std::collections::{BTreeMap, BTreeSet};

use async_trait::async_trait;
use chrono::{DateTime, Duration, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::{
    BridgeName, MacAddress, NativeVmName, NodeName, PveReadError, PveReadPort, StorageName, VmUuid,
    Vmid,
};

#[async_trait]
pub trait PvePreflightReadPort: PveReadPort {
    async fn node_status(&self, node: &NodeName) -> Result<NodeStatus, PveReadError>;
    async fn storage_status(
        &self,
        node: &NodeName,
        storage: &StorageName,
    ) -> Result<StorageStatus, PveReadError>;
    async fn bridges(&self, node: &NodeName) -> Result<BridgeInventory, PveReadError>;
    async fn cluster_vms(&self) -> Result<ClusterVmInventory, PveReadError>;
    async fn native_vm_config(
        &self,
        node: &NodeName,
        vmid: Vmid,
    ) -> Result<NativeVmConfig, PveReadError>;
    async fn vm_status(&self, node: &NodeName, vmid: Vmid) -> Result<VmPowerStatus, PveReadError>;
}

/// A config 404 alone has no absence authority. Read both sources before
/// sampling the clock, and preserve read failures separately from presence.
pub async fn observe_target_absence<P: PvePreflightReadPort + ?Sized>(
    port: &P,
    node: &NodeName,
    vmid: Vmid,
) -> Result<bool, PveReadError> {
    observe_target_absence_with_clock(port, node, vmid, Utc::now).await
}

pub async fn observe_target_absence_with_clock<P, C>(
    port: &P,
    node: &NodeName,
    vmid: Vmid,
    clock: C,
) -> Result<bool, PveReadError>
where
    P: PvePreflightReadPort + ?Sized,
    C: FnOnce() -> DateTime<Utc>,
{
    let inventory = port.cluster_vms().await;
    let config = port.native_vm_config(node, vmid).await;
    let as_of = clock();
    let inventory = inventory?;
    match config {
        Err(PveReadError::NotFound) => {
            Ok(inventory.is_fresh(as_of) && inventory.find(vmid).is_none())
        }
        Err(error) => Err(error),
        Ok(config) if config.node() != node || config.vmid() != vmid => {
            Err(PveReadError::InvalidResponse)
        }
        Ok(_) => Ok(false),
    }
}

fn fresh(observed_at: DateTime<Utc>, as_of: DateTime<Utc>) -> bool {
    observed_at <= as_of && as_of - observed_at <= Duration::seconds(30)
}

macro_rules! timestamp_accessors {
    () => {
        #[must_use]
        pub const fn observed_at(&self) -> DateTime<Utc> {
            self.observed_at
        }
        #[must_use]
        pub fn is_fresh(&self, as_of: DateTime<Utc>) -> bool {
            fresh(self.observed_at, as_of)
        }
    };
}

fn object(data: &Value) -> Result<&Map<String, Value>, PveReadError> {
    data.as_object().ok_or(PveReadError::InvalidResponse)
}
fn text<'a>(data: &'a Map<String, Value>, key: &str) -> Result<&'a str, PveReadError> {
    data.get(key)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or(PveReadError::InvalidResponse)
}
fn number(data: &Map<String, Value>, key: &str) -> Result<u64, PveReadError> {
    data.get(key)
        .and_then(Value::as_u64)
        .ok_or(PveReadError::InvalidResponse)
}
fn flag(value: &Value) -> Result<bool, PveReadError> {
    match value.as_u64() {
        Some(0) => Ok(false),
        Some(1) => Ok(true),
        _ => Err(PveReadError::InvalidResponse),
    }
}
fn optional_flag(data: &Map<String, Value>, key: &str) -> Result<Option<bool>, PveReadError> {
    data.get(key).map(flag).transpose()
}
fn bind_node(data: &Map<String, Value>, node: &NodeName) -> Result<(), PveReadError> {
    if let Some(value) = data.get("node") {
        let response_node = NodeName::parse(value.as_str().ok_or(PveReadError::InvalidResponse)?)
            .map_err(|_| PveReadError::InvalidResponse)?;
        if &response_node != node {
            return Err(PveReadError::InvalidResponse);
        }
    }
    Ok(())
}
fn parse_vmid(value: &Value) -> Result<Vmid, PveReadError> {
    let value = value
        .as_u64()
        .and_then(|value| u32::try_from(value).ok())
        .ok_or(PveReadError::InvalidResponse)?;
    Vmid::new(value).map_err(|_| PveReadError::InvalidResponse)
}
fn bind_vm(data: &Map<String, Value>, node: &NodeName, vmid: Vmid) -> Result<(), PveReadError> {
    bind_node(data, node)?;
    if let Some(value) = data.get("vmid")
        && parse_vmid(value)? != vmid
    {
        return Err(PveReadError::InvalidResponse);
    }
    Ok(())
}
fn safe_atom(value: &str, max: usize) -> bool {
    !value.is_empty()
        && value.len() <= max
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct NodeStatus {
    node: NodeName,
    online: bool,
    uptime: u64,
    observed_at: DateTime<Utc>,
}
impl NodeStatus {
    pub fn from_wire(
        node: NodeName,
        data: Value,
        observed_at: DateTime<Utc>,
    ) -> Result<Self, PveReadError> {
        let data = object(&data)?;
        bind_node(data, &node)?;
        let uptime = number(data, "uptime")?;
        if uptime == 0 {
            return Err(PveReadError::InvalidResponse);
        }
        Ok(Self {
            node,
            online: true,
            uptime,
            observed_at,
        })
    }
    pub const fn node(&self) -> &NodeName {
        &self.node
    }
    pub const fn online(&self) -> bool {
        self.online
    }
    pub const fn uptime(&self) -> u64 {
        self.uptime
    }
    timestamp_accessors!();
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct StorageStatus {
    node: NodeName,
    storage: StorageName,
    available_bytes: u64,
    observed_at: DateTime<Utc>,
}
impl StorageStatus {
    pub fn from_wire(
        node: NodeName,
        storage: StorageName,
        data: Value,
        observed_at: DateTime<Utc>,
    ) -> Result<Self, PveReadError> {
        let data = object(&data)?;
        bind_node(data, &node)?;
        if let Some(value) = data.get("storage") {
            let returned = StorageName::parse(value.as_str().ok_or(PveReadError::InvalidResponse)?)
                .map_err(|_| PveReadError::InvalidResponse)?;
            if returned != storage {
                return Err(PveReadError::InvalidResponse);
            }
        }
        if optional_flag(data, "active")? != Some(true)
            || optional_flag(data, "enabled")? != Some(true)
        {
            return Err(PveReadError::InvalidResponse);
        }
        let content = text(data, "content")?.split(',').collect::<BTreeSet<_>>();
        if !content.contains("images")
            || content.iter().any(|value| {
                !matches!(
                    *value,
                    "images" | "rootdir" | "iso" | "vztmpl" | "backup" | "snippets" | "import"
                )
            })
        {
            return Err(PveReadError::InvalidResponse);
        }
        let available_bytes = number(data, "avail")?;
        Ok(Self {
            node,
            storage,
            available_bytes,
            observed_at,
        })
    }
    pub const fn node(&self) -> &NodeName {
        &self.node
    }
    pub const fn storage(&self) -> &StorageName {
        &self.storage
    }
    pub const fn available_bytes(&self) -> u64 {
        self.available_bytes
    }
    timestamp_accessors!();
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct BridgeInventory {
    node: NodeName,
    bridges: BTreeMap<BridgeName, bool>,
    observed_at: DateTime<Utc>,
}
impl BridgeInventory {
    pub fn from_wire(
        node: NodeName,
        data: Value,
        observed_at: DateTime<Utc>,
    ) -> Result<Self, PveReadError> {
        let entries = data.as_array().ok_or(PveReadError::InvalidResponse)?;
        let mut bridges = BTreeMap::new();
        for entry in entries {
            let entry = object(entry)?;
            bind_node(entry, &node)?;
            if text(entry, "type")? != "bridge" {
                continue;
            }
            let name = BridgeName::parse(text(entry, "iface")?)
                .map_err(|_| PveReadError::InvalidResponse)?;
            let active = optional_flag(entry, "active")?.ok_or(PveReadError::InvalidResponse)?;
            if bridges.insert(name, active).is_some() {
                return Err(PveReadError::InvalidResponse);
            }
        }
        Ok(Self {
            node,
            bridges,
            observed_at,
        })
    }
    pub const fn node(&self) -> &NodeName {
        &self.node
    }
    pub const fn bridges(&self) -> &BTreeMap<BridgeName, bool> {
        &self.bridges
    }
    pub fn has_active(&self, bridge: &BridgeName) -> bool {
        self.bridges.get(bridge) == Some(&true)
    }
    timestamp_accessors!();
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PowerState {
    Running,
    Stopped,
}
impl PowerState {
    fn parse(value: &str) -> Result<Self, PveReadError> {
        match value {
            "running" => Ok(Self::Running),
            "stopped" => Ok(Self::Stopped),
            _ => Err(PveReadError::InvalidResponse),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ClusterVm {
    vmid: Vmid,
    node: NodeName,
    name: NativeVmName,
    template: bool,
    power: PowerState,
}
impl ClusterVm {
    pub const fn vmid(&self) -> Vmid {
        self.vmid
    }
    pub const fn node(&self) -> &NodeName {
        &self.node
    }
    pub const fn name(&self) -> &NativeVmName {
        &self.name
    }
    pub const fn is_template(&self) -> bool {
        self.template
    }
    pub const fn power(&self) -> PowerState {
        self.power
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ClusterVmInventory {
    vms: BTreeMap<Vmid, ClusterVm>,
    observed_at: DateTime<Utc>,
}
impl ClusterVmInventory {
    pub fn from_wire(data: Value, observed_at: DateTime<Utc>) -> Result<Self, PveReadError> {
        let entries = data.as_array().ok_or(PveReadError::InvalidResponse)?;
        let mut vms = BTreeMap::new();
        for entry in entries {
            let entry = object(entry)?;
            if text(entry, "type")? != "qemu" {
                return Err(PveReadError::InvalidResponse);
            }
            let vmid = parse_vmid(entry.get("vmid").ok_or(PveReadError::InvalidResponse)?)?;
            let node =
                NodeName::parse(text(entry, "node")?).map_err(|_| PveReadError::InvalidResponse)?;
            let name = NativeVmName::parse(text(entry, "name")?)
                .map_err(|_| PveReadError::InvalidResponse)?;
            let template =
                optional_flag(entry, "template")?.ok_or(PveReadError::InvalidResponse)?;
            let power = PowerState::parse(text(entry, "status")?)?;
            if vms
                .insert(
                    vmid,
                    ClusterVm {
                        vmid,
                        node,
                        name,
                        template,
                        power,
                    },
                )
                .is_some()
            {
                return Err(PveReadError::InvalidResponse);
            }
        }
        Ok(Self { vms, observed_at })
    }
    pub const fn vms(&self) -> &BTreeMap<Vmid, ClusterVm> {
        &self.vms
    }
    pub fn find(&self, vmid: Vmid) -> Option<&ClusterVm> {
        self.vms.get(&vmid)
    }
    timestamp_accessors!();
}

/// Unsupported values never survive as raw evidence. Every consumer must reject
/// a nonempty reason set before treating the config as a native-v1 layout.
#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum UnsupportedConfig {
    Args,
    AdditionalNic,
    AdditionalDisk,
    NicProperties,
    DiskProperties,
    SmbiosProperties,
    AgentProperties,
    BootOrder,
    UnknownField,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct BootDisk {
    storage: StorageName,
    volume: String,
}
impl BootDisk {
    pub const fn storage(&self) -> &StorageName {
        &self.storage
    }
    pub fn volume(&self) -> &str {
        &self.volume
    }
}

fn properties(value: &str) -> Result<BTreeMap<&str, &str>, PveReadError> {
    let mut properties = BTreeMap::new();
    for property in value.split(',') {
        let (key, value) = property
            .split_once('=')
            .ok_or(PveReadError::InvalidResponse)?;
        if key.is_empty() || value.is_empty() || properties.insert(key, value).is_some() {
            return Err(PveReadError::InvalidResponse);
        }
    }
    Ok(properties)
}
fn device_key(key: &str, prefix: &str) -> bool {
    key.strip_prefix(prefix).is_some_and(|suffix| {
        !suffix.is_empty() && suffix.bytes().all(|byte| byte.is_ascii_digit())
    })
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct NativeVmConfig {
    fake_clone_provenance: Option<crate::FakeCloneProvenance>,
    node: NodeName,
    vmid: Vmid,
    digest: String,
    name: NativeVmName,
    cores: u32,
    memory_mib: u64,
    boot_disk: BootDisk,
    uuid: VmUuid,
    mac: MacAddress,
    bridge: BridgeName,
    agent_enabled: bool,
    boots_scsi0: bool,
    template: bool,
    locked: bool,
    unsupported: BTreeSet<UnsupportedConfig>,
    observed_at: DateTime<Utc>,
}
impl NativeVmConfig {
    pub const fn fake_clone_provenance(&self) -> Option<&crate::FakeCloneProvenance> {
        self.fake_clone_provenance.as_ref()
    }
    pub(crate) fn with_fake_provenance(mut self, provenance: crate::FakeCloneProvenance) -> Self {
        self.fake_clone_provenance = Some(provenance);
        self
    }
    pub fn from_wire(
        node: NodeName,
        vmid: Vmid,
        data: Value,
        observed_at: DateTime<Utc>,
    ) -> Result<Self, PveReadError> {
        let data = object(&data)?;
        bind_vm(data, &node, vmid)?;
        let digest = text(data, "digest")?;
        if !safe_atom(digest, 256) {
            return Err(PveReadError::InvalidResponse);
        }
        let name =
            NativeVmName::parse(text(data, "name")?).map_err(|_| PveReadError::InvalidResponse)?;
        let cores =
            u32::try_from(number(data, "cores")?).map_err(|_| PveReadError::InvalidResponse)?;
        let memory_mib = number(data, "memory")?;
        if cores == 0 || memory_mib == 0 {
            return Err(PveReadError::InvalidResponse);
        }
        let mut unsupported = BTreeSet::new();
        let smbios = properties(text(data, "smbios1")?)?;
        let uuid = VmUuid::parse(smbios.get("uuid").ok_or(PveReadError::InvalidResponse)?)
            .map_err(|_| PveReadError::InvalidResponse)?;
        if smbios.len() != 1 {
            unsupported.insert(UnsupportedConfig::SmbiosProperties);
        }
        let nic = properties(text(data, "net0")?)?;
        let mac = MacAddress::parse(nic.get("virtio").ok_or(PveReadError::InvalidResponse)?)
            .map_err(|_| PveReadError::InvalidResponse)?;
        let bridge = BridgeName::parse(*nic.get("bridge").ok_or(PveReadError::InvalidResponse)?)
            .map_err(|_| PveReadError::InvalidResponse)?;
        if nic.iter().any(|(key, value)| {
            !matches!((*key, *value), ("virtio" | "bridge", _) | ("firewall", "0"))
        }) {
            unsupported.insert(UnsupportedConfig::NicProperties);
        }
        let disk = text(data, "scsi0")?;
        let (volume, options) = disk
            .split_once(',')
            .map_or((disk, None), |(volume, options)| (volume, Some(options)));
        let (storage, volume) = volume
            .split_once(':')
            .ok_or(PveReadError::InvalidResponse)?;
        let storage = StorageName::parse(storage).map_err(|_| PveReadError::InvalidResponse)?;
        if !safe_atom(volume, 128) {
            return Err(PveReadError::InvalidResponse);
        }
        if let Some(options) = options {
            for (key, value) in properties(options)? {
                if key != "size" || !valid_disk_size(value) {
                    unsupported.insert(UnsupportedConfig::DiskProperties);
                }
            }
        }
        let boot_disk = BootDisk {
            storage,
            volume: volume.to_owned(),
        };
        let agent = data.get("agent").ok_or(PveReadError::InvalidResponse)?;
        let agent_enabled = match agent {
            Value::Number(_) => flag(agent)?,
            Value::String(value) if value == "1" || value == "0" => value == "1",
            Value::String(value) => {
                let props = properties(value)?;
                let enabled = match props.get("enabled") {
                    Some(&"1") => true,
                    Some(&"0") => false,
                    _ => return Err(PveReadError::InvalidResponse),
                };
                if props.iter().any(|(key, value)| {
                    !matches!((*key, *value), ("enabled", _) | ("type", "virtio"))
                }) {
                    unsupported.insert(UnsupportedConfig::AgentProperties);
                }
                enabled
            }
            _ => return Err(PveReadError::InvalidResponse),
        };
        let boot = text(data, "boot")?;
        properties(boot)?;
        let boots_scsi0 = boot == "order=scsi0";
        if !boots_scsi0 {
            unsupported.insert(UnsupportedConfig::BootOrder);
        }
        let template = optional_flag(data, "template")?.unwrap_or(false);
        let locked = if let Some(lock) = data.get("lock") {
            let lock = lock.as_str().ok_or(PveReadError::InvalidResponse)?;
            if !safe_atom(lock, 64) {
                return Err(PveReadError::InvalidResponse);
            }
            true
        } else {
            false
        };
        for key in data.keys() {
            if matches!(
                key.as_str(),
                "node"
                    | "vmid"
                    | "digest"
                    | "name"
                    | "cores"
                    | "memory"
                    | "scsi0"
                    | "smbios1"
                    | "net0"
                    | "agent"
                    | "boot"
                    | "template"
                    | "lock"
            ) {
                continue;
            }
            let reason = if key == "args" {
                UnsupportedConfig::Args
            } else if device_key(key, "net") {
                let nic = properties(text(data, key)?)?;
                MacAddress::parse(nic.get("virtio").ok_or(PveReadError::InvalidResponse)?)
                    .map_err(|_| PveReadError::InvalidResponse)?;
                BridgeName::parse(*nic.get("bridge").ok_or(PveReadError::InvalidResponse)?)
                    .map_err(|_| PveReadError::InvalidResponse)?;
                UnsupportedConfig::AdditionalNic
            } else if [
                "scsi", "sata", "ide", "virtio", "efidisk", "tpmstate", "unused",
            ]
            .iter()
            .any(|prefix| device_key(key, prefix))
            {
                UnsupportedConfig::AdditionalDisk
            } else {
                UnsupportedConfig::UnknownField
            };
            unsupported.insert(reason);
        }
        Ok(Self {
            node,
            vmid,
            digest: digest.to_owned(),
            name,
            cores,
            memory_mib,
            boot_disk,
            fake_clone_provenance: None,
            uuid,
            mac,
            bridge,
            agent_enabled,
            boots_scsi0,
            template,
            locked,
            unsupported,
            observed_at,
        })
    }
    pub const fn node(&self) -> &NodeName {
        &self.node
    }
    pub const fn vmid(&self) -> Vmid {
        self.vmid
    }
    pub fn digest(&self) -> &str {
        &self.digest
    }
    pub const fn name(&self) -> &NativeVmName {
        &self.name
    }
    pub const fn cores(&self) -> u32 {
        self.cores
    }
    pub const fn memory_mib(&self) -> u64 {
        self.memory_mib
    }
    pub const fn boot_disk(&self) -> &BootDisk {
        &self.boot_disk
    }
    pub const fn uuid(&self) -> VmUuid {
        self.uuid
    }
    pub const fn mac(&self) -> &MacAddress {
        &self.mac
    }
    pub const fn bridge(&self) -> &BridgeName {
        &self.bridge
    }
    pub const fn agent_enabled(&self) -> bool {
        self.agent_enabled
    }
    pub const fn boots_scsi0(&self) -> bool {
        self.boots_scsi0
    }
    pub const fn is_template(&self) -> bool {
        self.template
    }
    pub const fn locked(&self) -> bool {
        self.locked
    }
    pub const fn unsupported(&self) -> &BTreeSet<UnsupportedConfig> {
        &self.unsupported
    }
    timestamp_accessors!();
}
fn valid_disk_size(value: &str) -> bool {
    let digits = value.strip_suffix(['K', 'M', 'G', 'T']).unwrap_or(value);
    !digits.is_empty()
        && digits.bytes().all(|byte| byte.is_ascii_digit())
        && digits.parse::<u64>().is_ok_and(|size| size > 0)
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct VmPowerStatus {
    node: NodeName,
    vmid: Vmid,
    power: PowerState,
    locked: Option<bool>,
    observed_at: DateTime<Utc>,
}
impl VmPowerStatus {
    pub fn from_wire(
        node: NodeName,
        vmid: Vmid,
        data: Value,
        observed_at: DateTime<Utc>,
    ) -> Result<Self, PveReadError> {
        let data = object(&data)?;
        bind_vm(data, &node, vmid)?;
        parse_vmid(data.get("vmid").ok_or(PveReadError::InvalidResponse)?)?;
        let power = PowerState::parse(text(data, "status")?)?;
        let locked = optional_flag(data, "locked")?;
        Ok(Self {
            node,
            vmid,
            power,
            locked,
            observed_at,
        })
    }
    pub const fn node(&self) -> &NodeName {
        &self.node
    }
    pub const fn vmid(&self) -> Vmid {
        self.vmid
    }
    pub const fn power(&self) -> PowerState {
        self.power
    }
    pub const fn locked(&self) -> Option<bool> {
        self.locked
    }
    timestamp_accessors!();
}

//! Pure, explicit synthetic provisioning-v1 facts. Values never grant mutation
//! authority, certify disk contents, or supply omitted real-PVE defaults.
mod config;
mod expectations;
mod serde_wire;

use crate::{
    BridgeName, FakeCloneProvenance, MacAddress, NativeEvidenceSource, NativeVmName, NativeVmPlan,
    NodeName, PveReadError, StorageName, VmUuid, Vmid,
};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;

#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
#[error("invalid provisioning contract")]
pub struct InvalidProvisioning;

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProvisioningActionV1 {
    Clone,
    EnsureCapacity,
    ConfigurePe,
    StartPe,
    EnsureStopped,
    ConfigureDisk,
    StartDisk,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProvisioningBootProfile {
    PeMedia,
    InstalledDisk,
}

pub struct ProvisioningExpectationsInputV1<'a> {
    pub vm: NativeVmPlan,
    pub template_config_sha256: &'a str,
    pub template_capacity_bytes: u64,
    pub effective_capacity_bytes: u64,
    pub system_serial: &'a str,
    pub disk_serial: &'a str,
    pub deployment_iso_volid: &'a str,
    pub driver_iso_volid: &'a str,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ProvisioningExpectationsV1 {
    contract_version: u16,
    vm: NativeVmPlan,
    template_config_sha256: String,
    template_capacity_bytes: u64,
    effective_capacity_bytes: u64,
    system_serial: String,
    disk_serial: String,
    deployment_iso_volid: String,
    driver_iso_volid: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ProvisioningOperationPlanV1 {
    contract_version: u16,
    action: ProvisioningActionV1,
    expected: ProvisioningExpectationsV1,
}

fn fingerprint(value: &impl Serialize) -> Result<String, InvalidProvisioning> {
    let value = serde_json::to_value(value).map_err(|_| InvalidProvisioning)?;
    event_journal::payload_digest(&value).map_err(|_| InvalidProvisioning)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProvisioningFirmwareV1 {
    Seabios,
    Unsupported,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProvisioningCpuV1 {
    Host,
    Unsupported,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProvisioningQgaChannelV1 {
    Virtio,
    Unsupported,
}

/// Unsupported facts keep only these fixed classes, never rejected keys/values.
#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProvisioningUnsupportedV1 {
    Firmware,
    Cpu,
    Balloon,
    QgaChannel,
    AgentProperties,
    BootOrder,
    DeploymentMedia,
    DriverMedia,
    DiskProperties,
    SmbiosProperties,
    NicProperties,
    AdditionalNic,
    AdditionalDisk,
    EfiDisk,
    TpmState,
    UnusedDisk,
    Oem,
    Args,
    UnknownField,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum ProvisioningMediaSlotV1 {
    Absent,
    Iso { volid: String },
    Unsupported,
}
impl ProvisioningMediaSlotV1 {
    pub fn volid(&self) -> Option<&str> {
        match self {
            Self::Iso { volid } => Some(volid),
            _ => None,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ProvisioningPrimaryDiskV1 {
    storage: StorageName,
    volume: String,
    capacity_bytes: u64,
    serial: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ProvisioningVmConfigV1 {
    contract_version: u16,
    node: NodeName,
    vmid: Vmid,
    source: NativeEvidenceSource,
    digest: String,
    name: NativeVmName,
    cores: u32,
    memory_mib: u64,
    primary_disk: ProvisioningPrimaryDiskV1,
    uuid: VmUuid,
    mac: MacAddress,
    bridge: BridgeName,
    system_serial: Option<String>,
    firmware: ProvisioningFirmwareV1,
    cpu: ProvisioningCpuV1,
    balloon_mib: u64,
    qga_enabled: bool,
    qga_channel: ProvisioningQgaChannelV1,
    boot_profile: Option<ProvisioningBootProfile>,
    deployment_iso: ProvisioningMediaSlotV1,
    driver_iso: ProvisioningMediaSlotV1,
    template: bool,
    locked: bool,
    unsupported: BTreeSet<ProvisioningUnsupportedV1>,
    fake_clone_provenance: Option<FakeCloneProvenance>,
    observed_at: DateTime<Utc>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProvisioningCoverageV1 {
    Complete,
    Partial,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ProvisioningMediaInventoryV1 {
    contract_version: u16,
    node: NodeName,
    storage: StorageName,
    iso_volids: Vec<String>,
    coverage: ProvisioningCoverageV1,
    observed_at: DateTime<Utc>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ProvisioningQgaObservationV1 {
    contract_version: u16,
    node: NodeName,
    vmid: Vmid,
    reachable: bool,
    observed_at: DateTime<Utc>,
}

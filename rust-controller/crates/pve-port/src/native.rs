//! Immutable synthetic native-v1 contracts. Encoders are for fake inspection;
//! no transport consumes them and the marker is never a live PVE parameter.
use crate::{
    BridgeName, MacAddress, NativeVmConfig, NativeVmName, NodeName, StorageName, Upid, VmUuid, Vmid,
};
use async_trait::async_trait;
use chrono::{DateTime, Utc};
use controller_domain::OperationId;
use serde::{Deserialize, Serialize, de};
use uuid::Uuid;

#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
#[error("invalid native VM plan")]
pub struct InvalidNativePlan;

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct NativeVmPlan {
    contract_version: u16,
    cluster_key: NativeVmName,
    node: NodeName,
    source_vmid: Vmid,
    target_vmid: Vmid,
    name: NativeVmName,
    storage: StorageName,
    bridge: BridgeName,
    uuid: VmUuid,
    mac: MacAddress,
    cores: u32,
    memory_mib: u64,
    minimum_storage_bytes: u64,
}
macro_rules! value_getters { ($($name:ident:$ty:ty),* $(,)?) => {$(pub const fn $name(&self)->$ty {self.$name})*}; }
macro_rules! ref_getters { ($($name:ident:$ty:ty),* $(,)?) => {$(pub const fn $name(&self)->&$ty {&self.$name})*}; }
impl NativeVmPlan {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        contract_version: u16,
        cluster_key: NativeVmName,
        node: NodeName,
        source_vmid: Vmid,
        target_vmid: Vmid,
        name: NativeVmName,
        storage: StorageName,
        bridge: BridgeName,
        uuid: VmUuid,
        mac: MacAddress,
        cores: u32,
        memory_mib: u64,
        minimum_storage_bytes: u64,
    ) -> Result<Self, InvalidNativePlan> {
        if contract_version != 1
            || source_vmid == target_vmid
            || !(1..=128).contains(&cores)
            || !(512..=1_048_576).contains(&memory_mib)
            || !memory_mib.is_multiple_of(128)
            || minimum_storage_bytes == 0
        {
            return Err(InvalidNativePlan);
        }
        Ok(Self {
            contract_version,
            cluster_key,
            node,
            source_vmid,
            target_vmid,
            name,
            storage,
            bridge,
            uuid,
            mac,
            cores,
            memory_mib,
            minimum_storage_bytes,
        })
    }
    value_getters!(contract_version:u16,source_vmid:Vmid,target_vmid:Vmid,uuid:VmUuid,cores:u32,memory_mib:u64,minimum_storage_bytes:u64);
    ref_getters!(cluster_key:NativeVmName,node:NodeName,name:NativeVmName,storage:StorageName,bridge:BridgeName,mac:MacAddress);
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PlanWire {
    contract_version: u16,
    cluster_key: NativeVmName,
    node: NodeName,
    source_vmid: Vmid,
    target_vmid: Vmid,
    name: NativeVmName,
    storage: StorageName,
    bridge: BridgeName,
    uuid: VmUuid,
    mac: MacAddress,
    cores: u32,
    memory_mib: u64,
    minimum_storage_bytes: u64,
}
impl<'de> Deserialize<'de> for NativeVmPlan {
    fn deserialize<D: de::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let w = PlanWire::deserialize(d).map_err(|_| de::Error::custom(InvalidNativePlan))?;
        Self::new(
            w.contract_version,
            w.cluster_key,
            w.node,
            w.source_vmid,
            w.target_vmid,
            w.name,
            w.storage,
            w.bridge,
            w.uuid,
            w.mac,
            w.cores,
            w.memory_mib,
            w.minimum_storage_bytes,
        )
        .map_err(de::Error::custom)
    }
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NativeStep {
    Clone,
    Configure,
    Start,
}
impl NativeStep {
    pub const fn operation_key(self) -> &'static str {
        match self {
            Self::Clone => "pve.clone.v1",
            Self::Configure => "pve.configure.v1",
            Self::Start => "pve.start.v1",
        }
    }
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct NativeOperationPlan {
    step: NativeStep,
    vm: NativeVmPlan,
}
impl NativeOperationPlan {
    pub const fn new(step: NativeStep, vm: NativeVmPlan) -> Self {
        Self { step, vm }
    }
    value_getters!(step:NativeStep);
    ref_getters!(vm:NativeVmPlan);
}
impl<'de> Deserialize<'de> for NativeOperationPlan {
    fn deserialize<D: de::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Wire {
            step: NativeStep,
            vm: NativeVmPlan,
        }
        let w = Wire::deserialize(d).map_err(|_| de::Error::custom(InvalidNativePlan))?;
        Ok(Self::new(w.step, w.vm))
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PveWriteError {
    Unauthorized,
    Conflict,
    Rejected,
    OutcomeUnknown,
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum MutationReceipt {
    Task(Upid),
    SynchronousAccepted,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct CloneRequest {
    vm: NativeVmPlan,
    operation_id: OperationId,
    request_marker: Uuid,
}
impl<'de> Deserialize<'de> for CloneRequest {
    fn deserialize<D: de::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Wire {
            vm: NativeVmPlan,
            operation_id: OperationId,
            request_marker: Uuid,
        }
        let w = Wire::deserialize(d).map_err(|_| de::Error::custom("invalid clone request"))?;
        if w.request_marker.is_nil() {
            return Err(de::Error::custom("invalid clone request"));
        }
        Ok(Self {
            vm: w.vm,
            operation_id: w.operation_id,
            request_marker: w.request_marker,
        })
    }
}
impl CloneRequest {
    /// Generate metadata before computing the digest; persist this request before sending.
    pub fn new(vm: NativeVmPlan, operation_id: OperationId) -> Self {
        Self {
            vm,
            operation_id,
            request_marker: Uuid::now_v7(),
        }
    }
    ref_getters!(vm:NativeVmPlan);
    value_getters!(operation_id:OperationId,request_marker:Uuid);
    pub fn request_digest(&self) -> String {
        canonical_digest(self)
    }
    pub const fn method(&self) -> &'static str {
        "POST"
    }
    pub fn path_segments(&self) -> Vec<String> {
        route(&self.vm, self.vm.source_vmid(), &["clone"])
    }
    pub fn form(&self) -> Vec<(&'static str, String)> {
        vec![
            ("newid", self.vm.target_vmid().to_string()),
            ("name", self.vm.name().to_string()),
            ("full", "1".into()),
            ("storage", self.vm.storage().to_string()),
        ]
    }
}
pub(crate) fn canonical_digest(value: &impl Serialize) -> String {
    event_journal::payload_digest(
        &serde_json::to_value(value).expect("typed native fields serialize"),
    )
    .expect("typed native fields have canonical JSON")
}
fn route(vm: &NativeVmPlan, vmid: Vmid, suffix: &[&str]) -> Vec<String> {
    [
        "nodes".into(),
        vm.node().to_string(),
        "qemu".into(),
        vmid.to_string(),
    ]
    .into_iter()
    .chain(suffix.iter().map(|s| (*s).into()))
    .collect()
}

/// Fake provenance is evidence, not authority. The future evaluator must also
/// establish matching task success and resource identity before binding it.
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct FakeCloneProvenance {
    operation_id: OperationId,
    request_marker: Uuid,
    source_vmid: Vmid,
    target_vmid: Vmid,
    request_digest: String,
}
impl FakeCloneProvenance {
    pub(crate) fn from_request(request: &CloneRequest) -> Self {
        Self {
            operation_id: request.operation_id,
            request_marker: request.request_marker,
            source_vmid: request.vm.source_vmid,
            target_vmid: request.vm.target_vmid,
            request_digest: request.request_digest(),
        }
    }
    value_getters!(operation_id:OperationId,request_marker:Uuid,source_vmid:Vmid,target_vmid:Vmid);
    pub fn request_digest(&self) -> &str {
        &self.request_digest
    }
    pub fn matches(&self, request: &CloneRequest) -> bool {
        self == &Self::from_request(request)
    }
}
impl<'de> Deserialize<'de> for FakeCloneProvenance {
    fn deserialize<D: de::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Wire {
            operation_id: OperationId,
            request_marker: Uuid,
            source_vmid: Vmid,
            target_vmid: Vmid,
            request_digest: String,
        }
        let w =
            Wire::deserialize(d).map_err(|_| de::Error::custom("invalid fake clone provenance"))?;
        if w.request_marker.is_nil()
            || w.source_vmid == w.target_vmid
            || w.request_digest.len() != 64
            || !w
                .request_digest
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(de::Error::custom("invalid fake clone provenance"));
        }
        Ok(Self {
            operation_id: w.operation_id,
            request_marker: w.request_marker,
            source_vmid: w.source_vmid,
            target_vmid: w.target_vmid,
            request_digest: w.request_digest,
        })
    }
}

/// The historical snapshot is an immutable ownership reference supplied by the
/// caller. This boundary checks consistency; it does not certify its proof.
fn validate_owned(
    vm: &NativeVmPlan,
    clone: &CloneRequest,
    bound: &NativeVmConfig,
    current: &NativeVmConfig,
    as_of: DateTime<Utc>,
) -> Result<(), PveWriteError> {
    if vm != clone.vm() {
        return Err(PveWriteError::Conflict);
    }
    for config in [bound, current] {
        let provenance = config
            .fake_clone_provenance()
            .ok_or(PveWriteError::OutcomeUnknown)?;
        if !provenance.matches(clone)
            || config.node() != vm.node()
            || config.vmid() != vm.target_vmid()
            || config.name() != vm.name()
            || config.is_template()
            || config.locked()
            || !config.unsupported().is_empty()
            || config.boot_disk().storage() != vm.storage()
        {
            return Err(PveWriteError::Conflict);
        }
    }
    if bound.boot_disk() != current.boot_disk() {
        return Err(PveWriteError::Conflict);
    }
    if !current.is_fresh(as_of) {
        return Err(PveWriteError::OutcomeUnknown);
    }
    Ok(())
}
pub(crate) fn final_fields(vm: &NativeVmPlan, current: &NativeVmConfig) -> bool {
    current.uuid() == vm.uuid()
        && current.mac() == vm.mac()
        && current.bridge() == vm.bridge()
        && current.cores() == vm.cores()
        && current.memory_mib() == vm.memory_mib()
        && current.agent_enabled()
        && current.boots_scsi0()
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ConfigureRequest {
    vm: NativeVmPlan,
    expected: NativeVmConfig,
}
impl ConfigureRequest {
    pub fn new(
        vm: NativeVmPlan,
        clone: &CloneRequest,
        bound: &NativeVmConfig,
        current: &NativeVmConfig,
        as_of: DateTime<Utc>,
    ) -> Result<Self, PveWriteError> {
        validate_owned(&vm, clone, bound, current, as_of)?;
        if bound.uuid() != current.uuid() || bound.mac() != current.mac() {
            return Err(PveWriteError::Conflict);
        }
        Ok(Self {
            vm,
            expected: current.clone(),
        })
    }
    ref_getters!(vm:NativeVmPlan,expected:NativeVmConfig);
    pub const fn method(&self) -> &'static str {
        "PUT"
    }
    pub fn path_segments(&self) -> Vec<String> {
        route(&self.vm, self.vm.target_vmid(), &["config"])
    }
    pub fn form(&self) -> Vec<(&'static str, String)> {
        vec![
            ("digest", self.expected.digest().into()),
            ("smbios1", format!("uuid={}", self.vm.uuid())),
            (
                "net0",
                format!(
                    "virtio={},bridge={},firewall=0",
                    self.vm.mac(),
                    self.vm.bridge()
                ),
            ),
            ("cores", self.vm.cores().to_string()),
            ("memory", self.vm.memory_mib().to_string()),
            ("agent", "enabled=1,type=virtio".into()),
            ("boot", "order=scsi0".into()),
        ]
    }
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct StartRequest {
    vm: NativeVmPlan,
    expected: NativeVmConfig,
}
impl StartRequest {
    pub fn new(
        vm: NativeVmPlan,
        clone: &CloneRequest,
        bound: &NativeVmConfig,
        current: &NativeVmConfig,
        as_of: DateTime<Utc>,
    ) -> Result<Self, PveWriteError> {
        validate_owned(&vm, clone, bound, current, as_of)?;
        if !final_fields(&vm, current) {
            return Err(PveWriteError::Conflict);
        }
        Ok(Self {
            vm,
            expected: current.clone(),
        })
    }
    ref_getters!(vm:NativeVmPlan,expected:NativeVmConfig);
    pub const fn method(&self) -> &'static str {
        "POST"
    }
    pub fn path_segments(&self) -> Vec<String> {
        route(&self.vm, self.vm.target_vmid(), &["status", "start"])
    }
    pub fn form(&self) -> Vec<(&'static str, String)> {
        vec![]
    }
}

pub(crate) mod sealed {
    pub trait FakeMutationCapability {}
}
/// Only the in-memory fake has this sealed capability. Downstream observers
/// cannot implement it (the local wrapper avoids relying on the orphan rule).
/// ```compile_fail
/// use pve_port::*;
/// struct RealObserver(ReqwestPveObserver);
/// #[async_trait::async_trait]
/// impl PveMutationPort for RealObserver {
///     async fn clone_vm(&self, _: &CloneRequest)->Result<MutationReceipt,PveWriteError> {unimplemented!()}
///     async fn configure_vm(&self, _: &ConfigureRequest)->Result<MutationReceipt,PveWriteError> {unimplemented!()}
///     async fn start_vm(&self, _: &StartRequest)->Result<MutationReceipt,PveWriteError> {unimplemented!()}
/// }
/// ```
#[async_trait]
pub trait PveMutationPort: sealed::FakeMutationCapability + Send + Sync {
    async fn clone_vm(&self, request: &CloneRequest) -> Result<MutationReceipt, PveWriteError>;
    async fn configure_vm(
        &self,
        request: &ConfigureRequest,
    ) -> Result<MutationReceipt, PveWriteError>;
    async fn start_vm(&self, request: &StartRequest) -> Result<MutationReceipt, PveWriteError>;
}

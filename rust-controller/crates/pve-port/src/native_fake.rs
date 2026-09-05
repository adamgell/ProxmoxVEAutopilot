//! Owned in-memory synthetic PVE. No URL, credentials, transport or processes.
use crate::*;
use async_trait::async_trait;
use chrono::Utc;
use serde_json::json;
use std::{
    collections::{BTreeMap, VecDeque},
    sync::{Arc, Mutex},
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FakeMutationOutcome {
    Accepted,
    Rejected(PveWriteError),
    AppliedResponseLost,
    AcceptedTaskFails,
    AcceptedTaskDelayed,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
#[error("task-only outcome is unsupported for synchronous configure")]
pub struct UnsupportedFakeOutcome;
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum NativeMutationRequest {
    Clone(CloneRequest),
    Configure(ConfigureRequest),
    Start(StartRequest),
}
impl NativeMutationRequest {
    fn step(&self) -> NativeStep {
        match self {
            Self::Clone(_) => NativeStep::Clone,
            Self::Configure(_) => NativeStep::Configure,
            Self::Start(_) => NativeStep::Start,
        }
    }
    fn vm(&self) -> &NativeVmPlan {
        match self {
            Self::Clone(r) => r.vm(),
            Self::Configure(r) => r.vm(),
            Self::Start(r) => r.vm(),
        }
    }
}
#[derive(Clone)]
struct FakeVm {
    config: NativeVmConfig,
    power: PowerState,
}
#[derive(Default)]
struct State {
    vms: BTreeMap<Vmid, FakeVm>,
    nodes: BTreeMap<NodeName, NodeStatus>,
    storage: BTreeMap<(NodeName, StorageName), StorageStatus>,
    bridges: BTreeMap<NodeName, BridgeInventory>,
    outcomes: BTreeMap<NativeStep, VecDeque<FakeMutationOutcome>>,
    requests: Vec<NativeMutationRequest>,
    tasks: BTreeMap<Upid, TaskState>,
    pending: VecDeque<(Upid, NativeMutationRequest)>,
    sequence: u32,
}
#[derive(Clone, Default)]
pub struct NativeFakePve {
    state: Arc<Mutex<State>>,
}
impl NativeFakePve {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn insert_vm(
        &self,
        config: NativeVmConfig,
        power: PowerState,
    ) -> Result<(), PveWriteError> {
        let mut s = self.state.lock().unwrap();
        if s.vms.contains_key(&config.vmid()) {
            return Err(PveWriteError::Conflict);
        }
        s.vms.insert(config.vmid(), FakeVm { config, power });
        Ok(())
    }
    /// Explicit fixture change for modeling out-of-band edits. No observed
    /// provenance is minted here; the supplied typed snapshot is preserved.
    pub fn replace_vm(
        &self,
        config: NativeVmConfig,
        power: PowerState,
    ) -> Result<(), PveWriteError> {
        let mut s = self.state.lock().unwrap();
        if !s.vms.contains_key(&config.vmid()) {
            return Err(PveWriteError::Rejected);
        }
        s.vms.insert(config.vmid(), FakeVm { config, power });
        Ok(())
    }
    pub fn set_node_status(&self, status: NodeStatus) {
        self.state
            .lock()
            .unwrap()
            .nodes
            .insert(status.node().clone(), status);
    }
    pub fn set_storage_status(&self, status: StorageStatus) {
        self.state
            .lock()
            .unwrap()
            .storage
            .insert((status.node().clone(), status.storage().clone()), status);
    }
    pub fn set_bridges(&self, status: BridgeInventory) {
        self.state
            .lock()
            .unwrap()
            .bridges
            .insert(status.node().clone(), status);
    }
    /// Configure supports only Accepted, Rejected and AppliedResponseLost.
    pub fn enqueue_outcome(
        &self,
        step: NativeStep,
        outcome: FakeMutationOutcome,
    ) -> Result<(), UnsupportedFakeOutcome> {
        if step == NativeStep::Configure
            && matches!(
                outcome,
                FakeMutationOutcome::AcceptedTaskFails | FakeMutationOutcome::AcceptedTaskDelayed
            )
        {
            return Err(UnsupportedFakeOutcome);
        }
        self.state
            .lock()
            .unwrap()
            .outcomes
            .entry(step)
            .or_default()
            .push_back(outcome);
        Ok(())
    }
    pub fn recorded_requests(&self) -> Vec<NativeMutationRequest> {
        self.state.lock().unwrap().requests.clone()
    }
    /// Complete the next delayed clone/start, rechecking conflicts at application.
    /// A conflict records task failure and leaves the occupant unchanged.
    pub fn complete_pending(&self) -> Result<(), PveWriteError> {
        let mut s = self.state.lock().unwrap();
        let (upid, request) = s.pending.pop_front().ok_or(PveWriteError::Rejected)?;
        let result = validate(&s, &request).and_then(|()| apply(&mut s, &request));
        s.tasks.insert(
            upid,
            if result.is_ok() {
                TaskState::CompleteSuccess
            } else {
                TaskState::CompleteFailure
            },
        );
        result
    }
    fn submit(&self, request: NativeMutationRequest) -> Result<MutationReceipt, PveWriteError> {
        let mut s = self.state.lock().unwrap();
        s.requests.push(request.clone());
        validate(&s, &request)?;
        let outcome = s
            .outcomes
            .entry(request.step())
            .or_default()
            .pop_front()
            .unwrap_or(FakeMutationOutcome::Accepted);
        if let FakeMutationOutcome::Rejected(error) = outcome {
            return Err(error);
        }
        if request.step() == NativeStep::Configure {
            apply(&mut s, &request)?;
            return if outcome == FakeMutationOutcome::AppliedResponseLost {
                Err(PveWriteError::OutcomeUnknown)
            } else {
                Ok(MutationReceipt::SynchronousAccepted)
            };
        }
        s.sequence = s.sequence.checked_add(1).ok_or(PveWriteError::Rejected)?;
        // Explicit synthetic contract: qmclone identifies the SOURCE VMID.
        let (worker, id) = if request.step() == NativeStep::Clone {
            ("qmclone", request.vm().source_vmid())
        } else {
            ("qmstart", request.vm().target_vmid())
        };
        let upid = Upid::parse(format!(
            "UPID:{}:{:08X}:00000001:00000001:{worker}:{id}:fake@pve:",
            request.vm().node(),
            s.sequence
        ))
        .map_err(|_| PveWriteError::Rejected)?;
        match outcome {
            FakeMutationOutcome::AcceptedTaskFails => {
                s.tasks.insert(upid.clone(), TaskState::CompleteFailure);
            }
            FakeMutationOutcome::AcceptedTaskDelayed => {
                s.tasks.insert(upid.clone(), TaskState::Running);
                s.pending.push_back((upid.clone(), request));
            }
            _ => {
                apply(&mut s, &request)?;
                s.tasks.insert(upid.clone(), TaskState::CompleteSuccess);
            }
        }
        if outcome == FakeMutationOutcome::AppliedResponseLost {
            Err(PveWriteError::OutcomeUnknown)
        } else {
            Ok(MutationReceipt::Task(upid))
        }
    }
}
fn lookup<'a>(s: &'a State, node: &NodeName, vmid: Vmid) -> Result<&'a FakeVm, PveReadError> {
    s.vms
        .get(&vmid)
        .filter(|vm| vm.config.node() == node)
        .ok_or(PveReadError::NotFound)
}
fn validate(s: &State, request: &NativeMutationRequest) -> Result<(), PveWriteError> {
    let p = request.vm();
    match request {
        NativeMutationRequest::Clone(_) => {
            if s.vms.contains_key(&p.target_vmid()) {
                return Err(PveWriteError::Conflict);
            }
            let source =
                lookup(s, p.node(), p.source_vmid()).map_err(|_| PveWriteError::Rejected)?;
            if source.power != PowerState::Stopped
                || !source.config.is_template()
                || source.config.locked()
                || !source.config.unsupported().is_empty()
            {
                return Err(PveWriteError::Rejected);
            }
        }
        NativeMutationRequest::Configure(r) => validate_expected(s, p, r.expected())?,
        NativeMutationRequest::Start(r) => {
            validate_expected(s, p, r.expected())?;
            let current = &lookup(s, p.node(), p.target_vmid())
                .map_err(|_| PveWriteError::Conflict)?
                .config;
            if !crate::native::final_fields(p, current) {
                return Err(PveWriteError::Conflict);
            }
        }
    }
    Ok(())
}
fn validate_expected(
    s: &State,
    p: &NativeVmPlan,
    expected: &NativeVmConfig,
) -> Result<(), PveWriteError> {
    let vm = lookup(s, p.node(), p.target_vmid()).map_err(|_| PveWriteError::Conflict)?;
    let c = &vm.config;
    // Independently reject changes between request construction and submission.
    if vm.power != PowerState::Stopped
        || c.locked()
        || c.is_template()
        || !c.unsupported().is_empty()
        || c.digest() != expected.digest()
        || c.uuid() != expected.uuid()
        || c.mac() != expected.mac()
        || c.boot_disk() != expected.boot_disk()
        || c.name() != expected.name()
        || c.fake_clone_provenance() != expected.fake_clone_provenance()
    {
        return Err(PveWriteError::Conflict);
    }
    Ok(())
}
fn wire(c: &NativeVmConfig) -> serde_json::Value {
    json!({"digest":c.digest(),"name":c.name(),"cores":c.cores(),"memory":c.memory_mib(),
        "scsi0":format!("{}:{}",c.boot_disk().storage(),c.boot_disk().volume()),
        "smbios1":format!("uuid={}",c.uuid()),"net0":format!("virtio={},bridge={},firewall=0",c.mac(),c.bridge()),
        "agent":u8::from(c.agent_enabled()),"boot":"order=scsi0","template":u8::from(c.is_template())})
}
fn apply(s: &mut State, request: &NativeMutationRequest) -> Result<(), PveWriteError> {
    let p = request.vm();
    match request {
        NativeMutationRequest::Clone(r) => {
            let source = &lookup(s, p.node(), p.source_vmid())
                .map_err(|_| PveWriteError::Rejected)?
                .config;
            let mut data = wire(source);
            let fresh_uuid = uuid::Uuid::now_v7();
            let bytes = fresh_uuid.as_bytes();
            data["digest"] = json!(format!("clone-{}", r.request_marker()));
            data["name"] = json!(p.name());
            data["template"] = json!(0);
            data["scsi0"] = json!(format!("{}:vm-{}-disk-0", p.storage(), p.target_vmid()));
            data["smbios1"] = json!(format!("uuid={fresh_uuid}"));
            data["net0"] = json!(format!(
                "virtio=02:{:02X}:{:02X}:{:02X}:{:02X}:{:02X},bridge={},firewall=0",
                bytes[11],
                bytes[12],
                bytes[13],
                bytes[14],
                bytes[15],
                source.bridge()
            ));
            let config =
                NativeVmConfig::from_wire(p.node().clone(), p.target_vmid(), data, Utc::now())
                    .map_err(|_| PveWriteError::Rejected)?
                    .with_fake_provenance(FakeCloneProvenance::from_request(r));
            s.vms.insert(
                p.target_vmid(),
                FakeVm {
                    config,
                    power: PowerState::Stopped,
                },
            );
        }
        NativeMutationRequest::Configure(r) => {
            let vm = s
                .vms
                .get_mut(&p.target_vmid())
                .ok_or(PveWriteError::Conflict)?;
            let mut data = wire(&vm.config);
            for (key, value) in r.form() {
                if key != "digest" {
                    data[key] = json!(value);
                }
            }
            data["cores"] = json!(p.cores());
            data["memory"] = json!(p.memory_mib());
            data["digest"] = json!(format!("config-{}", uuid::Uuid::now_v7()));
            let provenance = vm
                .config
                .fake_clone_provenance()
                .cloned()
                .ok_or(PveWriteError::Conflict)?;
            vm.config =
                NativeVmConfig::from_wire(p.node().clone(), p.target_vmid(), data, Utc::now())
                    .map_err(|_| PveWriteError::Rejected)?
                    .with_fake_provenance(provenance);
        }
        NativeMutationRequest::Start(_) => {
            s.vms
                .get_mut(&p.target_vmid())
                .ok_or(PveWriteError::Conflict)?
                .power = PowerState::Running;
        }
    }
    Ok(())
}
impl crate::native::sealed::FakeMutationCapability for NativeFakePve {}
#[async_trait]
impl PveMutationPort for NativeFakePve {
    async fn clone_vm(&self, r: &CloneRequest) -> Result<MutationReceipt, PveWriteError> {
        self.submit(NativeMutationRequest::Clone(r.clone()))
    }
    async fn configure_vm(&self, r: &ConfigureRequest) -> Result<MutationReceipt, PveWriteError> {
        self.submit(NativeMutationRequest::Configure(r.clone()))
    }
    async fn start_vm(&self, r: &StartRequest) -> Result<MutationReceipt, PveWriteError> {
        self.submit(NativeMutationRequest::Start(r.clone()))
    }
}
#[async_trait]
impl PvePreflightReadPort for NativeFakePve {
    async fn node_status(&self, node: &NodeName) -> Result<NodeStatus, PveReadError> {
        self.state
            .lock()
            .unwrap()
            .nodes
            .get(node)
            .cloned()
            .ok_or(PveReadError::NotFound)
    }
    async fn storage_status(
        &self,
        node: &NodeName,
        storage: &StorageName,
    ) -> Result<StorageStatus, PveReadError> {
        self.state
            .lock()
            .unwrap()
            .storage
            .get(&(node.clone(), storage.clone()))
            .cloned()
            .ok_or(PveReadError::NotFound)
    }
    async fn bridges(&self, node: &NodeName) -> Result<BridgeInventory, PveReadError> {
        self.state
            .lock()
            .unwrap()
            .bridges
            .get(node)
            .cloned()
            .ok_or(PveReadError::NotFound)
    }
    async fn cluster_vms(&self) -> Result<ClusterVmInventory, PveReadError> {
        let s = self.state.lock().unwrap();
        ClusterVmInventory::from_wire(json!(s.vms.values().map(|vm|json!({"vmid":vm.config.vmid(),"node":vm.config.node(),"name":vm.config.name(),"type":"qemu","template":u8::from(vm.config.is_template()),"status":vm.power})).collect::<Vec<_>>()),Utc::now())
    }
    async fn native_vm_config(
        &self,
        node: &NodeName,
        vmid: Vmid,
    ) -> Result<NativeVmConfig, PveReadError> {
        let s = self.state.lock().unwrap();
        let c = &lookup(&s, node, vmid)?.config;
        // Reload sanitized facts to refresh time without weakening unsupported/lock facts.
        let mut value = serde_json::to_value(c).map_err(|_| PveReadError::InvalidResponse)?;
        value["observed_at"] = json!(Utc::now());
        serde_json::from_value(value).map_err(|_| PveReadError::InvalidResponse)
    }
    async fn vm_status(&self, node: &NodeName, vmid: Vmid) -> Result<VmPowerStatus, PveReadError> {
        let s = self.state.lock().unwrap();
        let vm = lookup(&s, node, vmid)?;
        VmPowerStatus::from_wire(
            node.clone(),
            vmid,
            json!({"vmid":vmid,"status":vm.power,"locked":u8::from(vm.config.locked())}),
            Utc::now(),
        )
    }
}
#[async_trait]
impl PveReadPort for NativeFakePve {
    async fn vm_config(&self, node: &NodeName, vmid: Vmid) -> Result<VmConfig, PveReadError> {
        let c = self.native_vm_config(node, vmid).await?;
        Ok(VmConfig::new(
            vmid,
            Some(c.uuid()),
            [c.mac().clone()].into_iter().collect(),
            c.observed_at(),
        ))
    }
    async fn task_status(&self, node: &NodeName, upid: &Upid) -> Result<TaskStatus, PveReadError> {
        let s = self.state.lock().unwrap();
        if upid.node() != node {
            return Err(PveReadError::InvalidResponse);
        }
        Ok(TaskStatus::new(
            upid.clone(),
            *s.tasks.get(upid).ok_or(PveReadError::NotFound)?,
            Utc::now(),
        ))
    }
    async fn storage_content(
        &self,
        node: &NodeName,
        storage: &StorageName,
    ) -> Result<Vec<Volume>, PveReadError> {
        self.state
            .lock()
            .unwrap()
            .vms
            .values()
            .filter(|vm| vm.config.node() == node && vm.config.boot_disk().storage() == storage)
            .map(|vm| {
                Volume::new(
                    format!("{}:{}", storage, vm.config.boot_disk().volume()),
                    Utc::now(),
                )
                .map_err(|_| PveReadError::InvalidResponse)
            })
            .collect()
    }
    async fn qga_ping(&self, node: &NodeName, vmid: Vmid) -> Result<QgaStatus, PveReadError> {
        let s = self.state.lock().unwrap();
        lookup(&s, node, vmid)?;
        // Power and agent configuration do not prove a guest exists or is ready.
        Ok(QgaStatus::new(false, Utc::now()))
    }
}

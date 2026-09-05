//! Owned in-memory synthetic PVE. No URL, credentials, transport or processes.
use crate::*;
mod provisioning;
use async_trait::async_trait;
use chrono::Utc;
pub use provisioning::*;
use serde_json::json;
use std::{
    collections::{BTreeMap, VecDeque},
    sync::{Arc, Mutex},
};

/// Closed in-memory rendezvous. It cannot execute callbacks or select a destination.
#[derive(Default)]
pub struct FakePause {
    entered: tokio::sync::Notify,
    released: tokio::sync::Notify,
}
impl FakePause {
    pub async fn entered(&self) {
        self.entered.notified().await;
    }
    pub fn release(&self) {
        self.released.notify_one();
    }
    async fn wait(&self) {
        self.entered.notify_one();
        self.released.notified().await;
    }
}
pub enum FakeConfigRead {
    Error(PveReadError),
    Snapshot(Box<NativeVmConfig>),
    Pause(Arc<FakePause>),
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
pub enum FakeControllerCheckpoint {
    DispatchCommitted,
}

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
    config: FakeConfig,
    power: PowerState,
    incarnation: u64,
    qga_reachable: bool,
}
#[derive(Clone)]
enum FakeConfig {
    NativeV1(NativeVmConfig),
    ProvisioningV1(ProvisioningVmConfigV1),
}
impl FakeConfig {
    fn native(&self) -> Result<&NativeVmConfig, PveReadError> {
        match self {
            Self::NativeV1(c) => Ok(c),
            _ => Err(PveReadError::InvalidResponse),
        }
    }
    fn provisioning(&self) -> Result<&ProvisioningVmConfigV1, PveReadError> {
        match self {
            Self::ProvisioningV1(c) => Ok(c),
            _ => Err(PveReadError::InvalidResponse),
        }
    }
    fn identity(&self) -> Result<ProvisioningIdentitySnapshotV1, PveReadError> {
        match self {
            Self::NativeV1(c) => {
                ProvisioningIdentitySnapshotV1::from_native(c, NativeEvidenceSource::FakePve)
                    .map_err(|_| PveReadError::InvalidResponse)
            }
            Self::ProvisioningV1(c) => Ok(ProvisioningIdentitySnapshotV1::from_provisioning(c)),
        }
    }
    fn node(&self) -> &NodeName {
        match self {
            Self::NativeV1(c) => c.node(),
            Self::ProvisioningV1(c) => c.node(),
        }
    }
    fn vmid(&self) -> Vmid {
        match self {
            Self::NativeV1(c) => c.vmid(),
            Self::ProvisioningV1(c) => c.vmid(),
        }
    }
    fn name(&self) -> &NativeVmName {
        match self {
            Self::NativeV1(c) => c.name(),
            Self::ProvisioningV1(c) => c.name(),
        }
    }
    fn is_template(&self) -> bool {
        match self {
            Self::NativeV1(c) => c.is_template(),
            Self::ProvisioningV1(c) => c.is_template(),
        }
    }
    fn locked(&self) -> bool {
        match self {
            Self::NativeV1(c) => c.locked(),
            Self::ProvisioningV1(c) => c.locked(),
        }
    }
    fn storage(&self) -> &StorageName {
        match self {
            Self::NativeV1(c) => c.boot_disk().storage(),
            Self::ProvisioningV1(c) => c.primary_disk().storage(),
        }
    }
    fn volume(&self) -> &str {
        match self {
            Self::NativeV1(c) => c.boot_disk().volume(),
            Self::ProvisioningV1(c) => c.primary_disk().volume(),
        }
    }
}
enum PendingMutation {
    NativeV1 {
        upid: Upid,
        request: Box<NativeMutationRequest>,
    },
    ProvisioningV1 {
        upid: Upid,
        request: Box<ProvisioningMutationRequestV1>,
        source_incarnation: Option<u64>,
        target_incarnation: Option<u64>,
    },
}
impl PendingMutation {
    fn upid(&self) -> &Upid {
        match self {
            Self::NativeV1 { upid, .. } | Self::ProvisioningV1 { upid, .. } => upid,
        }
    }
    fn resources(&self) -> Vec<Vmid> {
        match self {
            Self::NativeV1 { request, .. } => {
                provisioning::resources(request.vm(), request.step() == NativeStep::Clone)
            }
            Self::ProvisioningV1 { request, .. } => provisioning::resources(
                request.plan().expected().vm(),
                request.plan().action() == ProvisioningActionV1::Clone,
            ),
        }
    }
}
#[derive(Default)]
struct State {
    checkpoints: BTreeMap<FakeControllerCheckpoint, VecDeque<Arc<FakePause>>>,
    config_reads: BTreeMap<Vmid, VecDeque<FakeConfigRead>>,
    submissions: VecDeque<Arc<FakePause>>,
    vms: BTreeMap<Vmid, FakeVm>,
    nodes: BTreeMap<NodeName, NodeStatus>,
    storage: BTreeMap<(NodeName, StorageName), StorageStatus>,
    bridges: BTreeMap<NodeName, BridgeInventory>,
    outcomes: BTreeMap<NativeStep, VecDeque<FakeMutationOutcome>>,
    requests: Vec<NativeMutationRequest>,
    tasks: BTreeMap<Upid, TaskState>,
    pending: VecDeque<PendingMutation>,
    sequence: u32,
    incarnation: u64,
    provisioning: provisioning::Controls,
}
impl State {
    fn next_incarnation(&mut self) -> Result<u64, PveWriteError> {
        self.incarnation = self
            .incarnation
            .checked_add(1)
            .ok_or(PveWriteError::Rejected)?;
        Ok(self.incarnation)
    }
}
#[derive(Clone, Default)]
pub struct NativeFakePve {
    state: Arc<Mutex<State>>,
}
impl NativeFakePve {
    pub fn pause_controller_checkpoint(&self, point: FakeControllerCheckpoint) -> Arc<FakePause> {
        let gate = Arc::new(FakePause::default());
        self.state
            .lock()
            .unwrap()
            .checkpoints
            .entry(point)
            .or_default()
            .push_back(gate.clone());
        gate
    }
    pub async fn controller_checkpoint(&self, point: FakeControllerCheckpoint) {
        let gate = self
            .state
            .lock()
            .unwrap()
            .checkpoints
            .get_mut(&point)
            .and_then(VecDeque::pop_front);
        if let Some(gate) = gate {
            gate.wait().await;
        }
    }
    pub fn enqueue_config_read(&self, vmid: Vmid, read: FakeConfigRead) {
        self.state
            .lock()
            .unwrap()
            .config_reads
            .entry(vmid)
            .or_default()
            .push_back(read);
    }
    pub fn pause_next_submission(&self) -> Arc<FakePause> {
        let gate = Arc::new(FakePause::default());
        self.state
            .lock()
            .unwrap()
            .submissions
            .push_back(gate.clone());
        gate
    }
    pub fn pause_next_config_read(&self, vmid: Vmid) -> Arc<FakePause> {
        let gate = Arc::new(FakePause::default());
        self.enqueue_config_read(vmid, FakeConfigRead::Pause(gate.clone()));
        gate
    }
    async fn submission_pause(&self) {
        let gate = self.state.lock().unwrap().submissions.pop_front();
        if let Some(gate) = gate {
            gate.wait().await;
        }
    }
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
        let incarnation = s.next_incarnation()?;
        s.vms.insert(
            config.vmid(),
            FakeVm {
                config: FakeConfig::NativeV1(config),
                power,
                incarnation,
                qga_reachable: false,
            },
        );
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
        let incarnation = s.next_incarnation()?;
        s.vms.insert(
            config.vmid(),
            FakeVm {
                config: FakeConfig::NativeV1(config),
                power,
                incarnation,
                qga_reachable: false,
            },
        );
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
        let index = s
            .pending
            .iter()
            .position(|p| matches!(p, PendingMutation::NativeV1 { .. }))
            .ok_or(PveWriteError::Rejected)?;
        let Some(PendingMutation::NativeV1 { upid, request }) = s.pending.remove(index) else {
            return Err(PveWriteError::Rejected);
        };
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
                s.pending.push_back(PendingMutation::NativeV1 {
                    upid: upid.clone(),
                    request: Box::new(request),
                });
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
    let resources = provisioning::resources(p, request.step() == NativeStep::Clone);
    if s.pending.iter().any(|pending| {
        matches!(pending, PendingMutation::ProvisioningV1 { .. })
            && pending.resources().iter().any(|id| resources.contains(id))
    }) {
        return Err(PveWriteError::Conflict);
    }
    match request {
        NativeMutationRequest::Clone(_) => {
            if s.vms.contains_key(&p.target_vmid()) {
                return Err(PveWriteError::Conflict);
            }
            let source =
                lookup(s, p.node(), p.source_vmid()).map_err(|_| PveWriteError::Rejected)?;
            let config = source
                .config
                .native()
                .map_err(|_| PveWriteError::Rejected)?;
            if source.power != PowerState::Stopped
                || !config.is_template()
                || config.locked()
                || !config.unsupported().is_empty()
            {
                return Err(PveWriteError::Rejected);
            }
        }
        NativeMutationRequest::Configure(r) => validate_expected(s, p, r.expected())?,
        NativeMutationRequest::Start(r) => {
            validate_expected(s, p, r.expected())?;
            let current = lookup(s, p.node(), p.target_vmid())
                .map_err(|_| PveWriteError::Conflict)?
                .config
                .native()
                .map_err(|_| PveWriteError::Conflict)?;
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
    let c = vm.config.native().map_err(|_| PveWriteError::Conflict)?;
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
            let source = lookup(s, p.node(), p.source_vmid())
                .map_err(|_| PveWriteError::Rejected)?
                .config
                .native()
                .map_err(|_| PveWriteError::Rejected)?;
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
            let incarnation = s.next_incarnation()?;
            s.vms.insert(
                p.target_vmid(),
                FakeVm {
                    config: FakeConfig::NativeV1(config),
                    power: PowerState::Stopped,
                    incarnation,
                    qga_reachable: false,
                },
            );
        }
        NativeMutationRequest::Configure(r) => {
            let vm = s
                .vms
                .get_mut(&p.target_vmid())
                .ok_or(PveWriteError::Conflict)?;
            let current = vm.config.native().map_err(|_| PveWriteError::Conflict)?;
            let mut data = wire(current);
            for (key, value) in r.form() {
                if key != "digest" {
                    data[key] = json!(value);
                }
            }
            data["cores"] = json!(p.cores());
            data["memory"] = json!(p.memory_mib());
            data["digest"] = json!(format!("config-{}", uuid::Uuid::now_v7()));
            let provenance = current
                .fake_clone_provenance()
                .cloned()
                .ok_or(PveWriteError::Conflict)?;
            vm.config = FakeConfig::NativeV1(
                NativeVmConfig::from_wire(p.node().clone(), p.target_vmid(), data, Utc::now())
                    .map_err(|_| PveWriteError::Rejected)?
                    .with_fake_provenance(provenance),
            );
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
        self.submission_pause().await;
        self.submit(NativeMutationRequest::Clone(r.clone()))
    }
    async fn configure_vm(&self, r: &ConfigureRequest) -> Result<MutationReceipt, PveWriteError> {
        self.submission_pause().await;
        self.submit(NativeMutationRequest::Configure(r.clone()))
    }
    async fn start_vm(&self, r: &StartRequest) -> Result<MutationReceipt, PveWriteError> {
        self.submission_pause().await;
        self.submit(NativeMutationRequest::Start(r.clone()))
    }
}
#[async_trait]
impl PvePreflightReadPort for NativeFakePve {
    async fn node_status(&self, node: &NodeName) -> Result<NodeStatus, PveReadError> {
        let state = self.state.lock().unwrap();
        refresh_observation(state.nodes.get(node).ok_or(PveReadError::NotFound)?)
    }
    async fn storage_status(
        &self,
        node: &NodeName,
        storage: &StorageName,
    ) -> Result<StorageStatus, PveReadError> {
        let state = self.state.lock().unwrap();
        refresh_observation(
            state
                .storage
                .get(&(node.clone(), storage.clone()))
                .ok_or(PveReadError::NotFound)?,
        )
    }
    async fn bridges(&self, node: &NodeName) -> Result<BridgeInventory, PveReadError> {
        let state = self.state.lock().unwrap();
        refresh_observation(state.bridges.get(node).ok_or(PveReadError::NotFound)?)
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
        let scripted = self
            .state
            .lock()
            .unwrap()
            .config_reads
            .get_mut(&vmid)
            .and_then(VecDeque::pop_front);
        match scripted {
            Some(FakeConfigRead::Error(error)) => return Err(error),
            Some(FakeConfigRead::Snapshot(config)) => {
                return if config.node() == node && config.vmid() == vmid {
                    Ok(*config)
                } else {
                    Err(PveReadError::InvalidResponse)
                };
            }
            Some(FakeConfigRead::Pause(gate)) => gate.wait().await,
            None => {}
        }
        let s = self.state.lock().unwrap();
        let c = lookup(&s, node, vmid)?.config.native()?;
        refresh_observation(c)
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

// Normal reads observe current fake state; explicit queued snapshots bypass this
// helper so stale-evidence fault injection retains its original timestamp.
fn refresh_observation<T: serde::Serialize + serde::de::DeserializeOwned>(
    snapshot: &T,
) -> Result<T, PveReadError> {
    let mut value = serde_json::to_value(snapshot).map_err(|_| PveReadError::InvalidResponse)?;
    value["observed_at"] = json!(Utc::now());
    serde_json::from_value(value).map_err(|_| PveReadError::InvalidResponse)
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
            .filter(|vm| vm.config.node() == node && vm.config.storage() == storage)
            .map(|vm| {
                Volume::new(format!("{}:{}", storage, vm.config.volume()), Utc::now())
                    .map_err(|_| PveReadError::InvalidResponse)
            })
            .collect()
    }
    async fn qga_ping(&self, node: &NodeName, vmid: Vmid) -> Result<QgaStatus, PveReadError> {
        let s = self.state.lock().unwrap();
        let vm = lookup(&s, node, vmid)?;
        // Power and agent configuration do not prove a guest exists or is ready.
        Ok(QgaStatus::new(vm.qga_reachable, Utc::now()))
    }
}

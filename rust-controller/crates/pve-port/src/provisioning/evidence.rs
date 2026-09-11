use super::*;
use crate::{
    BridgeInventory, ClusterVmInventory, MutationReceipt, NativeRead, NativeVmConfig, NodeStatus,
    StorageStatus, TaskStatus, VmPowerStatus,
};
use controller_domain::{AttemptId, EventId, OperationId, RunId};
mod identity;
mod validation;
pub(super) mod wire;
pub use identity::*;

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ProvisioningBindingV1 {
    contract_version: u16,
    run_id: RunId,
    operation_id: OperationId,
    attempt_id: AttemptId,
    workflow_sha256: String,
    operation_plan_sha256: String,
    evidence_fence: u64,
}
impl ProvisioningBindingV1 {
    pub fn new(
        run_id: RunId,
        operation_id: OperationId,
        attempt_id: AttemptId,
        workflow_sha256: &str,
        plan: &ProvisioningOperationPlanV1,
        evidence_fence: u64,
    ) -> Result<Self, InvalidProvisioning> {
        if !hash_valid(workflow_sha256) {
            return Err(InvalidProvisioning);
        }
        Ok(Self {
            contract_version: 1,
            run_id,
            operation_id,
            attempt_id,
            workflow_sha256: workflow_sha256.to_ascii_lowercase(),
            operation_plan_sha256: plan.fingerprint()?,
            evidence_fence,
        })
    }
    pub fn contract_version(&self) -> u16 {
        self.contract_version
    }
    pub fn run_id(&self) -> RunId {
        self.run_id
    }
    pub fn operation_id(&self) -> OperationId {
        self.operation_id
    }
    pub fn attempt_id(&self) -> AttemptId {
        self.attempt_id
    }
    pub fn workflow_sha256(&self) -> &str {
        &self.workflow_sha256
    }
    pub fn operation_plan_sha256(&self) -> &str {
        &self.operation_plan_sha256
    }
    pub fn evidence_fence(&self) -> u64 {
        self.evidence_fence
    }
    pub fn same_operation_attempt(&self, other: &Self) -> bool {
        self.contract_version == other.contract_version
            && self.run_id == other.run_id
            && self.operation_id == other.operation_id
            && self.attempt_id == other.attempt_id
            && self.workflow_sha256 == other.workflow_sha256
            && self.operation_plan_sha256 == other.operation_plan_sha256
    }
    pub(super) fn validates(&self, plan: &ProvisioningOperationPlanV1) -> bool {
        plan.fingerprint()
            .is_ok_and(|h| h == self.operation_plan_sha256)
    }
    pub(super) fn same_workflow(&self, other: &Self) -> bool {
        self.run_id == other.run_id && self.workflow_sha256 == other.workflow_sha256
    }
}

pub(super) fn hash_valid(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit())
}

pub struct ProvisioningDispatchInputV1 {
    pub request: ProvisioningMutationRequestV1,
    pub source: NativeEvidenceSource,
    pub preflight_event_id: EventId,
    pub original_generation: i64,
    pub dispatch_revision: u64,
    pub dispatched_at: DateTime<Utc>,
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ProvisioningDispatchV1 {
    request: ProvisioningMutationRequestV1,
    source: NativeEvidenceSource,
    preflight_event_id: EventId,
    original_generation: i64,
    dispatch_revision: u64,
    dispatched_at: DateTime<Utc>,
    request_sha256: String,
}
impl ProvisioningDispatchV1 {
    pub fn new(i: ProvisioningDispatchInputV1) -> Result<Self, InvalidProvisioning> {
        let before = i.request.expected_before();
        if i.original_generation <= 0
            || i.request
                .binding()
                .evidence_fence()
                .checked_add(1)
                .is_none_or(|v| i.dispatch_revision <= v)
            || i.dispatched_at < before.config().observed_at()
            || i.dispatched_at < before.power().observed_at()
            || i.source != NativeEvidenceSource::FakePve
            || before.config().source() != i.source
        {
            return Err(InvalidProvisioning);
        }
        let request_sha256 = i.request.request_digest()?;
        Ok(Self {
            request: i.request,
            source: i.source,
            preflight_event_id: i.preflight_event_id,
            original_generation: i.original_generation,
            dispatch_revision: i.dispatch_revision,
            dispatched_at: i.dispatched_at,
            request_sha256,
        })
    }
    pub fn request(&self) -> &ProvisioningMutationRequestV1 {
        &self.request
    }
    pub fn source(&self) -> NativeEvidenceSource {
        self.source
    }
    pub fn preflight_event_id(&self) -> EventId {
        self.preflight_event_id
    }
    pub fn original_generation(&self) -> i64 {
        self.original_generation
    }
    pub fn dispatch_revision(&self) -> u64 {
        self.dispatch_revision
    }
    pub fn dispatched_at(&self) -> DateTime<Utc> {
        self.dispatched_at
    }
    pub fn request_sha256(&self) -> &str {
        &self.request_sha256
    }
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ProvisioningReceiptV1 {
    dispatch: ProvisioningDispatchV1,
    accepted_at: DateTime<Utc>,
    receipt: MutationReceipt,
}
impl ProvisioningReceiptV1 {
    pub fn new(
        dispatch: ProvisioningDispatchV1,
        accepted_at: DateTime<Utc>,
        receipt: MutationReceipt,
    ) -> Result<Self, InvalidProvisioning> {
        if accepted_at < dispatch.dispatched_at
            || !provisioning_receipt_matches(dispatch.request.plan(), &receipt)
        {
            return Err(InvalidProvisioning);
        }
        Ok(Self {
            dispatch,
            accepted_at,
            receipt,
        })
    }
    pub fn dispatch(&self) -> &ProvisioningDispatchV1 {
        &self.dispatch
    }
    pub fn accepted_at(&self) -> DateTime<Utc> {
        self.accepted_at
    }
    pub fn receipt(&self) -> &MutationReceipt {
        &self.receipt
    }
}
pub(crate) fn provisioning_receipt_matches(
    plan: &ProvisioningOperationPlanV1,
    receipt: &MutationReceipt,
) -> bool {
    use ProvisioningActionV1::*;
    match (plan.action(), receipt) {
        (ConfigurePe | ConfigureDisk, MutationReceipt::SynchronousAccepted) => true,
        (
            Clone | EnsureCapacity | StartPe | StartDisk | EnsureStopped,
            MutationReceipt::Task(upid),
        ) => {
            let (worker, id) = match plan.action() {
                Clone => ("qmclone", plan.expected().vm().source_vmid()),
                EnsureCapacity => ("resize", plan.expected().vm().target_vmid()),
                EnsureStopped => ("qmstop", plan.expected().vm().target_vmid()),
                _ => ("qmstart", plan.expected().vm().target_vmid()),
            };
            upid.node() == plan.expected().vm().node()
                && upid.worker_type() == worker
                && upid.worker_id() == Some(id.to_string().as_str())
        }
        _ => false,
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ProvisioningEvidenceInputV1 {
    pub binding: ProvisioningBindingV1,
    pub plan: ProvisioningOperationPlanV1,
    pub source: NativeEvidenceSource,
    pub collected_at: DateTime<Utc>,
    pub node: Option<NativeRead<NodeStatus>>,
    pub storage: Option<NativeRead<StorageStatus>>,
    pub bridges: Option<NativeRead<BridgeInventory>>,
    pub inventory: Option<NativeRead<ClusterVmInventory>>,
    pub inventory_coverage: ProvisioningCoverageV1,
    pub identities: Vec<ProvisioningIdentityReadV1>,
    pub source_config: Option<NativeRead<ProvisioningVmConfigV1>>,
    pub source_power: Option<NativeRead<VmPowerStatus>>,
    pub target_config: Option<NativeRead<ProvisioningVmConfigV1>>,
    pub target_power: Option<NativeRead<VmPowerStatus>>,
    pub media: Vec<NativeRead<ProvisioningMediaInventoryV1>>,
    pub qga: Option<NativeRead<ProvisioningQgaObservationV1>>,
    pub task: Option<NativeRead<TaskStatus>>,
    pub receipt: Option<ProvisioningReceiptV1>,
}
/// A collection contains facts only and cannot grant a send capability.
/// ```compile_fail
/// use pve_port::{ProvisioningEvidenceV1, ProvisioningEvidenceInputV1};
/// fn unchecked(input: ProvisioningEvidenceInputV1) { let _ = ProvisioningEvidenceV1(input); }
/// ```
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
#[serde(transparent)]
pub struct ProvisioningEvidenceV1(ProvisioningEvidenceInputV1);
impl ProvisioningEvidenceV1 {
    pub fn new(input: ProvisioningEvidenceInputV1) -> Result<Self, InvalidProvisioning> {
        validation::validate(&input)?;
        Ok(Self(input))
    }
    pub fn facts(&self) -> &ProvisioningEvidenceInputV1 {
        &self.0
    }
}

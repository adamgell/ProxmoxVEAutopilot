//! Sanitized facts, not dispatch capabilities. Store code must obtain context
//! from locked rows and bind each collection to its persisted evidence event.
use super::*;
use crate::{
    BridgeInventory, ClusterVmInventory, NodeStatus, PveReadError, StorageStatus, TaskStatus,
    VmPowerStatus,
};
use controller_domain::{AttemptId, ExecutionState, RunId};

#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
#[error("invalid native evidence")]
pub struct InvalidNativeEvidence;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NativeEvidenceSource {
    PveApi,
    FakePve,
}

/// `evidence_fence` is the journal revision immediately before this evidence
/// event's FIRST append, not the current aggregate revision. On idempotent
/// reload the store derives it from the actual persisted event revision - 1.
/// Current revision CAS and intervening decision/attempt invalidation remain
/// separate store checks; this pure value does not authorize reconciliation.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NativeBinding {
    run_id: RunId,
    operation_id: OperationId,
    attempt_id: AttemptId,
    plan_digest: String,
    evidence_fence: u64,
}
impl NativeBinding {
    pub fn new(
        run_id: RunId,
        operation_id: OperationId,
        attempt_id: AttemptId,
        plan: &NativeOperationPlan,
        evidence_fence: u64,
    ) -> Self {
        Self {
            run_id,
            operation_id,
            attempt_id,
            plan_digest: canonical_digest(plan),
            evidence_fence,
        }
    }
    pub const fn run_id(&self) -> RunId {
        self.run_id
    }
    pub const fn operation_id(&self) -> OperationId {
        self.operation_id
    }
    pub const fn attempt_id(&self) -> AttemptId {
        self.attempt_id
    }
    pub const fn evidence_fence(&self) -> u64 {
        self.evidence_fence
    }
    pub fn plan_digest(&self) -> &str {
        &self.plan_digest
    }
    pub(super) fn same_attempt(&self, other: &Self) -> bool {
        self.run_id == other.run_id
            && self.operation_id == other.operation_id
            && self.attempt_id == other.attempt_id
            && self.plan_digest == other.plan_digest
    }
}

/// The collection timestamp dates errors too, including authoritative 404s.
/// A successful snapshot's own timestamp is checked independently.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NativeRead<T> {
    pub observed_at: DateTime<Utc>,
    pub result: Result<T, PveReadError>,
}
impl<T> NativeRead<T> {
    pub const fn new(observed_at: DateTime<Utc>, result: Result<T, PveReadError>) -> Self {
        Self {
            observed_at,
            result,
        }
    }
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NativeIdentityRead {
    pub node: NodeName,
    pub vmid: Vmid,
    pub read: NativeRead<NativeVmConfig>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NativeReceipt {
    pub binding: NativeBinding,
    pub accepted_at: DateTime<Utc>,
    pub receipt: MutationReceipt,
}

/// Input fields remain convenient for collectors. Only `NativeEvidence::new`
/// or validated deserialization produces evaluable evidence.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NativeEvidenceInput {
    pub binding: NativeBinding,
    pub plan: NativeOperationPlan,
    pub source: NativeEvidenceSource,
    pub collected_at: DateTime<Utc>,
    pub evaluated_at: DateTime<Utc>,
    pub node: Option<NativeRead<NodeStatus>>,
    pub storage: Option<NativeRead<StorageStatus>>,
    pub bridges: Option<NativeRead<BridgeInventory>>,
    pub inventory: Option<NativeRead<ClusterVmInventory>>,
    pub identities: Vec<NativeIdentityRead>,
    pub source_config: Option<NativeRead<NativeVmConfig>>,
    pub source_power: Option<NativeRead<VmPowerStatus>>,
    pub target_config: Option<NativeRead<NativeVmConfig>>,
    pub target_power: Option<NativeRead<VmPowerStatus>>,
    pub task: Option<NativeRead<TaskStatus>>,
    pub receipt: Option<NativeReceipt>,
    pub bound_intermediate: Option<NativeVmConfig>,
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
#[serde(transparent)]
pub struct NativeEvidence(NativeEvidenceInput);
impl NativeEvidence {
    pub fn new(input: NativeEvidenceInput) -> Result<Self, InvalidNativeEvidence> {
        if input.binding.plan_digest != canonical_digest(&input.plan) {
            return Err(InvalidNativeEvidence);
        }
        if let Some(receipt) = &input.receipt {
            // A persisted receipt survives later evidence collections. Only
            // its attempt/plan identity is compared, not the evidence anchor.
            if !receipt.binding.same_attempt(&input.binding)
                || !receipt_matches(&receipt.receipt, &input.plan)
            {
                return Err(InvalidNativeEvidence);
            }
            if let Some(Ok(task)) = input.task.as_ref().map(|r| &r.result)
                && receipt.receipt != MutationReceipt::Task(task.upid().clone())
            {
                return Err(InvalidNativeEvidence);
            }
        }
        if let Some(Ok(task)) = input.task.as_ref().map(|r| &r.result)
            && !receipt_matches(&MutationReceipt::Task(task.upid().clone()), &input.plan)
        {
            return Err(InvalidNativeEvidence);
        }
        if input.source == NativeEvidenceSource::PveApi {
            let configs = input
                .identities
                .iter()
                .filter_map(|r| r.read.result.as_ref().ok())
                .chain(
                    input
                        .source_config
                        .iter()
                        .filter_map(|r| r.result.as_ref().ok()),
                )
                .chain(
                    input
                        .target_config
                        .iter()
                        .filter_map(|r| r.result.as_ref().ok()),
                )
                .chain(input.bound_intermediate.iter());
            if configs
                .into_iter()
                .any(|c| c.fake_clone_provenance().is_some())
            {
                return Err(InvalidNativeEvidence);
            }
        }
        Ok(Self(input))
    }
    pub const fn facts(&self) -> &NativeEvidenceInput {
        &self.0
    }
}
impl<'de> Deserialize<'de> for NativeEvidence {
    fn deserialize<D: de::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let input = NativeEvidenceInput::deserialize(d)
            .map_err(|_| de::Error::custom(InvalidNativeEvidence))?;
        Self::new(input).map_err(de::Error::custom)
    }
}
pub(super) fn receipt_matches(receipt: &MutationReceipt, plan: &NativeOperationPlan) -> bool {
    match (plan.step(), receipt) {
        (NativeStep::Configure, MutationReceipt::SynchronousAccepted) => true,
        (NativeStep::Clone | NativeStep::Start, MutationReceipt::Task(upid)) => {
            let (worker, id) = if plan.step() == NativeStep::Clone {
                ("qmclone", plan.vm().source_vmid())
            } else {
                ("qmstart", plan.vm().target_vmid())
            };
            upid.node() == plan.vm().node()
                && upid.worker_type() == worker
                && upid.worker_id() == Some(id.to_string().as_str())
        }
        _ => false,
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NativeEvaluationMode {
    Preflight,
    Outcome,
    Reconciliation,
}

/// This is deliberately NOT deserializable evidence. Task 4 must derive state,
/// cancellation, dispatch history, predecessor success and fence validity from
/// locked authoritative rows. `Ready` still cannot mint a send permit.
#[derive(Clone, Debug)]
pub struct NativeEvaluationContext {
    pub binding: NativeBinding,
    pub source: NativeEvidenceSource,
    pub state: ExecutionState,
    pub mode: NativeEvaluationMode,
    pub cancelled: bool,
    pub possible_dispatch: bool,
    pub mutation_deadline: Option<DateTime<Utc>>,
    pub transport_lost: bool,
    /// Reconstructed from persisted exact dispatch/attempt receipt facts.
    /// A recovered UPID must first pass that store binding, never be guessed.
    pub receipt: Option<NativeReceipt>,
    /// Original durable dispatch timestamp; required even if a synchronous
    /// configure response was lost and no receipt can be recovered.
    pub dispatched_at: Option<DateTime<Utc>>,
    pub configure_satisfied: bool,
    pub uninterrupted_workflow: bool,
    pub clone_request: CloneRequest,
    pub clone_ownership: Option<NativeCloneOwnership>,
}

/// Complete historical clone outcome proof, including inventory and unique
/// per-VM identity coverage. Store must load it from a satisfied predecessor.
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct NativeCloneOwnership {
    request: CloneRequest,
    proof: NativeEvidence,
    /// Preserve the original mutation boundary: some valid observations may
    /// precede receipt acceptance while still following dispatch.
    dispatched_at: DateTime<Utc>,
    bound_at: DateTime<Utc>,
}
impl NativeCloneOwnership {
    pub fn from_satisfied_clone(
        context: &NativeEvaluationContext,
        proof: NativeEvidence,
        bound_at: DateTime<Utc>,
    ) -> Result<Self, InvalidNativeEvidence> {
        if proof.facts().plan.step() != NativeStep::Clone
            || context.clone_ownership.is_some()
            || proof.facts().bound_intermediate.is_some()
            || evaluate_native_outcome(context, &proof, bound_at).decision
                != NativeDecision::Satisfied
        {
            return Err(InvalidNativeEvidence);
        }
        Ok(Self {
            request: context.clone_request.clone(),
            proof,
            dispatched_at: context.dispatched_at.ok_or(InvalidNativeEvidence)?,
            bound_at,
        })
    }
    pub const fn request(&self) -> &CloneRequest {
        &self.request
    }
    pub const fn proof(&self) -> &NativeEvidence {
        &self.proof
    }
    pub fn config(&self) -> &NativeVmConfig {
        self.proof
            .facts()
            .target_config
            .as_ref()
            .expect("satisfied proof contains target read")
            .result
            .as_ref()
            .expect("satisfied proof contains target config")
    }
}
impl<'de> Deserialize<'de> for NativeCloneOwnership {
    fn deserialize<D: de::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Wire {
            request: CloneRequest,
            proof: NativeEvidence,
            dispatched_at: DateTime<Utc>,
            bound_at: DateTime<Utc>,
        }
        let w = Wire::deserialize(d).map_err(|_| de::Error::custom(InvalidNativeEvidence))?;
        let context = NativeEvaluationContext {
            binding: w.proof.facts().binding.clone(),
            source: w.proof.facts().source,
            state: ExecutionState::Running,
            mutation_deadline: None,
            transport_lost: false,
            receipt: w.proof.facts().receipt.clone(),
            dispatched_at: Some(w.dispatched_at),
            mode: NativeEvaluationMode::Outcome,
            cancelled: false,
            possible_dispatch: true,
            configure_satisfied: false,
            uninterrupted_workflow: true,
            clone_request: w.request,
            clone_ownership: None,
        };
        Self::from_satisfied_clone(&context, w.proof, w.bound_at).map_err(de::Error::custom)
    }
}

//! Pure physical advice. Durable eligibility, lease/CAS checks, callbacks and
//! grace-timeout/force-stop authority belong to the existing store.
use super::*;
use crate::{
    CloneRequest, MutationReceipt, NativeDecision, NativeRead, PowerState, PveReadError, TaskState,
    VmPowerStatus,
};
use controller_domain::ExecutionState;
use requests::state as physical;
mod context;
mod observations;
mod outcome;
pub use context::*;
use observations::*;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ProvisioningReasonV1 {
    BindingMismatch,
    StateNotEligible,
    Cancelled,
    Unauthorized,
    ObservationMissing,
    ObservationUnavailable,
    ObservationNotFresh,
    UnsupportedLayout,
    UnsupportedInfrastructure,
    InsufficientCapacity,
    IncompleteIdentityCoverage,
    InventoryContradiction,
    IdentityCollision,
    TargetOccupied,
    TemplateMismatch,
    MediaMissing,
    MediaCoverageIncomplete,
    ProvenanceMissing,
    ProvenanceMismatch,
    OwnershipMissing,
    PredecessorMismatch,
    BoundIdentityChanged,
    BeforeStateChanged,
    CapacityMismatch,
    ProfileMismatch,
    VmNotStoppedUnlocked,
    DispatchAlreadyPossible,
    DispatchProvenanceMissing,
    ReceiptMissing,
    ReceiptMismatch,
    TaskRunning,
    TaskFailed,
    PostconditionPending,
    DeadlineExpired,
    PowerMismatch,
    CloneReady,
    CloneSatisfied,
    CapacityReady,
    CapacitySatisfied,
    ConfigurePeReady,
    ConfigurePeSatisfied,
    StartPeReady,
    StartPeSatisfied,
    StopReady,
    StopSatisfied,
    ConfigureDiskReady,
    ConfigureDiskSatisfied,
    StartDiskReady,
    StartDiskSatisfied,
    ObservedNoChange,
}
impl<'de> Deserialize<'de> for ProvisioningReasonV1 {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        // Decode only the canonical string form, including standalone use.
        let s = String::deserialize(d)?;
        const ALL: &[ProvisioningReasonV1] = &[
            ProvisioningReasonV1::BindingMismatch,
            ProvisioningReasonV1::StateNotEligible,
            ProvisioningReasonV1::Cancelled,
            ProvisioningReasonV1::Unauthorized,
            ProvisioningReasonV1::ObservationMissing,
            ProvisioningReasonV1::ObservationUnavailable,
            ProvisioningReasonV1::ObservationNotFresh,
            ProvisioningReasonV1::UnsupportedLayout,
            ProvisioningReasonV1::UnsupportedInfrastructure,
            ProvisioningReasonV1::InsufficientCapacity,
            ProvisioningReasonV1::IncompleteIdentityCoverage,
            ProvisioningReasonV1::InventoryContradiction,
            ProvisioningReasonV1::IdentityCollision,
            ProvisioningReasonV1::TargetOccupied,
            ProvisioningReasonV1::TemplateMismatch,
            ProvisioningReasonV1::MediaMissing,
            ProvisioningReasonV1::MediaCoverageIncomplete,
            ProvisioningReasonV1::ProvenanceMissing,
            ProvisioningReasonV1::ProvenanceMismatch,
            ProvisioningReasonV1::OwnershipMissing,
            ProvisioningReasonV1::PredecessorMismatch,
            ProvisioningReasonV1::BoundIdentityChanged,
            ProvisioningReasonV1::BeforeStateChanged,
            ProvisioningReasonV1::CapacityMismatch,
            ProvisioningReasonV1::ProfileMismatch,
            ProvisioningReasonV1::VmNotStoppedUnlocked,
            ProvisioningReasonV1::DispatchAlreadyPossible,
            ProvisioningReasonV1::DispatchProvenanceMissing,
            ProvisioningReasonV1::ReceiptMissing,
            ProvisioningReasonV1::ReceiptMismatch,
            ProvisioningReasonV1::TaskRunning,
            ProvisioningReasonV1::TaskFailed,
            ProvisioningReasonV1::PostconditionPending,
            ProvisioningReasonV1::DeadlineExpired,
            ProvisioningReasonV1::PowerMismatch,
            ProvisioningReasonV1::CloneReady,
            ProvisioningReasonV1::CloneSatisfied,
            ProvisioningReasonV1::CapacityReady,
            ProvisioningReasonV1::CapacitySatisfied,
            ProvisioningReasonV1::ConfigurePeReady,
            ProvisioningReasonV1::ConfigurePeSatisfied,
            ProvisioningReasonV1::StartPeReady,
            ProvisioningReasonV1::StartPeSatisfied,
            ProvisioningReasonV1::StopReady,
            ProvisioningReasonV1::StopSatisfied,
            ProvisioningReasonV1::ConfigureDiskReady,
            ProvisioningReasonV1::ConfigureDiskSatisfied,
            ProvisioningReasonV1::StartDiskReady,
            ProvisioningReasonV1::StartDiskSatisfied,
            ProvisioningReasonV1::ObservedNoChange,
        ];
        ALL.iter()
            .find(|v| serde_json::to_value(v).is_ok_and(|v| v == s))
            .copied()
            .ok_or_else(|| serde::de::Error::custom(InvalidProvisioning))
    }
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub struct ProvisioningEvaluationV1 {
    pub decision: NativeDecision,
    pub reason: ProvisioningReasonV1,
}
type Check<T> = Result<T, ProvisioningEvaluationV1>;
fn result(decision: NativeDecision, reason: ProvisioningReasonV1) -> ProvisioningEvaluationV1 {
    ProvisioningEvaluationV1 { decision, reason }
}
fn unknown(reason: ProvisioningReasonV1) -> ProvisioningEvaluationV1 {
    result(NativeDecision::Unknown, reason)
}
fn conflict(reason: ProvisioningReasonV1) -> ProvisioningEvaluationV1 {
    result(NativeDecision::Conflicted, reason)
}
fn blocked(reason: ProvisioningReasonV1) -> ProvisioningEvaluationV1 {
    result(NativeDecision::Blocked, reason)
}

pub fn evaluate_provisioning_preflight(
    context: &ProvisioningEvaluationContextV1,
    evidence: &ProvisioningEvidenceV1,
    as_of: DateTime<Utc>,
) -> ProvisioningEvaluationV1 {
    preflight(context, evidence, as_of).unwrap_or_else(|e| e)
}
pub fn evaluate_provisioning_outcome(
    context: &ProvisioningEvaluationContextV1,
    evidence: &ProvisioningEvidenceV1,
    as_of: DateTime<Utc>,
) -> ProvisioningEvaluationV1 {
    outcome::evaluate(context, evidence, as_of).unwrap_or_else(|e| e)
}
fn common<'a>(
    context: &'a ProvisioningEvaluationContextV1,
    evidence: &'a ProvisioningEvidenceV1,
    now: DateTime<Utc>,
    preflight: bool,
) -> Check<(
    &'a ProvisioningEvaluationContextInputV1,
    &'a ProvisioningEvidenceInputV1,
)> {
    use ProvisioningReasonV1::*;
    let c = context.facts();
    let e = evidence.facts();
    if c.binding != e.binding || c.plan != e.plan || c.source != e.source {
        return Err(conflict(BindingMismatch));
    }
    if let ProvisioningDispatchStateV1::Recorded { receipt, .. } = &c.dispatch {
        if receipt != &e.receipt {
            return Err(conflict(ReceiptMismatch));
        }
    } else if e.receipt.is_some() {
        return Err(conflict(ReceiptMismatch));
    }
    if c.cancelled {
        return Err(unknown(Cancelled));
    }
    if c.source != NativeEvidenceSource::FakePve {
        return Err(blocked(UnsupportedInfrastructure));
    }
    if preflight {
        if !matches!(c.dispatch, ProvisioningDispatchStateV1::NotDispatched) {
            return Err(unknown(DispatchAlreadyPossible));
        }
        if c.mode != ProvisioningEvaluationModeV1::Preflight
            || !matches!(c.state, ExecutionState::Leased | ExecutionState::Running)
        {
            return Err(unknown(StateNotEligible));
        }
    } else {
        if !matches!(c.dispatch, ProvisioningDispatchStateV1::Recorded { .. }) {
            return Err(unknown(DispatchProvenanceMissing));
        }
        if !matches!(
            (c.mode, c.state),
            (
                ProvisioningEvaluationModeV1::Outcome,
                ExecutionState::Running | ExecutionState::Waiting
            ) | (
                ProvisioningEvaluationModeV1::Reconciliation,
                ExecutionState::Unknown
            )
        ) {
            return Err(unknown(StateNotEligible));
        }
    }
    if unauthorized(e) {
        return Err(blocked(Unauthorized));
    }
    if now >= c.mutation_deadline {
        return Err(unknown(DeadlineExpired));
    }
    if !physical::fresh(e.collected_at, now, c.freshness_seconds) {
        return Err(unknown(ObservationNotFresh));
    }
    Ok((c, e))
}
fn preflight(
    context: &ProvisioningEvaluationContextV1,
    evidence: &ProvisioningEvidenceV1,
    now: DateTime<Utc>,
) -> Check<ProvisioningEvaluationV1> {
    use ProvisioningActionV1::*;
    use ProvisioningReasonV1::*;
    let (c, e) = common(context, evidence, now, true)?;
    infrastructure(c, e, now)?;
    media(c, e, now)?;
    if c.plan.action() == Clone {
        let source = read(
            &e.source_config,
            c,
            e,
            now,
            None,
            ProvisioningVmConfigV1::observed_at,
        )?;
        let power = read(&e.source_power, c, e, now, None, VmPowerStatus::observed_at)?;
        if !physical::template(source, power, &c.plan) {
            return Err(conflict(TemplateMismatch));
        }
        identities(c, e, now, None, false)?;
        let target = e
            .target_config
            .as_ref()
            .ok_or_else(|| unknown(ObservationMissing))?;
        clock(target.observed_at, target.observed_at, c, e, now, None)?;
        if target.result != Err(PveReadError::NotFound) {
            return Err(if target.result.is_ok() {
                conflict(TargetOccupied)
            } else {
                unknown(ObservationUnavailable)
            });
        }
        if c.plan.expected().disk_serial().len() > 20 {
            return Err(blocked(UnsupportedLayout));
        }
        return Ok(result(NativeDecision::Ready, CloneReady));
    }
    let target = read(
        &e.target_config,
        c,
        e,
        now,
        None,
        ProvisioningVmConfigV1::observed_at,
    )?;
    let power = read(&e.target_power, c, e, now, None, VmPowerStatus::observed_at)?;
    identities(c, e, now, None, true)?;
    let owner = c
        .clone_ownership
        .as_ref()
        .ok_or_else(|| unknown(OwnershipMissing))?;
    let prior = c
        .predecessor
        .as_ref()
        .ok_or_else(|| unknown(PredecessorMismatch))?;
    if !physical::stage_before(
        &c.plan,
        &c.clone_request,
        owner.config(),
        prior.config(),
        target,
    ) {
        return Err(conflict(BeforeStateChanged));
    }
    if !physical::power_valid(power) {
        return Err(conflict(VmNotStoppedUnlocked));
    }
    if c.plan.expected().disk_serial().len() > 20 {
        return Err(blocked(UnsupportedLayout));
    }
    if c.plan.action() == EnsureCapacity
        && target.primary_disk().capacity_bytes() == c.plan.expected().effective_capacity_bytes()
        && physical::stopped(power)
        || c.plan.action() == EnsureStopped && physical::stopped(power)
    {
        return Ok(result(NativeDecision::Satisfied, ObservedNoChange));
    }
    if c.plan.action() != EnsureStopped && !physical::stopped(power) {
        return Err(unknown(PowerMismatch));
    }
    let before = ProvisioningBeforeStateV1::new(target.clone(), power.clone())
        .map_err(|_| conflict(BeforeStateChanged))?;
    let input = ProvisioningOwnedRequestInputV1 {
        binding: c.binding.clone(),
        plan: c.plan.clone(),
        ownership: owner,
        predecessor: prior,
        expected_before: before,
        as_of: now,
        freshness_seconds: c.freshness_seconds,
    };
    let valid = match c.plan.action() {
        EnsureCapacity => GrowDiskRequestV1::new(input).is_ok(),
        ConfigurePe | ConfigureDisk => ConfigureProvisioningRequestV1::new(input).is_ok(),
        StartPe | StartDisk => StartProvisioningRequestV1::new(input).is_ok(),
        EnsureStopped => StopProvisioningRequestV1::new(input).is_ok(),
        Clone => false,
    };
    if !valid {
        return Err(conflict(BeforeStateChanged));
    }
    Ok(result(
        NativeDecision::Ready,
        match c.plan.action() {
            EnsureCapacity => CapacityReady,
            ConfigurePe => ConfigurePeReady,
            StartPe => StartPeReady,
            EnsureStopped => StopReady,
            ConfigureDisk => ConfigureDiskReady,
            StartDisk => StartDiskReady,
            Clone => unreachable!(),
        },
    ))
}

//! Deterministic, capability-free rules shared with the future store transaction.
use super::*;
use crate::{
    ClusterVmInventory, NativeVmConfig, PowerState, PveReadError, TaskState, VmPowerStatus,
};
use chrono::Duration;
use controller_domain::ExecutionState;
use std::collections::BTreeSet;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NativeDecision {
    Ready,
    Waiting,
    Satisfied,
    Failed,
    Blocked,
    Unknown,
    Conflicted,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NativeReason {
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
    CloneReady,
    TaskRunning,
    TaskFailed,
    CloneSatisfied,
    ProvenanceMissing,
    ProvenanceMismatch,
    ReceiptMissing,
    OwnershipMissing,
    BoundIdentityChanged,
    ConfigureReady,
    DesiredConfigPending,
    ConfigureSatisfied,
    ConfigureNotSatisfied,
    StartReady,
    StartSatisfied,
    PowerNotRunning,
    VmNotStoppedUnlocked,
    AlreadySatisfied,
    DispatchAlreadyPossible,
    DeadlineExpired,
    TransportLost,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NativeEvaluation {
    pub decision: NativeDecision,
    pub reason: NativeReason,
}
fn result(decision: NativeDecision, reason: NativeReason) -> NativeEvaluation {
    NativeEvaluation { decision, reason }
}
fn unknown(reason: NativeReason) -> NativeEvaluation {
    result(NativeDecision::Unknown, reason)
}
fn conflict(reason: NativeReason) -> NativeEvaluation {
    result(NativeDecision::Conflicted, reason)
}
fn blocked(reason: NativeReason) -> NativeEvaluation {
    result(NativeDecision::Blocked, reason)
}

pub fn is_fresh(observed_at: DateTime<Utc>, as_of: DateTime<Utc>) -> bool {
    observed_at <= as_of && as_of - observed_at <= Duration::seconds(30)
}

/// Advice only. In particular there is no dispatch permit in this return type.
pub fn evaluate_native_preflight(
    context: &NativeEvaluationContext,
    evidence: &NativeEvidence,
    as_of: DateTime<Utc>,
) -> NativeEvaluation {
    evaluate(context, evidence, as_of, true).unwrap_or_else(|r| r)
}
pub fn evaluate_native_outcome(
    context: &NativeEvaluationContext,
    evidence: &NativeEvidence,
    as_of: DateTime<Utc>,
) -> NativeEvaluation {
    evaluate(context, evidence, as_of, false).unwrap_or_else(|r| r)
}
type EvaluationResult<T> = Result<T, NativeEvaluation>;

fn read_error(error: &PveReadError, preflight: bool) -> NativeEvaluation {
    if *error == PveReadError::Unauthorized {
        result(
            if preflight {
                NativeDecision::Blocked
            } else {
                NativeDecision::Unknown
            },
            NativeReason::Unauthorized,
        )
    } else {
        unknown(NativeReason::ObservationUnavailable)
    }
}
fn read<'a, T>(
    value: &'a Option<NativeRead<T>>,
    facts: &NativeEvidenceInput,
    as_of: DateTime<Utc>,
    preflight: bool,
) -> EvaluationResult<&'a T> {
    let r = value
        .as_ref()
        .ok_or(unknown(NativeReason::ObservationMissing))?;
    if let Err(e) = &r.result {
        return Err(read_error(e, preflight));
    }
    timestamp(r.observed_at, facts, as_of)?;
    r.result.as_ref().map_err(|e| read_error(e, preflight))
}
fn timestamp(
    observed_at: DateTime<Utc>,
    facts: &NativeEvidenceInput,
    as_of: DateTime<Utc>,
) -> EvaluationResult<()> {
    if !is_fresh(observed_at, as_of) || observed_at > facts.collected_at {
        return Err(unknown(NativeReason::ObservationNotFresh));
    }
    Ok(())
}
fn target(
    facts: &NativeEvidenceInput,
    as_of: DateTime<Utc>,
    preflight: bool,
) -> EvaluationResult<Option<&NativeVmConfig>> {
    let r = facts
        .target_config
        .as_ref()
        .ok_or(unknown(NativeReason::ObservationMissing))?;
    if let Err(e) = &r.result
        && *e != PveReadError::NotFound
    {
        return Err(read_error(e, preflight));
    }
    timestamp(r.observed_at, facts, as_of)?;
    match &r.result {
        Ok(config) => {
            timestamp(config.observed_at(), facts, as_of)?;
            Ok(Some(config))
        }
        Err(PveReadError::NotFound) => Ok(None),
        Err(e) => Err(read_error(e, preflight)),
    }
}

fn binding(context: &NativeEvaluationContext, facts: &NativeEvidenceInput) -> EvaluationResult<()> {
    let vm = facts.plan.vm();
    if context.binding != facts.binding
        || context.source != facts.source
        || context.clone_request.vm() != vm
        || (facts.plan.step() == NativeStep::Clone
            && context.clone_request.operation_id() != facts.binding.operation_id())
    {
        return Err(conflict(NativeReason::BindingMismatch));
    }
    if facts
        .target_config
        .as_ref()
        .and_then(|r| r.result.as_ref().ok())
        .and_then(|c| c.fake_clone_provenance())
        .is_some_and(|p| !p.matches(&context.clone_request))
    {
        return Err(conflict(NativeReason::ProvenanceMismatch));
    }
    if let Some(observed) = &facts.receipt {
        let expected = context
            .receipt
            .as_ref()
            .ok_or(conflict(NativeReason::BindingMismatch))?;
        if !expected.binding.same_attempt(&facts.binding)
            || observed.receipt != expected.receipt
            || observed.accepted_at != expected.accepted_at
        {
            return Err(conflict(NativeReason::BindingMismatch));
        }
    }
    // Mismatched routes/identities are contradictions, even when another read
    // would otherwise make the operation wait. No caller can swap VM facts.
    let matches_vm = |c: &NativeVmConfig, id| c.node() == vm.node() && c.vmid() == id;
    if facts
        .node
        .as_ref()
        .and_then(|r| r.result.as_ref().ok())
        .is_some_and(|n| n.node() != vm.node())
        || facts
            .storage
            .as_ref()
            .and_then(|r| r.result.as_ref().ok())
            .is_some_and(|s| s.node() != vm.node() || s.storage() != vm.storage())
        || facts
            .bridges
            .as_ref()
            .and_then(|r| r.result.as_ref().ok())
            .is_some_and(|b| b.node() != vm.node())
        || facts
            .source_config
            .as_ref()
            .and_then(|r| r.result.as_ref().ok())
            .is_some_and(|c| !matches_vm(c, vm.source_vmid()))
        || facts
            .target_config
            .as_ref()
            .and_then(|r| r.result.as_ref().ok())
            .is_some_and(|c| !matches_vm(c, vm.target_vmid()) || c.name() != vm.name())
        || facts
            .source_power
            .as_ref()
            .and_then(|r| r.result.as_ref().ok())
            .is_some_and(|p| p.node() != vm.node() || p.vmid() != vm.source_vmid())
        || facts
            .target_power
            .as_ref()
            .and_then(|r| r.result.as_ref().ok())
            .is_some_and(|p| p.node() != vm.node() || p.vmid() != vm.target_vmid())
        || facts.identities.iter().any(|r| {
            r.read
                .result
                .as_ref()
                .is_ok_and(|c| c.node() != &r.node || c.vmid() != r.vmid)
        })
    {
        return Err(conflict(NativeReason::BindingMismatch));
    }
    if let Some(ownership) = &context.clone_ownership {
        if ownership.request() != &context.clone_request
            || ownership.proof().facts().binding.run_id() != facts.binding.run_id()
            || ownership.proof().facts().source != facts.source
        {
            return Err(conflict(NativeReason::BindingMismatch));
        }
        if facts
            .bound_intermediate
            .as_ref()
            .is_some_and(|c| c != ownership.config())
        {
            return Err(conflict(NativeReason::BoundIdentityChanged));
        }
        if let Some(current) = facts
            .target_config
            .as_ref()
            .and_then(|r| r.result.as_ref().ok())
        {
            let bound = ownership.config();
            if current.boot_disk() != bound.boot_disk() {
                return Err(conflict(NativeReason::BoundIdentityChanged));
            }
            if current
                .fake_clone_provenance()
                .is_some_and(|p| !p.matches(&context.clone_request))
            {
                return Err(conflict(NativeReason::ProvenanceMismatch));
            }
            let historical = current.uuid() == bound.uuid() && current.mac() == bound.mac();
            let final_identity = current.uuid() == vm.uuid() && current.mac() == vm.mac();
            if !historical && !final_identity {
                return Err(conflict(NativeReason::BoundIdentityChanged));
            }
        }
    }
    Ok(())
}
fn state(context: &NativeEvaluationContext, preflight: bool) -> EvaluationResult<()> {
    let eligible = if preflight {
        context.mode == NativeEvaluationMode::Preflight && context.state == ExecutionState::Leased
    } else {
        match context.mode {
            NativeEvaluationMode::Outcome => matches!(
                context.state,
                ExecutionState::Running | ExecutionState::Waiting | ExecutionState::Cancelling
            ),
            NativeEvaluationMode::Reconciliation => context.state == ExecutionState::Unknown,
            NativeEvaluationMode::Preflight => false,
        }
    };
    if !eligible {
        let decision = match context.state {
            ExecutionState::Conflicted => NativeDecision::Conflicted,
            ExecutionState::Failed => NativeDecision::Failed,
            ExecutionState::Blocked => NativeDecision::Blocked,
            _ => NativeDecision::Unknown,
        };
        return Err(result(decision, NativeReason::StateNotEligible));
    }
    if context.cancelled
        && !(context.mode == NativeEvaluationMode::Reconciliation
            && context.possible_dispatch
            && !preflight)
    {
        return Err(unknown(NativeReason::Cancelled));
    }
    if preflight
        && (context.possible_dispatch
            || context.receipt.is_some()
            || context.dispatched_at.is_some())
    {
        return Err(unknown(NativeReason::DispatchAlreadyPossible));
    }
    if !preflight && (!context.possible_dispatch || context.dispatched_at.is_none()) {
        return Err(unknown(NativeReason::StateNotEligible));
    }
    Ok(())
}

fn coverage(
    facts: &NativeEvidenceInput,
    inventory: &ClusterVmInventory,
    current: Option<&NativeVmConfig>,
    as_of: DateTime<Utc>,
    preflight: bool,
) -> EvaluationResult<()> {
    let vm = facts.plan.vm();
    let mut ids = BTreeSet::new();
    let mut uuids = BTreeSet::new();
    let mut macs = BTreeSet::new();
    let mut volumes = BTreeSet::new();
    for identity in &facts.identities {
        if !ids.insert(identity.vmid) {
            return Err(conflict(NativeReason::InventoryContradiction));
        }
        let resource = inventory
            .find(identity.vmid)
            .ok_or(conflict(NativeReason::InventoryContradiction))?;
        if resource.node() != &identity.node {
            return Err(conflict(NativeReason::InventoryContradiction));
        }
        let config = identity
            .read
            .result
            .as_ref()
            .map_err(|e| read_error(e, preflight))?;
        timestamp(identity.read.observed_at, facts, as_of)?;
        timestamp(config.observed_at(), facts, as_of)?;
        if config.name() != resource.name() || config.is_template() != resource.is_template() {
            return Err(conflict(NativeReason::InventoryContradiction));
        }
        // Additional NICs and unsupported fields prevent complete identity
        // coverage; this type does not pretend its single MAC is exhaustive.
        if !config.unsupported().is_empty() {
            return Err(unknown(NativeReason::IncompleteIdentityCoverage));
        }
        if !uuids.insert(config.uuid()) || !macs.insert(config.mac().clone()) {
            return Err(conflict(NativeReason::IdentityCollision));
        }
        if !volumes.insert((
            config.node().clone(),
            config.boot_disk().storage().clone(),
            config.boot_disk().volume().to_owned(),
        )) {
            return Err(conflict(NativeReason::IdentityCollision));
        }
        if config.vmid() != vm.target_vmid()
            && (config.uuid() == vm.uuid() || config.mac() == vm.mac())
        {
            return Err(conflict(NativeReason::IdentityCollision));
        }
        if config.vmid() == vm.target_vmid() && current.is_none_or(|c| !same_config(c, config)) {
            return Err(conflict(NativeReason::InventoryContradiction));
        }
        if config.vmid() == vm.source_vmid()
            && let Some(source) = facts
                .source_config
                .as_ref()
                .and_then(|r| r.result.as_ref().ok())
            && !same_config(source, config)
        {
            return Err(conflict(NativeReason::InventoryContradiction));
        }
    }
    if ids.len() != inventory.vms().len() {
        return Err(unknown(NativeReason::IncompleteIdentityCoverage));
    }
    Ok(())
}
fn same_config(a: &NativeVmConfig, b: &NativeVmConfig) -> bool {
    a.node() == b.node()
        && a.vmid() == b.vmid()
        && a.name() == b.name()
        && a.digest() == b.digest()
        && a.uuid() == b.uuid()
        && a.mac() == b.mac()
        && a.bridge() == b.bridge()
        && a.boot_disk() == b.boot_disk()
        && a.cores() == b.cores()
        && a.memory_mib() == b.memory_mib()
        && a.agent_enabled() == b.agent_enabled()
        && a.boots_scsi0() == b.boots_scsi0()
        && a.is_template() == b.is_template()
        && a.locked() == b.locked()
        && a.unsupported() == b.unsupported()
        && a.fake_clone_provenance() == b.fake_clone_provenance()
}
fn unauthorized<T>(r: &Option<NativeRead<T>>) -> bool {
    r.as_ref()
        .is_some_and(|r| matches!(r.result, Err(PveReadError::Unauthorized)))
}
fn required_unauthorized(f: &NativeEvidenceInput, preflight: bool) -> bool {
    unauthorized(&f.inventory)
        || unauthorized(&f.target_config)
        || f.identities
            .iter()
            .any(|r| matches!(r.read.result, Err(PveReadError::Unauthorized)))
        || (preflight
            && (unauthorized(&f.node) || unauthorized(&f.storage) || unauthorized(&f.bridges)))
        || (preflight
            && f.plan.step() == NativeStep::Clone
            && (unauthorized(&f.source_config) || unauthorized(&f.source_power)))
        || ((!preflight || f.plan.step() != NativeStep::Clone) && unauthorized(&f.target_power))
        || (!preflight && f.plan.step() != NativeStep::Configure && unauthorized(&f.task))
}
fn power<'a>(
    read_value: &'a Option<NativeRead<VmPowerStatus>>,
    facts: &NativeEvidenceInput,
    inventory: &ClusterVmInventory,
    as_of: DateTime<Utc>,
    preflight: bool,
) -> EvaluationResult<&'a VmPowerStatus> {
    let power = read(read_value, facts, as_of, preflight)?;
    timestamp(power.observed_at(), facts, as_of)?;
    if inventory
        .find(power.vmid())
        .is_none_or(|r| r.node() != power.node() || r.power() != power.power())
    {
        return Err(conflict(NativeReason::InventoryContradiction));
    }
    Ok(power)
}

fn outcome_after_dispatch(
    context: &NativeEvaluationContext,
    facts: &NativeEvidenceInput,
) -> EvaluationResult<()> {
    let dispatched_at = context
        .dispatched_at
        .ok_or(unknown(NativeReason::StateNotEligible))?;
    let mut timestamps = Vec::new();
    macro_rules! collect {
        ($read:expr) => {
            if let Some(r) = $read {
                timestamps.push(r.observed_at);
                if let Ok(snapshot) = &r.result {
                    timestamps.push(snapshot.observed_at());
                }
            }
        };
    }
    collect!(&facts.inventory);
    collect!(&facts.target_config);
    collect!(&facts.target_power);
    collect!(&facts.task);
    for identity in &facts.identities {
        if identity.vmid == facts.plan.vm().target_vmid() {
            collect!(Some(&identity.read));
        }
    }
    if timestamps.into_iter().any(|t| t < dispatched_at)
        || facts
            .receipt
            .as_ref()
            .is_some_and(|r| r.accepted_at < dispatched_at)
    {
        return Err(unknown(NativeReason::ObservationNotFresh));
    }
    Ok(())
}

fn evaluate(
    context: &NativeEvaluationContext,
    evidence: &NativeEvidence,
    as_of: DateTime<Utc>,
    preflight: bool,
) -> EvaluationResult<NativeEvaluation> {
    let facts = evidence.facts();
    let vm = facts.plan.vm();
    binding(context, facts)?;
    state(context, preflight)?;
    if required_unauthorized(facts, preflight) {
        return Err(read_error(&PveReadError::Unauthorized, preflight));
    }
    if context.mode != NativeEvaluationMode::Reconciliation {
        if context
            .mutation_deadline
            .is_some_and(|deadline| as_of >= deadline)
        {
            return Err(unknown(NativeReason::DeadlineExpired));
        }
        if context.transport_lost {
            return Err(unknown(NativeReason::TransportLost));
        }
    }
    if !is_fresh(facts.collected_at, as_of)
        || !is_fresh(facts.evaluated_at, as_of)
        || facts.collected_at > facts.evaluated_at
    {
        return Err(unknown(NativeReason::ObservationNotFresh));
    }
    if !preflight {
        outcome_after_dispatch(context, facts)?;
        let receiptless_configure = context.mode == NativeEvaluationMode::Reconciliation
            && facts.plan.step() == NativeStep::Configure
            && context.receipt.is_none();
        if facts.receipt.is_none() && !receiptless_configure {
            return Err(unknown(NativeReason::ReceiptMissing));
        }
        if facts
            .receipt
            .as_ref()
            .is_some_and(|r| r.accepted_at > facts.collected_at)
            || context
                .dispatched_at
                .is_some_and(|t| t > facts.collected_at)
        {
            return Err(unknown(NativeReason::ObservationNotFresh));
        }
    }
    let inventory = read(&facts.inventory, facts, as_of, preflight)?;
    timestamp(inventory.observed_at(), facts, as_of)?;
    let current = target(facts, as_of, preflight)?;
    if !preflight
        && current.is_some_and(|c| {
            c.observed_at()
                < facts
                    .receipt
                    .as_ref()
                    .map_or(context.dispatched_at.expect("checked dispatch"), |r| {
                        r.accepted_at
                    })
        })
    {
        return Err(unknown(NativeReason::ObservationNotFresh));
    }
    let target_resource = inventory.find(vm.target_vmid());
    if preflight
        && facts.plan.step() == NativeStep::Clone
        && (current.is_some() || target_resource.is_some())
    {
        return Err(conflict(NativeReason::TargetOccupied));
    }
    if current.is_some() != target_resource.is_some() {
        return Err(conflict(NativeReason::InventoryContradiction));
    }
    if target_resource
        .is_some_and(|r| r.node() != vm.node() || r.name() != vm.name() || r.is_template())
    {
        return Err(conflict(NativeReason::BindingMismatch));
    }
    if preflight {
        let node = read(&facts.node, facts, as_of, true)?;
        timestamp(node.observed_at(), facts, as_of)?;
        let storage = read(&facts.storage, facts, as_of, true)?;
        timestamp(storage.observed_at(), facts, as_of)?;
        let bridges = read(&facts.bridges, facts, as_of, true)?;
        timestamp(bridges.observed_at(), facts, as_of)?;
        if !node.online() || !bridges.has_active(vm.bridge()) {
            return Err(blocked(NativeReason::UnsupportedInfrastructure));
        }
        if facts.plan.step() == NativeStep::Clone {
            if storage.available_bytes() < vm.minimum_storage_bytes() {
                return Err(blocked(NativeReason::InsufficientCapacity));
            }
            let source = read(&facts.source_config, facts, as_of, true)?;
            timestamp(source.observed_at(), facts, as_of)?;
            if !source.is_template() || !source.unsupported().is_empty() || !source.boots_scsi0() {
                return Err(blocked(NativeReason::UnsupportedLayout));
            }
            let source_power = power(&facts.source_power, facts, inventory, as_of, true)?;
            if source.locked()
                || source_power.locked() == Some(true)
                || source_power.power() != PowerState::Stopped
            {
                return Err(blocked(NativeReason::VmNotStoppedUnlocked));
            }
        }
        if current.is_some_and(|c| !c.unsupported().is_empty() || c.is_template()) {
            return Err(blocked(NativeReason::UnsupportedLayout));
        }
    }
    coverage(facts, inventory, current, as_of, preflight)?;
    if facts.plan.step() == NativeStep::Clone {
        if preflight {
            return Ok(result(NativeDecision::Ready, NativeReason::CloneReady));
        }
        if let Some(current) = current {
            provenance(context, current)?;
        }
        let task = read(&facts.task, facts, as_of, false)?;
        timestamp(task.observed_at(), facts, as_of)?;
        if task.observed_at() < facts.receipt.as_ref().expect("checked receipt").accepted_at {
            return Err(unknown(NativeReason::ObservationNotFresh));
        }
        match task.state() {
            TaskState::Running => {
                return Ok(result(NativeDecision::Waiting, NativeReason::TaskRunning));
            }
            TaskState::CompleteFailure if current.is_none() => {
                return Ok(result(NativeDecision::Failed, NativeReason::TaskFailed));
            }
            TaskState::CompleteFailure => return Err(unknown(NativeReason::TaskFailed)),
            TaskState::CompleteSuccess => {}
        }
        let current = current.ok_or(unknown(NativeReason::ObservationMissing))?;
        provenance(context, current)?;
        if current.is_template()
            || !current.unsupported().is_empty()
            || current.boot_disk().storage() != vm.storage()
        {
            return Err(conflict(NativeReason::BoundIdentityChanged));
        }
        let target_power = power(&facts.target_power, facts, inventory, as_of, false)?;
        if current.locked()
            || target_power.locked() == Some(true)
            || target_power.power() != PowerState::Stopped
        {
            return Err(unknown(NativeReason::VmNotStoppedUnlocked));
        }
        return Ok(result(
            NativeDecision::Satisfied,
            NativeReason::CloneSatisfied,
        ));
    }
    let ownership = context
        .clone_ownership
        .as_ref()
        .ok_or(unknown(NativeReason::OwnershipMissing))?;
    if facts.bound_intermediate.is_none() {
        return Err(unknown(NativeReason::OwnershipMissing));
    }
    let current = current.ok_or(unknown(NativeReason::ObservationMissing))?;
    provenance(context, current)?;
    let target_power = power(&facts.target_power, facts, inventory, as_of, preflight)?;
    if current.locked() || target_power.locked() == Some(true) {
        return Err(if preflight {
            blocked(NativeReason::VmNotStoppedUnlocked)
        } else {
            unknown(NativeReason::VmNotStoppedUnlocked)
        });
    }
    let final_fields = final_fields(vm, current);
    if facts.plan.step() == NativeStep::Configure {
        if target_power.power() != PowerState::Stopped {
            return Err(if preflight {
                blocked(NativeReason::VmNotStoppedUnlocked)
            } else {
                unknown(NativeReason::VmNotStoppedUnlocked)
            });
        }
        if final_fields {
            return Ok(result(
                NativeDecision::Satisfied,
                if preflight {
                    NativeReason::AlreadySatisfied
                } else {
                    NativeReason::ConfigureSatisfied
                },
            ));
        }
        if !preflight {
            return Ok(result(
                if facts.receipt.is_some() {
                    NativeDecision::Waiting
                } else {
                    NativeDecision::Unknown
                },
                NativeReason::DesiredConfigPending,
            ));
        }
        ConfigureRequest::new(
            vm.clone(),
            &context.clone_request,
            ownership.config(),
            current,
            as_of,
        )
        .map_err(|_| conflict(NativeReason::BoundIdentityChanged))?;
        return Ok(result(NativeDecision::Ready, NativeReason::ConfigureReady));
    }
    if !context.configure_satisfied {
        return Err(if preflight {
            blocked(NativeReason::ConfigureNotSatisfied)
        } else {
            unknown(NativeReason::ConfigureNotSatisfied)
        });
    }
    if !final_fields {
        return Err(conflict(NativeReason::BoundIdentityChanged));
    }
    StartRequest::new(
        vm.clone(),
        &context.clone_request,
        ownership.config(),
        current,
        as_of,
    )
    .map_err(|_| conflict(NativeReason::BoundIdentityChanged))?;
    if preflight {
        if target_power.power() == PowerState::Running {
            return if context.uninterrupted_workflow {
                Ok(result(
                    NativeDecision::Satisfied,
                    NativeReason::AlreadySatisfied,
                ))
            } else {
                Err(unknown(NativeReason::StateNotEligible))
            };
        }
        return Ok(result(NativeDecision::Ready, NativeReason::StartReady));
    }
    let task = read(&facts.task, facts, as_of, false)?;
    timestamp(task.observed_at(), facts, as_of)?;
    if task.observed_at() < facts.receipt.as_ref().expect("checked receipt").accepted_at {
        return Err(unknown(NativeReason::ObservationNotFresh));
    }
    match task.state() {
        TaskState::Running => Ok(result(NativeDecision::Waiting, NativeReason::TaskRunning)),
        TaskState::CompleteFailure if target_power.power() == PowerState::Stopped => {
            Ok(result(NativeDecision::Failed, NativeReason::TaskFailed))
        }
        TaskState::CompleteFailure => Err(unknown(NativeReason::TaskFailed)),
        TaskState::CompleteSuccess if target_power.power() == PowerState::Running => Ok(result(
            NativeDecision::Satisfied,
            NativeReason::StartSatisfied,
        )),
        TaskState::CompleteSuccess => Err(unknown(NativeReason::PowerNotRunning)),
    }
}
fn provenance(context: &NativeEvaluationContext, current: &NativeVmConfig) -> EvaluationResult<()> {
    let marker = current
        .fake_clone_provenance()
        .ok_or(unknown(NativeReason::ProvenanceMissing))?;
    if context.source != NativeEvidenceSource::FakePve || !marker.matches(&context.clone_request) {
        return Err(conflict(NativeReason::ProvenanceMismatch));
    }
    Ok(())
}

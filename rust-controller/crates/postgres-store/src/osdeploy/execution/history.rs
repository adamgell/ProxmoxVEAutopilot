//! Reconstruct physical history at its original selected time. No send values
//! or successful predecessors can be restored without their indexed evidence.
use super::{
    OsDeployExecutionError as Error,
    load::{self, Records},
    wire::{self, Detail},
};
use chrono::{DateTime, Utc};
use controller_domain::{EventId, ExecutionState, OperationId};
use osdeploy_adapter::OsDeployStage;
use pve_port::*;
use sqlx::{Postgres, Transaction};

#[derive(Default)]
struct PhysicalHistory {
    owner: Option<ProvisioningCloneOwnershipV1>,
    prior: Option<ProvisioningStageBaselineV1>,
}

pub(crate) fn enabled(stage: OsDeployStage) -> Result<(), Error> {
    match stage {
        OsDeployStage::Clone | OsDeployStage::DiskCapacity | OsDeployStage::ConfigurePe => Ok(()),
        #[cfg(feature = "fixture-ipc")]
        OsDeployStage::StartPe => Ok(()),
        _ => Err(Error::CapabilityUnavailable),
    }
}

fn selected(records: &Records) -> Option<&load::Decision> {
    records
        .decisions
        .iter()
        .filter(|d| {
            d.value
                .resolution
                .is_some_and(|r| r != NativeDecision::Ready)
        })
        .max_by_key(|d| d.revision)
}

fn context(
    records: &Records,
    history: &PhysicalHistory,
    fence: u64,
    mode: ProvisioningEvaluationModeV1,
    before_revision: i64,
    at: Option<DateTime<Utc>>,
) -> Result<ProvisioningEvaluationContextV1, Error> {
    let s = &records.snapshot;
    let plan = s.plan().pve().ok_or(Error::CapabilityUnavailable)?.clone();
    let clone_operation = records.registration.ids().operation(OsDeployStage::Clone);
    let clone_request = serde_json::from_value(serde_json::json!({
        "vm": records.registration.plan().vm(), "operation_id": clone_operation,
        "request_marker": clone_operation.as_uuid()
    }))?;
    let state = records
        .journal
        .values()
        .filter(|e| e.revision <= before_revision)
        .filter_map(|e| e.state.map(|state| (e.revision, state)))
        .max_by_key(|(revision, _)| *revision)
        .map(|(_, state)| state)
        .unwrap_or(ExecutionState::Pending);
    let dispatch = if let Some(dispatch) = s
        .dispatch()
        .filter(|d| d.dispatch_revision() <= before_revision as u64)
    {
        ProvisioningDispatchStateV1::Recorded {
            dispatch: dispatch.clone(),
            receipt: s
                .receipt()
                .filter(|_| {
                    records
                        .receipt_revision
                        .is_some_and(|r| r <= before_revision)
                })
                .cloned(),
        }
    } else {
        ProvisioningDispatchStateV1::NotDispatched
    };
    ProvisioningEvaluationContextV1::new(ProvisioningEvaluationContextInputV1 {
        binding: ProvisioningBindingV1::new(
            s.run_id(),
            s.operation_id(),
            s.attempt_id().ok_or(Error::Validation)?,
            s.plan().workflow_sha256(),
            &plan,
            fence,
        )
        .map_err(|_| Error::Validation)?,
        plan,
        source: NativeEvidenceSource::FakePve,
        state,
        mode,
        cancelled: at.map_or(s.cancelled(), |at| {
            records.cancelled_at.is_some_and(|t| t <= at)
        }),
        mutation_deadline: s.deadline_at().ok_or(Error::Validation)?,
        freshness_seconds: records
            .registration
            .plan()
            .policy()
            .evidence_freshness_seconds()
            .try_into()
            .map_err(|_| Error::Validation)?,
        dispatch,
        clone_request,
        clone_ownership: history.owner.clone(),
        predecessor: history.prior.clone(),
    })
    .map_err(|_| Error::Validation)
}

/// Rebuild from the actual observed source/target. The as-of time validates
/// freshness but is not stored in the request, allowing exact re-comparison.
pub(crate) fn request(
    c: &ProvisioningEvaluationContextV1,
    evidence: &ProvisioningEvidenceV1,
    at: DateTime<Utc>,
) -> Result<ProvisioningMutationRequestV1, Error> {
    let facts = c.facts();
    wire::require(
        facts.binding == evidence.facts().binding
            && facts.plan == evidence.facts().plan
            && facts.source == evidence.facts().source,
    )?;
    wire::require(
        evaluate_provisioning_preflight(c, evidence, at).decision == NativeDecision::Ready,
    )?;
    let e = evidence.facts();
    let (config, power) = if facts.plan.action() == ProvisioningActionV1::Clone {
        (&e.source_config, &e.source_power)
    } else {
        (&e.target_config, &e.target_power)
    };
    let before = ProvisioningBeforeStateV1::new(
        config
            .as_ref()
            .and_then(|r| r.result.as_ref().ok())
            .ok_or(Error::Validation)?
            .clone(),
        power
            .as_ref()
            .and_then(|r| r.result.as_ref().ok())
            .ok_or(Error::Validation)?
            .clone(),
    )
    .map_err(|_| Error::Validation)?;
    if facts.plan.action() == ProvisioningActionV1::Clone {
        return CloneProvisioningRequestV1::new(
            facts.binding.clone(),
            facts.plan.clone(),
            facts.clone_request.clone(),
            before,
            at,
            facts.freshness_seconds,
        )
        .map(ProvisioningMutationRequestV1::Clone)
        .map_err(|_| Error::Validation);
    }
    let input = ProvisioningOwnedRequestInputV1 {
        binding: facts.binding.clone(),
        plan: facts.plan.clone(),
        ownership: facts.clone_ownership.as_ref().ok_or(Error::Validation)?,
        predecessor: facts.predecessor.as_ref().ok_or(Error::Validation)?,
        expected_before: before,
        as_of: at,
        freshness_seconds: facts.freshness_seconds,
    };
    match facts.plan.action() {
        ProvisioningActionV1::EnsureCapacity => {
            GrowDiskRequestV1::new(input).map(ProvisioningMutationRequestV1::GrowDisk)
        }
        ProvisioningActionV1::ConfigurePe => {
            ConfigureProvisioningRequestV1::new(input).map(ProvisioningMutationRequestV1::Configure)
        }
        #[cfg(feature = "fixture-ipc")]
        ProvisioningActionV1::StartPe => {
            StartProvisioningRequestV1::new(input).map(ProvisioningMutationRequestV1::Start)
        }
        _ => return Err(Error::CapabilityUnavailable),
    }
    .map_err(|_| Error::Validation)
}

fn validate_physical(records: &Records, history: &PhysicalHistory) -> Result<(), Error> {
    if let Some(dispatch) = records.snapshot.dispatch() {
        let e = records
            .evidence
            .get(&dispatch.preflight_event_id().as_uuid())
            .ok_or(Error::Validation)?;
        let before_revision =
            i64::try_from(dispatch.dispatch_revision()).map_err(|_| Error::Validation)? - 1;
        let c = context(
            records,
            history,
            e.value.facts().binding.evidence_fence(),
            ProvisioningEvaluationModeV1::Preflight,
            before_revision,
            Some(dispatch.dispatched_at()),
        )?;
        let rebuilt = request(&c, &e.value, dispatch.dispatched_at())?;
        wire::require(
            &rebuilt == dispatch.request()
                && rebuilt.request_digest().map_err(|_| Error::Validation)?
                    == dispatch.request_sha256(),
        )?;
    }
    for decision in &records.decisions {
        let Detail::PveEvaluated(value) = &decision.value.detail else {
            continue;
        };
        let evidence = records
            .evidence
            .get(&value.evidence_event_id.as_uuid())
            .ok_or(Error::Validation)?;
        let mode = mode(value.mode);
        let c = context(
            records,
            history,
            evidence.value.facts().binding.evidence_fence(),
            mode,
            decision.value.before_revision,
            Some(decision.value.evaluated_at),
        )?;
        let advice = if mode == ProvisioningEvaluationModeV1::Preflight {
            evaluate_provisioning_preflight(&c, &evidence.value, decision.value.evaluated_at)
        } else {
            evaluate_provisioning_outcome(&c, &evidence.value, decision.value.evaluated_at)
        };
        wire::require(advice.decision == value.advice && advice.reason == value.reason)?;
    }
    Ok(())
}
fn mode(mode: wire::Mode) -> ProvisioningEvaluationModeV1 {
    match mode {
        wire::Mode::Preflight => ProvisioningEvaluationModeV1::Preflight,
        wire::Mode::Outcome => ProvisioningEvaluationModeV1::Outcome,
        wire::Mode::Reconciliation => ProvisioningEvaluationModeV1::Reconciliation,
    }
}

/// Walk only the enabled physical prefix, never recurse through public reload.
/// Each constructor replays the already selected decision at its original time.
async fn predecessors(
    tx: &mut Transaction<'_, Postgres>,
    target: &Records,
) -> Result<PhysicalHistory, Error> {
    let mut history = PhysicalHistory::default();
    for stage in OsDeployStage::ALL {
        if stage == target.snapshot.plan().stage() {
            return Ok(history);
        }
        enabled(stage)?;
        let prior = Box::pin(load::load_records(
            tx,
            target.registration.ids().operation(stage),
        ))
        .await?;
        validate_physical(&prior, &history)?;
        let decision = selected(&prior).ok_or(Error::Validation)?;
        wire::require(
            prior.snapshot.state() == ExecutionState::Satisfied
                && decision.value.resolution == Some(NativeDecision::Satisfied)
                && target
                    .snapshot
                    .activated_at()
                    .is_some_and(|at| decision.value.evaluated_at <= at),
        )?;
        let Detail::PveEvaluated(value) = &decision.value.detail else {
            return Err(Error::Validation);
        };
        let evidence = prior
            .evidence
            .get(&value.evidence_event_id.as_uuid())
            .ok_or(Error::Validation)?;
        let c = context(
            &prior,
            &history,
            evidence.value.facts().binding.evidence_fence(),
            mode(value.mode),
            decision.value.before_revision,
            Some(decision.value.evaluated_at),
        )?;
        if stage == OsDeployStage::Clone {
            history.owner = Some(
                ProvisioningCloneOwnershipV1::from_satisfied_clone(
                    &c,
                    &evidence.value,
                    decision.value.evaluated_at,
                )
                .map_err(|_| Error::Validation)?,
            );
        }
        history.prior = Some(
            ProvisioningStageBaselineV1::from_satisfied(
                &c,
                &evidence.value,
                decision.value.evaluated_at,
            )
            .map_err(|_| Error::Validation)?,
        );
    }
    Err(Error::Validation)
}

pub(super) async fn validate(
    tx: &mut Transaction<'_, Postgres>,
    records: &Records,
) -> Result<(), Error> {
    // Declaration-only later stages remain readable and confer no capability.
    if records.snapshot.attempt_id().is_none() {
        return Ok(());
    }
    #[cfg(feature = "fixture-ipc")]
    if records.snapshot.plan().stage() == OsDeployStage::PeRegister {
        // Validate every selected physical predecessor, then restore only the
        // inherited callback authority. Physical context builders stay closed.
        Box::pin(predecessors(tx, records)).await?;
        wire::require(
            records.snapshot.dispatch().is_none()
                && records.evidence.is_empty()
                && !records
                    .decisions
                    .iter()
                    .any(|d| matches!(d.value.detail, Detail::PveEvaluated(_))),
        )?;
        return load::require_fixture_registration_origin(
            tx,
            &records.registration,
            records.snapshot.activated_at().ok_or(Error::Validation)?,
        )
        .await;
    }
    enabled(records.snapshot.plan().stage())?;
    let history = Box::pin(predecessors(tx, records)).await?;
    validate_physical(records, &history)
}

pub(crate) async fn load_context(
    tx: &mut Transaction<'_, Postgres>,
    operation: OperationId,
    revision: i64,
    mode: ProvisioningEvaluationModeV1,
) -> Result<ProvisioningEvaluationContextV1, Error> {
    let records = Box::pin(load::load_records(tx, operation)).await?;
    if records.snapshot.plan().stage() == OsDeployStage::StartPe
        && records.snapshot.attempt_id().is_none()
    {
        return Err(Error::CapabilityUnavailable);
    }
    enabled(records.snapshot.plan().stage())?;
    if revision < 0 || records.snapshot.revision() != revision {
        return Err(Error::FenceLost);
    }
    let history = Box::pin(predecessors(tx, &records)).await?;
    validate_physical(&records, &history)?;
    context(&records, &history, revision as u64, mode, revision, None)
}

/// The provenance row is mandatory even when generic journal JSON looks valid.
/// CAS belongs to the current snapshot; the binding keeps the original fence.
pub(crate) async fn preflight(
    tx: &mut Transaction<'_, Postgres>,
    operation: OperationId,
    revision: i64,
    event: EventId,
) -> Result<(ProvisioningEvaluationContextV1, ProvisioningEvidenceV1), Error> {
    // Keep the complete predecessor/evidence future off the dispatch caller's
    // frame; the recovered three-stage prefix exercises this nested reload.
    Box::pin(indexed_context(
        tx,
        operation,
        revision,
        event,
        ProvisioningEvaluationModeV1::Preflight,
    ))
    .await
}

/// Reconstruct a decision from indexed evidence, retaining its original fence.
pub(crate) async fn indexed_context(
    tx: &mut Transaction<'_, Postgres>,
    operation: OperationId,
    revision: i64,
    event: EventId,
    mode: ProvisioningEvaluationModeV1,
) -> Result<(ProvisioningEvaluationContextV1, ProvisioningEvidenceV1), Error> {
    let records = Box::pin(load::load_records(tx, operation)).await?;
    enabled(records.snapshot.plan().stage())?;
    if revision < 0 || records.snapshot.revision() != revision {
        return Err(Error::FenceLost);
    }
    let history = Box::pin(predecessors(tx, &records)).await?;
    validate_physical(&records, &history)?;
    let e = records
        .evidence
        .get(&event.as_uuid())
        .ok_or(Error::Validation)?;
    let c = context(
        &records,
        &history,
        e.value.facts().binding.evidence_fence(),
        mode,
        revision,
        None,
    )?;
    Ok((c, e.value.clone()))
}

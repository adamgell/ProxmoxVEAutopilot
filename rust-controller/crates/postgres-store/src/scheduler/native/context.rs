use super::*;

/// Called only after authority. Every native mutator shares this run lock and
/// locks all three operations in UUID order before touching an attempt/lease.
pub(super) async fn locked_workflow(
    tx: &mut Transaction<'_, Postgres>,
    operation: OperationId,
) -> Result<(NativeWorkflowIds, NativeOperationSnapshot), NativeStoreError> {
    let run = operation_run(tx, operation).await?;
    lock_run(tx, run).await?;
    let ids = lock_workflow(tx, run).await?;
    let snapshot = records::load(tx, operation).await?;
    Ok((ids, snapshot))
}
pub(super) async fn lock_workflow(
    tx: &mut Transaction<'_, Postgres>,
    run: RunId,
) -> Result<NativeWorkflowIds, NativeStoreError> {
    let ids = workflow_ids(tx, run).await?;
    let mut operations = ids.operations();
    operations.sort_by_key(|id| id.as_uuid());
    for operation in operations {
        lock_operation(tx, operation).await?;
    }
    // Decode every canonical command and predecessor binding after the locks.
    for operation in ids.operations() {
        records::load(tx, operation).await?;
    }
    Ok(ids)
}
pub(super) async fn evidence_for(
    tx: &mut Transaction<'_, Postgres>,
    snapshot: &NativeOperationSnapshot,
    attempt: AttemptId,
    event: EventId,
    expected_revision: i64,
) -> Result<NativeEvidence, NativeStoreError> {
    if snapshot.revision != expected_revision || snapshot.attempt_id != Some(attempt) {
        return Err(NativeStoreError::FenceMismatch);
    }
    let (revision, facts) = records::load_evidence(tx, snapshot.operation_id, event).await?;
    let b = &facts.facts().binding;
    if b.run_id() != snapshot.run_id
        || b.operation_id() != snapshot.operation_id
        || b.attempt_id() != attempt
        || facts.facts().plan != snapshot.plan
    {
        return Err(NativeStoreError::FenceMismatch);
    }
    let superseded:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.journal_events WHERE operation_id=$1 AND aggregate_revision>$2 AND event_kind<>'evidence_recorded')")
        .bind(snapshot.operation_id.as_uuid()).bind(revision).fetch_one(&mut **tx).await?;
    if superseded {
        return Err(NativeStoreError::StaleEvidence);
    }
    Ok(facts)
}
pub(super) fn clone_request(
    vm: &NativeVmPlan,
    operation: OperationId,
    marker: Uuid,
) -> Result<CloneRequest, NativeStoreError> {
    Ok(serde_json::from_value(
        json!({"vm":vm,"operation_id":operation,"request_marker":marker}),
    )?)
}
pub(super) async fn build_context(
    tx: &mut Transaction<'_, Postgres>,
    ids: &NativeWorkflowIds,
    snapshot: &NativeOperationSnapshot,
    evidence: &NativeEvidence,
    mode: NativeEvaluationMode,
) -> Result<NativeEvaluationContext, NativeStoreError> {
    let clone = records::load(tx, ids.clone_id()).await?;
    let request = if let Some(d) = &clone.dispatch {
        let request = clone_request(clone.plan.vm(), clone.operation_id, d.request_marker)?;
        if request.request_digest() != d.request_digest {
            return Err(NativeStoreError::Validation);
        }
        request
    } else {
        // Placeholder used only by clone preflight; no ownership exists and it
        // is never persisted or returned as a dispatch request.
        clone_request(
            clone.plan.vm(),
            clone.operation_id,
            clone.operation_id.as_uuid(),
        )?
    };
    let ownership = if snapshot.plan.step() != NativeStep::Clone {
        Some(load_ownership(tx, &clone, &request).await?)
    } else {
        None
    };
    let configure = records::load(tx, ids.configure_id()).await?;
    let interrupted:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.attempts a JOIN rust_controller.operations o USING(operation_id) WHERE o.run_id=$1 AND (a.attempt_number>1 OR a.state IN ('unknown','failed','blocked','conflicted','cancelling')))")
        .bind(ids.run_id().as_uuid()).fetch_one(&mut **tx).await?;
    let transport_lost = snapshot.dispatch.is_some()
        && snapshot.receipt.is_none()
        && snapshot.state == ExecutionState::Unknown;
    Ok(NativeEvaluationContext {
        binding: evidence.facts().binding.clone(),
        source: NativeEvidenceSource::FakePve,
        state: snapshot.state,
        mode,
        cancelled: snapshot.cancelled,
        possible_dispatch: snapshot.dispatch.is_some(),
        mutation_deadline: snapshot.deadline,
        transport_lost,
        receipt: snapshot.receipt.clone(),
        dispatched_at: snapshot.dispatch.as_ref().map(|d| d.dispatched_at),
        configure_satisfied: configure.state == ExecutionState::Satisfied
            && configure
                .decision
                .as_ref()
                .is_some_and(|d| d.evaluation.decision == NativeDecision::Satisfied),
        uninterrupted_workflow: !interrupted && !snapshot.cancelled,
        clone_request: request,
        clone_ownership: ownership,
    })
}
async fn load_ownership(
    tx: &mut Transaction<'_, Postgres>,
    clone: &NativeOperationSnapshot,
    request: &CloneRequest,
) -> Result<NativeCloneOwnership, NativeStoreError> {
    let decision = clone
        .decision
        .as_ref()
        .ok_or(NativeStoreError::Validation)?;
    let dispatch = clone
        .dispatch
        .as_ref()
        .ok_or(NativeStoreError::Validation)?;
    if clone.state != ExecutionState::Satisfied
        || decision.evaluation.decision != NativeDecision::Satisfied
        || clone.attempt_id != Some(decision.attempt_id)
        || decision.attempt_id != dispatch.attempt_id
    {
        return Err(NativeStoreError::Validation);
    }
    let (_, proof) =
        records::load_evidence(tx, clone.operation_id, decision.evidence_event_id).await?;
    if proof.facts().plan != clone.plan
        || proof.facts().binding.attempt_id() != dispatch.attempt_id
        || proof.facts().binding.run_id() != clone.run_id
    {
        return Err(NativeStoreError::Validation);
    }
    // Re-evaluate the complete persisted Satisfied proof using the independent
    // original receipt/request/dispatch and its historical database clock.
    let context = NativeEvaluationContext {
        binding: proof.facts().binding.clone(),
        source: NativeEvidenceSource::FakePve,
        state: ExecutionState::Running,
        mode: NativeEvaluationMode::Outcome,
        cancelled: false,
        possible_dispatch: true,
        mutation_deadline: None,
        transport_lost: false,
        receipt: clone.receipt.clone(),
        dispatched_at: Some(dispatch.dispatched_at),
        configure_satisfied: false,
        uninterrupted_workflow: true,
        clone_request: request.clone(),
        clone_ownership: None,
    };
    NativeCloneOwnership::from_satisfied_clone(&context, proof, decision.evaluated_at)
        .map_err(|_| NativeStoreError::Validation)
}
pub(super) fn request_digest(
    context: &NativeEvaluationContext,
    evidence: &NativeEvidence,
    operation: OperationId,
    marker: Uuid,
    as_of: DateTime<Utc>,
) -> Result<String, NativeStoreError> {
    let plan = &evidence.facts().plan;
    if plan.step() == NativeStep::Clone {
        return Ok(clone_request(plan.vm(), operation, marker)?.request_digest());
    }
    let bound = context
        .clone_ownership
        .as_ref()
        .ok_or(NativeStoreError::Validation)?
        .config();
    let current = evidence
        .facts()
        .target_config
        .as_ref()
        .and_then(|r| r.result.as_ref().ok())
        .ok_or(NativeStoreError::Validation)?;
    match plan.step() {
        NativeStep::Configure => digest(
            &ConfigureRequest::new(
                plan.vm().clone(),
                &context.clone_request,
                bound,
                current,
                as_of,
            )
            .map_err(|_| NativeStoreError::Validation)?,
        ),
        NativeStep::Start => digest(
            &StartRequest::new(
                plan.vm().clone(),
                &context.clone_request,
                bound,
                current,
                as_of,
            )
            .map_err(|_| NativeStoreError::Validation)?,
        ),
        NativeStep::Clone => unreachable!(),
    }
}

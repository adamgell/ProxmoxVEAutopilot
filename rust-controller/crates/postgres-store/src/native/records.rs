//! Validated reload of immutable native bindings, never caller authority.
use super::*;
use sqlx::Row;

pub(crate) fn state(value: &str) -> Result<ExecutionState, NativeStoreError> {
    serde_json::from_value(serde_json::Value::String(value.to_owned())).map_err(Into::into)
}
pub(crate) async fn load(
    tx: &mut Transaction<'_, Postgres>,
    operation: OperationId,
) -> Result<NativeOperationSnapshot, NativeStoreError> {
    let row=sqlx::query("SELECT o.run_id,o.state,o.revision,o.operation_key,o.contract_version,p.predecessor_id,p.step,p.payload_digest,p.plan,r.plan_digest AS vm_digest,r.cluster_key,r.vmid,r.vm_uuid,r.mac,(SELECT state FROM rust_controller.operations WHERE operation_id=p.predecessor_id) AS predecessor_state,EXISTS(SELECT 1 FROM rust_controller.native_run_cancellations WHERE run_id=o.run_id) AS cancelled FROM rust_controller.operations o JOIN rust_controller.native_operation_plans p USING(operation_id) JOIN rust_controller.native_vm_reservations r ON r.run_id=o.run_id WHERE o.operation_id=$1 AND o.workflow_kind='native_pve_vm_boot'")
        .bind(operation.as_uuid()).fetch_optional(&mut **tx).await?.ok_or(NativeStoreError::Validation)?;
    let plan: NativeOperationPlan = serde_json::from_value(row.try_get("plan")?)?;
    let hash = digest(&plan)?;
    let commands: Vec<String> = sqlx::query_scalar(
        "SELECT DISTINCT payload_digest FROM rust_controller.commands WHERE operation_id=$1",
    )
    .bind(operation.as_uuid())
    .fetch_all(&mut **tx)
    .await?;
    if commands != vec![hash.clone()]
        || hash != row.try_get::<String, _>("payload_digest")?
        || digest(plan.vm())? != row.try_get::<String, _>("vm_digest")?
        || step_name(plan.step()) != row.try_get::<String, _>("step")?
        || plan.step().operation_key() != row.try_get::<String, _>("operation_key")?
        || row.try_get::<i16, _>("contract_version")? != 1
        || plan.vm().cluster_key().to_string() != row.try_get::<String, _>("cluster_key")?
        || plan.vm().target_vmid().to_string() != row.try_get::<i32, _>("vmid")?.to_string()
        || plan.vm().uuid().to_string() != row.try_get::<Uuid, _>("vm_uuid")?.to_string()
        || plan.vm().mac().to_string() != row.try_get::<String, _>("mac")?
    {
        return Err(NativeStoreError::Validation);
    }
    let run_id: RunId = id(row.try_get("run_id")?)?;
    let predecessor_id: Option<OperationId> = row
        .try_get::<Option<Uuid>, _>("predecessor_id")?
        .map(id)
        .transpose()?;
    if plan.step() == NativeStep::Clone {
        if predecessor_id.is_some() {
            return Err(NativeStoreError::Validation);
        }
    } else {
        let pred = predecessor_id.ok_or(NativeStoreError::Validation)?;
        let p=sqlx::query("SELECT o.run_id,p.step,p.plan FROM rust_controller.native_operation_plans p JOIN rust_controller.operations o USING(operation_id) WHERE p.operation_id=$1 AND o.workflow_kind='native_pve_vm_boot'").bind(pred.as_uuid()).fetch_one(&mut **tx).await?;
        let predecessor_plan: NativeOperationPlan = serde_json::from_value(p.try_get("plan")?)?;
        let expected = if plan.step() == NativeStep::Configure {
            NativeStep::Clone
        } else {
            NativeStep::Configure
        };
        if p.try_get::<Uuid, _>("run_id")? != run_id.as_uuid()
            || predecessor_plan.vm() != plan.vm()
            || predecessor_plan.step() != expected
            || p.try_get::<String, _>("step")? != step_name(expected)
        {
            return Err(NativeStoreError::Validation);
        }
    }
    let attempt=sqlx::query("SELECT attempt_id,state,deadline_at FROM rust_controller.attempts WHERE operation_id=$1 ORDER BY attempt_number DESC LIMIT 1").bind(operation.as_uuid()).fetch_optional(&mut **tx).await?;
    let (attempt_id, attempt_state, deadline) = if let Some(a) = attempt {
        (
            Some(id(a.try_get("attempt_id")?)?),
            Some(state(&a.try_get::<String, _>("state")?)?),
            Some(a.try_get("deadline_at")?),
        )
    } else {
        (None, None, None)
    };
    let dispatch = load_dispatch(tx, operation, &hash).await?;
    let receipt = if let Some(d) = &dispatch {
        load_receipt(tx, run_id, &plan, d).await?
    } else {
        None
    };
    let evidence_id:Option<Uuid>=sqlx::query_scalar("SELECT event_id FROM rust_controller.journal_events WHERE operation_id=$1 AND event_kind='evidence_recorded' ORDER BY aggregate_revision DESC LIMIT 1").bind(operation.as_uuid()).fetch_optional(&mut **tx).await?;
    // Generic observations may be untyped. They remain journal evidence, but
    // cannot become a native decision input without validated decoding.
    let evidence = if let Some(e) = evidence_id {
        let event = id(e)?;
        match load_evidence(tx, operation, event).await {
            Ok((_, facts)) => Some((event, facts)),
            Err(NativeStoreError::Validation) => None,
            Err(e) => return Err(e),
        }
    } else {
        None
    };
    let decision = load_decision(tx, operation, &hash).await?;
    Ok(NativeOperationSnapshot {
        operation_id: operation,
        run_id,
        plan,
        state: state(&row.try_get::<String, _>("state")?)?,
        revision: row.try_get("revision")?,
        predecessor_id,
        predecessor_state: row
            .try_get::<Option<String>, _>("predecessor_state")?
            .map(|s| state(&s))
            .transpose()?,
        attempt_id,
        attempt_state,
        deadline,
        dispatch,
        receipt,
        evidence,
        decision,
        cancelled: row.try_get("cancelled")?,
    })
}
pub(crate) async fn load_dispatch(
    tx: &mut Transaction<'_, Postgres>,
    operation: OperationId,
    hash: &str,
) -> Result<Option<NativeDispatch>, NativeStoreError> {
    let row = sqlx::query("SELECT * FROM rust_controller.native_dispatches WHERE operation_id=$1")
        .bind(operation.as_uuid())
        .fetch_optional(&mut **tx)
        .await?;
    row.map(|r| {
        let d = NativeDispatch {
            operation_id: operation,
            attempt_id: id(r.try_get("attempt_id")?)?,
            plan_digest: r.try_get("plan_digest")?,
            generation: r.try_get("generation")?,
            revision: r.try_get("dispatch_revision")?,
            request_digest: r.try_get("request_digest")?,
            request_marker: r.try_get("request_marker")?,
            preflight_event_id: id(r.try_get("preflight_event_id")?)?,
            dispatched_at: r.try_get("dispatched_at")?,
        };
        if d.plan_digest != hash {
            return Err(NativeStoreError::Validation);
        }
        Ok(d)
    })
    .transpose()
}
pub(crate) async fn load_receipt(
    tx: &mut Transaction<'_, Postgres>,
    run: RunId,
    plan: &NativeOperationPlan,
    dispatch: &NativeDispatch,
) -> Result<Option<NativeReceipt>, NativeStoreError> {
    let row=sqlx::query("SELECT receipt_kind,upid,recorded_at FROM rust_controller.native_receipts WHERE operation_id=$1").bind(dispatch.operation_id.as_uuid()).fetch_optional(&mut **tx).await?;
    let Some(r) = row else { return Ok(None) };
    let upid: Option<String> = r.try_get("upid")?;
    let receipt = match (r.try_get::<String, _>("receipt_kind")?.as_str(), upid) {
        ("synchronous", None) => MutationReceipt::SynchronousAccepted,
        ("task", Some(upid)) => {
            MutationReceipt::Task(Upid::parse(upid).map_err(|_| NativeStoreError::Validation)?)
        }
        _ => return Err(NativeStoreError::Validation),
    };
    validate_receipt(plan, &receipt)?;
    let (anchor, _) = load_evidence(tx, dispatch.operation_id, dispatch.preflight_event_id).await?;
    Ok(Some(NativeReceipt {
        binding: NativeBinding::new(
            run,
            dispatch.operation_id,
            dispatch.attempt_id,
            plan,
            (anchor - 1)
                .try_into()
                .map_err(|_| NativeStoreError::Validation)?,
        ),
        accepted_at: r.try_get("recorded_at")?,
        receipt,
    }))
}
pub(crate) fn validate_receipt(
    plan: &NativeOperationPlan,
    receipt: &MutationReceipt,
) -> Result<(), NativeStoreError> {
    let valid = match (plan.step(), receipt) {
        (NativeStep::Configure, MutationReceipt::SynchronousAccepted) => true,
        (NativeStep::Clone | NativeStep::Start, MutationReceipt::Task(upid)) => {
            let (kind, vmid) = if plan.step() == NativeStep::Clone {
                ("qmclone", plan.vm().source_vmid())
            } else {
                ("qmstart", plan.vm().target_vmid())
            };
            upid.node() == plan.vm().node()
                && upid.worker_type() == kind
                && upid.worker_id() == Some(vmid.to_string().as_str())
        }
        _ => false,
    };
    if valid {
        Ok(())
    } else {
        Err(NativeStoreError::Validation)
    }
}
pub(crate) async fn load_evidence(
    tx: &mut Transaction<'_, Postgres>,
    operation: OperationId,
    event: EventId,
) -> Result<(i64, NativeEvidence), NativeStoreError> {
    let r=sqlx::query("SELECT aggregate_revision,attempt_id,payload,payload_digest FROM rust_controller.journal_events WHERE event_id=$1 AND operation_id=$2 AND event_kind='evidence_recorded'").bind(event.as_uuid()).bind(operation.as_uuid()).fetch_optional(&mut **tx).await?.ok_or(NativeStoreError::Validation)?;
    let facts: NativeEvidence = serde_json::from_value(r.try_get("payload")?)?;
    let revision: i64 = r.try_get("aggregate_revision")?;
    if facts.facts().binding.operation_id() != operation
        || Some(facts.facts().binding.attempt_id().as_uuid())
            != r.try_get::<Option<Uuid>, _>("attempt_id")?
        || facts.facts().binding.evidence_fence()
            != u64::try_from(revision - 1).map_err(|_| NativeStoreError::Validation)?
        || digest(&facts)? != r.try_get::<String, _>("payload_digest")?
        || facts.facts().source != NativeEvidenceSource::FakePve
    {
        return Err(NativeStoreError::Validation);
    }
    Ok((revision, facts))
}
async fn load_decision(
    tx: &mut Transaction<'_, Postgres>,
    operation: OperationId,
    hash: &str,
) -> Result<Option<NativeDecisionRecord>, NativeStoreError> {
    let row=sqlx::query("SELECT d.*,e.observed_at,e.payload FROM rust_controller.native_decisions d JOIN rust_controller.journal_events e ON e.operation_id=d.operation_id AND e.aggregate_revision=d.decision_revision AND e.event_kind='decision_recorded' WHERE d.operation_id=$1 ORDER BY d.decision_revision DESC LIMIT 1").bind(operation.as_uuid()).fetch_optional(&mut **tx).await?;
    row.map(|r| {
        if r.try_get::<String, _>("plan_digest")? != hash {
            return Err(NativeStoreError::Validation);
        }
        let evaluation = NativeEvaluation {
            decision: serde_json::from_value(serde_json::Value::String(r.try_get("decision")?))?,
            reason: serde_json::from_value(serde_json::Value::String(r.try_get("reason")?))?,
        };
        let payload: serde_json::Value = r.try_get("payload")?;
        if payload["evaluation"] != serde_json::to_value(evaluation)? {
            return Err(NativeStoreError::Validation);
        }
        Ok(NativeDecisionRecord {
            revision: r.try_get("decision_revision")?,
            attempt_id: id(r.try_get("attempt_id")?)?,
            evidence_event_id: id(r.try_get("evidence_event_id")?)?,
            generation: r.try_get("generation")?,
            evaluation,
            evaluated_at: r.try_get("observed_at")?,
        })
    })
    .transpose()
}

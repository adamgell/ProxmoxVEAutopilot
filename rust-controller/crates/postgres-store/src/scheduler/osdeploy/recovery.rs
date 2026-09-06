use super::*;
use controller_domain::RunId;

impl Scheduler {
    pub async fn reconcile_osdeploy_unknown(
        &self,
        operation: OperationId,
        original_attempt: AttemptId,
        expected_revision: i64,
        evidence_event: EventId,
        workflow_sha256: &str,
    ) -> Result<crate::OsDeployProgress, Error> {
        Box::pin(async move {
            let mut tx=self.store.pool().begin().await?;
            authority(self,&mut tx).await?;
            let snapshot=Box::pin(locked_execution(&mut tx,operation)).await?;
            admit(&snapshot,workflow_sha256)?;
            if snapshot.attempt_id()!=Some(original_attempt) { return Err(Error::FenceLost); }
            if let Some(row)=sqlx::query("SELECT payload_canonical_json FROM rust_controller.osdeploy_decisions WHERE operation_id=$1 AND action='pve_evaluated' AND payload_canonical_json::jsonb->'detail'->>'mode'='reconciliation' AND payload_canonical_json::jsonb->'detail'->>'evidence_event_id'=$2").bind(operation.as_uuid()).bind(evidence_event.as_uuid().to_string()).fetch_optional(&mut *tx).await? {
                let prior=wire::DecisionEnvelope::decode(&row.try_get::<String,_>("payload_canonical_json")?)?;
                if prior.before_revision!=expected_revision { return Err(Error::Conflict); }
                let state=decision_state(prior.resolution.ok_or(Error::Validation)?)?;
                tx.commit().await?;
                return Ok(crate::OsDeployProgress::Decided(state));
            }
            if snapshot.state()!=ExecutionState::Unknown || snapshot.revision()!=expected_revision { return Err(Error::FenceLost); }
            let lease:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.worker_leases WHERE operation_id=$1)").bind(operation.as_uuid()).fetch_one(&mut *tx).await?;
            if lease { return Err(Error::FenceLost); }
            let dispatch=snapshot.dispatch().ok_or(Error::Validation)?;
            if now(&mut tx).await?>=snapshot.deadline_at().ok_or(Error::Validation)? { return Err(Error::FenceLost); }
            if task_receipt_required(dispatch) && snapshot.receipt().is_none() { tx.commit().await?; return Ok(crate::OsDeployProgress::Idle); }
            let (context,evidence)=Box::pin(crate::osdeploy::execution::history::indexed_context(&mut tx,operation,expected_revision,evidence_event,pve_port::ProvisioningEvaluationModeV1::Reconciliation)).await?;
            let proof=Box::pin(OsDeployTransitionProof::original_dispatch_reconciliation(self,&mut tx,operation,original_attempt,expected_revision,evidence_event)).await?;
            append_osdeploy_transition(&mut tx,proof).await?;
            settle_selected(&mut tx,&snapshot).await?;
            let selected=sqlx::query("SELECT payload_canonical_json FROM rust_controller.osdeploy_decisions WHERE operation_id=$1 AND action='pve_evaluated' ORDER BY decision_revision DESC LIMIT 1").bind(operation.as_uuid()).fetch_one(&mut *tx).await?;
            let selected=wire::DecisionEnvelope::decode(&selected.try_get::<String,_>("payload_canonical_json")?)?;
            let wire::Detail::PveEvaluated(value)=&selected.detail else { return Err(Error::Validation); };
            let target=decision_state(selected.resolution.ok_or(Error::Validation)?)?;
            let updated=Box::pin(load::load_execution(&mut tx,operation)).await?;
            if target==ExecutionState::Unknown { restore_unknown_schedule(self,&mut tx,&updated,Some(value.reason)).await?; }
            Box::pin(load::load_execution(&mut tx,operation)).await?;
            let final_at=now(&mut tx).await?;
            if final_at>=snapshot.deadline_at().ok_or(Error::Validation)? { return Err(Error::FenceLost); }
            let advice=pve_port::evaluate_provisioning_outcome(&context,&evidence,final_at);
            if advice.decision!=value.advice || advice.reason!=value.reason { return Err(Error::FenceLost); }
            tx.commit().await?;
            Ok(crate::OsDeployProgress::Decided(target))
        }).await
    }
    pub async fn reap_osdeploy_expired(&self) -> Result<OsDeployMaintenanceSummary, Error> {
        Box::pin(async move {
            let rows = sqlx::query("SELECT l.operation_id,l.attempt_id,encode(sha256(convert_to(l.lease_token,'UTF8')),'hex') AS token_hash FROM rust_controller.worker_leases l JOIN rust_controller.operations o USING(operation_id) JOIN rust_controller.osdeploy_operation_plans p USING(operation_id) WHERE o.workflow_kind='os_deploy' AND (l.lease_expires_at<=clock_timestamp() OR o.state IN ('satisfied','failed','blocked','unknown','conflicted')) ORDER BY l.lease_expires_at,l.operation_id LIMIT 32")
                .fetch_all(self.store.pool()).await?;
            let mut result = OsDeployMaintenanceSummary::default();
            for row in rows {
                result.examined += 1;
                let op = load::id(row.try_get("operation_id")?)?;
                let expected_attempt: Uuid = row.try_get("attempt_id")?;
                let expected_hash: String = row.try_get("token_hash")?;
                match Box::pin(reap_one(self,op,expected_attempt,&expected_hash)).await {
                    Ok(true) => result.changed += 1,
                    Ok(false) => {},
                    Err(Error::Validation | Error::Conflict | Error::FenceLost | Error::CapabilityUnavailable) => result.rejected += 1,
                    Err(error) => return Err(error),
                }
            }
            Ok(result)
        }).await
    }

    pub async fn cancel_osdeploy_run(&self, run: RunId) -> Result<(), Error> {
        Box::pin(async move {
            let mut tx = self.store.pool().begin().await?;
            authority(self, &mut tx).await?;
            let anchor: Uuid = sqlx::query_scalar("SELECT operation_id FROM rust_controller.osdeploy_operation_plans WHERE run_id=$1 AND stage='clone'")
                .bind(run.as_uuid()).fetch_optional(&mut *tx).await?.ok_or(Error::Validation)?;
            let anchor = load::id(anchor)?;
            let first = Box::pin(locked_execution(&mut tx, anchor)).await?;
            if first.cancelled() { tx.commit().await?; return Ok(()); }
            let registration = load::load_registration(&mut tx, run).await?;
            let mut operations: Vec<_> = OsDeployStage::ALL.into_iter().map(|stage| registration.ids().operation(stage)).collect();
            operations.sort_by_key(|op| op.as_uuid());
            // Select already elapsed scopes before this transaction's fence.
            // Inherited expiry never bypasses a preexisting cancellation.
            for &op in &operations {
                let snapshot = Box::pin(load::load_execution(&mut tx, op)).await?;
                if snapshot.state().is_terminal() { continue; }
                let deadline: Option<DateTime<Utc>> = sqlx::query_scalar("SELECT deadline_at FROM rust_controller.osdeploy_deadlines WHERE run_id=$1 AND scope_key=$2")
                    .bind(run.as_uuid()).bind(scope_name(load::stage_scope(snapshot.plan().stage()))?).fetch_optional(&mut *tx).await?;
                if let Some(deadline) = deadline
                    && now(&mut tx).await? >= deadline {
                        let proof = if snapshot.attempt_id().is_some() {
                            OsDeployTransitionProof::activated_scope_expired(self, &mut tx, op, snapshot.revision()).await?
                        } else {
                            OsDeployTransitionProof::unactivated_scope_expired(self, &mut tx, op, snapshot.revision()).await?
                        };
                        append_osdeploy_transition(&mut tx, proof).await?;
                        settle_selected(&mut tx, &snapshot).await?;
                }
            }
            let snapshot = Box::pin(load::load_execution(&mut tx, anchor)).await?;
            let at = now(&mut tx).await?;
            let event = EventId::new();
            let decision = wire::DecisionEnvelope {
                contract_version: 1, run_id: run, operation_id: anchor,
                workflow_sha256: snapshot.plan().workflow_sha256().to_owned(),
                stage_sha256: snapshot.plan().fingerprint()?, attempt_id: snapshot.attempt_id(),
                generation: self.generation, before_revision: snapshot.revision(), evaluated_at: at,
                resolution: None, detail: wire::Detail::RunCancelled(wire::RunCancelled { reason: wire::Reason::RunCancellationRequested }),
            };
            append_osdeploy_decision(&mut tx, event, "osdeploy:run-cancelled", &decision).await?;
            sqlx::query("INSERT INTO rust_controller.osdeploy_run_cancellations(run_id,anchor_operation_id,decision_event_id,generation,requested_at) VALUES($1,$2,$3,$4,$5)")
                .bind(run.as_uuid()).bind(anchor.as_uuid()).bind(event.as_uuid()).bind(self.generation).bind(at).execute(&mut *tx).await?;
            for &op in &operations {
                let snapshot = Box::pin(load::load_execution(&mut tx, op)).await?;
                if snapshot.state().is_terminal() { terminal_cleanup(self,&mut tx,&snapshot).await?; continue; }
                let proof = if snapshot.dispatch().is_some() {
                    OsDeployTransitionProof::cancelled_exposed(self, &mut tx, op, snapshot.revision()).await?
                } else {
                    OsDeployTransitionProof::cancelled_unexposed(self, &mut tx, op, snapshot.revision()).await?
                };
                append_osdeploy_transition(&mut tx, proof).await?;
                settle_selected(&mut tx, &snapshot).await?;
            }
            for op in operations { Box::pin(load::load_execution(&mut tx, op)).await?; }
            tx.commit().await?;
            Ok(())
        }).await
    }
}

async fn reap_one(
    s: &Scheduler,
    op: OperationId,
    expected_attempt: Uuid,
    expected_hash: &str,
) -> Result<bool, Error> {
    let mut tx = s.store.pool().begin().await?;
    authority(s, &mut tx).await?;
    let snapshot = Box::pin(locked_execution(&mut tx, op)).await?;
    let lease = sqlx::query("SELECT l.attempt_id,l.lease_expires_at,encode(sha256(convert_to(l.lease_token,'UTF8')),'hex') AS token_hash FROM rust_controller.worker_leases l WHERE operation_id=$1").bind(op.as_uuid()).fetch_optional(&mut *tx).await?;
    let Some(lease) = lease else {
        tx.commit().await?;
        return Ok(false);
    };
    if lease.try_get::<Uuid, _>("attempt_id")? != expected_attempt
        || lease.try_get::<String, _>("token_hash")? != expected_hash
    {
        tx.commit().await?;
        return Ok(false);
    }
    if snapshot.state().is_terminal() {
        let changed = terminal_cleanup(s, &mut tx, &snapshot).await?;
        Box::pin(load::load_execution(&mut tx, op)).await?;
        tx.commit().await?;
        return Ok(changed);
    }
    if snapshot.cancelled() {
        return Err(Error::FenceLost);
    }
    let at = now(&mut tx).await?;
    if lease.try_get::<DateTime<Utc>, _>("lease_expires_at")? > at {
        tx.commit().await?;
        return Ok(false);
    }
    let proof = if at >= snapshot.deadline_at().ok_or(Error::Validation)? {
        OsDeployTransitionProof::activated_scope_expired(s, &mut tx, op, snapshot.revision())
            .await?
    } else if snapshot.dispatch().is_some() {
        OsDeployTransitionProof::expired_dispatched(s, &mut tx, op, snapshot.revision()).await?
    } else if snapshot.state() == ExecutionState::Leased {
        OsDeployTransitionProof::expired_unstarted_same_attempt(s, &mut tx, op, snapshot.revision())
            .await?
    } else {
        OsDeployTransitionProof::expired_read_only_evaluation(s, &mut tx, op, snapshot.revision())
            .await?
    };
    append_osdeploy_transition(&mut tx, proof).await?;
    settle_selected(&mut tx, &snapshot).await?;
    let updated = Box::pin(load::load_execution(&mut tx, op)).await?;
    if updated.state() == ExecutionState::Waiting {
        restore_waiting_projection(&mut tx, &updated).await?;
    }
    if updated.state() == ExecutionState::Unknown {
        restore_unknown_schedule(s, &mut tx, &updated, None).await?;
    }
    Box::pin(load::load_execution(&mut tx, op)).await?;
    if matches!(
        updated.state(),
        ExecutionState::Pending | ExecutionState::Waiting
    ) && now(&mut tx).await? >= updated.deadline_at().ok_or(Error::Validation)?
    {
        return Err(Error::FenceLost);
    }
    tx.commit().await?;
    Ok(true)
}

fn task_receipt_required(dispatch: &pve_port::ProvisioningDispatchV1) -> bool {
    !matches!(
        dispatch.request().plan().action(),
        pve_port::ProvisioningActionV1::ConfigurePe | pve_port::ProvisioningActionV1::ConfigureDisk
    )
}

fn decision_state(decision: pve_port::NativeDecision) -> Result<ExecutionState, Error> {
    Ok(match decision {
        pve_port::NativeDecision::Satisfied => ExecutionState::Satisfied,
        pve_port::NativeDecision::Failed => ExecutionState::Failed,
        pve_port::NativeDecision::Conflicted => ExecutionState::Conflicted,
        pve_port::NativeDecision::Unknown => ExecutionState::Unknown,
        _ => return Err(Error::Validation),
    })
}

pub(super) async fn restore_unknown_schedule(
    s: &Scheduler,
    tx: &mut Transaction<'_, Postgres>,
    snapshot: &OsDeployOperationSnapshot,
    observation: Option<pve_port::ProvisioningReasonV1>,
) -> Result<(), Error> {
    if snapshot.cancelled() || snapshot.state() != ExecutionState::Unknown {
        return Err(Error::FenceLost);
    }
    let Some(dispatch) = snapshot.dispatch() else {
        return Ok(());
    };
    let deadline = snapshot.deadline_at().ok_or(Error::Validation)?;
    let at = now(tx).await?;
    if at >= deadline {
        return Ok(());
    }
    let selected=sqlx::query("SELECT event_id,payload_canonical_json FROM rust_controller.osdeploy_decisions WHERE operation_id=$1 AND resolution IS NOT NULL AND resolution<>'ready' ORDER BY decision_revision DESC LIMIT 1").bind(snapshot.operation_id().as_uuid()).fetch_one(&mut **tx).await?;
    let terminal: EventId = load::id(selected.try_get("event_id")?)?;
    let selected =
        wire::DecisionEnvelope::decode(&selected.try_get::<String, _>("payload_canonical_json")?)?;
    if matches!(
        selected.detail,
        wire::Detail::ActivatedScopeExpired(_)
            | wire::Detail::ScopeExpiredBeforeActivation(_)
            | wire::Detail::StageCancelledExposed(_)
    ) {
        return Ok(());
    }
    wire::require(selected.resolution == Some(pve_port::NativeDecision::Unknown))?;
    let missing = task_receipt_required(dispatch) && snapshot.receipt().is_none();
    let prior=sqlx::query("SELECT event_id,payload_canonical_json FROM rust_controller.osdeploy_decisions WHERE operation_id=$1 AND action='reconciliation_scheduled' ORDER BY decision_revision DESC LIMIT 1").bind(snapshot.operation_id().as_uuid()).fetch_optional(&mut **tx).await?;
    let mut previous_count = 0;
    let mut receipt_arrived = false;
    if let Some(prior) = prior {
        let event = load::id(prior.try_get("event_id")?)?;
        let value =
            wire::DecisionEnvelope::decode(&prior.try_get::<String, _>("payload_canonical_json")?)?;
        let wire::Detail::ReconciliationScheduled(prior) = &value.detail else {
            return Err(Error::Validation);
        };
        previous_count = prior.schedule.unavailable_count;
        receipt_arrived = prior.schedule.next_check_at.is_none() && !missing;
        if prior.terminal_decision_event_id == terminal && !receipt_arrived && observation.is_none()
        {
            return write_schedule(tx, snapshot, event, &value, &prior.schedule).await;
        }
    }
    let observation = observation.or({
        if let wire::Detail::PveEvaluated(value) = &selected.detail {
            Some(value.reason)
        } else {
            None
        }
    });
    let unavailable = observation == Some(pve_port::ProvisioningReasonV1::ObservationUnavailable);
    let count = if unavailable {
        (previous_count + 1).min(4)
    } else {
        0
    };
    let delay = if unavailable {
        [2, 4, 8, 10][usize::from(count - 1)]
    } else {
        5
    };
    let schedule = wire::Schedule {
        mode: wire::ScheduleMode::UnknownReconciliation,
        next_check_at: (!missing).then_some((at + chrono::Duration::seconds(delay)).min(deadline)),
        unavailable_count: count,
    };
    let dispatch_event:Uuid=sqlx::query_scalar("SELECT dispatch_event_id FROM rust_controller.osdeploy_pve_dispatches WHERE operation_id=$1").bind(snapshot.operation_id().as_uuid()).fetch_one(&mut **tx).await?;
    let value = envelope(
        snapshot,
        snapshot.attempt_id().ok_or(Error::Validation)?,
        s.generation,
        snapshot.revision(),
        at,
        wire::Detail::ReconciliationScheduled(wire::ReconciliationSchedule {
            terminal_decision_event_id: terminal,
            dispatch_event_id: load::id(dispatch_event)?,
            scope_key: load::stage_scope(snapshot.plan().stage()),
            deadline_at: deadline,
            reason: if receipt_arrived {
                wire::Reason::OriginalReceiptCaptured
            } else if unavailable {
                wire::Reason::ObservationUnavailable
            } else if observation.is_some() {
                wire::Reason::ReconciliationStillUnknown
            } else {
                wire::Reason::OriginalDispatchUnresolved
            },
            schedule: schedule.clone(),
        }),
    )?;
    let event = EventId::new();
    append_osdeploy_decision(
        tx,
        event,
        &format!(
            "osdeploy:reconcile-schedule:{}:{}",
            terminal.as_uuid(),
            snapshot.revision()
        ),
        &value,
    )
    .await?;
    let updated = Box::pin(load::load_execution(tx, snapshot.operation_id())).await?;
    write_schedule(tx, &updated, event, &value, &schedule).await
}

pub(super) async fn restore_waiting_projection(
    tx: &mut Transaction<'_, Postgres>,
    snapshot: &OsDeployOperationSnapshot,
) -> Result<(), Error> {
    let row = sqlx::query("SELECT event_id,payload_canonical_json FROM rust_controller.osdeploy_decisions WHERE operation_id=$1 AND resolution IS NOT NULL AND resolution<>'ready' ORDER BY decision_revision DESC LIMIT 1").bind(snapshot.operation_id().as_uuid()).fetch_one(&mut **tx).await?;
    let event = load::id(row.try_get("event_id")?)?;
    let value =
        wire::DecisionEnvelope::decode(&row.try_get::<String, _>("payload_canonical_json")?)?;
    let schedule = match &value.detail {
        wire::Detail::PveEvaluated(value) => value.schedule.as_ref(),
        wire::Detail::EvaluationReparked(value) => Some(&value.schedule),
        _ => None,
    }
    .ok_or(Error::Validation)?;
    write_schedule(tx, snapshot, event, &value, schedule).await
}

pub(super) async fn write_schedule(
    tx: &mut Transaction<'_, Postgres>,
    snapshot: &OsDeployOperationSnapshot,
    event: EventId,
    value: &wire::DecisionEnvelope,
    schedule: &wire::Schedule,
) -> Result<(), Error> {
    sqlx::query("INSERT INTO rust_controller.osdeploy_schedule_projection(operation_id,run_id,attempt_id,mode,basis_event_id,basis_revision,scope_key,next_check_at,unavailable_count,rebuilt_through_revision) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT(operation_id) DO UPDATE SET run_id=EXCLUDED.run_id,attempt_id=EXCLUDED.attempt_id,mode=EXCLUDED.mode,basis_event_id=EXCLUDED.basis_event_id,basis_revision=EXCLUDED.basis_revision,scope_key=EXCLUDED.scope_key,next_check_at=EXCLUDED.next_check_at,unavailable_count=EXCLUDED.unavailable_count,rebuilt_through_revision=EXCLUDED.rebuilt_through_revision WHERE (osdeploy_schedule_projection.run_id,osdeploy_schedule_projection.attempt_id,osdeploy_schedule_projection.mode,osdeploy_schedule_projection.basis_event_id,osdeploy_schedule_projection.basis_revision,osdeploy_schedule_projection.scope_key,osdeploy_schedule_projection.next_check_at,osdeploy_schedule_projection.unavailable_count,osdeploy_schedule_projection.rebuilt_through_revision) IS DISTINCT FROM (EXCLUDED.run_id,EXCLUDED.attempt_id,EXCLUDED.mode,EXCLUDED.basis_event_id,EXCLUDED.basis_revision,EXCLUDED.scope_key,EXCLUDED.next_check_at,EXCLUDED.unavailable_count,EXCLUDED.rebuilt_through_revision)")
        .bind(snapshot.operation_id().as_uuid()).bind(snapshot.run_id().as_uuid()).bind(snapshot.attempt_id().ok_or(Error::Validation)?.as_uuid())
        .bind(if schedule.mode == wire::ScheduleMode::Waiting { "waiting" } else { "unknown_reconciliation" })
        .bind(event.as_uuid()).bind(value.before_revision+1).bind(scope_name(load::stage_scope(snapshot.plan().stage()))?).bind(schedule.next_check_at).bind(i16::from(schedule.unavailable_count)).bind(snapshot.revision()).execute(&mut **tx).await?;
    Ok(())
}

pub(super) fn scope_name(scope: wire::Scope) -> Result<String, Error> {
    serde_json::to_value(scope)?
        .as_str()
        .map(str::to_owned)
        .ok_or(Error::Validation)
}

/// Apply the selected private decision to its original attempt and discard
/// only a lease already proved by the strict locked reload.
pub(super) async fn settle_selected(
    tx: &mut Transaction<'_, Postgres>,
    snapshot: &OsDeployOperationSnapshot,
) -> Result<(), Error> {
    let op = snapshot.operation_id();
    let row = sqlx::query("SELECT state FROM rust_controller.operations WHERE operation_id=$1")
        .bind(op.as_uuid())
        .fetch_one(&mut **tx)
        .await?;
    let state: ExecutionState = serde_json::from_value(json!(row.try_get::<String, _>("state")?))?;
    if let Some(attempt) = snapshot.attempt_id() {
        let completed: Option<DateTime<Utc>> = if state.is_terminal() {
            sqlx::query_scalar("SELECT observed_at FROM rust_controller.journal_events WHERE operation_id=$1 AND event_kind='execution_state_changed' ORDER BY aggregate_revision DESC LIMIT 1")
                .bind(op.as_uuid()).fetch_optional(&mut **tx).await?
        } else {
            None
        };
        sqlx::query("UPDATE rust_controller.attempts SET state=$3,completed_at=$4 WHERE operation_id=$1 AND attempt_id=$2")
            .bind(op.as_uuid()).bind(attempt.as_uuid()).bind(execution_state_name(state)).bind(completed).execute(&mut **tx).await?;
        sqlx::query(
            "DELETE FROM rust_controller.worker_leases WHERE operation_id=$1 AND attempt_id=$2",
        )
        .bind(op.as_uuid())
        .bind(attempt.as_uuid())
        .execute(&mut **tx)
        .await?;
    }
    sqlx::query("DELETE FROM rust_controller.osdeploy_schedule_projection WHERE operation_id=$1")
        .bind(op.as_uuid())
        .execute(&mut **tx)
        .await?;
    Ok(())
}

async fn terminal_cleanup(
    s: &Scheduler,
    tx: &mut Transaction<'_, Postgres>,
    snapshot: &OsDeployOperationSnapshot,
) -> Result<bool, Error> {
    wire::require(snapshot.state().is_terminal())?;
    let op = snapshot.operation_id();
    let row=sqlx::query("SELECT e.acquisition_event_id,l.attempt_id FROM rust_controller.worker_leases l JOIN rust_controller.osdeploy_lease_epochs e ON e.operation_id=l.operation_id AND e.attempt_id=l.attempt_id AND e.lease_token_sha256=encode(sha256(convert_to(l.lease_token,'UTF8')),'hex') WHERE l.operation_id=$1").bind(op.as_uuid()).fetch_optional(&mut **tx).await?;
    let mut changed = false;
    if let Some(row) = row {
        let attempt = load::id(row.try_get("attempt_id")?)?;
        wire::require(snapshot.attempt_id() == Some(attempt))?;
        let epoch: EventId = load::id(row.try_get("acquisition_event_id")?)?;
        let terminal:Uuid=sqlx::query_scalar("SELECT event_id FROM rust_controller.osdeploy_decisions WHERE operation_id=$1 AND resolution IS NOT NULL AND resolution<>'ready' ORDER BY decision_revision DESC LIMIT 1").bind(op.as_uuid()).fetch_one(&mut **tx).await?;
        let value = envelope(
            snapshot,
            attempt,
            s.generation,
            snapshot.revision(),
            now(tx).await?,
            wire::Detail::ResidualLeaseRevoked(wire::Revoked {
                terminal_decision_event_id: load::id(terminal)?,
                lease_acquisition_event_id: epoch,
                reason: wire::Reason::TerminalLeaseRevoked,
            }),
        )?;
        append_osdeploy_decision(
            tx,
            EventId::new(),
            &format!("osdeploy:revoke:{}", epoch.as_uuid()),
            &value,
        )
        .await?;
        let count = sqlx::query(
            "DELETE FROM rust_controller.worker_leases WHERE operation_id=$1 AND attempt_id=$2",
        )
        .bind(op.as_uuid())
        .bind(attempt.as_uuid())
        .execute(&mut **tx)
        .await?
        .rows_affected();
        wire::require(count == 1)?;
        changed = true;
    }
    let removed = sqlx::query(
        "DELETE FROM rust_controller.osdeploy_schedule_projection WHERE operation_id=$1",
    )
    .bind(op.as_uuid())
    .execute(&mut **tx)
    .await?
    .rows_affected();
    Ok(changed || removed > 0)
}

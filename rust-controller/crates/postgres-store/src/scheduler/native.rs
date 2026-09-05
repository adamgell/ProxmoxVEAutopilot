//! Native lifecycle capabilities. All authority is derived inside transactions.
mod context;
use super::*;
use crate::native::{records, *};
use context::*;
use controller_domain::RunId;
use pve_port::*;

impl Scheduler {
    pub(super) async fn reap_native_expired(&self) -> Result<ReapSummary, SchedulerError> {
        // Discovery takes no row locks. Each bounded candidate is rechecked
        // after authority -> run -> sorted operations -> lease locking.
        let candidates:Vec<Uuid>=sqlx::query_scalar("SELECT o.operation_id FROM rust_controller.operations o JOIN rust_controller.worker_leases l USING(operation_id) WHERE o.workflow_kind='native_pve_vm_boot' AND l.lease_expires_at<=clock_timestamp() ORDER BY o.operation_id LIMIT $1").bind(REAP_BATCH_LIMIT).fetch_all(self.store.pool()).await?;
        let mut summary = ReapSummary::default();
        for operation in candidates {
            let operation = decode_operation_id(operation)?;
            let mut tx = self.store.pool().begin().await?;
            self.lock_authority(&mut tx).await?;
            let (_, snapshot) = locked_workflow(&mut tx, operation)
                .await
                .map_err(|_| SchedulerError::PlanBindingMismatch)?;
            let exists: bool = sqlx::query_scalar(
                "SELECT EXISTS(SELECT 1 FROM rust_controller.worker_leases WHERE operation_id=$1)",
            )
            .bind(operation.as_uuid())
            .fetch_one(&mut *tx)
            .await?;
            if !exists {
                tx.commit().await?;
                continue;
            }
            let lease = lock_lease(&mut tx, operation).await?;
            if lease.active {
                tx.commit().await?;
                continue;
            }
            let attempt = decode_attempt_id(lease.attempt_id)?;
            let started:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.journal_events WHERE operation_id=$1 AND attempt_id=$2 AND event_kind='attempt_started')").bind(operation.as_uuid()).bind(attempt.as_uuid()).fetch_one(&mut *tx).await?;
            let (target, policy) = if snapshot.state == ExecutionState::Leased
                && !started
                && snapshot.dispatch.is_none()
            {
                (ExecutionState::Pending, TransitionPolicy::ExpiredUnstarted)
            } else if matches!(
                snapshot.state,
                ExecutionState::Leased
                    | ExecutionState::Running
                    | ExecutionState::Waiting
                    | ExecutionState::Cancelling
            ) {
                (ExecutionState::Unknown, TransitionPolicy::ExpiredAfterStart)
            } else {
                return Err(SchedulerError::PlanBindingMismatch);
            };
            let now: DateTime<Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
                .fetch_one(&mut *tx)
                .await?;
            append_state_event(&mut tx,StateAppend{operation_id:operation,attempt_id:Some(attempt),revision:snapshot.revision,current:snapshot.state,target,semantic_key:format!("scheduler:lease-expired:{}",attempt.as_uuid()),payload:json!({"reason":"native_lease_expired","state":execution_state_name(target)}),observed_at:now,policy}).await?;
            sqlx::query("UPDATE rust_controller.attempts SET state='unknown',completed_at=clock_timestamp() WHERE operation_id=$1 AND attempt_id=$2").bind(operation.as_uuid()).bind(attempt.as_uuid()).execute(&mut *tx).await?;
            sqlx::query("DELETE FROM rust_controller.worker_leases WHERE operation_id=$1")
                .bind(operation.as_uuid())
                .execute(&mut *tx)
                .await?;
            tx.commit().await?;
            if target == ExecutionState::Pending {
                summary.reset_to_pending += 1
            } else {
                summary.marked_unknown += 1
            }
        }
        Ok(summary)
    }
    pub async fn claim_native_bound(
        &self,
        operation: OperationId,
        fingerprint: &str,
        cap: u32,
    ) -> Result<Option<LeaseGrant>, NativeStoreError> {
        if cap == 0 {
            return Err(NativeStoreError::Validation);
        }
        let mut tx = self.store.pool().begin().await?;
        self.lock_authority(&mut tx).await?;
        let run = operation_run(&mut tx, operation).await?;
        lock_run(&mut tx, run).await?;
        sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended('rust-controller:scheduler:native_pve_vm_boot',0))").execute(&mut *tx).await?;
        lock_workflow(&mut tx, run).await?;
        let snapshot = records::load(&mut tx, operation).await?;
        if snapshot.cancelled {
            return Err(NativeStoreError::Cancelled);
        }
        if digest(&snapshot.plan)? != fingerprint {
            return Err(NativeStoreError::FenceMismatch);
        }
        if snapshot.state != ExecutionState::Pending
            || snapshot.dispatch.is_some()
            || snapshot
                .predecessor_state
                .is_some_and(|s| s != ExecutionState::Satisfied)
        {
            tx.commit().await?;
            return Ok(None);
        }
        let active:i64=sqlx::query_scalar("SELECT count(*) FROM rust_controller.worker_leases l JOIN rust_controller.operations o USING(operation_id) WHERE o.workflow_kind='native_pve_vm_boot' AND l.executor_kind=$1 AND l.generation=$2 AND l.lease_expires_at>clock_timestamp()")
            .bind(self.executor_kind.as_str()).bind(self.generation).fetch_one(&mut *tx).await?;
        if active >= i64::from(cap) {
            tx.commit().await?;
            return Ok(None);
        }
        let grant = self
            .claim_operation_tx(&mut tx, operation, snapshot.revision)
            .await?;
        tx.commit().await?;
        Ok(Some(grant))
    }
    pub async fn start_native_bound(
        &self,
        grant: &LeaseGrant,
        fingerprint: &str,
    ) -> Result<ExecutionState, NativeStoreError> {
        let mut tx = self.store.pool().begin().await?;
        self.lock_authority(&mut tx).await?;
        self.validate_grant_owner(grant)?;
        let (_, snapshot) = locked_workflow(&mut tx, grant.operation_id()).await?;
        if snapshot.cancelled {
            return Err(NativeStoreError::Cancelled);
        }
        let lease = lock_lease(&mut tx, grant.operation_id()).await?;
        validate_persisted_lease(grant, &lease)?;
        if !lease.active
            || snapshot.state != ExecutionState::Leased
            || lease.attempt_state != ExecutionState::Leased
            || snapshot
                .predecessor_state
                .is_some_and(|s| s != ExecutionState::Satisfied)
            || digest(&snapshot.plan)? != fingerprint
        {
            return Err(NativeStoreError::FenceMismatch);
        }
        self.start_operation_tx(&mut tx, grant, snapshot.revision)
            .await?;
        tx.commit().await?;
        Ok(ExecutionState::Running)
    }
    pub async fn begin_native_dispatch(
        &self,
        grant: &LeaseGrant,
        expected_revision: i64,
        preflight_event: EventId,
        request_hash: &str,
        request_marker: Uuid,
    ) -> Result<NativeDispatchPermit, NativeStoreError> {
        let mut tx = self.store.pool().begin().await?;
        self.lock_authority(&mut tx).await?;
        self.validate_grant_owner(grant)?;
        let (ids, snapshot) = locked_workflow(&mut tx, grant.operation_id()).await?;
        if snapshot.cancelled {
            return Err(NativeStoreError::Cancelled);
        }
        if snapshot.dispatch.is_some() {
            return Err(NativeStoreError::AlreadyDispatched);
        }
        let lease = lock_lease(&mut tx, grant.operation_id()).await?;
        validate_persisted_lease(grant, &lease)?;
        if !lease.active
            || snapshot.state != ExecutionState::Running
            || lease.attempt_state != snapshot.state
            || snapshot
                .predecessor_state
                .is_some_and(|s| s != ExecutionState::Satisfied)
            || request_marker.is_nil()
        {
            return Err(NativeStoreError::FenceMismatch);
        }
        let evidence = evidence_for(
            &mut tx,
            &snapshot,
            grant.attempt_id(),
            preflight_event,
            expected_revision,
        )
        .await?;
        let context = build_context(
            &mut tx,
            &ids,
            &snapshot,
            &evidence,
            NativeEvaluationMode::Preflight,
        )
        .await?;
        let now = db_now(&mut tx).await?;
        let evaluation = evaluate_native_preflight(&context, &evidence, now);
        if evaluation.decision != NativeDecision::Ready {
            return Err(if evaluation.reason == NativeReason::ObservationNotFresh {
                NativeStoreError::StaleEvidence
            } else {
                NativeStoreError::Validation
            });
        }
        let actual_hash = request_digest(
            &context,
            &evidence,
            grant.operation_id(),
            request_marker,
            now,
        )?;
        if actual_hash != request_hash {
            return Err(NativeStoreError::FenceMismatch);
        }
        let (_,revision)=append_decision_event(&mut tx,&snapshot,Some(grant.attempt_id()),json!({"action":"begin_native_dispatch","contract_version":1,"generation":self.generation,"request_digest":actual_hash,"request_marker":request_marker,"preflight_event_id":preflight_event}),now).await?;
        let dispatched_at:DateTime<Utc>=sqlx::query_scalar("INSERT INTO rust_controller.native_dispatches(operation_id,attempt_id,plan_digest,generation,dispatch_revision,request_digest,request_marker,preflight_event_id) VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING dispatched_at")
            .bind(grant.operation_id().as_uuid()).bind(grant.attempt_id().as_uuid()).bind(digest(&snapshot.plan)?).bind(self.generation).bind(revision).bind(&actual_hash).bind(request_marker).bind(preflight_event.as_uuid()).fetch_one(&mut *tx).await?;
        let dispatch = NativeDispatch {
            operation_id: grant.operation_id(),
            attempt_id: grant.attempt_id(),
            plan_digest: digest(&snapshot.plan)?,
            generation: self.generation,
            revision,
            request_digest: actual_hash,
            request_marker,
            preflight_event_id: preflight_event,
            dispatched_at,
        };
        // Durable writes can block after the first clock sample. Recheck the
        // held lease and the same bound facts immediately before commit; a
        // timeout rolls the dispatch, journal, projection and outbox back.
        let lease = lock_lease(&mut tx, grant.operation_id()).await?;
        validate_persisted_lease(grant, &lease)?;
        if !lease.active {
            return Err(NativeStoreError::FenceMismatch);
        }
        let commit_clock = db_now(&mut tx).await?;
        if evaluate_native_preflight(&context, &evidence, commit_clock).decision
            != NativeDecision::Ready
        {
            return Err(NativeStoreError::StaleEvidence);
        }
        tx.commit().await?;
        Ok(NativeDispatchPermit { dispatch })
    }
    pub async fn record_native_receipt(
        &self,
        permit: &NativeDispatchPermit,
        receipt: &MutationReceipt,
    ) -> Result<(), NativeStoreError> {
        // Receipt capture is non-authoritative evidence: an old worker may
        // preserve its exact committed dispatch response after expiry/handoff.
        let mut tx = self.store.pool().begin().await?;
        // Fence row is shared to preserve global lock order, but capturing a
        // receipt is allowed from the original dispatch generation.
        sqlx::query("SELECT singleton_key FROM rust_controller.orchestration_authority WHERE singleton_key=1 FOR SHARE").fetch_one(&mut *tx).await?;
        let (_, snapshot) = locked_workflow(&mut tx, permit.dispatch.operation_id).await?;
        let d = snapshot
            .dispatch
            .as_ref()
            .ok_or(NativeStoreError::FenceMismatch)?;
        let supplied = &permit.dispatch;
        if d.attempt_id != supplied.attempt_id
            || d.plan_digest != supplied.plan_digest
            || d.generation != supplied.generation
            || d.revision != supplied.revision
            || d.request_digest != supplied.request_digest
            || d.request_marker != supplied.request_marker
            || d.preflight_event_id != supplied.preflight_event_id
            || d.dispatched_at != supplied.dispatched_at
        {
            return Err(NativeStoreError::FenceMismatch);
        }
        records::validate_receipt(&snapshot.plan, receipt)?;
        if let Some(existing) = snapshot.receipt {
            if &existing.receipt != receipt {
                return Err(NativeStoreError::Conflict);
            }
            tx.commit().await?;
            return Ok(());
        }
        let (kind, upid) = match receipt {
            MutationReceipt::Task(upid) => ("task", Some(upid.to_string())),
            MutationReceipt::SynchronousAccepted => ("synchronous", None),
        };
        sqlx::query("INSERT INTO rust_controller.native_receipts(operation_id,receipt_kind,upid) VALUES($1,$2,$3)").bind(d.operation_id.as_uuid()).bind(kind).bind(upid).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(())
    }
    pub async fn decide_native(
        &self,
        grant: &LeaseGrant,
        event: EventId,
        expected_revision: i64,
    ) -> Result<ExecutionState, NativeStoreError> {
        let mut tx = self.store.pool().begin().await?;
        self.lock_authority(&mut tx).await?;
        self.validate_grant_owner(grant)?;
        let (ids, snapshot) = locked_workflow(&mut tx, grant.operation_id()).await?;
        let lease = lock_lease(&mut tx, grant.operation_id()).await?;
        validate_persisted_lease(grant, &lease)?;
        if !lease.active
            || !matches!(
                snapshot.state,
                ExecutionState::Running | ExecutionState::Waiting | ExecutionState::Cancelling
            )
            || lease.attempt_state != snapshot.state
        {
            return Err(NativeStoreError::FenceMismatch);
        }
        let evidence = evidence_for(
            &mut tx,
            &snapshot,
            grant.attempt_id(),
            event,
            expected_revision,
        )
        .await?;
        let mode = if snapshot.dispatch.is_some() {
            NativeEvaluationMode::Outcome
        } else {
            NativeEvaluationMode::Preflight
        };
        let context = build_context(&mut tx, &ids, &snapshot, &evidence, mode).await?;
        let now = db_now(&mut tx).await?;
        let evaluation = if snapshot.cancelled {
            NativeEvaluation {
                decision: NativeDecision::Unknown,
                reason: NativeReason::Cancelled,
            }
        } else if snapshot.dispatch.is_some() {
            evaluate_native_outcome(&context, &evidence, now)
        } else {
            evaluate_native_preflight(&context, &evidence, now)
        };
        let result = self
            .persist_native_evaluation(
                &mut tx,
                &snapshot,
                grant.attempt_id(),
                event,
                evaluation,
                now,
                false,
            )
            .await?;
        tx.commit().await?;
        Ok(result)
    }
    pub async fn reconcile_native_unknown(
        &self,
        operation: OperationId,
        attempt: AttemptId,
        event: EventId,
        expected_revision: i64,
        fingerprint: &str,
    ) -> Result<ExecutionState, NativeStoreError> {
        let mut tx = self.store.pool().begin().await?;
        self.lock_authority(&mut tx).await?;
        let (ids, snapshot) = locked_workflow(&mut tx, operation).await?;
        let active:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.worker_leases WHERE operation_id=$1 AND lease_expires_at>clock_timestamp() AND deadline_at>clock_timestamp())").bind(operation.as_uuid()).fetch_one(&mut *tx).await?;
        if active
            || snapshot.state != ExecutionState::Unknown
            || snapshot.attempt_state != Some(ExecutionState::Unknown)
            || snapshot.attempt_id != Some(attempt)
            || digest(&snapshot.plan)? != fingerprint
            || snapshot
                .dispatch
                .as_ref()
                .is_none_or(|d| d.attempt_id != attempt)
        {
            return Err(NativeStoreError::FenceMismatch);
        }
        let evidence = evidence_for(&mut tx, &snapshot, attempt, event, expected_revision).await?;
        let context = build_context(
            &mut tx,
            &ids,
            &snapshot,
            &evidence,
            NativeEvaluationMode::Reconciliation,
        )
        .await?;
        let now = db_now(&mut tx).await?;
        let evaluation = evaluate_native_outcome(&context, &evidence, now);
        // A running original task is observation advice during recovery, not
        // authorization to restore active Waiting or acquire a new lease.
        if matches!(
            evaluation.decision,
            NativeDecision::Unknown | NativeDecision::Waiting
        ) {
            tx.commit().await?;
            return Ok(ExecutionState::Unknown);
        }
        if !matches!(
            evaluation.decision,
            NativeDecision::Satisfied | NativeDecision::Failed | NativeDecision::Conflicted
        ) {
            return Err(NativeStoreError::Validation);
        }
        let result = self
            .persist_native_evaluation(&mut tx, &snapshot, attempt, event, evaluation, now, true)
            .await?;
        tx.commit().await?;
        Ok(result)
    }
    pub async fn cancel_native_run(&self, run: RunId) -> Result<(), NativeStoreError> {
        let mut tx = self.store.pool().begin().await?;
        self.lock_authority(&mut tx).await?;
        lock_run(&mut tx, run).await?;
        let ids = lock_workflow(&mut tx, run).await?;
        let mut clone = records::load(&mut tx, ids.clone_id()).await?;
        if clone.cancelled {
            tx.commit().await?;
            return Ok(());
        }
        let now = db_now(&mut tx).await?;
        let (event, revision) = append_decision_event(
            &mut tx,
            &clone,
            clone.attempt_id,
            json!({"action":"cancel_native_run","contract_version":1}),
            now,
        )
        .await?;
        clone.revision = revision;
        sqlx::query("INSERT INTO rust_controller.native_run_cancellations(run_id,clone_operation_id,decision_event_id,generation) VALUES($1,$2,$3,$4)").bind(run.as_uuid()).bind(ids.clone_id().as_uuid()).bind(event.as_uuid()).bind(self.generation).execute(&mut *tx).await?;
        for operation in ids.operations() {
            let snapshot = if operation == clone.operation_id {
                clone.clone()
            } else {
                records::load(&mut tx, operation).await?
            };
            if matches!(
                snapshot.state,
                ExecutionState::Leased | ExecutionState::Running | ExecutionState::Waiting
            ) {
                let lease = lock_lease(&mut tx, operation).await?;
                let attempt = decode_attempt_id(lease.attempt_id)?;
                append_state_event(
                    &mut tx,
                    StateAppend {
                        operation_id: operation,
                        attempt_id: Some(attempt),
                        revision: snapshot.revision,
                        current: snapshot.state,
                        target: ExecutionState::Cancelling,
                        semantic_key: format!("native:cancel:{}", run.as_uuid()),
                        payload: json!({"reason":"cancellation_requested","state":"cancelling"}),
                        observed_at: now,
                        policy: if snapshot.state == ExecutionState::Leased {
                            TransitionPolicy::Cancellation
                        } else {
                            TransitionPolicy::Domain
                        },
                    },
                )
                .await?;
                sqlx::query("UPDATE rust_controller.attempts SET state='cancelling' WHERE attempt_id=$1 AND operation_id=$2").bind(attempt.as_uuid()).bind(operation.as_uuid()).execute(&mut *tx).await?;
            }
        }
        tx.commit().await?;
        Ok(())
    }
    #[allow(clippy::too_many_arguments)]
    async fn persist_native_evaluation(
        &self,
        tx: &mut Transaction<'_, Postgres>,
        snapshot: &NativeOperationSnapshot,
        attempt: AttemptId,
        event: EventId,
        evaluation: NativeEvaluation,
        now: DateTime<Utc>,
        reconciliation: bool,
    ) -> Result<ExecutionState, NativeStoreError> {
        let target = match evaluation.decision {
            NativeDecision::Ready => return Err(NativeStoreError::Validation),
            NativeDecision::Waiting => ExecutionState::Waiting,
            NativeDecision::Satisfied => ExecutionState::Satisfied,
            NativeDecision::Failed => ExecutionState::Failed,
            NativeDecision::Blocked => ExecutionState::Blocked,
            NativeDecision::Unknown => ExecutionState::Unknown,
            NativeDecision::Conflicted => ExecutionState::Conflicted,
        };
        if snapshot.dispatch.is_none()
            && target == ExecutionState::Satisfied
            && (snapshot.plan.step() == NativeStep::Clone
                || evaluation.reason != NativeReason::AlreadySatisfied)
        {
            return Err(NativeStoreError::Validation);
        }
        let (_,revision)=append_decision_event(tx,snapshot,Some(attempt),json!({"action":"native_evaluation","contract_version":1,"evaluation":evaluation,"evidence_event_id":event,"plan_digest":digest(&snapshot.plan)?,"generation":self.generation,"dispatch_present":snapshot.dispatch.is_some(),"dispatch_generation":snapshot.dispatch.as_ref().map(|d|d.generation),"reconciliation":reconciliation}),now).await?;
        if target != ExecutionState::Waiting {
            sqlx::query("INSERT INTO rust_controller.native_decisions(operation_id,decision_revision,attempt_id,evidence_event_id,plan_digest,generation,decision,reason) VALUES($1,$2,$3,$4,$5,$6,$7,$8)").bind(snapshot.operation_id.as_uuid()).bind(revision).bind(attempt.as_uuid()).bind(event.as_uuid()).bind(digest(&snapshot.plan)?).bind(self.generation).bind(execution_state_name(target)).bind(serde_json::to_value(evaluation.reason)?.as_str().ok_or(NativeStoreError::Validation)?).execute(&mut **tx).await?;
        }
        if target != snapshot.state {
            append_state_event(
                tx,
                StateAppend {
                    operation_id: snapshot.operation_id,
                    attempt_id: Some(attempt),
                    revision,
                    current: snapshot.state,
                    target,
                    semantic_key: format!("native:state:{revision}"),
                    payload: json!({"state":execution_state_name(target)}),
                    observed_at: now,
                    policy: if reconciliation {
                        TransitionPolicy::NativeReconciliation
                    } else if snapshot.state == ExecutionState::Cancelling {
                        TransitionPolicy::CancellationUnknown
                    } else {
                        TransitionPolicy::Domain
                    },
                },
            )
            .await?;
        }
        sqlx::query("UPDATE rust_controller.attempts SET state=$3,completed_at=CASE WHEN $4 THEN clock_timestamp() ELSE NULL END WHERE operation_id=$1 AND attempt_id=$2").bind(snapshot.operation_id.as_uuid()).bind(attempt.as_uuid()).bind(execution_state_name(target)).bind(target.is_terminal()).execute(&mut **tx).await?;
        if target.is_terminal() {
            sqlx::query("DELETE FROM rust_controller.worker_leases WHERE operation_id=$1")
                .bind(snapshot.operation_id.as_uuid())
                .execute(&mut **tx)
                .await?;
        }
        Ok(target)
    }
}
async fn db_now(tx: &mut Transaction<'_, Postgres>) -> Result<DateTime<Utc>, NativeStoreError> {
    Ok(sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut **tx)
        .await?)
}
async fn append_decision_event(
    tx: &mut Transaction<'_, Postgres>,
    snapshot: &NativeOperationSnapshot,
    attempt: Option<AttemptId>,
    payload: serde_json::Value,
    now: DateTime<Utc>,
) -> Result<(EventId, i64), NativeStoreError> {
    let revision = snapshot
        .revision
        .checked_add(1)
        .ok_or(NativeStoreError::Validation)?;
    let event = JournalEvent::new(
        EventId::new(),
        snapshot.operation_id,
        attempt,
        revision,
        format!("native:decision:{revision}"),
        digest(&payload)?,
        EventKind::DecisionRecorded,
        payload,
        now,
    )
    .map_err(|_| NativeStoreError::Validation)?;
    sqlx::query("INSERT INTO rust_controller.journal_events(event_id,operation_id,attempt_id,aggregate_revision,semantic_key,payload_digest,event_kind,execution_state,payload,observed_at) VALUES($1,$2,$3,$4,$5,$6,'decision_recorded',NULL,$7,$8)").bind(event.event_id().as_uuid()).bind(snapshot.operation_id.as_uuid()).bind(attempt.map(|a|a.as_uuid())).bind(revision).bind(event.semantic_key()).bind(event.payload_digest()).bind(event.payload()).bind(now).execute(&mut **tx).await?;
    sqlx::query("INSERT INTO rust_controller.outbox(event_id,operation_id,topic,payload) VALUES($1,$2,'journal_event',$3)").bind(event.event_id().as_uuid()).bind(snapshot.operation_id.as_uuid()).bind(serde_json::to_value(&event)?).execute(&mut **tx).await?;
    sqlx::query("UPDATE rust_controller.operations SET revision=$2,updated_at=clock_timestamp() WHERE operation_id=$1").bind(snapshot.operation_id.as_uuid()).bind(revision).execute(&mut **tx).await?;
    sqlx::query("UPDATE rust_controller.operation_projection SET revision=$2,last_event_id=$3,rebuilt_at=clock_timestamp() WHERE operation_id=$1").bind(snapshot.operation_id.as_uuid()).bind(revision).bind(event.event_id().as_uuid()).execute(&mut **tx).await?;
    Ok((event.event_id(), revision))
}

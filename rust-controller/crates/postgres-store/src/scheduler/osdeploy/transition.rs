//! Private proof boundary. No public caller supplies a target state.
use super::*;

pub(super) struct LifecycleTransition {
    decision: EventId,
    operation: OperationId,
    attempt: AttemptId,
    revision: i64,
    current: ExecutionState,
    target: ExecutionState,
    at: DateTime<Utc>,
}

pub(super) struct ExceptionalTransition {
    event: EventId,
    value: wire::DecisionEnvelope,
    current: ExecutionState,
    semantic_key: String,
}

pub(super) enum OsDeployTransitionProof {
    InitialLease(LifecycleTransition),
    ReclaimedLease(LifecycleTransition),
    #[cfg_attr(not(feature = "fixture-ipc"), allow(dead_code))]
    ReclaimedCredentialLease(LifecycleTransition),
    FirstStart(LifecycleTransition),
    ResumedStart(LifecycleTransition),
    #[cfg(feature = "fixture-ipc")]
    FixtureRegistered(LifecycleTransition),
    #[cfg(feature = "fixture-ipc")]
    FixtureCompleted(LifecycleTransition),
    #[cfg(feature = "fixture-ipc")]
    FixtureGraceWaiting(LifecycleTransition),
    UnactivatedScopeExpired(ExceptionalTransition),
    ActivatedScopeExpired(ExceptionalTransition),
    ExpiredUnstartedSameAttempt(ExceptionalTransition),
    #[cfg_attr(not(feature = "fixture-ipc"), allow(dead_code))]
    ExpiredUnacknowledgedCredential(ExceptionalTransition),
    ExpiredReadOnlyEvaluation(ExceptionalTransition),
    ExpiredDispatched(ExceptionalTransition),
    CancelledUnexposed(ExceptionalTransition),
    CancelledExposed(ExceptionalTransition),
    OriginalDispatchReconciliation(ExceptionalTransition),
}

struct ExceptionalFacts {
    snapshot: OsDeployOperationSnapshot,
    generation: i64,
    at: DateTime<Utc>,
}

struct Cancellation {
    event: EventId,
    scope: Option<(wire::Scope, DateTime<Utc>)>,
}

// Selection of the already validated, locked scope lookup. This does not
// reconstruct an anchor or grant authority; the caller owns those checks.
fn cancellation_scope(
    attempt: Option<AttemptId>,
    bound_deadline: Option<DateTime<Utc>>,
    scope: wire::Scope,
    opened_deadline: Option<DateTime<Utc>>,
    at: DateTime<Utc>,
) -> Result<Option<(wire::Scope, DateTime<Utc>)>, Error> {
    if opened_deadline.is_some_and(|deadline| at >= deadline) {
        return Err(Error::FenceLost);
    }
    if attempt.is_some() {
        let deadline = opened_deadline.ok_or(Error::Validation)?;
        if bound_deadline != Some(deadline) {
            return Err(Error::Validation);
        }
    }
    Ok(opened_deadline.map(|deadline| (scope, deadline)))
}

impl ExceptionalFacts {
    async fn locked(
        scheduler: &Scheduler,
        tx: &mut Transaction<'_, Postgres>,
        operation: OperationId,
        revision: i64,
    ) -> Result<Self, Error> {
        authority(scheduler, tx).await?;
        let snapshot = locked_execution(tx, operation).await?;
        if snapshot.revision() != revision {
            return Err(Error::FenceLost);
        }
        Ok(Self {
            snapshot,
            generation: scheduler.generation,
            at: now(tx).await?,
        })
    }

    fn decision(
        self,
        detail: wire::Detail,
        resolution: Option<pve_port::NativeDecision>,
        semantic_key: String,
    ) -> Result<ExceptionalTransition, Error> {
        let value = wire::DecisionEnvelope {
            contract_version: 1,
            run_id: self.snapshot.run_id(),
            operation_id: self.snapshot.operation_id(),
            workflow_sha256: self.snapshot.plan().workflow_sha256().to_owned(),
            stage_sha256: self.snapshot.plan().fingerprint()?,
            attempt_id: self.snapshot.attempt_id(),
            generation: self.generation,
            before_revision: self.snapshot.revision(),
            evaluated_at: self.at,
            resolution,
            detail,
        };
        value.validate()?;
        Ok(ExceptionalTransition {
            event: EventId::new(),
            value,
            current: self.snapshot.state(),
            semantic_key,
        })
    }

    async fn scope(
        &self,
        tx: &mut Transaction<'_, Postgres>,
    ) -> Result<(wire::Scope, OperationId, EventId, DateTime<Utc>), Error> {
        let scope = load::stage_scope(self.snapshot.plan().stage());
        let scope_name = serde_json::to_value(scope)?
            .as_str()
            .ok_or(Error::Validation)?
            .to_owned();
        let row = sqlx::query("SELECT anchor_operation_id,anchor_event_id,deadline_at FROM rust_controller.osdeploy_deadlines WHERE run_id=$1 AND scope_key=$2")
            .bind(self.snapshot.run_id().as_uuid()).bind(scope_name).fetch_optional(&mut **tx).await?.ok_or(Error::Validation)?;
        let deadline = row.try_get("deadline_at")?;
        if self.snapshot.deadline_at().is_some_and(|v| v != deadline) {
            return Err(Error::Validation);
        }
        Ok((
            scope,
            load::id(row.try_get("anchor_operation_id")?)?,
            load::id(row.try_get("anchor_event_id")?)?,
            deadline,
        ))
    }

    async fn expired_epoch(
        &self,
        tx: &mut Transaction<'_, Postgres>,
    ) -> Result<(EventId, wire::Purpose), Error> {
        let row = sqlx::query("SELECT e.acquisition_event_id,e.purpose,l.lease_expires_at FROM rust_controller.worker_leases l JOIN rust_controller.osdeploy_lease_epochs e ON e.operation_id=l.operation_id AND e.lease_token_sha256=encode(sha256(convert_to(l.lease_token,'UTF8')),'hex') WHERE l.operation_id=$1")
            .bind(self.snapshot.operation_id().as_uuid()).fetch_optional(&mut **tx).await?.ok_or(Error::Validation)?;
        if row.try_get::<DateTime<Utc>, _>("lease_expires_at")? > self.at {
            return Err(Error::FenceLost);
        }
        Ok((
            load::id(row.try_get("acquisition_event_id")?)?,
            serde_json::from_value(json!(row.try_get::<String, _>("purpose")?))?,
        ))
    }

    async fn cancellation(
        &self,
        tx: &mut Transaction<'_, Postgres>,
    ) -> Result<Cancellation, Error> {
        if !self.snapshot.cancelled() || self.snapshot.state().is_terminal() {
            return Err(Error::FenceLost);
        }
        // Existing terminal outcomes win; an elapsed opened scope must instead
        // consume an expiry proof, even when cancellation was just recorded.
        let scope = load::stage_scope(self.snapshot.plan().stage());
        let name = serde_json::to_value(scope)?
            .as_str()
            .ok_or(Error::Validation)?
            .to_owned();
        let deadline: Option<DateTime<Utc>> = sqlx::query_scalar("SELECT deadline_at FROM rust_controller.osdeploy_deadlines WHERE run_id=$1 AND scope_key=$2")
            .bind(self.snapshot.run_id().as_uuid()).bind(name).fetch_optional(&mut **tx).await?;
        let scope = cancellation_scope(
            self.snapshot.attempt_id(),
            self.snapshot.deadline_at(),
            scope,
            deadline,
            self.at,
        )?;
        let event: Uuid = sqlx::query_scalar("SELECT decision_event_id FROM rust_controller.osdeploy_run_cancellations WHERE run_id=$1")
            .bind(self.snapshot.run_id().as_uuid()).fetch_one(&mut **tx).await?;
        Ok(Cancellation {
            event: load::id(event)?,
            scope,
        })
    }
}

impl OsDeployTransitionProof {
    #[cfg(feature = "fixture-ipc")]
    pub(super) async fn fixture_completed(
        tx: &mut Transaction<'_, Postgres>,
        event: EventId,
    ) -> Result<Self, Error> {
        let (proof, value) = lifecycle_facts(tx, event, false).await?;
        let wire::Detail::FixturePeCompleted(selected) = value.detail else {
            return Err(Error::Validation);
        };
        let row: Option<(Uuid, String, bool)> = sqlx::query_as("SELECT attempt_id,report_sha256,succeeded FROM rust_controller.fixture_pe_completions WHERE operation_id=$1 AND selected_event_id=$2")
            .bind(proof.operation.as_uuid()).bind(event.as_uuid()).fetch_optional(&mut **tx).await?;
        wire::require(
            proof.current == ExecutionState::Running
                && row
                    == Some((
                        proof.attempt.as_uuid(),
                        selected.report_sha256,
                        selected.succeeded,
                    )),
        )?;
        Ok(Self::FixtureCompleted(LifecycleTransition {
            target: if selected.succeeded {
                ExecutionState::Satisfied
            } else {
                ExecutionState::Failed
            },
            ..proof
        }))
    }
    #[cfg(feature = "fixture-ipc")]
    pub(super) async fn fixture_grace_waiting(
        tx: &mut Transaction<'_, Postgres>,
        event: EventId,
    ) -> Result<Self, Error> {
        let row = sqlx::query("SELECT d.payload_canonical_json,d.decision_revision,o.state,o.revision,b.attempt_id,b.deadline_at,s.anchor_event_id FROM rust_controller.osdeploy_decisions d JOIN rust_controller.operations o USING(operation_id) JOIN rust_controller.osdeploy_attempt_bindings b USING(operation_id) JOIN rust_controller.osdeploy_deadlines s ON s.run_id=o.run_id AND s.scope_key='shutdown_grace' JOIN rust_controller.fixture_pe_completions c ON c.selected_event_id=s.anchor_event_id WHERE d.event_id=$1 AND b.activation_mode='parked' AND b.scope_key='shutdown_grace' AND c.succeeded AND NOT EXISTS(SELECT 1 FROM rust_controller.worker_leases l WHERE l.operation_id=o.operation_id) AND NOT EXISTS(SELECT 1 FROM rust_controller.osdeploy_run_cancellations x WHERE x.run_id=o.run_id)")
            .bind(event.as_uuid()).fetch_one(&mut **tx).await?;
        let d =
            wire::DecisionEnvelope::decode(&row.try_get::<String, _>("payload_canonical_json")?)?;
        let wire::Detail::FixtureGraceWaiting(g) = &d.detail else {
            return Err(Error::Validation);
        };
        let attempt = d.attempt_id.ok_or(Error::Validation)?;
        let revision: i64 = row.try_get("revision")?;
        wire::require(
            row.try_get::<String, _>("state")? == "pending"
                && row.try_get::<Uuid, _>("attempt_id")? == attempt.as_uuid()
                && row.try_get::<Uuid, _>("anchor_event_id")? == g.completion_event_id.as_uuid()
                && row.try_get::<DateTime<Utc>, _>("deadline_at")? == g.deadline_at
                && d.before_revision.checked_add(1) == Some(revision)
                && row.try_get::<i64, _>("decision_revision")? == revision,
        )?;
        Ok(Self::FixtureGraceWaiting(LifecycleTransition {
            decision: event,
            operation: d.operation_id,
            attempt,
            revision,
            current: ExecutionState::Pending,
            target: ExecutionState::Waiting,
            at: d.evaluated_at,
        }))
    }
    #[cfg(feature = "fixture-ipc")]
    pub(super) async fn fixture_registered(
        tx: &mut Transaction<'_, Postgres>,
        event: EventId,
    ) -> Result<Self, Error> {
        let (proof, value) = lifecycle_facts(tx, event, false).await?;
        let wire::Detail::FixturePeRegistered(selected) = value.detail else {
            return Err(Error::Validation);
        };
        let row: Option<(Uuid, String)> = sqlx::query_as("SELECT attempt_id,identity_sha256 FROM rust_controller.fixture_pe_registrations WHERE operation_id=$1 AND selected_event_id=$2")
            .bind(proof.operation.as_uuid()).bind(event.as_uuid()).fetch_optional(&mut **tx).await?;
        wire::require(
            proof.current == ExecutionState::Running
                && row == Some((proof.attempt.as_uuid(), selected.identity_sha256)),
        )?;
        Ok(Self::FixtureRegistered(LifecycleTransition {
            target: ExecutionState::Satisfied,
            ..proof
        }))
    }
    pub(super) async fn original_dispatch_reconciliation(
        s: &Scheduler,
        tx: &mut Transaction<'_, Postgres>,
        op: OperationId,
        attempt: AttemptId,
        revision: i64,
        evidence_event: EventId,
    ) -> Result<Self, Error> {
        let f = ExceptionalFacts::locked(s, tx, op, revision).await?;
        admit(s, &f.snapshot, f.snapshot.plan().workflow_sha256())?;
        if f.snapshot.state() != ExecutionState::Unknown || f.snapshot.attempt_id() != Some(attempt)
        {
            return Err(Error::FenceLost);
        }
        let dispatch = f.snapshot.dispatch().ok_or(Error::Validation)?;
        wire::require(dispatch.request().binding().attempt_id() == attempt)?;
        let lease: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM rust_controller.worker_leases WHERE operation_id=$1)",
        )
        .bind(op.as_uuid())
        .fetch_one(&mut **tx)
        .await?;
        if lease {
            return Err(Error::FenceLost);
        }
        let (scope, _, _, deadline) = f.scope(tx).await?;
        if f.at >= deadline {
            return Err(Error::FenceLost);
        }
        let (context, evidence) = Box::pin(crate::osdeploy::execution::history::indexed_context(
            tx,
            op,
            revision,
            evidence_event,
            pve_port::ProvisioningEvaluationModeV1::Reconciliation,
        ))
        .await?;
        let advice = pve_port::evaluate_provisioning_outcome(&context, &evidence, f.at);
        use pve_port::NativeDecision;
        let resolution = match advice.decision {
            NativeDecision::Satisfied
            | NativeDecision::Failed
            | NativeDecision::Conflicted
            | NativeDecision::Unknown => advice.decision,
            NativeDecision::Waiting => NativeDecision::Unknown,
            NativeDecision::Blocked | NativeDecision::Ready => return Err(Error::Validation),
        };
        let detail = wire::Detail::PveEvaluated(wire::Evaluated {
            mode: wire::Mode::Reconciliation,
            advice: advice.decision,
            evidence_event_id: evidence_event,
            evidence_sha256: wire::digest(&evidence)?,
            scope_key: scope,
            deadline_at: deadline,
            reason: advice.reason,
            lease_acquisition_event_id: None,
            schedule: None,
        });
        Ok(Self::OriginalDispatchReconciliation(f.decision(
            detail,
            Some(resolution),
            format!(
                "osdeploy:evaluate:reconciliation:{}",
                evidence_event.as_uuid()
            ),
        )?))
    }

    pub(super) async fn expired_dispatched(
        s: &Scheduler,
        tx: &mut Transaction<'_, Postgres>,
        op: OperationId,
        revision: i64,
    ) -> Result<Self, Error> {
        let f = ExceptionalFacts::locked(s, tx, op, revision).await?;
        if f.snapshot.cancelled()
            || !matches!(
                f.snapshot.state(),
                ExecutionState::Running | ExecutionState::Waiting
            )
        {
            return Err(Error::FenceLost);
        }
        f.snapshot.dispatch().ok_or(Error::Validation)?;
        let dispatch_event_id: Uuid = sqlx::query_scalar("SELECT dispatch_event_id FROM rust_controller.osdeploy_pve_dispatches WHERE operation_id=$1").bind(op.as_uuid()).fetch_one(&mut **tx).await?;
        let dispatch_event_id = load::id(dispatch_event_id)?;
        let (scope, _, _, deadline) = f.scope(tx).await?;
        if f.at >= deadline {
            return Err(Error::FenceLost);
        }
        let (epoch, _) = f.expired_epoch(tx).await?;
        Ok(Self::ExpiredDispatched(f.decision(
            wire::Detail::LeaseExpiredUncertain(wire::UncertainExpiry {
                lease_acquisition_event_id: epoch,
                dispatch_event_id,
                scope_key: scope,
                deadline_at: deadline,
                reason: wire::Reason::LeaseExpiredAfterDispatch,
            }),
            Some(pve_port::NativeDecision::Unknown),
            format!("osdeploy:uncertain:{}", epoch.as_uuid()),
        )?))
    }

    pub(super) async fn reclaimed_lease(
        tx: &mut Transaction<'_, Postgres>,
        event: EventId,
    ) -> Result<Self, Error> {
        let (proof, value) = lifecycle_facts(tx, event, false).await?;
        let wire::Detail::LeaseAcquired(acquisition) = value.detail else {
            return Err(Error::Validation);
        };
        let started: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.journal_events WHERE operation_id=$1 AND event_kind='attempt_started')").bind(proof.operation.as_uuid()).fetch_one(&mut **tx).await?;
        let last: String = sqlx::query_scalar("SELECT d.action FROM rust_controller.journal_events j JOIN rust_controller.osdeploy_decisions d ON d.event_id=(j.payload->>'decision_event_id')::uuid WHERE j.operation_id=$1 AND j.event_kind='execution_state_changed' ORDER BY j.aggregate_revision DESC LIMIT 1").bind(proof.operation.as_uuid()).fetch_one(&mut **tx).await?;
        if proof.current != ExecutionState::Pending
            || acquisition.purpose != wire::Purpose::ReclaimedEvaluation
            || started
            || last != "lease_reclaimed_same_attempt"
        {
            return Err(Error::Validation);
        }
        Ok(Self::ReclaimedLease(LifecycleTransition {
            target: ExecutionState::Leased,
            ..proof
        }))
    }

    #[cfg(feature = "fixture-ipc")]
    pub(super) async fn reclaimed_credential_lease(
        tx: &mut Transaction<'_, Postgres>,
        event: EventId,
    ) -> Result<Self, Error> {
        let (proof, value) = lifecycle_facts(tx, event, true).await?;
        let wire::Detail::LeaseAcquired(acquisition) = value.detail else {
            return Err(Error::Validation);
        };
        let last: String = sqlx::query_scalar("SELECT d.action FROM rust_controller.journal_events j JOIN rust_controller.osdeploy_decisions d ON d.event_id=(j.payload->>'decision_event_id')::uuid WHERE j.operation_id=$1 AND j.event_kind='execution_state_changed' ORDER BY j.aggregate_revision DESC LIMIT 1")
            .bind(proof.operation.as_uuid()).fetch_one(&mut **tx).await?;
        if proof.current != ExecutionState::Pending
            || acquisition.purpose != wire::Purpose::ReclaimedCredentialDelivery
            || last != "credential_delivery_reclaimed"
            || !fixture_delivery::unacknowledged(tx, proof.operation).await?
        {
            return Err(Error::Validation);
        }
        Ok(Self::ReclaimedCredentialLease(LifecycleTransition {
            target: ExecutionState::Running,
            ..proof
        }))
    }

    #[cfg(feature = "fixture-ipc")]
    pub(super) async fn expired_unacknowledged_credential(
        s: &Scheduler,
        tx: &mut Transaction<'_, Postgres>,
        op: OperationId,
        revision: i64,
    ) -> Result<Self, Error> {
        let f = ExceptionalFacts::locked(s, tx, op, revision).await?;
        if !s.fixture_credential_delivery
            || f.snapshot.cancelled()
            || f.snapshot.state() != ExecutionState::Running
            || !requires_delivery(tx, &f.snapshot).await?
            || f.snapshot.dispatch().is_none()
            || !fixture_delivery::unacknowledged(tx, op).await?
        {
            return Err(Error::FenceLost);
        }
        let (scope, _, _, deadline) = f.scope(tx).await?;
        if f.at >= deadline {
            return Err(Error::FenceLost);
        }
        let (epoch, _) = f.expired_epoch(tx).await?;
        Ok(Self::ExpiredUnacknowledgedCredential(f.decision(
            wire::Detail::CredentialDeliveryReclaimed(wire::Reclaimed {
                lease_acquisition_event_id: epoch,
                scope_key: scope,
                deadline_at: deadline,
                reason: wire::Reason::LeaseExpiredBeforeCredentialAck,
            }),
            None,
            format!("osdeploy:credential-reclaim:{}", epoch.as_uuid()),
        )?))
    }

    pub(super) async fn unactivated_scope_expired(
        s: &Scheduler,
        tx: &mut Transaction<'_, Postgres>,
        op: OperationId,
        revision: i64,
    ) -> Result<Self, Error> {
        let f = ExceptionalFacts::locked(s, tx, op, revision).await?;
        if f.snapshot.cancelled()
            || f.snapshot.state() != ExecutionState::Pending
            || f.snapshot.attempt_id().is_some()
            || f.snapshot.dispatch().is_some()
            || f.snapshot.plan().pve().is_some()
        {
            return Err(Error::FenceLost);
        }
        let (scope, anchor, anchor_event, deadline) = f.scope(tx).await?;
        if f.at < deadline {
            return Err(Error::FenceLost);
        }
        let detail = wire::Detail::ScopeExpiredBeforeActivation(wire::UnactivatedExpiry {
            scope_key: scope,
            anchor_operation_id: anchor,
            anchor_event_id: anchor_event,
            deadline_at: deadline,
            reason: wire::Reason::InheritedScopeDeadlineExpired,
        });
        Ok(Self::UnactivatedScopeExpired(f.decision(
            detail,
            Some(pve_port::NativeDecision::Unknown),
            format!("osdeploy:expire-before-activation:{}", scope_label(scope)?),
        )?))
    }

    pub(super) async fn activated_scope_expired(
        s: &Scheduler,
        tx: &mut Transaction<'_, Postgres>,
        op: OperationId,
        revision: i64,
    ) -> Result<Self, Error> {
        let f = ExceptionalFacts::locked(s, tx, op, revision).await?;
        if f.snapshot.state().is_terminal() && f.snapshot.state() != ExecutionState::Unknown {
            return Err(Error::FenceLost);
        }
        let attempt = f.snapshot.attempt_id().ok_or(Error::Validation)?;
        let (scope, anchor, anchor_event, deadline) = f.scope(tx).await?;
        if f.at < deadline {
            return Err(Error::FenceLost);
        }
        let grace = scope == wire::Scope::ShutdownGrace;
        if grace {
            #[cfg(feature = "fixture-ipc")]
            {
                if !s.fixture_credential_delivery {
                    return Err(Error::CapabilityUnavailable);
                }
                // The elapsed timer proves only that the original grace ended.
                // Its selected completion and parked attempt must survive reload;
                // no worker lease or observation of a stopped VM is manufactured.
                wire::require(
                    !f.snapshot.cancelled()
                        && f.snapshot.state() == ExecutionState::Waiting
                        && f.snapshot.dispatch().is_none(),
                )?;
                let valid: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.osdeploy_attempt_bindings b JOIN rust_controller.fixture_pe_completions c ON c.selected_event_id=$3 JOIN rust_controller.fixture_osdeploy_origins o ON o.run_id=b.run_id WHERE b.operation_id=$1 AND b.attempt_id=$2 AND b.activation_mode='parked' AND b.scope_key='shutdown_grace' AND b.deadline_at=$4 AND c.operation_id=$5 AND c.run_id=b.run_id AND c.succeeded AND o.completion_package AND NOT EXISTS(SELECT 1 FROM rust_controller.worker_leases l WHERE l.operation_id=b.operation_id))")
                    .bind(op.as_uuid()).bind(attempt.as_uuid()).bind(anchor_event.as_uuid()).bind(deadline).bind(anchor.as_uuid()).fetch_one(&mut **tx).await?;
                wire::require(valid)?;
            }
            #[cfg(not(feature = "fixture-ipc"))]
            return Err(Error::CapabilityUnavailable);
        }
        let detail = wire::Detail::ActivatedScopeExpired(wire::ActivatedExpiry {
            scope_key: scope,
            anchor_operation_id: anchor,
            anchor_event_id: anchor_event,
            deadline_at: deadline,
            pe_complete_operation_id: grace.then_some(anchor),
            pe_complete_decision_event_id: grace.then_some(anchor_event),
            reason: if grace {
                wire::Reason::ShutdownGraceDeadlineExpired
            } else {
                wire::Reason::PhaseDeadlineExpired
            },
        });
        Ok(Self::ActivatedScopeExpired(f.decision(
            detail,
            Some(pve_port::NativeDecision::Unknown),
            format!(
                "osdeploy:expire:{}:{}",
                attempt.as_uuid(),
                scope_label(scope)?
            ),
        )?))
    }

    pub(super) async fn expired_unstarted_same_attempt(
        s: &Scheduler,
        tx: &mut Transaction<'_, Postgres>,
        op: OperationId,
        revision: i64,
    ) -> Result<Self, Error> {
        let f = ExceptionalFacts::locked(s, tx, op, revision).await?;
        if f.snapshot.cancelled()
            || f.snapshot.state() != ExecutionState::Leased
            || f.snapshot.dispatch().is_some()
        {
            return Err(Error::FenceLost);
        }
        let (scope, _, _, deadline) = f.scope(tx).await?;
        if f.at >= deadline {
            return Err(Error::FenceLost);
        }
        let (epoch, _) = f.expired_epoch(tx).await?;
        let started:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.journal_events WHERE operation_id=$1 AND event_kind='attempt_started')")
            .bind(op.as_uuid()).fetch_one(&mut **tx).await?;
        if started {
            return Err(Error::Validation);
        }
        let detail = wire::Detail::LeaseReclaimedSameAttempt(wire::Reclaimed {
            lease_acquisition_event_id: epoch,
            scope_key: scope,
            deadline_at: deadline,
            reason: wire::Reason::LeaseExpiredBeforeStart,
        });
        Ok(Self::ExpiredUnstartedSameAttempt(f.decision(
            detail,
            None,
            format!("osdeploy:reclaim:{}", epoch.as_uuid()),
        )?))
    }

    pub(super) async fn expired_read_only_evaluation(
        s: &Scheduler,
        tx: &mut Transaction<'_, Postgres>,
        op: OperationId,
        revision: i64,
    ) -> Result<Self, Error> {
        let f = ExceptionalFacts::locked(s, tx, op, revision).await?;
        if f.snapshot.cancelled()
            || !matches!(
                f.snapshot.state(),
                ExecutionState::Running | ExecutionState::Waiting
            )
            || f.snapshot.dispatch().is_some()
        {
            return Err(Error::FenceLost);
        }
        let (scope, _, _, deadline) = f.scope(tx).await?;
        if f.at >= deadline {
            return Err(Error::FenceLost);
        }
        let (epoch, purpose) = f.expired_epoch(tx).await?;
        let activity = if f.snapshot.state() == ExecutionState::Waiting {
            if purpose != wire::Purpose::ResumeEvaluation {
                return Err(Error::Validation);
            }
            epoch
        } else {
            let events:Vec<Uuid>=sqlx::query_scalar("SELECT event_id FROM rust_controller.osdeploy_decisions WHERE operation_id=$1 AND action='evaluation_started' AND payload_canonical_json::jsonb->'detail'->>'lease_acquisition_event_id'=$2 AND payload_canonical_json::jsonb->'detail'->>'activity'='preflight_read'")
                .bind(op.as_uuid()).bind(epoch.as_uuid().to_string()).fetch_all(&mut **tx).await?;
            if events.len() != 1 {
                return Err(Error::Validation);
            }
            load::id(events[0])?
        };
        let detail = wire::Detail::EvaluationReparked(wire::Reparked {
            lease_acquisition_event_id: epoch,
            activity_event_id: activity,
            scope_key: scope,
            deadline_at: deadline,
            reason: wire::Reason::ReadOnlyEvaluatorLeaseExpired,
            schedule: wire::Schedule {
                mode: wire::ScheduleMode::Waiting,
                next_check_at: Some((f.at + chrono::Duration::seconds(2)).min(deadline)),
                unavailable_count: 0,
            },
        });
        Ok(Self::ExpiredReadOnlyEvaluation(f.decision(
            detail,
            Some(pve_port::NativeDecision::Waiting),
            format!("osdeploy:repark:{}", epoch.as_uuid()),
        )?))
    }

    pub(super) async fn cancelled_unexposed(
        s: &Scheduler,
        tx: &mut Transaction<'_, Postgres>,
        op: OperationId,
        revision: i64,
    ) -> Result<Self, Error> {
        let f = ExceptionalFacts::locked(s, tx, op, revision).await?;
        let cancellation = f.cancellation(tx).await?;
        if f.snapshot.dispatch().is_some() {
            return Err(Error::Validation);
        }
        let (scope, deadline) = cancellation.scope.unzip();
        let detail = wire::Detail::StageCancelledUnexposed(wire::UnexposedCancellation {
            cancellation_event_id: cancellation.event,
            scope_key: scope,
            deadline_at: deadline,
            reason: wire::Reason::RunCancelledBeforeExposure,
        });
        Ok(Self::CancelledUnexposed(f.decision(
            detail,
            Some(pve_port::NativeDecision::Blocked),
            format!("osdeploy:cancel:{}", cancellation.event.as_uuid()),
        )?))
    }

    pub(super) async fn cancelled_exposed(
        s: &Scheduler,
        tx: &mut Transaction<'_, Postgres>,
        op: OperationId,
        revision: i64,
    ) -> Result<Self, Error> {
        let f = ExceptionalFacts::locked(s, tx, op, revision).await?;
        let cancellation = f.cancellation(tx).await?;
        #[cfg(feature = "fixture-ipc")]
        let reclaimed_delivery = f.snapshot.state() == ExecutionState::Pending
            && requires_delivery(tx, &f.snapshot).await?
            && fixture_delivery::unacknowledged(tx, op).await?;
        #[cfg(not(feature = "fixture-ipc"))]
        let reclaimed_delivery = false;
        if f.snapshot.dispatch().is_none()
            || !(reclaimed_delivery
                || matches!(
                    f.snapshot.state(),
                    ExecutionState::Leased
                        | ExecutionState::Running
                        | ExecutionState::Waiting
                        | ExecutionState::Cancelling
                ))
        {
            return Err(Error::Validation);
        }
        let (scope, deadline) = cancellation.scope.ok_or(Error::Validation)?;
        let dispatch:Uuid=sqlx::query_scalar("SELECT dispatch_event_id FROM rust_controller.osdeploy_pve_dispatches WHERE operation_id=$1")
            .bind(op.as_uuid()).fetch_one(&mut **tx).await?;
        let detail = wire::Detail::StageCancelledExposed(wire::ExposedCancellation {
            cancellation_event_id: cancellation.event,
            scope_key: scope,
            deadline_at: deadline,
            dispatch_event_id: load::id(dispatch)?,
            reason: wire::Reason::RunCancelledOutcomeUncertain,
        });
        Ok(Self::CancelledExposed(f.decision(
            detail,
            Some(pve_port::NativeDecision::Unknown),
            format!("osdeploy:cancel:{}", cancellation.event.as_uuid()),
        )?))
    }
}

fn scope_label(scope: wire::Scope) -> Result<String, Error> {
    serde_json::to_value(scope)?
        .as_str()
        .map(str::to_owned)
        .ok_or(Error::Validation)
}

impl OsDeployTransitionProof {
    pub(super) async fn initial_lease(
        tx: &mut Transaction<'_, Postgres>,
        event: EventId,
    ) -> Result<Self, Error> {
        let (proof, value) = lifecycle_facts(tx, event, false).await?;
        let wire::Detail::LeaseAcquired(a) = value.detail else {
            return Err(Error::Validation);
        };
        let count: i64 = sqlx::query_scalar("SELECT count(*) FROM rust_controller.journal_events WHERE operation_id=$1 AND event_kind='attempt_started'")
            .bind(proof.operation.as_uuid()).fetch_one(&mut **tx).await?;
        if proof.current != ExecutionState::Pending
            || a.purpose != wire::Purpose::InitialEvaluation
            || a.acquisition_event_id != event
            || count != 0
        {
            return Err(Error::Validation);
        }
        let previous: String = sqlx::query_scalar("SELECT payload_canonical_json FROM rust_controller.osdeploy_decisions WHERE operation_id=$1 AND decision_revision=$2")
            .bind(proof.operation.as_uuid()).bind(proof.revision - 1).fetch_one(&mut **tx).await?;
        let previous = wire::DecisionEnvelope::decode(&previous)?;
        if previous.attempt_id != Some(proof.attempt)
            || previous.evaluated_at != proof.at
            || !matches!(previous.detail, wire::Detail::StageActivated(_))
        {
            return Err(Error::Validation);
        }
        validate_transition(
            proof.current,
            ExecutionState::Leased,
            TransitionPolicy::Domain,
        )
        .map_err(|_| Error::Validation)?;
        Ok(Self::InitialLease(LifecycleTransition {
            target: ExecutionState::Leased,
            ..proof
        }))
    }

    pub(super) async fn first_start(
        tx: &mut Transaction<'_, Postgres>,
        event: EventId,
    ) -> Result<Self, Error> {
        let (proof, value) = lifecycle_facts(tx, event, false).await?;
        let wire::Detail::EvaluationStarted(a) = value.detail else {
            return Err(Error::Validation);
        };
        if proof.current != ExecutionState::Leased || a.activity != wire::Activity::PreflightRead {
            return Err(Error::Validation);
        }
        let start = sqlx::query("SELECT aggregate_revision,attempt_id,observed_at,payload FROM rust_controller.journal_events WHERE operation_id=$1 AND event_kind='attempt_started'")
            .bind(proof.operation.as_uuid()).fetch_all(&mut **tx).await?;
        if start.len() != 1 {
            return Err(Error::Validation);
        }
        let start = &start[0];
        if start.try_get::<i64, _>("aggregate_revision")? != proof.revision - 1
            || start.try_get::<Uuid, _>("attempt_id")? != proof.attempt.as_uuid()
            || start.try_get::<DateTime<Utc>, _>("observed_at")? != proof.at
            || start.try_get::<serde_json::Value, _>("payload")?
                != json!({"phase":"mutation_started"})
        {
            return Err(Error::Validation);
        }
        validate_transition(
            proof.current,
            ExecutionState::Running,
            TransitionPolicy::Domain,
        )
        .map_err(|_| Error::Validation)?;
        Ok(Self::FirstStart(LifecycleTransition {
            target: ExecutionState::Running,
            ..proof
        }))
    }
}

impl OsDeployTransitionProof {
    pub(super) async fn resumed_start(
        tx: &mut Transaction<'_, Postgres>,
        event: EventId,
    ) -> Result<Self, Error> {
        let (proof, value) = lifecycle_facts(tx, event, true).await?;
        let wire::Detail::EvaluationStarted(started) = value.detail else {
            return Err(Error::Validation);
        };
        let epoch = sqlx::query("SELECT purpose FROM rust_controller.osdeploy_lease_epochs WHERE acquisition_event_id=$1").bind(started.lease_acquisition_event_id.as_uuid()).fetch_one(&mut **tx).await?;
        let count: i64 = sqlx::query_scalar("SELECT count(*) FROM rust_controller.journal_events WHERE operation_id=$1 AND attempt_id=$2 AND event_kind='attempt_started' AND aggregate_revision<$3")
            .bind(proof.operation.as_uuid()).bind(proof.attempt.as_uuid()).bind(value.before_revision).fetch_one(&mut **tx).await?;
        let dispatched: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.osdeploy_pve_dispatches WHERE operation_id=$1)").bind(proof.operation.as_uuid()).fetch_one(&mut **tx).await?;
        if proof.current != ExecutionState::Waiting
            || count != 1
            || epoch.try_get::<String, _>("purpose")? != "resume_evaluation"
            || started.activity
                != if dispatched {
                    wire::Activity::OutcomeRead
                } else {
                    wire::Activity::PreflightRead
                }
        {
            return Err(Error::Validation);
        }
        validate_transition(
            proof.current,
            ExecutionState::Running,
            TransitionPolicy::Domain,
        )
        .map_err(|_| Error::Validation)?;
        Ok(Self::ResumedStart(LifecycleTransition {
            target: ExecutionState::Running,
            ..proof
        }))
    }
}

async fn lifecycle_facts(
    tx: &mut Transaction<'_, Postgres>,
    event: EventId,
    resume: bool,
) -> Result<(LifecycleTransition, wire::DecisionEnvelope), Error> {
    // Called only after authority/run/all-operation/attempt/lease locks. The
    // constructor checks the actual persisted decision, CAS, binding and epoch;
    // it accepts no snapshot, arbitrary target or caller-provided payload.
    let row = sqlx::query("SELECT d.payload_canonical_json,d.decision_revision,o.state,o.revision,b.attempt_id,b.deadline_at,l.lease_expires_at,l.heartbeat_at,e.acquisition_event_id FROM rust_controller.osdeploy_decisions d JOIN rust_controller.operations o USING(operation_id) JOIN rust_controller.osdeploy_attempt_bindings b USING(operation_id) JOIN rust_controller.worker_leases l USING(operation_id) JOIN rust_controller.osdeploy_lease_epochs e ON e.operation_id=l.operation_id AND e.lease_token_sha256=encode(sha256(convert_to(l.lease_token,'UTF8')),'hex') WHERE d.event_id=$1 AND l.attempt_id=b.attempt_id AND e.attempt_id=b.attempt_id AND e.generation=d.generation AND e.worker_id=l.worker_id AND l.generation=e.generation AND l.executor_kind='rust' AND NOT EXISTS(SELECT 1 FROM rust_controller.osdeploy_run_cancellations c WHERE c.run_id=o.run_id) AND ($2 OR NOT EXISTS(SELECT 1 FROM rust_controller.osdeploy_pve_dispatches x WHERE x.operation_id=o.operation_id))")
        .bind(event.as_uuid()).bind(resume).fetch_optional(&mut **tx).await?.ok_or(Error::Validation)?;
    let d = wire::DecisionEnvelope::decode(&row.try_get::<String, _>("payload_canonical_json")?)?;
    let attempt = d.attempt_id.ok_or(Error::Validation)?;
    let epoch: EventId = load::id(row.try_get("acquisition_event_id")?)?;
    let matches_epoch = match &d.detail {
        wire::Detail::LeaseAcquired(a) => a.acquisition_event_id == epoch,
        wire::Detail::EvaluationStarted(a) => a.lease_acquisition_event_id == epoch,
        wire::Detail::FixturePeRegistered(a) => a.lease_acquisition_event_id == epoch,
        wire::Detail::FixturePeCompleted(a) => a.lease_acquisition_event_id == epoch,
        _ => false,
    };
    if !matches_epoch
        || match &d.detail {
            wire::Detail::FixturePeRegistered(_) => {
                d.resolution != Some(pve_port::NativeDecision::Satisfied)
            }
            wire::Detail::FixturePeCompleted(a) => {
                d.resolution
                    != Some(if a.succeeded {
                        pve_port::NativeDecision::Satisfied
                    } else {
                        pve_port::NativeDecision::Failed
                    })
            }
            _ => d.resolution.is_some(),
        }
        || attempt.as_uuid() != row.try_get::<Uuid, _>("attempt_id")?
        || d.before_revision.checked_add(1) != Some(row.try_get("revision")?)
        || row.try_get::<i64, _>("decision_revision")? != row.try_get::<i64, _>("revision")?
        || d.evaluated_at >= row.try_get::<DateTime<Utc>, _>("deadline_at")?
        || d.evaluated_at >= row.try_get::<DateTime<Utc>, _>("lease_expires_at")?
        || d.evaluated_at < row.try_get::<DateTime<Utc>, _>("heartbeat_at")?
    {
        return Err(Error::Validation);
    }
    let proof = LifecycleTransition {
        decision: event,
        operation: d.operation_id,
        attempt,
        revision: row.try_get("revision")?,
        current: decode_execution_state(&row.try_get::<String, _>("state")?)
            .map_err(|_| Error::Validation)?,
        target: ExecutionState::Pending,
        at: d.evaluated_at,
    };
    Ok((proof, d))
}

pub(super) async fn append_osdeploy_transition(
    tx: &mut Transaction<'_, Postgres>,
    proof: OsDeployTransitionProof,
) -> Result<i64, Error> {
    let policy = if matches!(&proof, OsDeployTransitionProof::ReclaimedCredentialLease(_)) {
        TransitionPolicy::CredentialDeliveryResume
    } else {
        TransitionPolicy::Domain
    };
    #[cfg(feature = "fixture-ipc")]
    let policy = if matches!(&proof, OsDeployTransitionProof::FixtureGraceWaiting(_)) {
        TransitionPolicy::FixtureGraceWaiting
    } else {
        policy
    };
    let p = match proof {
        #[cfg(feature = "fixture-ipc")]
        OsDeployTransitionProof::FixtureRegistered(p) => p,
        #[cfg(feature = "fixture-ipc")]
        OsDeployTransitionProof::FixtureCompleted(p)
        | OsDeployTransitionProof::FixtureGraceWaiting(p) => p,
        OsDeployTransitionProof::InitialLease(p)
        | OsDeployTransitionProof::ReclaimedLease(p)
        | OsDeployTransitionProof::ReclaimedCredentialLease(p)
        | OsDeployTransitionProof::FirstStart(p)
        | OsDeployTransitionProof::ResumedStart(p) => p,
        OsDeployTransitionProof::UnactivatedScopeExpired(p)
        | OsDeployTransitionProof::ActivatedScopeExpired(p)
        | OsDeployTransitionProof::ExpiredUnstartedSameAttempt(p)
        | OsDeployTransitionProof::ExpiredUnacknowledgedCredential(p)
        | OsDeployTransitionProof::ExpiredReadOnlyEvaluation(p)
        | OsDeployTransitionProof::ExpiredDispatched(p)
        | OsDeployTransitionProof::CancelledUnexposed(p)
        | OsDeployTransitionProof::CancelledExposed(p) => return append_exception(tx, p).await,
        OsDeployTransitionProof::OriginalDispatchReconciliation(p) => {
            return append_exception(tx, p).await;
        }
    };
    let row: (String, i64) = sqlx::query_as(
        "SELECT state,revision FROM rust_controller.operations WHERE operation_id=$1",
    )
    .bind(p.operation.as_uuid())
    .fetch_one(&mut **tx)
    .await?;
    if row != (execution_state_name(p.current).to_owned(), p.revision) {
        return Err(Error::FenceLost);
    }
    persist_state_event(
        tx,
        StateAppend {
            operation_id: p.operation,
            attempt_id: Some(p.attempt),
            revision: p.revision,
            current: p.current,
            target: p.target,
            semantic_key: format!("osdeploy:state:{}", p.decision.as_uuid()),
            payload: json!({"state":p.target,"decision_event_id":p.decision}),
            observed_at: p.at,
            policy,
        },
    )
    .await
    .map_err(scheduler_error)
}

async fn append_exception(
    tx: &mut Transaction<'_, Postgres>,
    p: ExceptionalTransition,
) -> Result<i64, Error> {
    let target = match p.value.detail {
        wire::Detail::ScopeExpiredBeforeActivation(_)
        | wire::Detail::ActivatedScopeExpired(_)
        | wire::Detail::LeaseExpiredUncertain(_)
        | wire::Detail::StageCancelledExposed(_) => ExecutionState::Unknown,
        wire::Detail::LeaseReclaimedSameAttempt(_)
        | wire::Detail::CredentialDeliveryReclaimed(_) => ExecutionState::Pending,
        wire::Detail::EvaluationReparked(_) => ExecutionState::Waiting,
        wire::Detail::StageCancelledUnexposed(_) => ExecutionState::Blocked,
        wire::Detail::PveEvaluated(ref value) if value.mode == wire::Mode::Reconciliation => {
            match p.value.resolution {
                Some(pve_port::NativeDecision::Satisfied) => ExecutionState::Satisfied,
                Some(pve_port::NativeDecision::Failed) => ExecutionState::Failed,
                Some(pve_port::NativeDecision::Conflicted) => ExecutionState::Conflicted,
                Some(pve_port::NativeDecision::Unknown) => ExecutionState::Unknown,
                _ => return Err(Error::Validation),
            }
        }
        _ => return Err(Error::Validation),
    };
    let revision = append_osdeploy_decision(tx, p.event, &p.semantic_key, &p.value).await?;
    if target == p.current {
        return Ok(revision);
    }
    persist_state_event(
        tx,
        StateAppend {
            operation_id: p.value.operation_id,
            attempt_id: p.value.attempt_id,
            revision,
            current: p.current,
            target,
            semantic_key: format!("osdeploy:state:{}", p.event.as_uuid()),
            payload: json!({"state":target,"decision_event_id":p.event}),
            observed_at: p.value.evaluated_at,
            policy: if matches!(p.value.detail, wire::Detail::CredentialDeliveryReclaimed(_)) {
                TransitionPolicy::CredentialDeliveryReclaim
            } else {
                TransitionPolicy::Domain
            },
        },
    )
    .await
    .map_err(scheduler_error)
}

#[cfg(test)]
const fn private_fixture_database_family(is_linux: bool) -> &'static str {
    if is_linux {
        "native_test"
    } else {
        "osdeploy_private_lifecycle_test"
    }
}

#[cfg(test)]
const LOCAL_DATABASE_NAME: &str = private_fixture_database_family(cfg!(target_os = "linux"));
#[cfg(test)]
#[path = "../../../../../proof_support/mod.rs"]
mod local_postgres;

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn private_fixture_database_family_preserves_macos_and_admits_linux() {
        assert_eq!(private_fixture_database_family(true), "native_test");
        assert_eq!(
            private_fixture_database_family(false),
            "osdeploy_private_lifecycle_test"
        );
        let expected = if cfg!(target_os = "linux") {
            "native_test"
        } else {
            "osdeploy_private_lifecycle_test"
        };
        assert_eq!(LOCAL_DATABASE_NAME, expected);
    }

    #[test]
    fn cancellation_scope_keeps_opened_inherited_deadline_without_an_attempt() {
        let at = "2026-09-05T12:00:00Z".parse::<DateTime<Utc>>().unwrap();
        let deadline = "2026-09-05T12:40:00Z".parse::<DateTime<Utc>>().unwrap();
        assert_eq!(
            cancellation_scope(None, None, wire::Scope::PeRegistration, Some(deadline), at),
            Ok(Some((wire::Scope::PeRegistration, deadline)))
        );
    }

    #[test]
    fn cancellation_scope_selection_preserves_unopened_bound_and_expired_cases() {
        let at = "2026-09-05T12:00:00Z".parse::<DateTime<Utc>>().unwrap();
        let deadline = "2026-09-05T12:05:00Z".parse::<DateTime<Utc>>().unwrap();
        let attempt = Some(AttemptId::new());
        assert_eq!(
            cancellation_scope(None, None, wire::Scope::PeRegistration, None, at),
            Ok(None)
        );
        assert_eq!(
            cancellation_scope(
                attempt,
                Some(deadline),
                wire::Scope::MutationClone,
                Some(deadline),
                at
            ),
            Ok(Some((wire::Scope::MutationClone, deadline)))
        );
        assert_eq!(
            cancellation_scope(
                attempt,
                Some(deadline),
                wire::Scope::MutationClone,
                None,
                at
            ),
            Err(Error::Validation)
        );
        assert_eq!(
            cancellation_scope(
                attempt,
                None,
                wire::Scope::MutationClone,
                Some(deadline),
                at
            ),
            Err(Error::Validation)
        );
        assert_eq!(
            cancellation_scope(
                attempt,
                Some(at),
                wire::Scope::MutationClone,
                Some(deadline),
                at
            ),
            Err(Error::Validation)
        );
        for attempt in [None, attempt] {
            let bound = attempt.map(|_| deadline);
            assert_eq!(
                cancellation_scope(
                    attempt,
                    bound,
                    wire::Scope::PeRegistration,
                    Some(deadline),
                    deadline
                ),
                Err(Error::FenceLost)
            );
            assert_eq!(
                cancellation_scope(
                    attempt,
                    bound,
                    wire::Scope::PeRegistration,
                    Some(deadline),
                    deadline + chrono::Duration::microseconds(1)
                ),
                Err(Error::FenceLost)
            );
        }
    }
    struct Database {
        pool: sqlx::PgPool,
        scheduler: Scheduler,
        store: PgStore,
        _container: local_postgres::Container,
    }
    impl Database {
        async fn new() -> Self {
            tokio::time::timeout(local_postgres::SETUP_BOUND,async {
                let (container,dsn)=local_postgres::Container::start().await;
                let pool=tokio::time::timeout(Duration::from_secs(15),async {
                    loop {
                        if let Ok(pool)=sqlx::postgres::PgPoolOptions::new().max_connections(4).acquire_timeout(Duration::from_secs(1)).connect(&dsn).await { break pool; }
                        tokio::time::sleep(Duration::from_millis(100)).await;
                    }
                }).await.expect("private_fixture_connect_timeout");
                let store=PgStore::new(pool.clone()); store.migrate().await.unwrap();
                sqlx::query("INSERT INTO rust_controller.orchestration_authority(singleton_key,executor_kind,generation,change_reference) VALUES(1,'rust',1,'osdeploy-private-tests')").execute(&pool).await.unwrap();
                let scheduler=Scheduler::new(store.clone(),ExecutorKind::Rust,1,"private-proof-worker").unwrap();
                Self {pool,scheduler,store,_container:container}
            }).await.expect("private_fixture_setup_timeout")
        }
        async fn register(&self, seconds: u32, other: bool) -> crate::OsDeployWorkflowIds {
            let mut value: serde_json::Value = serde_json::from_str(include_str!(
                "../../../../osdeploy-adapter/tests/fixtures/plan-v1.json"
            ))
            .unwrap();
            value["policy"]["mutation_seconds"] = json!(seconds);
            value["policy"]["evidence_freshness_seconds"] = json!(1);
            if other {
                value["vm"]["target_vmid"] = json!(902);
                value["vm"]["uuid"] = json!("88888888-8888-4888-8888-888888888888");
                value["vm"]["mac"] = json!("02:00:00:00:00:02");
                value["names"]["requested_name"] = json!("Another");
                value["names"]["windows_name"] = json!("Another");
                value["names"]["expected_agent_id"] = json!("agent-another");
            }
            let plan = osdeploy_adapter::restore_osdeploy_plan_v1(
                &value.to_string(),
                &wire::digest(&value).unwrap(),
            )
            .unwrap();
            self.store
                .enqueue_osdeploy(controller_domain::RunId::new(), &plan)
                .await
                .unwrap()
        }
    }

    #[test]
    fn private_clock_predicate_rejects_exact_lease_and_deadline_equality() {
        let at = "2026-09-05T12:00:00Z".parse::<DateTime<Utc>>().unwrap();
        let mut g = LeaseGrant::new(
            OperationId::new(),
            AttemptId::new(),
            1,
            ExecutorKind::Rust,
            1,
            "test".to_owned(),
            Uuid::now_v7(),
            at,
            at,
            at + chrono::Duration::seconds(30),
            at + chrono::Duration::seconds(300),
        );
        assert!(active_at(&g, g.lease_expires_at - chrono::Duration::microseconds(1)).is_ok());
        assert_eq!(active_at(&g, g.lease_expires_at), Err(Error::FenceLost));
        g.deadline_at = at + chrono::Duration::seconds(10);
        assert!(active_at(&g, g.deadline_at - chrono::Duration::microseconds(1)).is_ok());
        assert_eq!(active_at(&g, g.deadline_at), Err(Error::FenceLost));
        let status = OsDeployLeaseStatus {
            checked_at: g.deadline_at,
            grant: g.clone(),
            revision: 6,
        };
        assert_eq!(status.remaining(), Duration::ZERO);
        assert_eq!(
            OsDeployLeaseStatus {
                checked_at: g.deadline_at + chrono::Duration::seconds(1),
                grant: g,
                revision: 6
            }
            .remaining(),
            Duration::ZERO
        );
    }

    #[tokio::test]
    async fn private_stale_token_cas_and_all_disabled_lifecycle_entries_write_nothing() {
        let f = Database::new().await;
        let ids = f.register(300, false).await;
        let op = ids.operation(OsDeployStage::Clone);
        let g = f
            .scheduler
            .claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
            .await
            .unwrap()
            .unwrap();
        let mut stale = g.clone();
        stale.lease_token = Uuid::now_v7();
        assert!(matches!(
            f.scheduler
                .start_osdeploy_bound(&stale, ids.workflow_sha256())
                .await,
            Err(Error::FenceLost)
        ));
        assert!(matches!(
            f.scheduler
                .heartbeat_osdeploy_bound(&stale, ids.workflow_sha256())
                .await,
            Err(Error::FenceLost)
        ));
        assert!(matches!(
            f.scheduler
                .continuation_osdeploy_bound(&stale, ids.workflow_sha256())
                .await,
            Err(Error::FenceLost)
        ));
        for stage in OsDeployStage::ALL.into_iter().skip(3) {
            let mut candidate = g.clone();
            candidate.operation_id = ids.operation(stage);
            assert!(matches!(
                f.scheduler
                    .start_osdeploy_bound(&candidate, ids.workflow_sha256())
                    .await,
                Err(Error::CapabilityUnavailable)
            ));
            assert!(matches!(
                f.scheduler
                    .heartbeat_osdeploy_bound(&candidate, ids.workflow_sha256())
                    .await,
                Err(Error::CapabilityUnavailable)
            ));
            assert!(matches!(
                f.scheduler
                    .continuation_osdeploy_bound(&candidate, ids.workflow_sha256())
                    .await,
                Err(Error::CapabilityUnavailable)
            ));
        }
        let mut tx = f.pool.begin().await.unwrap();
        authority(&f.scheduler, &mut tx).await.unwrap();
        let snapshot = locked_execution(&mut tx, op).await.unwrap();
        let (_, epoch) = current_grant(&mut tx, &f.scheduler, &g, &snapshot)
            .await
            .unwrap();
        let stale = envelope(
            &snapshot,
            g.attempt_id(),
            1,
            2,
            now(&mut tx).await.unwrap(),
            wire::Detail::EvaluationStarted(wire::Started {
                lease_acquisition_event_id: epoch,
                activity: wire::Activity::PreflightRead,
            }),
        )
        .unwrap();
        assert_eq!(
            append_osdeploy_decision(&mut tx, EventId::new(), "test-stale-cas", &stale).await,
            Err(Error::FenceLost)
        );
        tx.commit().await.unwrap();
        let count: i64 = sqlx::query_scalar("SELECT count(*) FROM rust_controller.journal_events")
            .fetch_one(&f.pool)
            .await
            .unwrap();
        assert_eq!(count, 3);
        assert_eq!(
            f.store.load_osdeploy_operation(op).await.unwrap().state(),
            ExecutionState::Leased
        );
        // A closed cancellation-control fixture tests admission fencing only;
        // this does not implement the Task 7 all-stage cancellation consumer.
        let mut tx = f.pool.begin().await.unwrap();
        authority(&f.scheduler, &mut tx).await.unwrap();
        let snapshot = locked_execution(&mut tx, op).await.unwrap();
        let at = now(&mut tx).await.unwrap();
        let event = EventId::new();
        let control = envelope(
            &snapshot,
            g.attempt_id(),
            1,
            3,
            at,
            wire::Detail::RunCancelled(wire::RunCancelled {
                reason: wire::Reason::RunCancellationRequested,
            }),
        )
        .unwrap();
        append_osdeploy_decision(&mut tx, event, "private-run-cancellation-control", &control)
            .await
            .unwrap();
        sqlx::query("INSERT INTO rust_controller.osdeploy_run_cancellations(run_id,anchor_operation_id,decision_event_id,generation,requested_at) VALUES($1,$2,$3,1,$4)")
            .bind(ids.run_id().as_uuid()).bind(op.as_uuid()).bind(event.as_uuid()).bind(at).execute(&mut *tx).await.unwrap();
        tx.commit().await.unwrap();
        assert!(
            f.store
                .load_osdeploy_operation(op)
                .await
                .unwrap()
                .cancelled()
        );
        assert!(matches!(
            f.scheduler
                .claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
                .await,
            Err(Error::FenceLost)
        ));
        assert!(matches!(
            f.scheduler
                .start_osdeploy_bound(&g, ids.workflow_sha256())
                .await,
            Err(Error::FenceLost)
        ));
        assert!(matches!(
            f.scheduler
                .heartbeat_osdeploy_bound(&g, ids.workflow_sha256())
                .await,
            Err(Error::FenceLost)
        ));
        assert!(matches!(
            f.scheduler
                .continuation_osdeploy_bound(&g, ids.workflow_sha256())
                .await,
            Err(Error::FenceLost)
        ));
        let mut tx = f.pool.begin().await.unwrap();
        let proof = OsDeployTransitionProof::cancelled_unexposed(&f.scheduler, &mut tx, op, 4)
            .await
            .unwrap();
        let OsDeployTransitionProof::CancelledUnexposed(ref p) = proof else {
            panic!("wrong closed cancellation proof");
        };
        assert_eq!(p.value.attempt_id, Some(g.attempt_id()));
        assert_eq!(p.value.resolution, Some(pve_port::NativeDecision::Blocked));
        assert_eq!(append_osdeploy_transition(&mut tx, proof).await.unwrap(), 6);
        let state: String = sqlx::query_scalar(
            "SELECT state FROM rust_controller.operations WHERE operation_id=$1",
        )
        .bind(op.as_uuid())
        .fetch_one(&mut *tx)
        .await
        .unwrap();
        assert_eq!(state, "blocked");
        // Exercise the low-level append inside a rolled-back transaction only;
        // full attempt/lease cleanup is still the owning Task 7 consumer.
        tx.rollback().await.unwrap();
        let mut tx = f.pool.begin().await.unwrap();
        let proof = OsDeployTransitionProof::cancelled_unexposed(
            &f.scheduler,
            &mut tx,
            ids.operation(OsDeployStage::VerifyOperational),
            0,
        )
        .await
        .unwrap();
        let OsDeployTransitionProof::CancelledUnexposed(p) = proof else {
            panic!("wrong unactivated cancellation proof");
        };
        assert!(p.value.attempt_id.is_none());
        tx.rollback().await.unwrap();
        let count: i64 = sqlx::query_scalar("SELECT count(*) FROM rust_controller.journal_events")
            .fetch_one(&f.pool)
            .await
            .unwrap();
        assert_eq!(count, 4);
    }

    #[tokio::test]
    async fn private_expiry_requires_original_scope_and_current_locked_cas() {
        let f = Database::new().await;
        let ids = f.register(1, false).await;
        let op = ids.operation(OsDeployStage::Clone);
        let g = f
            .scheduler
            .claim_osdeploy_bound(op, ids.workflow_sha256(), 1)
            .await
            .unwrap()
            .unwrap();
        let mut tx = f.pool.begin().await.unwrap();
        assert!(matches!(
            OsDeployTransitionProof::activated_scope_expired(&f.scheduler, &mut tx, op, 2).await,
            Err(Error::FenceLost)
        ));
        tx.rollback().await.unwrap();
        tokio::time::timeout(Duration::from_secs(3),sqlx::query("SELECT pg_sleep(GREATEST(0,extract(epoch FROM ($1::timestamptz-clock_timestamp()))))").bind(g.deadline_at()).execute(&f.pool)).await.unwrap().unwrap();
        let mut tx = f.pool.begin().await.unwrap();
        let proof = OsDeployTransitionProof::activated_scope_expired(&f.scheduler, &mut tx, op, 3)
            .await
            .unwrap();
        let OsDeployTransitionProof::ActivatedScopeExpired(p) = proof else {
            panic!("wrong closed proof");
        };
        assert_eq!(p.value.attempt_id, Some(g.attempt_id()));
        assert!(p.value.evaluated_at >= *g.deadline_at());
        assert!(
            matches!(p.value.detail,wire::Detail::ActivatedScopeExpired(wire::ActivatedExpiry {deadline_at,reason:wire::Reason::PhaseDeadlineExpired,..}) if deadline_at==*g.deadline_at())
        );
        tx.rollback().await.unwrap();
        let mut tx = f.pool.begin().await.unwrap();
        assert!(
            OsDeployTransitionProof::unactivated_scope_expired(
                &f.scheduler,
                &mut tx,
                ids.operation(OsDeployStage::PeRegister),
                0
            )
            .await
            .is_err()
        );
        tx.rollback().await.unwrap();
        for exposed in [false, true] {
            let mut tx = f.pool.begin().await.unwrap();
            let result = if exposed {
                OsDeployTransitionProof::cancelled_exposed(&f.scheduler, &mut tx, op, 3).await
            } else {
                OsDeployTransitionProof::cancelled_unexposed(&f.scheduler, &mut tx, op, 3).await
            };
            assert!(matches!(result, Err(Error::FenceLost)));
            tx.rollback().await.unwrap();
        }
        assert_eq!(
            f.store.load_osdeploy_operation(op).await.unwrap().state(),
            ExecutionState::Leased
        );
    }

    #[tokio::test]
    async fn private_reclaim_and_repark_require_expired_exact_epoch_and_original_budget() {
        let f = Database::new().await;
        let a = f.register(300, false).await;
        let b = f.register(300, true).await;
        let op = a.operation(OsDeployStage::Clone);
        let running = b.operation(OsDeployStage::Clone);
        let first = f
            .scheduler
            .claim_osdeploy_bound(op, a.workflow_sha256(), 2)
            .await
            .unwrap()
            .unwrap();
        let second = f
            .scheduler
            .claim_osdeploy_bound(running, b.workflow_sha256(), 2)
            .await
            .unwrap()
            .unwrap();
        f.scheduler
            .start_osdeploy_bound(&second, b.workflow_sha256())
            .await
            .unwrap();
        let mut tx = f.pool.begin().await.unwrap();
        assert!(matches!(
            OsDeployTransitionProof::expired_unstarted_same_attempt(&f.scheduler, &mut tx, op, 3)
                .await,
            Err(Error::FenceLost)
        ));
        tx.rollback().await.unwrap();
        let mut tx = f.pool.begin().await.unwrap();
        assert!(matches!(
            OsDeployTransitionProof::expired_read_only_evaluation(
                &f.scheduler,
                &mut tx,
                running,
                6
            )
            .await,
            Err(Error::FenceLost)
        ));
        tx.rollback().await.unwrap();
        tokio::time::timeout(Duration::from_secs(32),sqlx::query("SELECT pg_sleep(GREATEST(0,extract(epoch FROM ($1::timestamptz-clock_timestamp()))))").bind(second.lease_expires_at()).execute(&f.pool)).await.unwrap().unwrap();
        let mut tx = f.pool.begin().await.unwrap();
        let proof =
            OsDeployTransitionProof::expired_unstarted_same_attempt(&f.scheduler, &mut tx, op, 3)
                .await
                .unwrap();
        let OsDeployTransitionProof::ExpiredUnstartedSameAttempt(p) = proof else {
            panic!("wrong closed proof");
        };
        assert_eq!(p.value.attempt_id, Some(first.attempt_id()));
        assert!(
            matches!(p.value.detail,wire::Detail::LeaseReclaimedSameAttempt(wire::Reclaimed {deadline_at,..}) if deadline_at==*first.deadline_at())
        );
        tx.rollback().await.unwrap();
        let mut tx = f.pool.begin().await.unwrap();
        let proof = OsDeployTransitionProof::expired_read_only_evaluation(
            &f.scheduler,
            &mut tx,
            running,
            6,
        )
        .await
        .unwrap();
        let OsDeployTransitionProof::ExpiredReadOnlyEvaluation(p) = proof else {
            panic!("wrong closed proof");
        };
        assert_eq!(p.value.attempt_id, Some(second.attempt_id()));
        assert!(
            matches!(p.value.detail,wire::Detail::EvaluationReparked(wire::Reparked {deadline_at,..}) if deadline_at==*second.deadline_at())
        );
        tx.rollback().await.unwrap();
        assert_eq!(
            f.store.load_osdeploy_operation(op).await.unwrap().state(),
            ExecutionState::Leased
        );
        assert_eq!(
            f.store
                .load_osdeploy_operation(running)
                .await
                .unwrap()
                .state(),
            ExecutionState::Running
        );
    }
}

//! One actual attempt, short evaluator leases, and read-only continuation.
use super::*;

// A disposable projection is only a due hint. Compare it to the latest actual
// selected park and immutable attempt/scope before allocating another epoch.
async fn waiting_basis(
    tx: &mut Transaction<'_, Postgres>,
    snapshot: &OsDeployOperationSnapshot,
) -> Result<(EventId, DateTime<Utc>), Error> {
    let row = sqlx::query("SELECT event_id,decision_revision,payload_canonical_json FROM rust_controller.osdeploy_decisions WHERE operation_id=$1 AND resolution IS NOT NULL AND resolution<>'ready' ORDER BY decision_revision DESC LIMIT 1")
        .bind(snapshot.operation_id().as_uuid()).fetch_one(&mut **tx).await?;
    let event: EventId = load::id(row.try_get("event_id")?)?;
    let decision =
        wire::DecisionEnvelope::decode(&row.try_get::<String, _>("payload_canonical_json")?)?;
    let schedule = match &decision.detail {
        wire::Detail::PveEvaluated(value) => value.schedule.as_ref(),
        wire::Detail::EvaluationReparked(value) => Some(&value.schedule),
        _ => None,
    }
    .ok_or(Error::Validation)?;
    let due = schedule.next_check_at.ok_or(Error::Validation)?;
    wire::require(
        decision.resolution == Some(pve_port::NativeDecision::Waiting)
            && decision.attempt_id == snapshot.attempt_id()
            && snapshot.next_check_at() == Some(due)
            && schedule.mode == wire::ScheduleMode::Waiting,
    )?;
    let projection = sqlx::query("SELECT * FROM rust_controller.osdeploy_schedule_projection WHERE operation_id=$1 FOR UPDATE").bind(snapshot.operation_id().as_uuid()).fetch_optional(&mut **tx).await?.ok_or(Error::Validation)?;
    wire::require(
        projection.try_get::<Uuid, _>("run_id")? == snapshot.run_id().as_uuid()
            && Some(load::id::<AttemptId>(projection.try_get("attempt_id")?)?)
                == snapshot.attempt_id()
            && projection.try_get::<String, _>("mode")? == "waiting"
            && projection.try_get::<Uuid, _>("basis_event_id")? == event.as_uuid()
            && projection.try_get::<i64, _>("basis_revision")?
                == row.try_get::<i64, _>("decision_revision")?
            && projection.try_get::<String, _>("scope_key")?
                == serde_json::to_value(load::stage_scope(snapshot.plan().stage()))?
                    .as_str()
                    .ok_or(Error::Validation)?
            && projection.try_get::<Option<DateTime<Utc>>, _>("next_check_at")? == Some(due)
            && projection.try_get::<i16, _>("unavailable_count")?
                == i16::from(schedule.unavailable_count)
            && projection.try_get::<i64, _>("rebuilt_through_revision")? <= snapshot.revision(),
    )?;
    Ok((event, due))
}

impl Scheduler {
    pub async fn resume_osdeploy_bound(
        &self,
        operation: OperationId,
        attempt: AttemptId,
        expected_revision: i64,
        workflow_sha256: &str,
        cap: u32,
    ) -> Result<Option<LeaseGrant>, Error> {
        Box::pin(async move {
            if cap == 0 { return Err(Error::Validation); }
            let mut tx = self.store.pool().begin().await?;
            authority(self, &mut tx).await?;
            let snapshot = Box::pin(locked_execution_with_cap(&mut tx, operation, true)).await?;
            admit_delivery_policy(&mut tx, self, &snapshot).await?;
            admit(self, &snapshot, workflow_sha256)?;
            if snapshot.revision() != expected_revision || snapshot.attempt_id() != Some(attempt) {
                return Err(Error::FenceLost);
            }
            if snapshot.state() != ExecutionState::Waiting { tx.commit().await?; return Ok(None); }
            let residual: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.worker_leases WHERE operation_id=$1)").bind(operation.as_uuid()).fetch_one(&mut *tx).await?;
            if residual { tx.commit().await?; return Ok(None); }
            let basis = waiting_basis(&mut tx, &snapshot).await?;
            let at = now(&mut tx).await?;
            let deadline = snapshot.deadline_at().ok_or(Error::Validation)?;
            if at >= deadline { return Err(Error::FenceLost); }
            if at < basis.1 { tx.commit().await?; return Ok(None); }
            let active: i64 = sqlx::query_scalar("SELECT count(*) FROM rust_controller.worker_leases l JOIN rust_controller.operations o USING(operation_id) WHERE o.workflow_kind='os_deploy' AND l.lease_expires_at>clock_timestamp()").fetch_one(&mut *tx).await?;
            if active >= i64::from(cap) { tx.commit().await?; return Ok(None); }
            let at = now(&mut tx).await?;
            if at >= deadline { return Err(Error::FenceLost); }
            let expiry = (at + chrono::Duration::seconds(30)).min(deadline);
            let event = EventId::new();
            let token = Uuid::now_v7();
            let token_hash: String = sqlx::query_scalar("SELECT encode(sha256(convert_to($1,'UTF8')),'hex')").bind(token.to_string()).fetch_one(&mut *tx).await?;
            let acquisition = envelope(&snapshot, attempt, self.generation, expected_revision, at,
                wire::Detail::LeaseAcquired(wire::Acquisition {
                    purpose: wire::Purpose::ResumeEvaluation, acquisition_event_id: event,
                    token_sha256: token_hash.clone(), worker_id: self.worker_id.clone(),
                    acquired_at: at, expires_at: expiry, deadline_at: deadline,
                    prior_schedule_event_id: Some(basis.0),
                }))?;
            append_osdeploy_decision(&mut tx,event,&format!("osdeploy:acquire:{}",event.as_uuid()),&acquisition).await?;
            sqlx::query("INSERT INTO rust_controller.osdeploy_lease_epochs(acquisition_event_id,operation_id,run_id,attempt_id,executor_kind,generation,worker_id,lease_token_sha256,acquired_at,initial_expires_at,deadline_at,purpose) VALUES($1,$2,$3,$4,'rust',$5,$6,$7,$8,$9,$10,'resume_evaluation')")
                .bind(event.as_uuid()).bind(operation.as_uuid()).bind(snapshot.run_id().as_uuid()).bind(attempt.as_uuid()).bind(self.generation).bind(&self.worker_id).bind(token_hash).bind(at).bind(expiry).bind(deadline).execute(&mut *tx).await?;
            sqlx::query("INSERT INTO rust_controller.worker_leases(operation_id,attempt_id,executor_kind,generation,worker_id,lease_token,acquired_at,heartbeat_at,lease_expires_at,deadline_at) VALUES($1,$2,'rust',$3,$4,$5,$6,$6,$7,$8)")
                .bind(operation.as_uuid()).bind(attempt.as_uuid()).bind(self.generation).bind(&self.worker_id).bind(token.to_string()).bind(at).bind(expiry).bind(deadline).execute(&mut *tx).await?;
            sqlx::query("DELETE FROM rust_controller.osdeploy_schedule_projection WHERE operation_id=$1").bind(operation.as_uuid()).execute(&mut *tx).await?;
            let grant = LeaseGrant::new(operation,attempt,1,ExecutorKind::Rust,self.generation,self.worker_id.clone(),token,at,at,expiry,deadline);
            Box::pin(load::load_execution(&mut tx,operation)).await?;
            active_at(&grant,now(&mut tx).await?)?;
            tx.commit().await?;
            Ok(Some(grant))
        }).await
    }

    pub async fn osdeploy_authority_snapshot(&self) -> Result<AuthoritySnapshot, Error> {
        let mut tx = self.store.pool().begin().await?;
        let result = authority(self, &mut tx).await?;
        tx.commit().await?;
        Ok(result)
    }

    pub async fn claim_osdeploy_bound(
        &self,
        operation: OperationId,
        workflow_sha256: &str,
        cap: u32,
    ) -> Result<Option<LeaseGrant>, Error> {
        Box::pin(async move {
        if cap == 0 {
            return Err(Error::Validation);
        }
        let mut tx = self.store.pool().begin().await?;
        authority(self, &mut tx).await?;
        let snapshot = locked_execution_with_cap(&mut tx, operation, true).await?;
        admit_delivery_policy(&mut tx, self, &snapshot).await?;
        if snapshot.plan().stage() == OsDeployStage::StartPe && !self.fixture_start_pe {
            return Err(Error::CapabilityUnavailable);
        }
        admit(self, &snapshot, workflow_sha256)?;
        if snapshot.state() == ExecutionState::Pending && snapshot.attempt_id().is_some() {
            let grant = reclaim_lease(self, &mut tx, &snapshot, cap).await?;
            tx.commit().await?;
            return Ok(grant);
        }
        if snapshot.state() != ExecutionState::Pending
            || snapshot.attempt_id().is_some()
            || snapshot.dispatch().is_some()
        {
            tx.commit().await?;
            return Ok(None);
        }
        let reg = load::load_registration(&mut tx, snapshot.run_id()).await?;
        let predecessor = match snapshot.plan().stage() {
            OsDeployStage::Clone => None,
            OsDeployStage::DiskCapacity => Some(reg.ids().operation(OsDeployStage::Clone)),
            OsDeployStage::ConfigurePe => Some(reg.ids().operation(OsDeployStage::DiskCapacity)),
            OsDeployStage::StartPe if self.fixture_start_pe => Some(reg.ids().operation(OsDeployStage::ConfigurePe)),
            _ => return Err(Error::CapabilityUnavailable),
        };
        let predecessor_event = if let Some(prior) = predecessor {
            let prior_snapshot = load::load_execution(&mut tx, prior).await?;
            if prior_snapshot.state() != ExecutionState::Satisfied {
                tx.commit().await?;
                return Ok(None);
            }
            // Exact selected history, never an operations.state-only predecessor.
            let event: Uuid = sqlx::query_scalar("SELECT event_id FROM rust_controller.osdeploy_decisions WHERE operation_id=$1 AND resolution IS NOT NULL AND resolution<>'ready' ORDER BY decision_revision DESC LIMIT 1")
                .bind(prior.as_uuid()).fetch_one(&mut *tx).await?;
            Some(load::id(event)?)
        } else {
            None
        };
        let active: i64 = sqlx::query_scalar("SELECT count(*) FROM rust_controller.worker_leases l JOIN rust_controller.operations o USING(operation_id) WHERE o.workflow_kind='os_deploy' AND l.lease_expires_at>clock_timestamp()")
            .fetch_one(&mut *tx).await?;
        if active >= i64::from(cap) {
            tx.commit().await?;
            return Ok(None);
        }
        let at = now(&mut tx).await?;
        let budget = reg.plan().policy().mutation_seconds();
        let deadline = at
            .checked_add_signed(chrono::Duration::seconds(i64::from(budget)))
            .ok_or(Error::Validation)?;
        let expiry = (at + chrono::Duration::seconds(30)).min(deadline);
        let attempt = AttemptId::new();
        let activation_event = EventId::new();
        let acquisition_event = EventId::new();
        let token = Uuid::now_v7();
        let token_hash: String =
            sqlx::query_scalar("SELECT encode(sha256(convert_to($1,'UTF8')),'hex')")
                .bind(token.to_string())
                .fetch_one(&mut *tx)
                .await?;
        let scope = load::stage_scope(snapshot.plan().stage());
        let scope_name = serde_json::to_value(scope)?
            .as_str()
            .ok_or(Error::Validation)?
            .to_owned();
        // Immediate FK order: attempt -> activation event/index -> scope ->
        // binding -> acquisition event/index -> epoch -> lease -> state.
        sqlx::query("INSERT INTO rust_controller.attempts(attempt_id,operation_id,attempt_number,state,started_at,deadline_at) VALUES($1,$2,1,'leased',$3,$4)")
            .bind(attempt.as_uuid()).bind(operation.as_uuid()).bind(at).bind(deadline).execute(&mut *tx).await?;
        let activation = envelope(
            &snapshot,
            attempt,
            self.generation,
            snapshot.revision(),
            at,
            wire::Detail::StageActivated(wire::Activation {
                scope_key: scope,
                anchor_operation_id: operation,
                anchor_event_id: activation_event,
                opened_at: at,
                budget_seconds: budget,
                deadline_at: deadline,
                predecessor_operation_id: predecessor,
                predecessor_decision_event_id: predecessor_event,
            }),
        )?;
        let revision = append_osdeploy_decision(
            &mut tx,
            activation_event,
            "osdeploy:activation",
            &activation,
        )
        .await?;
        sqlx::query("INSERT INTO rust_controller.osdeploy_deadlines(run_id,scope_key,anchor_operation_id,anchor_event_id,opened_at,budget_seconds,deadline_at) VALUES($1,$2,$3,$4,$5,$6,$7)")
            .bind(snapshot.run_id().as_uuid()).bind(&scope_name).bind(operation.as_uuid()).bind(activation_event.as_uuid()).bind(at).bind(i32::try_from(budget).map_err(|_| Error::Validation)?).bind(deadline).execute(&mut *tx).await?;
        sqlx::query("INSERT INTO rust_controller.osdeploy_attempt_bindings(operation_id,run_id,attempt_id,scope_key,activation_event_id,activated_at,deadline_at,activation_mode) VALUES($1,$2,$3,$4,$5,$6,$7,'leased')")
            .bind(operation.as_uuid()).bind(snapshot.run_id().as_uuid()).bind(attempt.as_uuid()).bind(&scope_name).bind(activation_event.as_uuid()).bind(at).bind(deadline).execute(&mut *tx).await?;
        let acquisition = envelope(
            &snapshot,
            attempt,
            self.generation,
            revision,
            at,
            wire::Detail::LeaseAcquired(wire::Acquisition {
                purpose: wire::Purpose::InitialEvaluation,
                acquisition_event_id: acquisition_event,
                token_sha256: token_hash.clone(),
                worker_id: self.worker_id.clone(),
                acquired_at: at,
                expires_at: expiry,
                deadline_at: deadline,
                prior_schedule_event_id: None,
            }),
        )?;
        append_osdeploy_decision(
            &mut tx,
            acquisition_event,
            &format!("osdeploy:acquire:{}", acquisition_event.as_uuid()),
            &acquisition,
        )
        .await?;
        sqlx::query("INSERT INTO rust_controller.osdeploy_lease_epochs(acquisition_event_id,operation_id,run_id,attempt_id,executor_kind,generation,worker_id,lease_token_sha256,acquired_at,initial_expires_at,deadline_at,purpose) VALUES($1,$2,$3,$4,'rust',$5,$6,$7,$8,$9,$10,'initial_evaluation')")
            .bind(acquisition_event.as_uuid()).bind(operation.as_uuid()).bind(snapshot.run_id().as_uuid()).bind(attempt.as_uuid()).bind(self.generation).bind(&self.worker_id).bind(token_hash).bind(at).bind(expiry).bind(deadline).execute(&mut *tx).await?;
        sqlx::query("INSERT INTO rust_controller.worker_leases(operation_id,attempt_id,executor_kind,generation,worker_id,lease_token,acquired_at,heartbeat_at,lease_expires_at,deadline_at) VALUES($1,$2,'rust',$3,$4,$5,$6,$6,$7,$8)")
            .bind(operation.as_uuid()).bind(attempt.as_uuid()).bind(self.generation).bind(&self.worker_id).bind(token.to_string()).bind(at).bind(expiry).bind(deadline).execute(&mut *tx).await?;
        let proof = OsDeployTransitionProof::initial_lease(&mut tx, acquisition_event).await?;
        append_osdeploy_transition(&mut tx, proof).await?;
        let grant = LeaseGrant::new(
            operation,
            attempt,
            1,
            ExecutorKind::Rust,
            self.generation,
            self.worker_id.clone(),
            token,
            at,
            at,
            expiry,
            deadline,
        );
        load::load_execution(&mut tx, operation).await?;
        active_at(&grant, now(&mut tx).await?)?;
        tx.commit().await?;
        Ok(Some(grant))
        }).await
    }

    pub async fn start_osdeploy_bound(
        &self,
        grant: &LeaseGrant,
        workflow_sha256: &str,
    ) -> Result<ExecutionState, Error> {
        Box::pin(async move {
        let mut tx = self.store.pool().begin().await?;
        authority(self, &mut tx).await?;
        let snapshot = locked_execution(&mut tx, grant.operation_id()).await?;
        admit(self, &snapshot, workflow_sha256)?;
        let (current, epoch) = current_grant(&mut tx, self, grant, &snapshot).await?;
        let at = now(&mut tx).await?;
        active_at(&current, at)?;
        let existing: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.osdeploy_decisions WHERE operation_id=$1 AND action='evaluation_started' AND payload_canonical_json::jsonb->'detail'->>'lease_acquisition_event_id'=$2)")
            .bind(grant.operation_id().as_uuid()).bind(epoch.as_uuid().to_string()).fetch_one(&mut *tx).await?;
        if existing {
            if snapshot.state() != ExecutionState::Running {
                return Err(Error::Conflict);
            }
            active_at(&current, now(&mut tx).await?)?;
            tx.commit().await?;
            return Ok(ExecutionState::Running);
        }
        let resumed = snapshot.state() == ExecutionState::Waiting;
        if !resumed && (snapshot.state() != ExecutionState::Leased || snapshot.dispatch().is_some()) {
            return Err(Error::FenceLost);
        }
        let revision = if resumed { snapshot.revision() } else { append_attempt_started_event(
            &mut tx,
            grant.operation_id(),
            grant.attempt_id(),
            snapshot.revision(),
            at,
        )
        .await
        .map_err(scheduler_error)? };
        let event = EventId::new();
        let started = envelope(
            &snapshot,
            grant.attempt_id(),
            self.generation,
            revision,
            at,
            wire::Detail::EvaluationStarted(wire::Started {
                lease_acquisition_event_id: epoch,
                activity: if snapshot.dispatch().is_some() { wire::Activity::OutcomeRead } else { wire::Activity::PreflightRead },
            }),
        )?;
        append_osdeploy_decision(
            &mut tx,
            event,
            &format!("osdeploy:start:{}", epoch.as_uuid()),
            &started,
        )
        .await?;
        let proof = if resumed { OsDeployTransitionProof::resumed_start(&mut tx,event).await? } else { OsDeployTransitionProof::first_start(&mut tx, event).await? };
        append_osdeploy_transition(&mut tx, proof).await?;
        sqlx::query("UPDATE rust_controller.attempts SET state='running' WHERE attempt_id=$1 AND operation_id=$2")
            .bind(grant.attempt_id().as_uuid()).bind(grant.operation_id().as_uuid()).execute(&mut *tx).await?;
        load::load_execution(&mut tx, grant.operation_id()).await?;
        active_at(&current, now(&mut tx).await?)?;
        tx.commit().await?;
        Ok(ExecutionState::Running)
        }).await
    }

    pub async fn heartbeat_osdeploy_bound(
        &self,
        grant: &LeaseGrant,
        workflow_sha256: &str,
    ) -> Result<OsDeployLeaseStatus, Error> {
        let mut tx = self.store.pool().begin().await?;
        authority(self, &mut tx).await?;
        let snapshot = locked_execution(&mut tx, grant.operation_id()).await?;
        admit(self, &snapshot, workflow_sha256)?;
        let (mut current, epoch) = current_grant(&mut tx, self, grant, &snapshot).await?;
        let at = now(&mut tx).await?;
        active_at(&current, at)?;
        let mut revision = snapshot.revision();
        // Suppress eager duplicate requests. Lease refresh is no more frequent
        // than ten seconds and never opens another logical attempt or budget.
        if at - current.heartbeat_at >= chrono::Duration::seconds(10) {
            let expiry = (at + chrono::Duration::seconds(30)).min(current.deadline_at);
            let renewal = envelope(
                &snapshot,
                grant.attempt_id(),
                self.generation,
                revision,
                at,
                wire::Detail::LeaseRenewed(wire::Renewed {
                    lease_acquisition_event_id: epoch,
                    heartbeat_at: at,
                    expires_at: expiry,
                    deadline_at: current.deadline_at,
                }),
            )?;
            revision = append_osdeploy_decision(
                &mut tx,
                EventId::new(),
                &format!("osdeploy:renew:{}:{revision}", epoch.as_uuid()),
                &renewal,
            )
            .await?;
            sqlx::query("UPDATE rust_controller.worker_leases SET heartbeat_at=$2,lease_expires_at=$3 WHERE operation_id=$1 AND lease_token=$4")
                .bind(grant.operation_id().as_uuid()).bind(at).bind(expiry).bind(grant.lease_token().to_string()).execute(&mut *tx).await?;
            current.heartbeat_at = at;
            current.lease_expires_at = expiry;
            load::load_execution(&mut tx, grant.operation_id()).await?;
        }
        let checked_at = now(&mut tx).await?;
        active_at(&current, checked_at)?;
        tx.commit().await?;
        Ok(OsDeployLeaseStatus {
            grant: current,
            revision,
            checked_at,
        })
    }

    pub async fn continuation_osdeploy_bound(
        &self,
        grant: &LeaseGrant,
        workflow_sha256: &str,
    ) -> Result<OsDeployLeaseStatus, Error> {
        let mut tx = self.store.pool().begin().await?;
        authority(self, &mut tx).await?;
        let snapshot = locked_execution(&mut tx, grant.operation_id()).await?;
        admit(self, &snapshot, workflow_sha256)?;
        let (current, _) = current_grant(&mut tx, self, grant, &snapshot).await?;
        let checked_at = now(&mut tx).await?;
        active_at(&current, checked_at)?;
        tx.commit().await?;
        Ok(OsDeployLeaseStatus {
            grant: current,
            revision: snapshot.revision(),
            checked_at,
        })
    }
}

async fn reclaim_lease(
    s: &Scheduler,
    tx: &mut Transaction<'_, Postgres>,
    snapshot: &OsDeployOperationSnapshot,
    cap: u32,
) -> Result<Option<LeaseGrant>, Error> {
    let operation = snapshot.operation_id();
    let attempt = snapshot.attempt_id().ok_or(Error::Validation)?;
    let deadline = snapshot.deadline_at().ok_or(Error::Validation)?;
    #[cfg(feature = "fixture-ipc")]
    let credential = s.fixture_credential_delivery
        && requires_delivery(tx, snapshot).await?
        && snapshot.dispatch().is_some()
        && fixture_delivery::unacknowledged(tx, operation).await?;
    #[cfg(not(feature = "fixture-ipc"))]
    let credential = false;
    let reclaimed: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.osdeploy_decisions WHERE operation_id=$1 AND action='lease_reclaimed_same_attempt') AND NOT EXISTS(SELECT 1 FROM rust_controller.worker_leases WHERE operation_id=$1) AND NOT EXISTS(SELECT 1 FROM rust_controller.journal_events WHERE operation_id=$1 AND event_kind='attempt_started')")
        .bind(operation.as_uuid()).fetch_one(&mut **tx).await?;
    let credential_reclaimed: bool = if credential {
        sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.osdeploy_decisions WHERE operation_id=$1 AND action='credential_delivery_reclaimed') AND NOT EXISTS(SELECT 1 FROM rust_controller.worker_leases WHERE operation_id=$1)")
            .bind(operation.as_uuid()).fetch_one(&mut **tx).await?
    } else {
        false
    };
    if !credential_reclaimed && (!reclaimed || snapshot.dispatch().is_some()) {
        return Err(Error::Validation);
    }
    let at = now(tx).await?;
    if at >= deadline {
        return Err(Error::FenceLost);
    }
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM rust_controller.worker_leases l JOIN rust_controller.operations o USING(operation_id) WHERE o.workflow_kind='os_deploy' AND l.lease_expires_at>clock_timestamp()").fetch_one(&mut **tx).await?;
    if count >= i64::from(cap) {
        return Ok(None);
    }
    let at = now(tx).await?;
    if at >= deadline {
        return Err(Error::FenceLost);
    }
    let expiry = (at + chrono::Duration::seconds(30)).min(deadline);
    let event = EventId::new();
    let token = Uuid::now_v7();
    let token_hash: String =
        sqlx::query_scalar("SELECT encode(sha256(convert_to($1,'UTF8')),'hex')")
            .bind(token.to_string())
            .fetch_one(&mut **tx)
            .await?;
    let value = envelope(
        snapshot,
        attempt,
        s.generation,
        snapshot.revision(),
        at,
        wire::Detail::LeaseAcquired(wire::Acquisition {
            purpose: if credential {
                wire::Purpose::ReclaimedCredentialDelivery
            } else {
                wire::Purpose::ReclaimedEvaluation
            },
            acquisition_event_id: event,
            token_sha256: token_hash.clone(),
            worker_id: s.worker_id.clone(),
            acquired_at: at,
            expires_at: expiry,
            deadline_at: deadline,
            prior_schedule_event_id: None,
        }),
    )?;
    append_osdeploy_decision(
        tx,
        event,
        &format!("osdeploy:acquire:{}", event.as_uuid()),
        &value,
    )
    .await?;
    sqlx::query("INSERT INTO rust_controller.osdeploy_lease_epochs(acquisition_event_id,operation_id,run_id,attempt_id,executor_kind,generation,worker_id,lease_token_sha256,acquired_at,initial_expires_at,deadline_at,purpose) VALUES($1,$2,$3,$4,'rust',$5,$6,$7,$8,$9,$10,$11)")
        .bind(event.as_uuid()).bind(operation.as_uuid()).bind(snapshot.run_id().as_uuid()).bind(attempt.as_uuid()).bind(s.generation).bind(&s.worker_id).bind(token_hash).bind(at).bind(expiry).bind(deadline)
        .bind(if credential { "reclaimed_credential_delivery" } else { "reclaimed_evaluation" }).execute(&mut **tx).await?;
    sqlx::query("INSERT INTO rust_controller.worker_leases(operation_id,attempt_id,executor_kind,generation,worker_id,lease_token,acquired_at,heartbeat_at,lease_expires_at,deadline_at) VALUES($1,$2,'rust',$3,$4,$5,$6,$6,$7,$8)")
        .bind(operation.as_uuid()).bind(attempt.as_uuid()).bind(s.generation).bind(&s.worker_id).bind(token.to_string()).bind(at).bind(expiry).bind(deadline).execute(&mut **tx).await?;
    let proof = if credential {
        #[cfg(feature = "fixture-ipc")]
        {
            OsDeployTransitionProof::reclaimed_credential_lease(tx, event).await?
        }
        #[cfg(not(feature = "fixture-ipc"))]
        {
            return Err(Error::CapabilityUnavailable);
        }
    } else {
        OsDeployTransitionProof::reclaimed_lease(tx, event).await?
    };
    append_osdeploy_transition(tx, proof).await?;
    sqlx::query(
        "UPDATE rust_controller.attempts SET state=$3 WHERE operation_id=$1 AND attempt_id=$2",
    )
    .bind(operation.as_uuid())
    .bind(attempt.as_uuid())
    .bind(if credential { "running" } else { "leased" })
    .execute(&mut **tx)
    .await?;
    let grant = LeaseGrant::new(
        operation,
        attempt,
        1,
        ExecutorKind::Rust,
        s.generation,
        s.worker_id.clone(),
        token,
        at,
        at,
        expiry,
        deadline,
    );
    Box::pin(load::load_execution(tx, operation)).await?;
    active_at(&grant, now(tx).await?)?;
    Ok(Some(grant))
}

//! Durable selection of the actual provisioning evaluator result.
use super::*;
use crate::OsDeployProgress;
use crate::osdeploy::execution::history;
use pve_port::{
    NativeDecision, ProvisioningEvaluationModeV1, evaluate_provisioning_outcome,
    evaluate_provisioning_preflight,
};

impl Scheduler {
    pub async fn decide_osdeploy_pve(
        &self,
        grant: &LeaseGrant,
        expected_revision: i64,
        evidence_event: EventId,
    ) -> Result<OsDeployProgress, Error> {
        Box::pin(async move {
        let mut tx = self.store.pool().begin().await?;
        authority(self, &mut tx).await?;
        let snapshot = Box::pin(locked_execution(&mut tx, grant.operation_id())).await?;
        admit(&snapshot, snapshot.plan().workflow_sha256())?;
        self.validate_grant_owner(grant).map_err(scheduler_error)?;
        if let Some(row) = sqlx::query("SELECT d.payload_canonical_json,e.attempt_id,e.generation,e.worker_id,e.acquired_at,e.deadline_at,e.lease_token_sha256=encode(sha256(convert_to($3,'UTF8')),'hex') AS token_matches FROM rust_controller.osdeploy_decisions d JOIN rust_controller.osdeploy_lease_epochs e ON e.acquisition_event_id=(d.payload_canonical_json::jsonb->'detail'->>'lease_acquisition_event_id')::uuid AND e.operation_id=d.operation_id WHERE d.operation_id=$1 AND d.action='pve_evaluated' AND d.payload_canonical_json::jsonb->'detail'->>'evidence_event_id'=$2")
            .bind(grant.operation_id().as_uuid()).bind(evidence_event.as_uuid().to_string()).bind(grant.lease_token().to_string()).fetch_optional(&mut *tx).await? {
            // This acknowledges an already selected event, without evaluating
            // again, reacquiring a lease, or minting any continuation authority.
            let prior = wire::DecisionEnvelope::decode(&row.try_get::<String,_>("payload_canonical_json")?)?;
            if row.try_get::<Uuid,_>("attempt_id")? != grant.attempt_id().as_uuid()
                || row.try_get::<i64,_>("generation")? != self.generation
                || row.try_get::<String,_>("worker_id")? != self.worker_id
                || row.try_get::<DateTime<Utc>,_>("acquired_at")? != *grant.acquired_at()
                || row.try_get::<DateTime<Utc>,_>("deadline_at")? != *grant.deadline_at()
                || !row.try_get::<bool,_>("token_matches")? || grant.attempt_number() != 1
            { return Err(Error::FenceLost); }
            if prior.before_revision != expected_revision { return Err(Error::Conflict); }
            let result = progress(prior.resolution.ok_or(Error::Validation)?)?;
            tx.commit().await?;
            return Ok(result);
        }
        if let Some(row) = sqlx::query("SELECT d.payload_canonical_json FROM rust_controller.osdeploy_decisions d JOIN rust_controller.osdeploy_lease_epochs e ON e.operation_id=d.operation_id AND e.attempt_id=d.attempt_id WHERE d.operation_id=$1 AND d.action='activated_scope_expired' AND e.lease_token_sha256=encode(sha256(convert_to($2,'UTF8')),'hex') AND e.acquisition_event_id=(SELECT prior.event_id FROM rust_controller.osdeploy_decisions prior WHERE prior.operation_id=d.operation_id AND prior.action='lease_acquired' AND prior.decision_revision<d.decision_revision ORDER BY prior.decision_revision DESC LIMIT 1) AND e.attempt_id=$3 AND e.generation=$4 AND e.worker_id=$5 AND e.acquired_at=$6 AND e.deadline_at=$7")
            .bind(grant.operation_id().as_uuid()).bind(grant.lease_token().to_string()).bind(grant.attempt_id().as_uuid()).bind(self.generation).bind(&self.worker_id).bind(grant.acquired_at()).bind(grant.deadline_at()).fetch_optional(&mut *tx).await? {
            let prior = wire::DecisionEnvelope::decode(&row.try_get::<String,_>("payload_canonical_json")?)?;
            if prior.before_revision != expected_revision || grant.attempt_number() != 1 { return Err(Error::Conflict); }
            tx.commit().await?;
            return Ok(OsDeployProgress::Decided(ExecutionState::Unknown));
        }
        let (current, epoch) = current_grant(&mut tx, self, grant, &snapshot).await?;
        if snapshot.state() != ExecutionState::Running || snapshot.revision() != expected_revision {
            return Err(Error::FenceLost);
        }
        let mode = if snapshot.dispatch().is_some() { ProvisioningEvaluationModeV1::Outcome } else { ProvisioningEvaluationModeV1::Preflight };
        let (context, evidence) = Box::pin(history::indexed_context(&mut tx, grant.operation_id(), expected_revision, evidence_event, mode)).await?;
        let at = now(&mut tx).await?;
        if scope_elapsed(&current, at) {
            let proof = OsDeployTransitionProof::activated_scope_expired(self,&mut tx,grant.operation_id(),expected_revision).await?;
            append_osdeploy_transition(&mut tx,proof).await?;
            let selected_at: DateTime<Utc> = sqlx::query_scalar("SELECT evaluated_at FROM rust_controller.osdeploy_decisions WHERE operation_id=$1 AND action='activated_scope_expired'").bind(grant.operation_id().as_uuid()).fetch_one(&mut *tx).await?;
            sqlx::query("UPDATE rust_controller.attempts SET state='unknown',completed_at=$3 WHERE operation_id=$1 AND attempt_id=$2").bind(grant.operation_id().as_uuid()).bind(grant.attempt_id().as_uuid()).bind(selected_at).execute(&mut *tx).await?;
            sqlx::query("DELETE FROM rust_controller.osdeploy_schedule_projection WHERE operation_id=$1").bind(grant.operation_id().as_uuid()).execute(&mut *tx).await?;
            remove_matching_lease(&mut tx,grant).await?;
            Box::pin(load::load_execution(&mut tx,grant.operation_id())).await?;
            tx.commit().await?;
            return Ok(OsDeployProgress::Decided(ExecutionState::Unknown));
        }
        active_at(&current, at)?;
        let evaluate = |at| if mode == ProvisioningEvaluationModeV1::Preflight {
            evaluate_provisioning_preflight(&context, &evidence, at)
        } else {
            evaluate_provisioning_outcome(&context, &evidence, at)
        };
        let advice = evaluate(at);
        let target = match advice.decision {
            NativeDecision::Ready => return Err(Error::Validation),
            NativeDecision::Waiting => ExecutionState::Waiting,
            NativeDecision::Satisfied => ExecutionState::Satisfied,
            NativeDecision::Failed => ExecutionState::Failed,
            NativeDecision::Blocked => ExecutionState::Blocked,
            NativeDecision::Unknown => ExecutionState::Unknown,
            NativeDecision::Conflicted => ExecutionState::Conflicted,
        };
        let schedule = (target == ExecutionState::Waiting).then_some(wire::Schedule {
            mode: wire::ScheduleMode::Waiting,
            next_check_at: Some((at + chrono::Duration::seconds(2)).min(*grant.deadline_at())),
            unavailable_count: 0,
        });
        let scope = load::stage_scope(snapshot.plan().stage());
        let mut decision = envelope(&snapshot, grant.attempt_id(), self.generation, expected_revision, at,
            wire::Detail::PveEvaluated(wire::Evaluated {
                mode: if mode == ProvisioningEvaluationModeV1::Preflight { wire::Mode::Preflight } else { wire::Mode::Outcome },
                advice: advice.decision,
                evidence_event_id: evidence_event,
                evidence_sha256: wire::digest(&evidence)?,
                scope_key: scope,
                deadline_at: *grant.deadline_at(),
                reason: advice.reason,
                lease_acquisition_event_id: Some(epoch),
                schedule: schedule.clone(),
            }))?;
        decision.resolution = Some(advice.decision);
        let event = EventId::new();
        let revision = append_osdeploy_decision(&mut tx, event,
            &format!("osdeploy:evaluate:{}:{}", if mode == ProvisioningEvaluationModeV1::Preflight { "preflight" } else { "outcome" }, evidence_event.as_uuid()), &decision).await?;
        // Target is derived only from the evaluator above, under this family's
        // authority/identity/CAS locks; no caller supplies a transition target.
        validate_transition(snapshot.state(), target, TransitionPolicy::Domain).map_err(|_| Error::Validation)?;
        let revision = persist_state_event(&mut tx, StateAppend {
            operation_id: grant.operation_id(), attempt_id: Some(grant.attempt_id()), revision,
            current: snapshot.state(), target,
            semantic_key: format!("osdeploy:state:{}", event.as_uuid()),
            payload: json!({"state":target,"decision_event_id":event}), observed_at: at,
            policy: TransitionPolicy::Domain,
        }).await.map_err(scheduler_error)?;
        sqlx::query("UPDATE rust_controller.attempts SET state=$3,completed_at=$4 WHERE operation_id=$1 AND attempt_id=$2")
            .bind(grant.operation_id().as_uuid()).bind(grant.attempt_id().as_uuid())
            .bind(execution_state_name(target)).bind(target.is_terminal().then_some(at)).execute(&mut *tx).await?;
        if let Some(schedule) = schedule {
            sqlx::query("INSERT INTO rust_controller.osdeploy_schedule_projection(operation_id,run_id,attempt_id,mode,basis_event_id,basis_revision,scope_key,next_check_at,unavailable_count,rebuilt_through_revision) VALUES($1,$2,$3,'waiting',$4,$5,$6,$7,0,$8) ON CONFLICT(operation_id) DO UPDATE SET run_id=EXCLUDED.run_id,attempt_id=EXCLUDED.attempt_id,mode=EXCLUDED.mode,basis_event_id=EXCLUDED.basis_event_id,basis_revision=EXCLUDED.basis_revision,scope_key=EXCLUDED.scope_key,next_check_at=EXCLUDED.next_check_at,unavailable_count=0,rebuilt_through_revision=EXCLUDED.rebuilt_through_revision")
                .bind(grant.operation_id().as_uuid()).bind(snapshot.run_id().as_uuid()).bind(grant.attempt_id().as_uuid()).bind(event.as_uuid()).bind(decision.before_revision + 1).bind(serde_json::to_value(scope)?.as_str().ok_or(Error::Validation)?).bind(schedule.next_check_at).bind(revision).execute(&mut *tx).await?;
        } else {
            sqlx::query("DELETE FROM rust_controller.osdeploy_schedule_projection WHERE operation_id=$1").bind(grant.operation_id().as_uuid()).execute(&mut *tx).await?;
        }
        remove_matching_lease(&mut tx,grant).await?;
        Box::pin(load::load_execution(&mut tx, grant.operation_id())).await?;
        let final_at = now(&mut tx).await?;
        active_at(&current, final_at)?;
        let final_advice = evaluate(final_at);
        if final_advice.decision != advice.decision || final_advice.reason != advice.reason {
            return Err(Error::FenceLost);
        }
        tx.commit().await?;
        Ok(if target == ExecutionState::Waiting { OsDeployProgress::Waiting } else { OsDeployProgress::Decided(target) })
        }).await
    }
}

fn scope_elapsed(grant: &LeaseGrant, at: DateTime<Utc>) -> bool {
    at >= *grant.deadline_at()
}

async fn remove_matching_lease(
    tx: &mut Transaction<'_, Postgres>,
    grant: &LeaseGrant,
) -> Result<(), Error> {
    if sqlx::query("DELETE FROM rust_controller.worker_leases WHERE operation_id=$1 AND attempt_id=$2 AND lease_token=$3")
        .bind(grant.operation_id().as_uuid()).bind(grant.attempt_id().as_uuid()).bind(grant.lease_token().to_string()).execute(&mut **tx).await?.rows_affected() != 1 {
        return Err(Error::FenceLost);
    }
    Ok(())
}

fn progress(value: NativeDecision) -> Result<OsDeployProgress, Error> {
    Ok(match value {
        NativeDecision::Ready => return Err(Error::Validation),
        NativeDecision::Waiting => OsDeployProgress::Waiting,
        NativeDecision::Satisfied => OsDeployProgress::Decided(ExecutionState::Satisfied),
        NativeDecision::Failed => OsDeployProgress::Decided(ExecutionState::Failed),
        NativeDecision::Blocked => OsDeployProgress::Decided(ExecutionState::Blocked),
        NativeDecision::Unknown => OsDeployProgress::Decided(ExecutionState::Unknown),
        NativeDecision::Conflicted => OsDeployProgress::Decided(ExecutionState::Conflicted),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn exact_scope_equality_selects_expiry_branch() {
        let at: DateTime<Utc> = "2026-09-06T00:00:00Z".parse().unwrap();
        let grant = LeaseGrant::new(
            OperationId::new(),
            AttemptId::new(),
            1,
            ExecutorKind::Rust,
            1,
            "owned".to_owned(),
            Uuid::now_v7(),
            at,
            at,
            at + chrono::Duration::seconds(2),
            at + chrono::Duration::seconds(2),
        );
        assert!(!scope_elapsed(
            &grant,
            *grant.deadline_at() - chrono::Duration::microseconds(1)
        ));
        assert!(scope_elapsed(&grant, *grant.deadline_at()));
        assert!(scope_elapsed(
            &grant,
            *grant.deadline_at() + chrono::Duration::microseconds(1)
        ));
    }
}

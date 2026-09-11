//! Fixture completion consumes the immutable package requirement, never a guest action.
use super::*;
use api_compat::run_bearer::{RunBearerIdentity, verify_run_bearer};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureBootFilesStagedResult {
    pub image_applied: bool,
    pub boot_files_staged: bool,
    pub boot_files_verified: bool,
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixturePeCompletionReport {
    pub definition_sha256: String,
    pub milestone_id: String,
    pub result: FixtureBootFilesStagedResult,
}
impl FixturePeCompletionReport {
    pub(crate) fn succeeded(&self) -> bool {
        self.result.image_applied
            && self.result.boot_files_staged
            && self.result.boot_files_verified
    }
    pub(crate) fn validate_package(
        &self,
        package: &crate::MaterializedPePackageSemanticsV1,
    ) -> Result<(), Error> {
        let body: serde_json::Value = serde_json::from_slice(package.canonical_bytes())?;
        wire::require(
            body["schema"] == "materialized_fixture_pe_completion_package_v1"
                && self.milestone_id == "boot-files-staged.v1"
                && body["completion_definition_sha256"] == self.definition_sha256
                && event_journal::payload_digest(&body["completion_requirement"])
                    .map_err(|_| Error::Validation)?
                    == self.definition_sha256,
        )
    }
}
#[derive(Debug)]
pub struct FixturePeCompletionResult {
    event: EventId,
    at: DateTime<Utc>,
    state: ExecutionState,
    replayed: bool,
}
impl FixturePeCompletionResult {
    pub fn selected_event_id(&self) -> EventId {
        self.event
    }
    pub fn selected_at(&self) -> DateTime<Utc> {
        self.at
    }
    pub fn state(&self) -> ExecutionState {
        self.state
    }
    pub fn replayed(&self) -> bool {
        self.replayed
    }
}

pub(super) async fn require_completion_origin(
    tx: &mut Transaction<'_, Postgres>,
    reg: &crate::OsDeployRegistrationV1,
) -> Result<(EventId, Vec<u8>), Error> {
    let registered = reg.ids().operation(OsDeployStage::PeRegister);
    let row: (Uuid, DateTime<Utc>, Vec<u8>, bool) = sqlx::query_as("SELECT r.selected_event_id,r.selected_at,r.alias_sha256,o.completion_package FROM rust_controller.fixture_pe_registrations r JOIN rust_controller.fixture_osdeploy_origins o USING(run_id) WHERE r.operation_id=$1 AND r.run_id=$2")
        .bind(registered.as_uuid()).bind(reg.ids().run_id().as_uuid()).fetch_one(&mut **tx).await?;
    wire::require(row.3)?;
    load::require_fixture_registration_origin(tx, reg, row.1).await?;
    wire::require(
        load::load_execution(tx, registered).await?.state() == ExecutionState::Satisfied,
    )?;
    Ok((load::id(row.0)?, row.2))
}

impl Scheduler {
    pub async fn accept_fixture_pe_completion(
        &self,
        grant: &LeaseGrant,
        authorization: &str,
        signing_secret: &[u8],
        report: &FixturePeCompletionReport,
    ) -> Result<FixturePeCompletionResult, Error> {
        if !self.fixture_credential_delivery {
            return Err(Error::CapabilityUnavailable);
        }
        let mut tx = self.store.pool().begin().await?;
        authority(self, &mut tx).await?;
        let snapshot = locked_execution(&mut tx, grant.operation_id()).await?;
        wire::require(snapshot.plan().stage() == OsDeployStage::PeComplete)?;
        admit(self, &snapshot, snapshot.plan().workflow_sha256())?;
        self.validate_grant_owner(grant).map_err(scheduler_error)?;
        if snapshot.attempt_id() != Some(grant.attempt_id()) {
            return Err(Error::FenceLost);
        }
        let reg = load::load_registration(&mut tx, snapshot.run_id()).await?;
        let (registration_event, alias) = require_completion_origin(&mut tx, &reg).await?;
        let package = load::fixture_package(&mut tx, &reg).await?;
        report.validate_package(&package)?;
        let at = now(&mut tx).await?;
        let claim = snapshot.run_id().as_uuid().to_string();
        let bearer = verify_run_bearer(Some(authorization), signing_secret, &claim, at.timestamp())
            .map_err(|_| Error::Validation)?;
        wire::require(
            bearer.metadata().identity() == RunBearerIdentity::Text(&claim)
                && alias == bearer.metadata().alias_sha256(),
        )?;
        let canonical = wire::canonical(report)?;
        let target = if report.succeeded() {
            ExecutionState::Satisfied
        } else {
            ExecutionState::Failed
        };
        if let Some(row) = sqlx::query("SELECT selected_event_id,selected_at,report_canonical_json FROM rust_controller.fixture_pe_completions WHERE operation_id=$1")
            .bind(grant.operation_id().as_uuid()).fetch_optional(&mut *tx).await? {
            let bound: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.osdeploy_lease_epochs e JOIN rust_controller.osdeploy_decisions d ON d.operation_id=e.operation_id AND d.attempt_id=e.attempt_id WHERE d.action='fixture_pe_completed' AND d.payload_canonical_json::jsonb->'detail'->>'lease_acquisition_event_id'=e.acquisition_event_id::text AND e.operation_id=$1 AND e.attempt_id=$2 AND e.generation=$3 AND e.worker_id=$4 AND e.lease_token_sha256=encode(sha256(convert_to($5,'UTF8')),'hex') AND e.acquired_at=$6 AND e.deadline_at=$7)")
                .bind(grant.operation_id().as_uuid()).bind(grant.attempt_id().as_uuid()).bind(self.generation).bind(&self.worker_id).bind(grant.lease_token().to_string()).bind(grant.acquired_at()).bind(grant.deadline_at()).fetch_one(&mut *tx).await?;
            if !bound || grant.attempt_number() != 1 { return Err(Error::FenceLost); }
            if row.try_get::<String,_>("report_canonical_json")? != canonical { return Err(Error::Conflict); }
            wire::require(snapshot.state() == target)?;
            let result = FixturePeCompletionResult { event: load::id(row.try_get("selected_event_id")?)?, at: row.try_get("selected_at")?, state: target, replayed: true };
            tx.commit().await?;
            return Ok(result);
        }
        let (current, epoch) = current_grant(&mut tx, self, grant, &snapshot).await?;
        active_at(&current, at)?;
        wire::require(snapshot.state() == ExecutionState::Running)?;
        let event = EventId::new();
        let value = envelope(
            &snapshot,
            grant.attempt_id(),
            self.generation,
            snapshot.revision(),
            at,
            wire::Detail::FixturePeCompleted(wire::FixtureCompletion {
                lease_acquisition_event_id: epoch,
                registration_event_id: registration_event,
                report_sha256: wire::digest(report)?,
                definition_sha256: report.definition_sha256.clone(),
                completion_deadline: *current.deadline_at(),
                succeeded: report.succeeded(),
            }),
        )?;
        append_osdeploy_decision(&mut tx, event, "osdeploy:fixture-completion", &value).await?;
        sqlx::query("INSERT INTO rust_controller.fixture_pe_completions(operation_id,run_id,attempt_id,selected_event_id,registration_event_id,alias_sha256,report_canonical_json,report_sha256,selected_at,succeeded) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)")
            .bind(grant.operation_id().as_uuid()).bind(snapshot.run_id().as_uuid()).bind(grant.attempt_id().as_uuid()).bind(event.as_uuid()).bind(registration_event.as_uuid()).bind(alias).bind(canonical).bind(wire::digest(report)?).bind(at).bind(report.succeeded()).execute(&mut *tx).await?;
        let proof = OsDeployTransitionProof::fixture_completed(&mut tx, event).await?;
        append_osdeploy_transition(&mut tx, proof).await?;
        recovery::settle_selected(&mut tx, &snapshot).await?;
        if report.succeeded() {
            activate_grace(self, &mut tx, &reg, event, at).await?;
        }
        load::load_execution(&mut tx, grant.operation_id()).await?;
        active_at(&current, now(&mut tx).await?)?;
        tx.commit().await?;
        Ok(FixturePeCompletionResult {
            event,
            at,
            state: target,
            replayed: false,
        })
    }
}

async fn activate_grace(
    s: &Scheduler,
    tx: &mut Transaction<'_, Postgres>,
    reg: &crate::OsDeployRegistrationV1,
    completion: EventId,
    at: DateTime<Utc>,
) -> Result<(), Error> {
    let operation = reg.ids().operation(OsDeployStage::PeShutdownGrace);
    let snapshot = load::load_execution(tx, operation).await?;
    wire::require(snapshot.state() == ExecutionState::Pending && snapshot.attempt_id().is_none())?;
    let budget = reg.plan().policy().shutdown_grace_seconds();
    let deadline = at
        .checked_add_signed(chrono::Duration::seconds(i64::from(budget)))
        .ok_or(Error::Validation)?;
    let attempt = AttemptId::new();
    let activation_event = EventId::new();
    let waiting_event = EventId::new();
    let anchor = reg.ids().operation(OsDeployStage::PeComplete);
    sqlx::query("INSERT INTO rust_controller.osdeploy_deadlines(run_id,scope_key,anchor_operation_id,anchor_event_id,opened_at,budget_seconds,deadline_at) VALUES($1,'shutdown_grace',$2,$3,$4,$5,$6)")
        .bind(reg.ids().run_id().as_uuid()).bind(anchor.as_uuid()).bind(completion.as_uuid()).bind(at).bind(i32::try_from(budget).map_err(|_| Error::Validation)?).bind(deadline).execute(&mut **tx).await?;
    sqlx::query("INSERT INTO rust_controller.attempts(attempt_id,operation_id,attempt_number,state,started_at,deadline_at) VALUES($1,$2,1,'pending',$3,$4)")
        .bind(attempt.as_uuid()).bind(operation.as_uuid()).bind(at).bind(deadline).execute(&mut **tx).await?;
    let activation = envelope(
        &snapshot,
        attempt,
        s.generation,
        snapshot.revision(),
        at,
        wire::Detail::StageActivated(wire::Activation {
            scope_key: wire::Scope::ShutdownGrace,
            anchor_operation_id: anchor,
            anchor_event_id: completion,
            opened_at: at,
            budget_seconds: budget,
            deadline_at: deadline,
            predecessor_operation_id: Some(anchor),
            predecessor_decision_event_id: Some(completion),
        }),
    )?;
    let revision =
        append_osdeploy_decision(tx, activation_event, "osdeploy:activation", &activation).await?;
    sqlx::query("INSERT INTO rust_controller.osdeploy_attempt_bindings(operation_id,run_id,attempt_id,scope_key,activation_event_id,activated_at,deadline_at,activation_mode) VALUES($1,$2,$3,'shutdown_grace',$4,$5,$6,'parked')")
        .bind(operation.as_uuid()).bind(reg.ids().run_id().as_uuid()).bind(attempt.as_uuid()).bind(activation_event.as_uuid()).bind(at).bind(deadline).execute(&mut **tx).await?;
    let waiting = envelope(
        &snapshot,
        attempt,
        s.generation,
        revision,
        at,
        wire::Detail::FixtureGraceWaiting(wire::FixtureGrace {
            completion_event_id: completion,
            deadline_at: deadline,
        }),
    )?;
    let decision_revision =
        append_osdeploy_decision(tx, waiting_event, "osdeploy:fixture-grace", &waiting).await?;
    let proof = OsDeployTransitionProof::fixture_grace_waiting(tx, waiting_event).await?;
    let revision = append_osdeploy_transition(tx, proof).await?;
    sqlx::query("UPDATE rust_controller.attempts SET state='waiting' WHERE attempt_id=$1 AND operation_id=$2")
        .bind(attempt.as_uuid()).bind(operation.as_uuid()).execute(&mut **tx).await?;
    sqlx::query("INSERT INTO rust_controller.osdeploy_schedule_projection(operation_id,run_id,attempt_id,mode,basis_event_id,basis_revision,scope_key,next_check_at,unavailable_count,rebuilt_through_revision) VALUES($1,$2,$3,'waiting',$4,$5,'shutdown_grace',$6,0,$7)")
        .bind(operation.as_uuid()).bind(reg.ids().run_id().as_uuid()).bind(attempt.as_uuid()).bind(waiting_event.as_uuid()).bind(decision_revision).bind(deadline).bind(revision).execute(&mut **tx).await?;
    load::load_execution(tx, operation).await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn completion_report_requires_exact_boolean_fields() {
        let valid = json!({"definition_sha256":"a".repeat(64),"milestone_id":"boot-files-staged.v1","result":{"image_applied":true,"boot_files_staged":true,"boot_files_verified":true}});
        assert!(
            serde_json::from_value::<FixturePeCompletionReport>(valid.clone())
                .unwrap()
                .succeeded()
        );
        for field in ["image_applied", "boot_files_staged", "boot_files_verified"] {
            let mut missing = valid.clone();
            missing["result"].as_object_mut().unwrap().remove(field);
            assert!(serde_json::from_value::<FixturePeCompletionReport>(missing).is_err());
            let mut wrong = valid.clone();
            wrong["result"][field] = json!("true");
            assert!(serde_json::from_value::<FixturePeCompletionReport>(wrong).is_err());
            let mut failed = valid.clone();
            failed["result"][field] = json!(false);
            assert!(
                !serde_json::from_value::<FixturePeCompletionReport>(failed)
                    .unwrap()
                    .succeeded()
            );
        }
        let mut extra = valid.clone();
        extra["result"]["extra"] = json!(true);
        assert!(serde_json::from_value::<FixturePeCompletionReport>(extra).is_err());
        let mut extra = valid;
        extra["attempt_id"] = json!(Uuid::now_v7());
        assert!(serde_json::from_value::<FixturePeCompletionReport>(extra).is_err());
    }
}

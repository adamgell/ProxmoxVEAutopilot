//! Authenticated fixture registration selection under the callback lease.
use super::*;
use api_compat::run_bearer::{RunBearerIdentity, verify_run_bearer};
use serde::{Deserialize, Serialize};

/// Reported client identity only. Expected identity is restored from the plan.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixturePeRegistrationIdentity {
    pub vm_uuid: String,
    pub mac: String,
    pub agent_id: String,
}
impl FixturePeRegistrationIdentity {
    pub(crate) fn expected(reg: &crate::OsDeployRegistrationV1) -> Self {
        Self {
            vm_uuid: reg.plan().vm().uuid().to_string(),
            mac: reg.plan().vm().mac().to_string(),
            agent_id: reg.plan().names().expected_agent_id().to_owned(),
        }
    }
}

/// A committed registration result. It grants no downstream action authority.
#[derive(Debug)]
pub struct FixturePeRegistrationResult {
    operation: OperationId,
    attempt: AttemptId,
    event: EventId,
    at: DateTime<Utc>,
    replayed: bool,
}
impl FixturePeRegistrationResult {
    pub fn operation_id(&self) -> OperationId {
        self.operation
    }
    pub fn attempt_id(&self) -> AttemptId {
        self.attempt
    }
    pub fn selected_event_id(&self) -> EventId {
        self.event
    }
    pub fn selected_at(&self) -> DateTime<Utc> {
        self.at
    }
    pub fn replayed(&self) -> bool {
        self.replayed
    }
}

impl Scheduler {
    /// Verify the delivered bearer and select one registration atomically.
    /// Exact replay requires current authority and an unexpired credential, but
    /// does not allocate another attempt or require a now-settled worker lease.
    pub async fn accept_fixture_pe_registration(
        &self,
        grant: &LeaseGrant,
        authorization: &str,
        signing_secret: &[u8],
        identity: &FixturePeRegistrationIdentity,
    ) -> Result<FixturePeRegistrationResult, Error> {
        if !self.fixture_credential_delivery {
            return Err(Error::CapabilityUnavailable);
        }
        let mut tx = self.store.pool().begin().await?;
        authority(self, &mut tx).await?;
        let snapshot = locked_execution(&mut tx, grant.operation_id()).await?;
        wire::require(snapshot.plan().stage() == OsDeployStage::PeRegister)?;
        admit(self, &snapshot, snapshot.plan().workflow_sha256())?;
        self.validate_grant_owner(grant).map_err(scheduler_error)?;
        if snapshot.attempt_id() != Some(grant.attempt_id()) {
            return Err(Error::FenceLost);
        }
        let reg = load::load_registration(&mut tx, snapshot.run_id()).await?;
        if identity != &FixturePeRegistrationIdentity::expected(&reg) {
            return Err(Error::Conflict);
        }
        let at = now(&mut tx).await?;
        let claim = snapshot.run_id().as_uuid().to_string();
        let bearer = verify_run_bearer(Some(authorization), signing_secret, &claim, at.timestamp())
            .map_err(|_| Error::Validation)?;
        wire::require(bearer.metadata().identity() == RunBearerIdentity::Text(&claim))?;
        load::require_fixture_registration_origin(&mut tx, &reg, at).await?;
        let start = reg.ids().operation(OsDeployStage::StartPe);
        let delivery: (Vec<u8>, Uuid, String) = sqlx::query_as("SELECT alias_sha256,dispatch_event_id,package_sha256 FROM rust_controller.fixture_pe_deliveries WHERE operation_id=$1")
            .bind(start.as_uuid()).fetch_one(&mut *tx).await?;
        wire::require(delivery.0 == bearer.metadata().alias_sha256())?;
        let canonical = wire::canonical(identity)?;
        let hash = wire::digest(identity)?;
        if let Some(row) = sqlx::query("SELECT selected_event_id,selected_at,identity_canonical_json FROM rust_controller.fixture_pe_registrations WHERE operation_id=$1")
            .bind(grant.operation_id().as_uuid()).fetch_optional(&mut *tx).await? {
            wire::require(snapshot.state() == ExecutionState::Satisfied)?;
            if row.try_get::<String,_>("identity_canonical_json")? != canonical { return Err(Error::Conflict); }
            let result = FixturePeRegistrationResult { operation: grant.operation_id(), attempt: grant.attempt_id(),
                event: load::id(row.try_get("selected_event_id")?)?, at: row.try_get("selected_at")?, replayed: true };
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
            wire::Detail::FixturePeRegistered(wire::FixtureRegistration {
                lease_acquisition_event_id: epoch,
                start_operation_id: start,
                dispatch_event_id: load::id(delivery.1)?,
                identity_sha256: hash.clone(),
                package_sha256: delivery.2,
                registration_deadline: *current.deadline_at(),
            }),
        )?;
        append_osdeploy_decision(&mut tx, event, "osdeploy:fixture-registration", &value).await?;
        sqlx::query("INSERT INTO rust_controller.fixture_pe_registrations(operation_id,run_id,attempt_id,selected_event_id,start_operation_id,alias_sha256,identity_canonical_json,identity_sha256,selected_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)")
            .bind(grant.operation_id().as_uuid()).bind(snapshot.run_id().as_uuid()).bind(grant.attempt_id().as_uuid())
            .bind(event.as_uuid()).bind(start.as_uuid()).bind(bearer.metadata().alias_sha256().as_slice())
            .bind(canonical).bind(hash).bind(at).execute(&mut *tx).await?;
        let budget = reg.plan().policy().pe_seconds();
        let deadline = at
            .checked_add_signed(chrono::Duration::seconds(i64::from(budget)))
            .ok_or(Error::Validation)?;
        sqlx::query("INSERT INTO rust_controller.osdeploy_deadlines(run_id,scope_key,anchor_operation_id,anchor_event_id,opened_at,budget_seconds,deadline_at) VALUES($1,'pe_completion',$2,$3,$4,$5,$6)")
            .bind(snapshot.run_id().as_uuid()).bind(grant.operation_id().as_uuid()).bind(event.as_uuid()).bind(at)
            .bind(i32::try_from(budget).map_err(|_| Error::Validation)?).bind(deadline).execute(&mut *tx).await?;
        let proof = OsDeployTransitionProof::fixture_registered(&mut tx, event).await?;
        append_osdeploy_transition(&mut tx, proof).await?;
        recovery::settle_selected(&mut tx, &snapshot).await?;
        load::load_execution(&mut tx, grant.operation_id()).await?;
        active_at(&current, now(&mut tx).await?)?;
        tx.commit().await?;
        Ok(FixturePeRegistrationResult {
            operation: grant.operation_id(),
            attempt: grant.attempt_id(),
            event,
            at,
            replayed: false,
        })
    }
}

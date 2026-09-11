//! Database-derived stop admission facts. These never authorize a physical send.
use super::*;
use pve_port::fixture_support::FixtureStopAuthorityV1;

impl Scheduler {
    /// Reload the guarded grace decision and current committed stop lease under
    /// the scheduler's normal lock order. The returned clocks expire naturally;
    /// callers must obtain fresh supervisor power evidence after this check.
    /// This observation does not release the durable fixture dispatch barrier.
    pub async fn fixture_stop_authority(
        &self,
        grant: &LeaseGrant,
    ) -> Result<FixtureStopAuthorityV1, Error> {
        if !self.fixture_credential_delivery {
            return Err(Error::CapabilityUnavailable);
        }
        let mut tx = self.store.pool().begin().await?;
        authority(self, &mut tx).await?;
        let snapshot = locked_execution(&mut tx, grant.operation_id()).await?;
        if snapshot.plan().stage() != OsDeployStage::PeEnsureStopped || snapshot.cancelled() {
            return Err(Error::CapabilityUnavailable);
        }
        let (current, _) = current_grant(&mut tx, self, grant, &snapshot).await?;
        let dispatch = snapshot.dispatch().ok_or(Error::CapabilityUnavailable)?;
        wire::require(matches!(
            dispatch.request(),
            pve_port::ProvisioningMutationRequestV1::Stop(_)
        ))?;
        let registration = load::load_registration(&mut tx, snapshot.run_id()).await?;
        let grace = registration.ids().operation(OsDeployStage::PeShutdownGrace);
        // Full reload validates the selected immutable completion, parked scope,
        // elapsed-grace reason, journal, and projections before consuming it.
        let prior = load::load_execution(&mut tx, grace).await?;
        wire::require(prior.state() == ExecutionState::Unknown && !prior.cancelled())?;
        let row = sqlx::query("SELECT event_id,payload_canonical_json FROM rust_controller.osdeploy_decisions WHERE operation_id=$1 AND resolution IS NOT NULL AND resolution<>'ready' ORDER BY decision_revision DESC LIMIT 1")
            .bind(grace.as_uuid()).fetch_one(&mut *tx).await?;
        let decision =
            wire::DecisionEnvelope::decode(&row.try_get::<String, _>("payload_canonical_json")?)?;
        wire::require(crate::osdeploy::execution::history::elapsed_grace(
            &decision,
        ))?;
        let due = prior.deadline_at().ok_or(Error::Validation)?;
        let checked = now(&mut tx).await?;
        active_at(&current, checked)?;
        wire::require(due <= decision.evaluated_at && decision.evaluated_at <= checked)?;
        let millis = |value: DateTime<Utc>| {
            u64::try_from(value.timestamp_millis()).map_err(|_| Error::Validation)
        };
        let result = FixtureStopAuthorityV1 {
            version: 1,
            grace_operation: grace.as_uuid(),
            decision_event: row.try_get("event_id")?,
            evidence_fence: dispatch.request().binding().evidence_fence(),
            grace_due_unix_ms: millis(due)?,
            decision_unix_ms: millis(decision.evaluated_at)?,
            lease_checked_unix_ms: millis(checked)?,
            lease_expires_unix_ms: millis(*current.lease_expires_at())?,
            original_deadline_unix_ms: millis(*current.deadline_at())?,
        };
        tx.commit().await?;
        Ok(result)
    }
}

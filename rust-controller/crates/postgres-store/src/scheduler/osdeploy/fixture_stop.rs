//! Database-derived stop admission facts. These never authorize a physical send.
use super::*;
use pve_port::fixture_support::FixtureStopAuthorityV1;

fn unchanged(
    prepared: &FixtureStopAuthorityV1,
    mut current: FixtureStopAuthorityV1,
) -> Result<(), Error> {
    wire::require(
        prepared.lease_checked_unix_ms >= current.decision_unix_ms
            && prepared.lease_checked_unix_ms <= current.lease_checked_unix_ms,
    )?;
    current.lease_checked_unix_ms = prepared.lease_checked_unix_ms;
    wire::require(&current == prepared)
}

impl Scheduler {
    /// Join a supervisor request to the committed dispatch under current lease
    /// and cancellation checks. Caller-supplied duplicate bytes are not evidence
    /// that a request was committed in PostgreSQL.
    pub async fn validate_fixture_stop_request(
        &self,
        grant: &LeaseGrant,
        request: &pve_port::fixture_ipc::FixtureStageRequest,
    ) -> Result<(), Error> {
        if !self.fixture_credential_delivery {
            return Err(Error::CapabilityUnavailable);
        }
        let mut tx = self.store.pool().begin().await?;
        authority(self, &mut tx).await?;
        let snapshot = locked_execution(&mut tx, grant.operation_id()).await?;
        wire::require(
            snapshot.plan().stage() == OsDeployStage::PeEnsureStopped && !snapshot.cancelled(),
        )?;
        current_grant(&mut tx, self, grant, &snapshot).await?;
        let dispatch = snapshot.dispatch().ok_or(Error::CapabilityUnavailable)?;
        wire::require(dispatch.request() == request.request())?;
        tx.commit().await?;
        Ok(())
    }

    /// Revalidate ownership and cancellation after obtaining a scoped power
    /// sample, without replacing the preparation clock embedded in that sample.
    /// This is a point-in-time database check, not an admission or send permit.
    pub async fn revalidate_fixture_stop_authority(
        &self,
        grant: &LeaseGrant,
        prepared: &FixtureStopAuthorityV1,
    ) -> Result<(), Error> {
        unchanged(prepared, self.fixture_stop_authority(grant).await?)
    }

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

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn prepared_stop_authority_preserves_clock_and_rejects_changed_scope() {
        let prepared = FixtureStopAuthorityV1 {
            version: 1,
            grace_operation: uuid::Uuid::now_v7(),
            decision_event: uuid::Uuid::now_v7(),
            evidence_fence: 1,
            grace_due_unix_ms: 10,
            decision_unix_ms: 11,
            lease_checked_unix_ms: 12,
            lease_expires_unix_ms: 30,
            original_deadline_unix_ms: 40,
        };
        let mut current = prepared.clone();
        current.lease_checked_unix_ms = 13;
        assert!(unchanged(&prepared, current.clone()).is_ok());
        for field in [
            "version",
            "grace_operation",
            "decision_event",
            "evidence_fence",
            "grace_due_unix_ms",
            "decision_unix_ms",
            "lease_checked_unix_ms",
            "lease_expires_unix_ms",
            "original_deadline_unix_ms",
        ] {
            let mut value = serde_json::to_value(&prepared).unwrap();
            value[field] = if field.ends_with("operation") || field == "decision_event" {
                serde_json::json!(uuid::Uuid::now_v7())
            } else {
                serde_json::json!(99)
            };
            assert!(
                unchanged(&serde_json::from_value(value).unwrap(), current.clone()).is_err(),
                "accepted changed {field}"
            );
        }
        current.lease_checked_unix_ms = 11;
        assert!(unchanged(&prepared, current).is_err());
    }
}

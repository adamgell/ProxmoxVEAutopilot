//! Capability-closed fixture session seam. Default migrations are unchanged.
use crate::{OsDeployExecutionError as Error, PgStore};
use osdeploy_adapter::{
    AuthenticatedPeWitnessV1, StartPeArmingRefusal, StartPeAtomicArmingProposalV1,
};
use sqlx::Row;

fn exact_millis(at: chrono::DateTime<chrono::Utc>) -> Result<u64, Error> {
    if !at.timestamp_subsec_nanos().is_multiple_of(1_000_000) {
        return Err(Error::Validation);
    }
    at.timestamp_millis()
        .try_into()
        .map_err(|_| Error::Validation)
}

/// Explicit test/fixture DDL, not automatically installed and not an arm permit.
pub const START_PE_SESSION_SCHEMA_V1: &str =
    include_str!("../schema/start_pe_session_proposals_v1.sql");
impl PgStore {
    /// No authenticated witness implementation exists. Refuse before connection,
    /// transaction, schema installation, or insertion. No success path exists.
    pub async fn arm_osdeploy_start_pe_session(
        &self,
        _proposal: &StartPeAtomicArmingProposalV1,
        witness: AuthenticatedPeWitnessV1,
    ) -> Result<(), Error> {
        match witness {
            AuthenticatedPeWitnessV1::Unavailable => Err(Error::CapabilityUnavailable),
        }
    }
    /// Rollback-only diagnostic transaction. Original durable dispatch and
    /// deadline are required; UUID proposal generation is NOT the bigint DB fence.
    /// No session or authority state is installed even when all checks match.
    pub async fn probe_osdeploy_start_pe_session_transaction(
        &self,
        proposal: &StartPeAtomicArmingProposalV1,
    ) -> Result<StartPeArmingRefusal, Error> {
        let mut tx = self.pool().begin().await?;
        let result=async {
            sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY").execute(&mut *tx).await?;
            let c=proposal.context(); let a=proposal.anchor();
            let row=sqlx::query("SELECT d.anchor_event_id,d.opened_at,d.budget_seconds,d.deadline_at,x.attempt_id,x.request_sha256,x.dispatched_at,x.dispatch_event_id,j.evaluated_at,j.action,clock_timestamp() AS checked_at FROM rust_controller.osdeploy_operation_plans p JOIN rust_controller.osdeploy_operation_plans r ON r.run_id=p.run_id AND r.operation_id=$3 AND r.stage='pe_register' JOIN rust_controller.osdeploy_deadlines d ON d.run_id=p.run_id AND d.scope_key='pe_registration' AND d.anchor_operation_id=p.operation_id JOIN rust_controller.osdeploy_pve_dispatches x ON x.operation_id=p.operation_id JOIN rust_controller.osdeploy_decisions j ON j.event_id=x.dispatch_event_id AND j.operation_id=p.operation_id WHERE p.run_id=$1 AND p.operation_id=$2 AND p.stage='start_pe'")
                .bind(c.run_id).bind(c.start_operation).bind(c.registration_operation).fetch_optional(&mut *tx).await?.ok_or(Error::CapabilityUnavailable)?;
            let ms=|key| -> Result<u64,Error> {exact_millis(row.try_get(key)?)};
            if row.try_get::<uuid::Uuid,_>("anchor_event_id")?!=a.dispatch_event() || row.try_get::<uuid::Uuid,_>("dispatch_event_id")?!=a.dispatch_event()
                || row.try_get::<uuid::Uuid,_>("attempt_id")?!=c.attempt || row.try_get::<String,_>("request_sha256")?!=c.request_sha256
                || row.try_get::<String,_>("action")?!="pve_dispatch_committed" || ms("opened_at")?!=a.opened_unix_ms() || ms("dispatched_at")?!=a.opened_unix_ms() || ms("evaluated_at")?!=a.opened_unix_ms()
                || ms("deadline_at")?!=a.deadline_unix_ms() || row.try_get::<i32,_>("budget_seconds")?!=a.budget_seconds() as i32 {return Err(Error::Validation);}
            let checked:chrono::DateTime<chrono::Utc>=row.try_get("checked_at")?;
            let checked=checked.timestamp_millis().try_into().map_err(|_|Error::Validation)?;
            proposal.assess(checked,AuthenticatedPeWitnessV1::Unavailable).map_err(|_|Error::Validation)
        }.await;
        tx.rollback().await?;
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn original_anchor_precision_is_never_silently_truncated() {
        assert_eq!(
            exact_millis(chrono::DateTime::from_timestamp(1, 123_000_000).unwrap()),
            Ok(1123)
        );
        assert_eq!(
            exact_millis(chrono::DateTime::from_timestamp(1, 123_001_000).unwrap()),
            Err(Error::Validation)
        );
    }
}

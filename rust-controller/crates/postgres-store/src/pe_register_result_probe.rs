//! Opt-in rollback-only result probes. No result insertion or success capability.
use crate::{OsDeployExecutionError as Error, PgStore};
use osdeploy_adapter::{
    AuthenticatedPeWitnessV1, PeRegisterAuthorityFenceV1, PeRegisterCallbackCandidateV1,
    PeRegisterVerifierContractV1, PeRegisterVerifierRefusal,
};
use sqlx::{Postgres, Row, Transaction};
use uuid::Uuid;

async fn lock_result(tx: &mut Transaction<'_, Postgres>, session: Uuid) -> Result<(), Error> {
    let locked: bool =
        sqlx::query_scalar("SELECT pg_try_advisory_xact_lock(hashtextextended($1,0))")
            .bind(format!("osdeploy:pe-register-result:{session}"))
            .fetch_one(&mut **tx)
            .await?;
    if !locked {
        return Err(Error::Conflict);
    }
    Ok(())
}
impl PgStore {
    /// Diagnostic concurrency/rollback seam only. Acquiring a session lock is
    /// never an authenticated result permit; the lock is released before return.
    pub async fn probe_pe_register_result_lock(&self, session: Uuid) -> Result<(), Error> {
        if session.is_nil() {
            return Err(Error::Validation);
        }
        let mut tx = self.pool().begin().await?;
        let result = lock_result(&mut tx, session)
            .await
            .and(Err(Error::CapabilityUnavailable));
        tx.rollback().await?;
        result
    }
    /// Reconstruct the existing StartPe fence under authority/run/row/session
    /// locks, assess the descriptor, and roll back. The current context carries
    /// StartPe attempt identity, NOT a separately admitted PeRegister attempt.
    /// No callback authentication or durable result-revision CAS is implemented.
    pub async fn probe_pe_register_result_transaction(
        &self,
        verifier: &PeRegisterVerifierContractV1,
        candidate: &PeRegisterCallbackCandidateV1,
        result_revision: u64,
        original: Option<(&PeRegisterCallbackCandidateV1, u64)>,
    ) -> Result<PeRegisterVerifierRefusal, Error> {
        let mut tx = self.pool().begin().await?;
        let result=async {
            sqlx::raw_sql("SET LOCAL lock_timeout='1000ms'; SET LOCAL statement_timeout='2000ms'").execute(&mut *tx).await?;
            let authority:Option<i64>=sqlx::query_scalar("SELECT generation FROM rust_controller.orchestration_authority WHERE singleton_key=1 AND executor_kind='rust' FOR SHARE").fetch_optional(&mut *tx).await?;
            let authority=authority.ok_or(Error::FenceLost)?;
            let c=verifier.context();
            sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1,0))").bind(format!("native:run:{}",c.run_id)).execute(&mut *tx).await?;
            let row=sqlx::query("SELECT o.revision,l.generation,l.worker_id,e.acquisition_event_id,clock_timestamp() AS checked_at FROM rust_controller.operations o JOIN rust_controller.osdeploy_operation_plans p ON p.operation_id=o.operation_id AND p.stage='start_pe' JOIN rust_controller.worker_leases l ON l.operation_id=o.operation_id AND l.attempt_id=$3 JOIN rust_controller.osdeploy_lease_epochs e ON e.operation_id=o.operation_id AND e.attempt_id=l.attempt_id AND e.generation=l.generation AND e.worker_id=l.worker_id AND e.acquired_at=l.acquired_at AND e.acquisition_event_id=$4 WHERE o.run_id=$1 AND o.operation_id=$2 AND o.state IN ('leased','running','waiting') AND l.executor_kind='rust' AND l.lease_expires_at>clock_timestamp() AND l.deadline_at>clock_timestamp() FOR SHARE OF o,l")
                .bind(c.run_id).bind(c.start_operation).bind(c.attempt).bind(verifier.expected_fence().lease_epoch).fetch_optional(&mut *tx).await?.ok_or(Error::FenceLost)?;
            let generation:i64=row.try_get("generation")?;
            if generation!=authority {return Err(Error::FenceLost);}
            let current=PeRegisterAuthorityFenceV1 {authority_generation:generation.try_into().map_err(|_|Error::Validation)?,worker_id:row.try_get("worker_id")?,lease_epoch:row.try_get("acquisition_event_id")?,operation_revision:row.try_get::<i64,_>("revision")?.try_into().map_err(|_|Error::Validation)?};
            lock_result(&mut tx,c.session_id).await?;
            let checked:chrono::DateTime<chrono::Utc>=row.try_get("checked_at")?;
            verifier.assess(candidate,&current,result_revision,AuthenticatedPeWitnessV1::Unavailable,original,checked.timestamp_micros().try_into().map_err(|_|Error::Validation)?).map_err(|_|Error::Validation)
        }.await;
        tx.rollback().await?;
        result
    }
}

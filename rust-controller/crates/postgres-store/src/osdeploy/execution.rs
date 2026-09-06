//! Observational execution history; loading never grants execution authority.
use super::OsDeployOperationPlanV1;
use crate::PgStore;
use chrono::{DateTime, Utc};
use controller_domain::{AttemptId, ExecutionState, OperationId, RunId};
use pve_port::{ProvisioningDispatchV1, ProvisioningReceiptV1};
pub(crate) mod load;
pub(crate) mod wire;

#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum OsDeployExecutionError {
    #[error("osdeploy_execution_validation_failed")]
    Validation,
    #[error("osdeploy_execution_conflict")]
    Conflict,
    #[error("osdeploy_execution_fence_lost")]
    FenceLost,
    #[error("osdeploy_execution_capability_unavailable")]
    CapabilityUnavailable,
    #[error("osdeploy_execution_storage_unavailable")]
    StorageUnavailable,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OsDeployProgress {
    Idle,
    Waiting,
    Decided(ExecutionState),
}
/// A validated observation, never a dispatch permit.
/// ```compile_fail
/// let _: postgres_store::OsDeployOperationSnapshot = serde_json::from_str("{}").unwrap();
/// ```
pub struct OsDeployOperationSnapshot {
    operation_id: OperationId,
    run_id: RunId,
    revision: i64,
    state: ExecutionState,
    cancelled: bool,
    plan: OsDeployOperationPlanV1,
    attempt_id: Option<AttemptId>,
    activated_at: Option<DateTime<Utc>>,
    deadline_at: Option<DateTime<Utc>>,
    next_check_at: Option<DateTime<Utc>>,
    dispatch: Option<ProvisioningDispatchV1>,
    receipt: Option<ProvisioningReceiptV1>,
}
impl OsDeployOperationSnapshot {
    pub fn operation_id(&self) -> OperationId {
        self.operation_id
    }
    pub fn run_id(&self) -> RunId {
        self.run_id
    }
    pub fn revision(&self) -> i64 {
        self.revision
    }
    pub fn state(&self) -> ExecutionState {
        self.state
    }
    pub fn cancelled(&self) -> bool {
        self.cancelled
    }
    pub fn plan(&self) -> &OsDeployOperationPlanV1 {
        &self.plan
    }
    pub fn attempt_id(&self) -> Option<AttemptId> {
        self.attempt_id
    }
    pub fn activated_at(&self) -> Option<DateTime<Utc>> {
        self.activated_at
    }
    pub fn deadline_at(&self) -> Option<DateTime<Utc>> {
        self.deadline_at
    }
    pub fn next_check_at(&self) -> Option<DateTime<Utc>> {
        self.next_check_at
    }
    pub fn dispatch(&self) -> Option<&ProvisioningDispatchV1> {
        self.dispatch.as_ref()
    }
    pub fn receipt(&self) -> Option<&ProvisioningReceiptV1> {
        self.receipt.as_ref()
    }
}
impl From<sqlx::Error> for OsDeployExecutionError {
    fn from(e: sqlx::Error) -> Self {
        match super::OsDeployStoreError::from(e) {
            super::OsDeployStoreError::Validation => Self::Validation,
            super::OsDeployStoreError::Conflict => Self::Conflict,
            super::OsDeployStoreError::StorageUnavailable => Self::StorageUnavailable,
        }
    }
}
impl From<super::OsDeployStoreError> for OsDeployExecutionError {
    fn from(e: super::OsDeployStoreError) -> Self {
        match e {
            super::OsDeployStoreError::Validation => Self::Validation,
            super::OsDeployStoreError::Conflict => Self::Conflict,
            super::OsDeployStoreError::StorageUnavailable => Self::StorageUnavailable,
        }
    }
}
impl From<serde_json::Error> for OsDeployExecutionError {
    fn from(_: serde_json::Error) -> Self {
        Self::Validation
    }
}
impl PgStore {
    pub async fn load_osdeploy_operation(
        &self,
        operation: OperationId,
    ) -> Result<OsDeployOperationSnapshot, OsDeployExecutionError> {
        let mut tx = self.pool().begin().await?;
        sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            .execute(&mut *tx)
            .await?;
        let loaded = load::load_execution(&mut tx, operation).await?;
        tx.commit().await?;
        Ok(loaded)
    }
}

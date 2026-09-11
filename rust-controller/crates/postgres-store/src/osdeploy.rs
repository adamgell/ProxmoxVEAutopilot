//! Typed, immutable OSDeploy registration. No execution capability is produced.
//!
//! Persisted data enters through the named loader, never an unchecked wrapper.
//! ```compile_fail
//! let _: postgres_store::OsDeployOperationPlanV1 = serde_json::from_str("{}").unwrap();
//! ```
//! ```compile_fail
//! let _: postgres_store::OsDeployWorkflowIds = serde_json::from_str("{}").unwrap();
//! ```
//! ```compile_fail
//! let _: postgres_store::OsDeployRegistrationV1 = serde_json::from_str("{}").unwrap();
//! ```
use controller_domain::{OperationId, RunId};
use osdeploy_adapter::{OsDeployPlanV1, OsDeployStage};
use pve_port::ProvisioningOperationPlanV1;
use serde::Serialize;
pub(crate) mod execution;
mod package_semantics;
mod records;
mod registration;
pub use package_semantics::RegisteredPePackageSemanticsV1;
mod stage;

#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum OsDeployStoreError {
    #[error("OSDeploy registration validation failed")]
    Validation,
    #[error("OSDeploy registration conflict")]
    Conflict,
    #[error("OSDeploy registration storage unavailable")]
    StorageUnavailable,
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OsDeployWorkflowIds {
    run_id: RunId,
    operations: [OperationId; 16],
    workflow_sha256: String,
}
impl OsDeployWorkflowIds {
    pub const fn run_id(&self) -> RunId {
        self.run_id
    }
    pub fn operation(&self, stage: OsDeployStage) -> OperationId {
        self.operations[ordinal(stage)]
    }
    pub const fn operations(&self) -> &[OperationId; 16] {
        &self.operations
    }
    pub fn workflow_sha256(&self) -> &str {
        &self.workflow_sha256
    }
}
fn ordinal(stage: OsDeployStage) -> usize {
    OsDeployStage::ALL
        .iter()
        .position(|s| *s == stage)
        .expect("closed manifest stage")
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct OsDeployOperationPlanV1 {
    contract_version: u16,
    workflow_sha256: String,
    stage: OsDeployStage,
    pve: Option<ProvisioningOperationPlanV1>,
}
impl OsDeployOperationPlanV1 {
    pub const fn contract_version(&self) -> u16 {
        self.contract_version
    }
    pub fn workflow_sha256(&self) -> &str {
        &self.workflow_sha256
    }
    pub const fn stage(&self) -> OsDeployStage {
        self.stage
    }
    pub const fn pve(&self) -> Option<&ProvisioningOperationPlanV1> {
        self.pve.as_ref()
    }
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OsDeployRegistrationV1 {
    ids: OsDeployWorkflowIds,
    plan: OsDeployPlanV1,
    stages: [OsDeployOperationPlanV1; 16],
}
impl OsDeployRegistrationV1 {
    pub const fn ids(&self) -> &OsDeployWorkflowIds {
        &self.ids
    }
    pub const fn plan(&self) -> &OsDeployPlanV1 {
        &self.plan
    }
    pub fn stage(&self, stage: OsDeployStage) -> &OsDeployOperationPlanV1 {
        &self.stages[ordinal(stage)]
    }
}
impl From<sqlx::Error> for OsDeployStoreError {
    fn from(error: sqlx::Error) -> Self {
        if error
            .as_database_error()
            .is_some_and(|e| e.is_unique_violation())
        {
            Self::Conflict
        } else if matches!(
            error,
            sqlx::Error::ColumnDecode { .. } | sqlx::Error::Decode(_) | sqlx::Error::RowNotFound
        ) {
            Self::Validation
        } else {
            Self::StorageUnavailable
        }
    }
}
impl From<serde_json::Error> for OsDeployStoreError {
    fn from(_: serde_json::Error) -> Self {
        Self::Validation
    }
}
impl From<crate::NativeStoreError> for OsDeployStoreError {
    fn from(error: crate::NativeStoreError) -> Self {
        match error {
            crate::NativeStoreError::StorageUnavailable => Self::StorageUnavailable,
            crate::NativeStoreError::Conflict => Self::Conflict,
            _ => Self::Validation,
        }
    }
}
impl From<crate::StoreError> for OsDeployStoreError {
    fn from(error: crate::StoreError) -> Self {
        match error {
            crate::StoreError::Database(error) => error.into(),
            crate::StoreError::CommandDigestConflict { .. } => Self::Conflict,
            _ => Self::Validation,
        }
    }
}

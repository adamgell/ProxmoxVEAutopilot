//! Pure OSDeploy stage and validated input contract.

mod input_values;
mod stages;

pub use input_values::{
    DeploymentNames, DiskCapacity, PhasePolicy, PhasePolicyInput, normalize_legacy_windows_name,
};
pub use stages::{OsDeployStage, StageDependency, StageKind};

/// Fixed errors never retain rejected input.
#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum ContractError {
    #[error("requested disk capacity must be at least 80 GiB")]
    InvalidRequestedDiskCapacity,
    #[error("template disk capacity must be positive")]
    InvalidTemplateCapacity,
    #[error("requested disk capacity exceeds the byte range")]
    DiskCapacityOverflow,
    #[error("requested name must contain 1 to 256 UTF-8 bytes and no control characters")]
    InvalidRequestedName,
    #[error("normalized Windows name is unsupported by native v1")]
    InvalidWindowsName,
    #[error("phase duration must be between 1 and 86400 seconds")]
    InvalidPhaseDuration,
    #[error(
        "evidence freshness must be between 1 and 300 seconds and no larger than the mutation budget"
    )]
    InvalidEvidenceFreshness,
}

//! Pure OSDeploy stage and validated input contract.

mod disk_boot_boundary;
mod guest_action;
mod verify_qga_boundary;
pub use verify_qga_boundary::{VerifyQgaBindingV1, VerifyQgaRefusal, VerifyQgaReportV1};
mod install_qga_boundary;
pub use disk_boot_boundary::{DiskBootEvidenceV1, DiskBootRefusal, DiskBootScopeV1};
pub use install_qga_boundary::{HostQgaClaimV1, InstallQgaRefusal, InstallQgaScopeV1};
mod pe_shutdown_boundary;
pub use pe_shutdown_boundary::{PeShutdownObservationV1, PeShutdownRefusal, PeShutdownScopeV1};
mod pe_complete_boundary;
pub use pe_complete_boundary::{
    PeCompleteRefusal, PeCompleteReportV1, PeCompleteScopeV1, PeCompleteStepStateV1,
    PeRegisterPrerequisiteV1,
};
mod pe_register_authority;
mod pe_register_callback;
pub use pe_register_authority::{
    PeRegisterAuthorityFenceV1, PeRegisterVerifierContractV1, PeRegisterVerifierRefusal,
};
pub use pe_register_callback::{
    PeRegisterCallbackCandidateV1, PeRegisterCallbackRefusal, PeRegisterRequestIdentityV1,
    PeRegisterResultIdentityV1,
};
mod pe_registration_precision;
pub use pe_registration_precision::PeRegistrationAnchorV2;
mod input_values;
mod plan;
mod provisioning;
mod restore;
mod stages;
mod start_pe_arming;
pub use provisioning::pve_expectations;
pub use restore::restore_osdeploy_plan_v1;
pub use start_pe_arming::{
    AuthenticatedPeWitnessV1, PeRegistrationAnchorV1, StartPeArmingContextV1, StartPeArmingError,
    StartPeArmingRefusal, StartPeAtomicArmingProposalV1,
};

pub use guest_action::GuestActionIdentity;
pub use input_values::{
    DeploymentNames, DiskCapacity, PhasePolicy, PhasePolicyInput, normalize_legacy_windows_name,
};
pub use plan::{OsDeployPlanInput, OsDeployPlanV1, PayloadDeclaration, PayloadDeclarationInput};
pub use stages::{OsDeployStage, StageDependency, StageKind};

/// Fixed errors never retain rejected input.
#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum ContractError {
    #[error("stage is not a guest action")]
    InvalidGuestActionStage,
    #[error("guest action operation and attempt identities must be non-nil")]
    InvalidGuestActionIdentity,
    #[error("persisted deployment plan is invalid")]
    InvalidPersistedPlan,
    #[error("desired PVE disk serial exceeds the 20-byte dispatch limit")]
    UnsupportedPveDiskSerial,
    #[error("PVE expectations could not be derived from the deployment plan")]
    InvalidPveExpectations,
    #[error("payload reference must be non-nil")]
    InvalidPayloadReference,
    #[error("payload SHA-256 must contain exactly 64 ASCII hexadecimal characters")]
    InvalidPayloadSha256,
    #[error("template configuration SHA-256 must contain exactly 64 ASCII hexadecimal characters")]
    InvalidTemplateConfigSha256,
    #[error("OSDeploy VM memory must be at least 4096 MiB")]
    InsufficientVmMemory,
    #[error("deployment PVE name must match the VM plan name")]
    VmNameMismatch,
    #[error("apply image index must fit a positive signed 32-bit integer")]
    InvalidApplyImageIndex,
    #[error("profile references must be non-nil")]
    InvalidProfileReference,
    #[error("media reference must match the native ISO volume policy")]
    InvalidMediaVolid,
    #[error("deployment and driver media references must be different")]
    DuplicateMediaVolid,
    #[error("serial must match the bounded native ASCII token policy")]
    InvalidSerial,
    #[error("OS label must match the bounded native ASCII label policy")]
    InvalidOsLabel,
    #[error("OS language must match the bounded native ASCII language policy")]
    InvalidOsLanguage,
    #[error("deployment fingerprint could not be computed")]
    FingerprintFailed,
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

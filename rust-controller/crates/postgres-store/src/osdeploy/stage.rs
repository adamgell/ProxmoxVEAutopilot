use super::*;
use osdeploy_adapter::pve_expectations;
use pve_port::ProvisioningActionV1;

impl OsDeployOperationPlanV1 {
    pub fn derive(plan: &OsDeployPlanV1, stage: OsDeployStage) -> Result<Self, OsDeployStoreError> {
        // Every stage must belong to a workflow whose entire PVE subset is usable.
        let expected = pve_expectations(plan).map_err(|_| OsDeployStoreError::Validation)?;
        let action = match stage {
            OsDeployStage::Clone => Some(ProvisioningActionV1::Clone),
            OsDeployStage::DiskCapacity => Some(ProvisioningActionV1::EnsureCapacity),
            OsDeployStage::ConfigurePe => Some(ProvisioningActionV1::ConfigurePe),
            OsDeployStage::StartPe => Some(ProvisioningActionV1::StartPe),
            OsDeployStage::PeEnsureStopped => Some(ProvisioningActionV1::EnsureStopped),
            OsDeployStage::ConfigureDisk => Some(ProvisioningActionV1::ConfigureDisk),
            OsDeployStage::StartDisk => Some(ProvisioningActionV1::StartDisk),
            OsDeployStage::PeRegister
            | OsDeployStage::PeComplete
            | OsDeployStage::PeShutdownGrace
            | OsDeployStage::InstallQga
            | OsDeployStage::VerifyQga
            | OsDeployStage::InstallQgaWatchdog
            | OsDeployStage::InstallAgent
            | OsDeployStage::AgentHeartbeat
            | OsDeployStage::VerifyOperational => None,
        };
        Ok(Self {
            contract_version: 1,
            workflow_sha256: plan
                .fingerprint()
                .map_err(|_| OsDeployStoreError::Validation)?,
            stage,
            pve: action.map(|action| ProvisioningOperationPlanV1::new(action, expected)),
        })
    }
    pub fn fingerprint(&self) -> Result<String, OsDeployStoreError> {
        event_journal::payload_digest(&serde_json::to_value(self)?)
            .map_err(|_| OsDeployStoreError::Validation)
    }
}
pub(super) fn name(stage: OsDeployStage) -> Result<String, OsDeployStoreError> {
    serde_json::to_value(stage)?
        .as_str()
        .map(str::to_owned)
        .ok_or(OsDeployStoreError::Validation)
}
pub(super) const fn dependency(ordinal: usize) -> &'static str {
    match ordinal {
        0 => "intake",
        7 => "grace_gate",
        _ => "satisfied",
    }
}
pub(super) fn command_key(run: RunId, stage: OsDeployStage) -> String {
    format!("osdeploy:{}:{}", run.as_uuid(), stage.operation_key())
}
pub(super) fn plans(
    plan: &OsDeployPlanV1,
) -> Result<[OsDeployOperationPlanV1; 16], OsDeployStoreError> {
    OsDeployStage::ALL
        .into_iter()
        .map(|stage| OsDeployOperationPlanV1::derive(plan, stage))
        .collect::<Result<Vec<_>, _>>()?
        .try_into()
        .map_err(|_| OsDeployStoreError::Validation)
}

use crate::{ContractError, OsDeployPlanV1};
use pve_port::{ProvisioningExpectationsInputV1, ProvisioningExpectationsV1};
/// Derive the executable PVE subset before workflow registration or dispatch.
/// The full OSDeploy fingerprint remains a separate workflow identity.
pub fn pve_expectations(
    plan: &OsDeployPlanV1,
) -> Result<ProvisioningExpectationsV1, ContractError> {
    if !plan.disk_serial().is_ascii() || plan.disk_serial().len() > 20 {
        return Err(ContractError::UnsupportedPveDiskSerial);
    }
    ProvisioningExpectationsV1::new(ProvisioningExpectationsInputV1 {
        vm: plan.vm().clone(),
        template_config_sha256: plan.template_config_sha256(),
        template_capacity_bytes: plan.disk().template_bytes(),
        effective_capacity_bytes: plan.disk().effective_bytes(),
        system_serial: plan.system_serial(),
        disk_serial: plan.disk_serial(),
        deployment_iso_volid: plan.deployment_iso_volid(),
        driver_iso_volid: plan.driver_iso_volid(),
    })
    .map_err(|_| ContractError::InvalidPveExpectations)
}

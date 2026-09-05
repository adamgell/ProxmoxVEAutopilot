use super::*;

impl ProvisioningExpectationsV1 {
    pub fn new(input: ProvisioningExpectationsInputV1<'_>) -> Result<Self, InvalidProvisioning> {
        if input.template_config_sha256.len() != 64
            || !input
                .template_config_sha256
                .bytes()
                .all(|b| b.is_ascii_hexdigit())
            || input.template_capacity_bytes == 0
            || input.effective_capacity_bytes < input.template_capacity_bytes
            || (input.effective_capacity_bytes > input.template_capacity_bytes
                && !input.effective_capacity_bytes.is_multiple_of(1_073_741_824))
            || !is_serial(input.system_serial)
            || !is_serial(input.disk_serial)
            || media_storage(input.deployment_iso_volid).is_none()
            || media_storage(input.driver_iso_volid).is_none()
            || input.deployment_iso_volid == input.driver_iso_volid
        {
            return Err(InvalidProvisioning);
        }
        Ok(Self {
            contract_version: 1,
            vm: input.vm,
            template_config_sha256: input.template_config_sha256.to_ascii_lowercase(),
            template_capacity_bytes: input.template_capacity_bytes,
            effective_capacity_bytes: input.effective_capacity_bytes,
            system_serial: input.system_serial.to_owned(),
            disk_serial: input.disk_serial.to_owned(),
            deployment_iso_volid: input.deployment_iso_volid.to_owned(),
            driver_iso_volid: input.driver_iso_volid.to_owned(),
        })
    }
    pub fn fingerprint(&self) -> Result<String, InvalidProvisioning> {
        fingerprint(self)
    }
    pub fn vm(&self) -> &NativeVmPlan {
        &self.vm
    }
    pub fn template_config_sha256(&self) -> &str {
        &self.template_config_sha256
    }
    pub fn template_capacity_bytes(&self) -> u64 {
        self.template_capacity_bytes
    }
    pub fn effective_capacity_bytes(&self) -> u64 {
        self.effective_capacity_bytes
    }
    pub fn system_serial(&self) -> &str {
        &self.system_serial
    }
    pub fn disk_serial(&self) -> &str {
        &self.disk_serial
    }
    pub fn deployment_iso_volid(&self) -> &str {
        &self.deployment_iso_volid
    }
    pub fn driver_iso_volid(&self) -> &str {
        &self.driver_iso_volid
    }
}

// These are the approved OSDeploy restricted token rules, kept below the
// adapter dependency boundary. Validation proves syntax, never media existence.
pub(super) fn is_serial(value: &str) -> bool {
    (1..=64).contains(&value.len())
        && value
            .as_bytes()
            .first()
            .is_some_and(u8::is_ascii_alphanumeric)
        && value
            .as_bytes()
            .last()
            .is_some_and(u8::is_ascii_alphanumeric)
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
}

pub(super) fn media_storage(value: &str) -> Option<crate::StorageName> {
    let (storage, path) = value.split_once(':')?;
    let storage = crate::StorageName::parse(storage).ok()?;
    let filename = path.strip_prefix("iso/")?;
    let stem = filename.strip_suffix(".iso")?;
    ((1..=128).contains(&stem.len())
        && !filename.contains("..")
        && stem.bytes().any(|b| b.is_ascii_alphanumeric())
        && stem
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-.".contains(&b)))
    .then_some(storage)
}

impl ProvisioningOperationPlanV1 {
    pub fn new(action: ProvisioningActionV1, expected: ProvisioningExpectationsV1) -> Self {
        Self {
            contract_version: 1,
            action,
            expected,
        }
    }
    pub fn action(&self) -> ProvisioningActionV1 {
        self.action
    }
    pub fn expected(&self) -> &ProvisioningExpectationsV1 {
        &self.expected
    }
    pub fn fingerprint(&self) -> Result<String, InvalidProvisioning> {
        fingerprint(self)
    }
}

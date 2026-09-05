use artifact_index::DeclaredArtifact;
use pve_port::{NativeVmPlan, StorageName};
use serde::Serialize;
use uuid::Uuid;

use crate::{ContractError, DeploymentNames, DiskCapacity, PhasePolicy};

/// Constructor input only; the reference is not evidence that a payload exists.
pub struct PayloadDeclarationInput<'a> {
    pub reference_id: Uuid,
    pub sha256: Option<&'a str>,
}

/// Optional declared hash metadata, never verified content.
///
/// Private construction cannot bypass admission:
/// ```compile_fail,E0451
/// use osdeploy_adapter::PayloadDeclaration;
/// fn bypass(existing: PayloadDeclaration) -> PayloadDeclaration {
///     PayloadDeclaration { reference_id: uuid::Uuid::nil(), ..existing }
/// }
/// ```
/// Deserialization cannot bypass admission:
/// ```compile_fail,E0277
/// use osdeploy_adapter::PayloadDeclaration;
/// let _: PayloadDeclaration = serde_json::from_str("{}").unwrap();
/// ```
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct PayloadDeclaration {
    reference_id: Uuid,
    sha256: Option<String>,
    evidence_level: &'static str,
}

impl PayloadDeclaration {
    pub fn new(input: PayloadDeclarationInput<'_>) -> Result<Self, ContractError> {
        if input.reference_id.is_nil() {
            return Err(ContractError::InvalidPayloadReference);
        }
        if input.sha256.is_some_and(|hash| !is_sha256(hash)) {
            return Err(ContractError::InvalidPayloadSha256);
        }
        Ok(Self {
            reference_id: input.reference_id,
            sha256: input.sha256.map(str::to_ascii_lowercase),
            evidence_level: if input.sha256.is_some() {
                "declared"
            } else {
                "unverified"
            },
        })
    }
    pub fn reference_id(&self) -> Uuid {
        self.reference_id
    }
    pub fn sha256(&self) -> Option<&str> {
        self.sha256.as_deref()
    }
}

/// Constructor input only; no run/operation identities, grants or secret values.
pub struct OsDeployPlanInput<'a> {
    pub vm: NativeVmPlan,
    pub names: DeploymentNames,
    pub artifact: DeclaredArtifact,
    pub disk: DiskCapacity,
    pub policy: PhasePolicy,
    pub driver_payload: PayloadDeclaration,
    pub osd_client_payload: PayloadDeclaration,
    pub agent_payload: PayloadDeclaration,
    pub template_config_sha256: &'a str,
    pub deployment_iso_volid: &'a str,
    pub driver_iso_volid: &'a str,
    pub system_serial: &'a str,
    pub disk_serial: &'a str,
    pub os_version: &'a str,
    pub os_edition: &'a str,
    pub os_language: &'a str,
    pub image_name: &'a str,
    pub secret_profile_id: Uuid,
    pub callback_profile_id: Uuid,
}

/// Immutable declarations, not observation, durable reload or execution authority.
///
/// Private construction cannot bypass admission:
/// ```compile_fail,E0451
/// use osdeploy_adapter::OsDeployPlanV1;
/// fn bypass(existing: OsDeployPlanV1) -> OsDeployPlanV1 {
///     OsDeployPlanV1 { system_serial: String::new(), ..existing }
/// }
/// ```
/// Deserialization cannot bypass admission:
/// ```compile_fail,E0277
/// use osdeploy_adapter::OsDeployPlanV1;
/// let _: OsDeployPlanV1 = serde_json::from_str("{}").unwrap();
/// ```
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct OsDeployPlanV1 {
    #[serde(flatten)]
    native_contract: NativeContract,
    vm: NativeVmPlan,
    names: DeploymentNames,
    artifact: DeclaredArtifact,
    disk: DiskCapacity,
    policy: PhasePolicy,
    driver_payload: PayloadDeclaration,
    osd_client_payload: PayloadDeclaration,
    agent_payload: PayloadDeclaration,
    template_config_sha256: String,
    deployment_iso_volid: String,
    driver_iso_volid: String,
    system_serial: String,
    disk_serial: String,
    os_version: String,
    os_edition: String,
    os_language: String,
    image_name: String,
    secret_profile_id: Uuid,
    callback_profile_id: Uuid,
}

impl OsDeployPlanV1 {
    pub fn new(input: OsDeployPlanInput<'_>) -> Result<Self, ContractError> {
        if input.vm.memory_mib() < 4096 {
            return Err(ContractError::InsufficientVmMemory);
        }
        if input.vm.name() != input.names.pve_name() {
            return Err(ContractError::VmNameMismatch);
        }
        if input.artifact.apply_image_index() > i32::MAX as u32 {
            return Err(ContractError::InvalidApplyImageIndex);
        }
        if input.secret_profile_id.is_nil() || input.callback_profile_id.is_nil() {
            return Err(ContractError::InvalidProfileReference);
        }
        if !is_sha256(input.template_config_sha256) {
            return Err(ContractError::InvalidTemplateConfigSha256);
        }
        if !is_media_volid(input.deployment_iso_volid) || !is_media_volid(input.driver_iso_volid) {
            return Err(ContractError::InvalidMediaVolid);
        }
        if input.deployment_iso_volid == input.driver_iso_volid {
            return Err(ContractError::DuplicateMediaVolid);
        }
        if !is_serial(input.system_serial) || !is_serial(input.disk_serial) {
            return Err(ContractError::InvalidSerial);
        }
        if ![input.os_version, input.os_edition, input.image_name]
            .into_iter()
            .all(is_os_label)
        {
            return Err(ContractError::InvalidOsLabel);
        }
        if !is_os_language(input.os_language) {
            return Err(ContractError::InvalidOsLanguage);
        }
        Ok(Self {
            native_contract: NativeContract::V1,
            vm: input.vm,
            names: input.names,
            artifact: input.artifact,
            disk: input.disk,
            policy: input.policy,
            driver_payload: input.driver_payload,
            osd_client_payload: input.osd_client_payload,
            agent_payload: input.agent_payload,
            template_config_sha256: input.template_config_sha256.to_ascii_lowercase(),
            deployment_iso_volid: input.deployment_iso_volid.to_owned(),
            driver_iso_volid: input.driver_iso_volid.to_owned(),
            system_serial: input.system_serial.to_owned(),
            disk_serial: input.disk_serial.to_owned(),
            os_version: input.os_version.to_owned(),
            os_edition: input.os_edition.to_owned(),
            os_language: input.os_language.to_owned(),
            image_name: input.image_name.to_owned(),
            secret_profile_id: input.secret_profile_id,
            callback_profile_id: input.callback_profile_id,
        })
    }
    /// Digest of the complete canonical snapshot, including fixed fields and provenance.
    /// Later reload must reconstruct through admission and compare this complete digest.
    pub fn fingerprint(&self) -> Result<String, ContractError> {
        let value = serde_json::to_value(self).map_err(|_| ContractError::FingerprintFailed)?;
        event_journal::payload_digest(&value).map_err(|_| ContractError::FingerprintFailed)
    }
    pub fn vm(&self) -> &NativeVmPlan {
        &self.vm
    }
    pub fn names(&self) -> &DeploymentNames {
        &self.names
    }
    pub fn artifact(&self) -> &DeclaredArtifact {
        &self.artifact
    }
    pub fn disk(&self) -> &DiskCapacity {
        &self.disk
    }
    pub fn policy(&self) -> &PhasePolicy {
        &self.policy
    }
    pub fn driver_payload(&self) -> &PayloadDeclaration {
        &self.driver_payload
    }
    pub fn osd_client_payload(&self) -> &PayloadDeclaration {
        &self.osd_client_payload
    }
    pub fn agent_payload(&self) -> &PayloadDeclaration {
        &self.agent_payload
    }
    pub fn template_config_sha256(&self) -> &str {
        &self.template_config_sha256
    }
    pub fn deployment_iso_volid(&self) -> &str {
        &self.deployment_iso_volid
    }
    pub fn driver_iso_volid(&self) -> &str {
        &self.driver_iso_volid
    }
    pub fn system_serial(&self) -> &str {
        &self.system_serial
    }
    pub fn disk_serial(&self) -> &str {
        &self.disk_serial
    }
    pub fn os_version(&self) -> &str {
        &self.os_version
    }
    pub fn os_edition(&self) -> &str {
        &self.os_edition
    }
    pub fn os_language(&self) -> &str {
        &self.os_language
    }
    pub fn image_name(&self) -> &str {
        &self.image_name
    }
    pub fn secret_profile_id(&self) -> Uuid {
        self.secret_profile_id
    }
    pub fn callback_profile_id(&self) -> Uuid {
        self.callback_profile_id
    }
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}

// Native-v1 media references only; this is not a general PVE volume parser or
// an observation that the media exists. Storage may differ from the VM disk.
fn is_media_volid(value: &str) -> bool {
    let Some((storage, path)) = value.split_once(':') else {
        return false;
    };
    if StorageName::parse(storage).is_err() {
        return false;
    }
    let Some(filename) = path.strip_prefix("iso/") else {
        return false;
    };
    let Some(stem) = filename.strip_suffix(".iso") else {
        return false;
    };
    (1..=128).contains(&stem.len())
        && !filename.contains("..")
        && stem.bytes().any(|byte| byte.is_ascii_alphanumeric())
        && stem
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_-.".contains(&byte))
}

fn alphanumeric_endpoints(value: &str) -> bool {
    value
        .as_bytes()
        .first()
        .is_some_and(u8::is_ascii_alphanumeric)
        && value
            .as_bytes()
            .last()
            .is_some_and(u8::is_ascii_alphanumeric)
}

fn is_serial(value: &str) -> bool {
    (1..=64).contains(&value.len())
        && alphanumeric_endpoints(value)
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_-".contains(&byte))
}

fn is_os_label(value: &str) -> bool {
    (1..=128).contains(&value.len())
        && alphanumeric_endpoints(value)
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b" ._-".contains(&byte))
}

fn is_os_language(value: &str) -> bool {
    (2..=16).contains(&value.len())
        && value
            .as_bytes()
            .first()
            .is_some_and(u8::is_ascii_alphabetic)
        && value.as_bytes().last().is_some_and(u8::is_ascii_alphabetic)
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphabetic() || byte == b'-')
}

// Fixed native-v1 declarations have no public input or mutation API. Device
// cleanliness and matching template state require later independent observation.
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
struct NativeContract {
    contract_version: u32,
    workflow_kind: &'static str,
    architecture: &'static str,
    server_role: &'static str,
    firmware: &'static str,
    secure_boot: bool,
    cpu: &'static str,
    balloon: bool,
    qga_channel: &'static str,
    primary_disk: &'static str,
    pe_iso_slot: &'static str,
    driver_iso_slot: &'static str,
    pe_boot_order: &'static str,
    disk_boot_order: &'static str,
    template_device_policy: &'static str,
    evidence_level: &'static str,
}

impl NativeContract {
    const V1: Self = Self {
        contract_version: 1,
        workflow_kind: "os_deploy",
        architecture: "amd64",
        server_role: "base",
        firmware: "seabios",
        secure_boot: false,
        cpu: "host",
        balloon: false,
        qga_channel: "virtio",
        primary_disk: "scsi0",
        pe_iso_slot: "ide2",
        driver_iso_slot: "ide3",
        pe_boot_order: "ide2;scsi0",
        disk_boot_order: "scsi0",
        template_device_policy: "reject_extra_devices",
        evidence_level: "declared_only",
    };
}

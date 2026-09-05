//! Private persisted shapes. Underscored fields are still required and typed;
//! their values are checked by the complete reconstructed snapshot comparison.

use pve_port::{NativeVmName, NativeVmPlan};
use serde::{Deserialize, de};
use serde_json::Value;
use uuid::Uuid;

pub(super) fn object<T: de::DeserializeOwned>(value: Value) -> Result<T, serde_json::Error> {
    if !value.is_object() {
        return Err(de::Error::custom("persisted object required"));
    }
    serde_json::from_value(value)
}

fn object_only<'de, D: de::Deserializer<'de>, T: de::DeserializeOwned>(
    deserializer: D,
) -> Result<T, D::Error> {
    object(Value::deserialize(deserializer)?).map_err(de::Error::custom)
}

// deserialize_with prevents serde's implicit missing-field default for Option.
// A present null is valid; an absent key is not.
fn required_nullable<'de, D: de::Deserializer<'de>, T: Deserialize<'de>>(
    deserializer: D,
) -> Result<Option<T>, D::Error> {
    Option::deserialize(deserializer)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Plan {
    #[serde(rename = "contract_version")]
    _contract_version: u32,
    #[serde(rename = "workflow_kind")]
    _workflow_kind: String,
    #[serde(rename = "architecture")]
    _architecture: String,
    #[serde(rename = "server_role")]
    _server_role: String,
    #[serde(rename = "firmware")]
    _firmware: String,
    #[serde(rename = "secure_boot")]
    _secure_boot: bool,
    #[serde(rename = "cpu")]
    _cpu: String,
    #[serde(rename = "balloon")]
    _balloon: bool,
    #[serde(rename = "qga_channel")]
    _qga_channel: String,
    #[serde(rename = "primary_disk")]
    _primary_disk: String,
    #[serde(rename = "pe_iso_slot")]
    _pe_iso_slot: String,
    #[serde(rename = "driver_iso_slot")]
    _driver_iso_slot: String,
    #[serde(rename = "pe_boot_order")]
    _pe_boot_order: String,
    #[serde(rename = "disk_boot_order")]
    _disk_boot_order: String,
    #[serde(rename = "template_device_policy")]
    _template_device_policy: String,
    #[serde(rename = "evidence_level")]
    _evidence_level: String,
    // Keep NativeVmPlan's legacy public decoder intact while requiring an object
    // here before delegating to its validated deserialization.
    #[serde(deserialize_with = "object_only")]
    pub vm: NativeVmPlan,
    #[serde(deserialize_with = "object_only")]
    pub names: Names,
    #[serde(deserialize_with = "object_only")]
    pub artifact: Artifact,
    #[serde(deserialize_with = "object_only")]
    pub disk: Disk,
    #[serde(deserialize_with = "object_only")]
    pub policy: Policy,
    #[serde(deserialize_with = "object_only")]
    pub driver_payload: Payload,
    #[serde(deserialize_with = "object_only")]
    pub osd_client_payload: Payload,
    #[serde(deserialize_with = "object_only")]
    pub agent_payload: Payload,
    pub template_config_sha256: String,
    pub deployment_iso_volid: String,
    pub driver_iso_volid: String,
    pub system_serial: String,
    pub disk_serial: String,
    pub os_version: String,
    pub os_edition: String,
    pub os_language: String,
    pub image_name: String,
    pub secret_profile_id: Uuid,
    pub callback_profile_id: Uuid,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Names {
    pub requested_name: String,
    pub pve_name: NativeVmName,
    #[serde(rename = "windows_name")]
    _windows_name: String,
    #[serde(rename = "expected_agent_id")]
    _expected_agent_id: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Artifact {
    #[serde(rename = "contract_version")]
    _contract_version: u32,
    pub artifact_id: Uuid,
    pub architecture: String,
    pub build_label: String,
    pub iso_sha256: String,
    pub wim_sha256: String,
    pub source_image_index: u32,
    #[serde(deserialize_with = "required_nullable")]
    pub output_image_index: Option<u32>,
    #[serde(rename = "apply_image_index")]
    _apply_image_index: u32,
    #[serde(rename = "image_index_source")]
    _image_index_source: String,
    #[serde(rename = "evidence_level")]
    _evidence_level: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Disk {
    pub requested_gib: u64,
    pub template_bytes: u64,
    #[serde(rename = "effective_bytes")]
    _effective_bytes: u64,
    #[serde(rename = "growth_required")]
    _growth_required: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Policy {
    pub registration_seconds: u32,
    pub pe_seconds: u32,
    pub shutdown_grace_seconds: u32,
    pub full_os_seconds: u32,
    pub mutation_seconds: u32,
    pub evidence_freshness_seconds: u32,
    pub allow_force_stop: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Payload {
    pub reference_id: Uuid,
    #[serde(deserialize_with = "required_nullable")]
    pub sha256: Option<String>,
    #[serde(rename = "evidence_level")]
    _evidence_level: String,
}

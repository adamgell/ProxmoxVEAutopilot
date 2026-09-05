//! Persisted declarations are untrusted inputs, never execution authority.
mod wire;

use artifact_index::{DeclaredArtifact, DeclaredArtifactInput};
use serde::{Deserialize, de};
use serde_json::{Map, Value};

use crate::{
    ContractError, DeploymentNames, DiskCapacity, OsDeployPlanInput, OsDeployPlanV1,
    PayloadDeclaration, PayloadDeclarationInput, PhasePolicy, PhasePolicyInput,
};

/// Restore a complete v1 declaration through admission and bind its full digest.
///
/// Object key order and whitespace are immaterial. All fields, including derived
/// claims and required nulls, must match the reconstructed canonical snapshot.
/// This neither looks up artifact bytes nor grants dispatch or execution authority.
/// Every rejected input returns the same payload-free error.
pub fn restore_osdeploy_plan_v1(
    canonical_json: &str,
    expected_workflow_sha256: &str,
) -> Result<OsDeployPlanV1, ContractError> {
    let invalid = ContractError::InvalidPersistedPlan;
    if canonical_json.len() > 65_536
        || expected_workflow_sha256.len() != 64
        || !expected_workflow_sha256
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(invalid);
    }

    // Deserialize text once, rejecting duplicate keys before Value can discard
    // them. serde_json's default recursion limit remains enabled.
    let StrictValue(original) = serde_json::from_str(canonical_json).map_err(|_| invalid)?;
    let wire = wire::object::<wire::Plan>(original.clone()).map_err(|_| invalid)?;
    let plan = reconstruct(wire).map_err(|_| invalid)?;
    let reconstructed = serde_json::to_value(&plan).map_err(|_| invalid)?;
    if original != reconstructed
        || plan.fingerprint().map_err(|_| invalid)? != expected_workflow_sha256
    {
        return Err(invalid);
    }
    Ok(plan)
}

fn reconstruct(wire: wire::Plan) -> Result<OsDeployPlanV1, ContractError> {
    let invalid = ContractError::InvalidPersistedPlan;
    let names = DeploymentNames::new(&wire.names.requested_name, wire.names.pve_name)?;
    let artifact = DeclaredArtifact::new(DeclaredArtifactInput {
        artifact_id: wire.artifact.artifact_id,
        architecture: &wire.artifact.architecture,
        build_label: &wire.artifact.build_label,
        iso_sha256: &wire.artifact.iso_sha256,
        wim_sha256: &wire.artifact.wim_sha256,
        source_image_index: wire.artifact.source_image_index,
        output_image_index: wire.artifact.output_image_index,
    })
    .map_err(|_| invalid)?;
    let disk = DiskCapacity::new(wire.disk.requested_gib, wire.disk.template_bytes)?;
    let policy = PhasePolicy::new(PhasePolicyInput {
        registration_seconds: wire.policy.registration_seconds,
        pe_seconds: wire.policy.pe_seconds,
        shutdown_grace_seconds: wire.policy.shutdown_grace_seconds,
        full_os_seconds: wire.policy.full_os_seconds,
        mutation_seconds: wire.policy.mutation_seconds,
        evidence_freshness_seconds: wire.policy.evidence_freshness_seconds,
        allow_force_stop: wire.policy.allow_force_stop,
    })?;
    OsDeployPlanV1::new(OsDeployPlanInput {
        vm: wire.vm,
        names,
        artifact,
        disk,
        policy,
        driver_payload: payload(wire.driver_payload)?,
        osd_client_payload: payload(wire.osd_client_payload)?,
        agent_payload: payload(wire.agent_payload)?,
        template_config_sha256: &wire.template_config_sha256,
        deployment_iso_volid: &wire.deployment_iso_volid,
        driver_iso_volid: &wire.driver_iso_volid,
        system_serial: &wire.system_serial,
        disk_serial: &wire.disk_serial,
        os_version: &wire.os_version,
        os_edition: &wire.os_edition,
        os_language: &wire.os_language,
        image_name: &wire.image_name,
        secret_profile_id: wire.secret_profile_id,
        callback_profile_id: wire.callback_profile_id,
    })
}

fn payload(wire: wire::Payload) -> Result<PayloadDeclaration, ContractError> {
    PayloadDeclaration::new(PayloadDeclarationInput {
        reference_id: wire.reference_id,
        sha256: wire.sha256.as_deref(),
    })
}

/// This contract contains no arrays or fractional numbers. Restricting the text
/// grammar here also prevents positional struct decoding in reused decoders.
struct StrictValue(Value);

impl<'de> Deserialize<'de> for StrictValue {
    fn deserialize<D: de::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Visitor;
        impl<'de> de::Visitor<'de> for Visitor {
            type Value = StrictValue;

            fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                formatter.write_str("a persisted plan value")
            }

            fn visit_map<A: de::MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
                let mut object = Map::new();
                while let Some(key) = map.next_key::<String>()? {
                    if object.contains_key(&key) {
                        return Err(de::Error::custom("duplicate persisted field"));
                    }
                    let StrictValue(value) = map.next_value()?;
                    object.insert(key, value);
                }
                Ok(StrictValue(Value::Object(object)))
            }

            fn visit_bool<E: de::Error>(self, value: bool) -> Result<Self::Value, E> {
                Ok(StrictValue(Value::Bool(value)))
            }

            fn visit_u64<E: de::Error>(self, value: u64) -> Result<Self::Value, E> {
                Ok(StrictValue(Value::from(value)))
            }

            fn visit_i64<E: de::Error>(self, value: i64) -> Result<Self::Value, E> {
                Ok(StrictValue(Value::from(value)))
            }

            fn visit_str<E: de::Error>(self, value: &str) -> Result<Self::Value, E> {
                Ok(StrictValue(Value::String(value.to_owned())))
            }

            fn visit_unit<E: de::Error>(self) -> Result<Self::Value, E> {
                Ok(StrictValue(Value::Null))
            }
        }
        deserializer.deserialize_any(Visitor)
    }
}

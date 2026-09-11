//! Additive cluster-inventory portion of full postconditions; grants no authority.
use super::{FixtureStageIdentity, stage_identity::invalid};
use crate::{
    NativeEvidenceSource, PowerState, ProvisioningCoverageV1, ProvisioningVmConfigV1,
    fixture_ipc::FixtureStageRequest,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{collections::BTreeSet, io};
use uuid::Uuid;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(
    tag = "kind",
    content = "value",
    rename_all = "snake_case",
    deny_unknown_fields
)]
pub enum FixtureConfigurationIdentityV2 {
    /// Opaque server configuration revision, never interpreted as a hash.
    PveDigest(String),
    /// SHA-256 of serde_json::to_vec(ProvisioningVmConfigV1), including its
    /// original observation timestamp and opaque digest. No normalization.
    CanonicalConfigSha256V1(String),
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureInventoryMemberV2 {
    pub identity: FixtureConfigurationIdentityV2,
    pub config: ProvisioningVmConfigV1,
    pub power: PowerState,
    pub coverage: ProvisioningCoverageV1,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureStageInventoryV2 {
    pub version: u8,
    pub fixture_id: Uuid,
    pub identity: FixtureStageIdentity,
    pub receipt_sha256: String,
    pub observed_unix_ms: u64,
    pub coverage: ProvisioningCoverageV1,
    pub members: Vec<FixtureInventoryMemberV2>,
}
impl FixtureStageInventoryV2 {
    /// The expected identity, original receipt and acceptance/publication clocks
    /// must come from the trusted durable record, not this untrusted document.
    /// Completeness is a fixture attestation, not independent collection proof.
    pub fn decode(
        bytes: &[u8],
        expected: &FixtureStageIdentity,
        request: &FixtureStageRequest,
        original_receipt: &[u8],
        accepted: u64,
        published: u64,
    ) -> io::Result<Self> {
        if bytes.len() > 65_536 {
            return Err(invalid());
        }
        let value: Self = serde_json::from_slice(bytes)?;
        expected.validate_request(request)?;
        request
            .decode_receipt(original_receipt)
            .map_err(|_| invalid())?;
        if value.version != 2
            || value.fixture_id != request.fixture_id()
            || &value.identity != expected
            || value.receipt_sha256 != format!("{:x}", Sha256::digest(original_receipt))
            || accepted == 0
            || value.observed_unix_ms <= accepted
            || value.observed_unix_ms > published
            || value.coverage != ProvisioningCoverageV1::Complete
            || value.members.is_empty()
            || value.members.len() > 32
        {
            return Err(invalid());
        }
        let mut ids = BTreeSet::new();
        let mut uuids = BTreeSet::new();
        let mut macs = BTreeSet::new();
        let mut disks = BTreeSet::new();
        for member in &value.members {
            let config = &member.config;
            let matched = match &member.identity {
                FixtureConfigurationIdentityV2::PveDigest(digest) => {
                    !digest.is_empty()
                        && digest.len() <= 255
                        && !digest.chars().any(char::is_control)
                        && digest == config.digest()
                }
                FixtureConfigurationIdentityV2::CanonicalConfigSha256V1(hash) => {
                    hash == &format!("{:x}", Sha256::digest(serde_json::to_vec(config)?))
                }
            };
            if !matched
                || member.coverage != ProvisioningCoverageV1::Complete
                || config.source() != NativeEvidenceSource::FakePve
                || config.locked()
                || !config.unsupported().is_empty()
                || config.observed_at().timestamp_millis() < 0
                || config.observed_at().timestamp_millis() as u64 != value.observed_unix_ms
                || !ids.insert((config.node().clone(), config.vmid()))
                || !uuids.insert(config.uuid())
                || !macs.insert(config.mac().to_string().to_ascii_lowercase())
                || !disks.insert((
                    config.primary_disk().storage().to_owned(),
                    config.primary_disk().volume().to_owned(),
                ))
            {
                return Err(invalid());
            }
        }
        let vm = request.request().plan().expected().vm();
        if !ids.contains(&(vm.node().clone(), vm.source_vmid()))
            || !ids.contains(&(vm.node().clone(), vm.target_vmid()))
        {
            return Err(invalid());
        }
        Ok(value)
    }
}

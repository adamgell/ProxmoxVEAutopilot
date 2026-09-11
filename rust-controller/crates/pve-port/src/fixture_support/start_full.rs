//! Read-only full observations: durable evidence publication, not VM mutation.
use super::*;
use crate::{
    PowerState, ProvisioningCoverageV1, ProvisioningMediaInventoryV1,
    ProvisioningMutationRequestV1, fixture_ipc::FixtureStageRequest,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use stage_identity::invalid;
use std::{
    fs,
    io::{self, Read, Write},
    path::Path,
};
use uuid::Uuid;

pub(super) const MAX_BYTES: usize = 131_072;
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureStartPeFullV1 {
    pub version: u8,
    pub inventory: FixtureStageInventoryV2,
    pub durable: StartObservationV1,
    pub deployment_media: ProvisioningMediaInventoryV1,
    pub driver_media: ProvisioningMediaInventoryV1,
    pub node_status: SeedNode,
    pub storage: Vec<SeedStorage>,
    pub bridges: Vec<SeedBridge>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureStartPeFullPublicationV1 {
    pub version: u8,
    pub published_unix_ms: u64,
    pub observation: FixtureStartPeFullV1,
    sha256: String,
}
impl FixtureStartPeFullPublicationV1 {
    pub(super) fn decode(
        bytes: &[u8],
        identity: &FixtureStageIdentity,
        request: &FixtureStageRequest,
        receipt: &[u8],
        durable: &StartObservationV1,
    ) -> io::Result<Self> {
        if bytes.is_empty() || bytes.len() > MAX_BYTES {
            return Err(invalid());
        }
        let record: Self = serde_json::from_slice(bytes)?;
        if record.version != 1
            || record.published_unix_ms > post_dispatch_publication::now()?
            || record.sha256
                != format!(
                    "{:x}",
                    Sha256::digest(serde_json::to_vec(&(
                        record.published_unix_ms,
                        &record.observation
                    ))?)
                )
        {
            return Err(invalid());
        }
        record.observation.validate(
            identity,
            request,
            receipt,
            durable,
            record.published_unix_ms,
        )?;
        Ok(record)
    }
}
impl FixtureStartPeFullV1 {
    fn validate(
        &self,
        identity: &FixtureStageIdentity,
        request: &FixtureStageRequest,
        receipt: &[u8],
        durable: &StartObservationV1,
        published: u64,
    ) -> io::Result<()> {
        let ProvisioningMutationRequestV1::Start(start) = request.request() else {
            return Err(invalid());
        };
        let (_, accepted, _) = durable.observation_clocks();
        durable.validate_restoration(
            identity.operation,
            &identity.request_sha256,
            identity.ledger_binding(),
            receipt,
            request.request().plan().expected().vm().target_vmid().get(),
        )?;
        if self.version != 1
            || serde_json::to_vec(&self.durable)? != serde_json::to_vec(durable)?
            || published < durable.published_unix_ms
        {
            return Err(invalid());
        }
        let inventory = FixtureStageInventoryV2::decode(
            &serde_json::to_vec(&self.inventory)?,
            identity,
            request,
            receipt,
            accepted,
            published,
        )?;
        let at = inventory.observed_unix_ms;
        if at < durable.published_unix_ms {
            return Err(invalid());
        }
        let expected = request.request().plan().expected();
        let vm = expected.vm();
        let source = inventory
            .members
            .iter()
            .find(|m| m.config.node() == vm.node() && m.config.vmid() == vm.source_vmid())
            .ok_or_else(invalid)?;
        let target = inventory
            .members
            .iter()
            .find(|m| m.config.node() == vm.node() && m.config.vmid() == vm.target_vmid())
            .ok_or_else(invalid)?;
        let normalized = |config: &crate::ProvisioningVmConfigV1| -> io::Result<serde_json::Value> {
            let mut value = serde_json::to_value(config)?;
            value
                .as_object_mut()
                .ok_or_else(invalid)?
                .remove("observed_at");
            Ok(value)
        };
        if source.power != PowerState::Stopped
            || target.power != PowerState::Running
            || source
                .config
                .template_fingerprint()
                .map_err(|_| invalid())?
                != expected.template_config_sha256()
            || normalized(&target.config)? != normalized(start.expected_before().config())?
        {
            return Err(invalid());
        }
        for (media, iso) in [
            (&self.deployment_media, expected.deployment_iso_volid()),
            (&self.driver_media, expected.driver_iso_volid()),
        ] {
            if media.node() != vm.node()
                || media.coverage() != ProvisioningCoverageV1::Complete
                || media.observed_at().timestamp_millis() < 0
                || media.observed_at().timestamp_millis() as u64 != at
                || !media.iso_volids().iter().any(|v| v == iso)
            {
                return Err(invalid());
            }
        }
        if self.deployment_media.storage() == self.driver_media.storage()
            && self.deployment_media != self.driver_media
        {
            return Err(invalid());
        }
        if !self.node_status.online
            || self.storage.is_empty()
            || self.storage.len() > 32
            || self.bridges.is_empty()
            || self.bridges.len() > 32
        {
            return Err(invalid());
        }
        let mut storage_names = std::collections::BTreeSet::new();
        let mut bridge_names = std::collections::BTreeSet::new();
        if self
            .storage
            .iter()
            .any(|s| s.name.is_empty() || !storage_names.insert(&s.name))
            || self
                .bridges
                .iter()
                .any(|b| b.name.is_empty() || !bridge_names.insert(&b.name))
        {
            return Err(invalid());
        }
        if !self.storage.iter().any(|s| {
            s.name == vm.storage().as_str()
                && s.active
                && s.enabled
                && s.available_bytes >= vm.minimum_storage_bytes()
                && s.content.iter().any(|v| v == "images")
        }) || !self
            .bridges
            .iter()
            .any(|b| b.name == vm.bridge().as_str() && b.active)
        {
            return Err(invalid());
        }
        Ok(())
    }
}

pub(super) fn handle(
    directory: &Path,
    log: &mut durable_fixture_log::FixtureLog,
    identity: FixtureStageIdentity,
    request: serde_json::Value,
    publication: Option<(FixtureStartPeFullV1, Uuid, u64)>,
) -> io::Result<Vec<u8>> {
    let request =
        FixtureStageRequest::decode(&serde_json::to_vec(&request)?).map_err(|_| invalid())?;
    identity.validate_request(&request)?;
    if identity.stage != FixtureLedgerStage::StartPe {
        return Err(invalid());
    }
    let effect = log
        .accepted_stage_effect(
            identity.operation,
            &identity.request_sha256,
            identity.ledger_binding(),
        )?
        .ok_or_else(invalid)?;
    let receipt = effect.receipt().ok_or_else(invalid)?;
    let durable = log
        .start_observation(
            identity.operation,
            &identity.request_sha256,
            identity.ledger_binding(),
        )?
        .ok_or_else(invalid)?;
    let path = directory.join(format!(
        "start-full-{}-{}.json",
        identity.operation, identity.request_sha256
    ));
    if let Some((observation, generation, accepted)) = publication {
        let (durable_generation, durable_accepted, _) = durable.observation_clocks();
        if generation != durable_generation || accepted != durable_accepted {
            return Err(invalid());
        }
        let published = post_dispatch_publication::now()?;
        observation.validate(&identity, &request, receipt, durable, published)?;
        let sha256 = format!(
            "{:x}",
            Sha256::digest(serde_json::to_vec(&(published, &observation))?)
        );
        let record = FixtureStartPeFullPublicationV1 {
            version: 1,
            published_unix_ms: published,
            observation,
            sha256,
        };
        let bytes = serde_json::to_vec(&record)?;
        if bytes.len() > MAX_BYTES {
            return Err(invalid());
        }
        // Exclusive creation prevents duplicate/reassignment overwrite. A torn
        // file remains a fail-closed read error, never a usable publication.
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .map_err(|error| {
                if error.kind() == io::ErrorKind::AlreadyExists {
                    invalid()
                } else {
                    error
                }
            })?;
        file.write_all(&bytes)?;
        file.sync_all()?;
        fs::File::open(directory)?.sync_all()?;
        return Ok(bytes);
    }
    let file = match fs::File::open(&path) {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(b"null".to_vec()),
        Err(error) => return Err(error),
    };
    let mut bytes = Vec::new();
    file.take((MAX_BYTES + 1) as u64).read_to_end(&mut bytes)?;
    FixtureStartPeFullPublicationV1::decode(&bytes, &identity, &request, receipt, durable)?;
    Ok(bytes)
}

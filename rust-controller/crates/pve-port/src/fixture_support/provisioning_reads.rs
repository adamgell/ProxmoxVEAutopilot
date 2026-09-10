//! Historical supervisor facts. Missing facts never imply absence or stopped power.
use super::SeedRead;
use crate::{PowerState, ProvisioningMediaInventoryV1, ProvisioningVmConfigV1};
use serde::{Deserialize, Serialize};
use std::{
    fs::File,
    io::{self, Read},
    path::Path,
};
use uuid::Uuid;
pub const MAX_PROVISIONING_READ_BYTES: usize = 65_536;
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureProvisioningIdentity {
    pub fixture_id: Uuid,
    pub operation: Uuid,
    pub request_sha256: String,
    pub node: String,
    pub source_vmid: u32,
    pub target_vmid: u32,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "presence", rename_all = "snake_case", deny_unknown_fields)]
pub enum SeedConfig {
    Absent {},
    Present { config: Box<ProvisioningVmConfigV1> },
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureProvisioningReads {
    pub version: u8,
    pub identity: FixtureProvisioningIdentity,
    pub source_config: SeedRead<SeedConfig>,
    pub target_config: SeedRead<SeedConfig>,
    pub source_power: SeedRead<PowerState>,
    pub target_power: SeedRead<PowerState>,
    pub deployment_media: SeedRead<ProvisioningMediaInventoryV1>,
    pub driver_media: SeedRead<ProvisioningMediaInventoryV1>,
}
fn invalid() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        "invalid fixture provisioning reads",
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn errors_remain_explicit_and_identity_and_unknown_fields_fail_closed() {
        let identity = FixtureProvisioningIdentity {
            fixture_id: Uuid::from_u128(1),
            operation: Uuid::from_u128(2),
            request_sha256: "a".repeat(64),
            node: "fixture-node".into(),
            source_vmid: 100,
            target_vmid: 101,
        };
        let error =
            serde_json::json!({"state":"error","observed_unix_ms":123,"error":"unavailable"});
        let value = serde_json::json!({"version":1,"identity":identity,"source_config":error,"target_config":error,"source_power":error,"target_power":error,"deployment_media":error,"driver_media":error});
        let bytes = serde_json::to_vec(&value).unwrap();
        let decoded = FixtureProvisioningReads::decode(&bytes, &identity).unwrap();
        assert!(matches!(
            decoded.target_power,
            SeedRead::Error {
                observed_unix_ms: 123,
                ..
            }
        ));
        let mut other = identity.clone();
        other.operation = Uuid::from_u128(3);
        assert!(FixtureProvisioningReads::decode(&bytes, &other).is_err());
        let mut unknown = value.clone();
        unknown["source_power"]["extra"] = true.into();
        assert!(
            FixtureProvisioningReads::decode(&serde_json::to_vec(&unknown).unwrap(), &identity)
                .is_err()
        );
        assert!(
            FixtureProvisioningReads::decode(
                &vec![b' '; MAX_PROVISIONING_READ_BYTES + 1],
                &identity
            )
            .is_err()
        );
        let duplicate = String::from_utf8(bytes).unwrap().replacen(
            "\"version\":1",
            "\"version\":1,\"version\":1",
            1,
        );
        assert!(FixtureProvisioningReads::decode(duplicate.as_bytes(), &identity).is_err());
    }
}
impl FixtureProvisioningIdentity {
    pub fn validate(&self) -> io::Result<()> {
        if self.fixture_id.is_nil()
            || self.operation.is_nil()
            || self.source_vmid == 0
            || self.target_vmid == 0
            || self.source_vmid == self.target_vmid
            || self.node.is_empty()
            || self.node.len() > 63
            || !self
                .node
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
            || self.request_sha256.len() != 64
            || !self
                .request_sha256
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(invalid());
        }
        Ok(())
    }
}
fn valid<T>(read: &SeedRead<T>, check: impl FnOnce(&T, u64) -> bool) -> bool {
    match read {
        SeedRead::Observed {
            observed_unix_ms,
            value,
        } => *observed_unix_ms > 0 && check(value, *observed_unix_ms),
        SeedRead::Error {
            observed_unix_ms, ..
        } => *observed_unix_ms > 0,
    }
}
impl FixtureProvisioningReads {
    pub fn decode(bytes: &[u8], expected: &FixtureProvisioningIdentity) -> io::Result<Self> {
        expected.validate()?;
        if bytes.is_empty() || bytes.len() > MAX_PROVISIONING_READ_BYTES {
            return Err(invalid());
        }
        let seed: Self = serde_json::from_slice(bytes).map_err(|_| invalid())?;
        let config = |value: &SeedConfig, time: u64, vmid: u32| match value {
            SeedConfig::Absent {} => true,
            SeedConfig::Present { config } => {
                config.node().as_str() == expected.node
                    && config.vmid().get() == vmid
                    && u64::try_from(config.observed_at().timestamp_millis()).ok() == Some(time)
            }
        };
        let media = |value: &ProvisioningMediaInventoryV1, time| {
            value.node().as_str() == expected.node
                && u64::try_from(value.observed_at().timestamp_millis()).ok() == Some(time)
                && value.iso_volids().len() <= 32
                && value
                    .iso_volids()
                    .iter()
                    .collect::<std::collections::BTreeSet<_>>()
                    .len()
                    == value.iso_volids().len()
        };
        if seed.version != 1
            || seed.identity != *expected
            || !valid(&seed.source_config, |v, t| {
                config(v, t, expected.source_vmid)
            })
            || !valid(&seed.target_config, |v, t| {
                config(v, t, expected.target_vmid)
            })
            || !valid(&seed.source_power, |_, _| true)
            || !valid(&seed.target_power, |_, _| true)
            || !valid(&seed.deployment_media, media)
            || !valid(&seed.driver_media, media)
        {
            return Err(invalid());
        }
        Ok(seed)
    }
    pub(crate) fn load_startup(path: &Path) -> io::Result<Option<Self>> {
        let file = match File::open(path) {
            Ok(f) => f,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(e),
        };
        let mut bytes = Vec::new();
        file.take((MAX_PROVISIONING_READ_BYTES + 1) as u64)
            .read_to_end(&mut bytes)?;
        if bytes.len() > MAX_PROVISIONING_READ_BYTES {
            return Err(invalid());
        }
        let seed: Self = serde_json::from_slice(&bytes).map_err(|_| invalid())?;
        Self::decode(&bytes, &seed.identity).map(Some)
    }
}

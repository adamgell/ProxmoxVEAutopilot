//! Historical supervisor observations; this envelope grants no dispatch capability.
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeSet,
    fs::File,
    io::{self, Read},
    path::Path,
};
use uuid::Uuid;

pub const MAX_CLONE_READ_BYTES: usize = 65_536;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "state", rename_all = "snake_case", deny_unknown_fields)]
pub enum SeedRead<T> {
    Observed {
        observed_unix_ms: u64,
        value: T,
    },
    Error {
        observed_unix_ms: u64,
        error: SeedReadError,
    },
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SeedReadError {
    Unavailable,
    Forbidden,
    InvalidResponse,
    Timeout,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SeedNode {
    pub online: bool,
    pub uptime_seconds: u64,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SeedStorage {
    pub name: String,
    pub active: bool,
    pub enabled: bool,
    pub available_bytes: u64,
    pub content: Vec<String>,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SeedBridge {
    pub name: String,
    pub active: bool,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SeedIdentity {
    pub node: String,
    pub vmid: u32,
    pub name: String,
    pub template: bool,
    pub config_sha256: String,
    pub uuid: Uuid,
    pub mac: String,
    pub primary_storage: String,
    pub primary_volume: String,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureCloneReads {
    pub version: u8,
    pub fixture_id: Uuid,
    pub node: String,
    pub node_status: SeedRead<SeedNode>,
    pub storage: SeedRead<Vec<SeedStorage>>,
    pub bridges: SeedRead<Vec<SeedBridge>>,
    /// Supervisor attests complete synthetic cluster coverage, capped at 32 VMs.
    pub cluster_inventory: SeedRead<Vec<SeedIdentity>>,
}
fn invalid() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "invalid fixture Clone reads")
}
fn atom(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 63
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
}
fn valid_read<T>(r: &SeedRead<T>, valid: impl FnOnce(&T) -> bool) -> bool {
    match r {
        SeedRead::Observed {
            observed_unix_ms,
            value,
        } => *observed_unix_ms > 0 && valid(value),
        SeedRead::Error {
            observed_unix_ms, ..
        } => *observed_unix_ms > 0,
    }
}
impl FixtureCloneReads {
    pub fn decode(bytes: &[u8], fixture_id: Uuid) -> io::Result<Self> {
        if bytes.is_empty() || bytes.len() > MAX_CLONE_READ_BYTES {
            return Err(invalid());
        }
        let seed: Self = serde_json::from_slice(bytes).map_err(|_| invalid())?;
        let valid = seed.version == 1
            && !fixture_id.is_nil()
            && seed.fixture_id == fixture_id
            && atom(&seed.node)
            && valid_read(&seed.node_status, |n| !n.online || n.uptime_seconds > 0)
            && valid_read(&seed.storage, |items| {
                let mut names = BTreeSet::new();
                items.len() <= 32
                    && items.iter().all(|s| {
                        atom(&s.name)
                            && names.insert(&s.name)
                            && s.content.len() <= 7
                            && s.content.iter().collect::<BTreeSet<_>>().len() == s.content.len()
                            && s.content.iter().all(|c| {
                                matches!(
                                    c.as_str(),
                                    "images"
                                        | "rootdir"
                                        | "iso"
                                        | "vztmpl"
                                        | "backup"
                                        | "snippets"
                                        | "import"
                                )
                            })
                    })
            })
            && valid_read(&seed.bridges, |items| {
                let mut names = BTreeSet::new();
                items.len() <= 32 && items.iter().all(|b| atom(&b.name) && names.insert(&b.name))
            })
            && valid_read(&seed.cluster_inventory, |items| {
                let mut ids = BTreeSet::new();
                items.len() <= 32
                    && items.iter().all(|v| {
                        atom(&v.node)
                            && v.vmid > 0
                            && ids.insert(v.vmid)
                            && atom(&v.name)
                            && !v.uuid.is_nil()
                            && atom(&v.primary_storage)
                            && !v.primary_volume.is_empty()
                            && v.primary_volume.len() <= 255
                            && v.primary_volume
                                .bytes()
                                .all(|b| b.is_ascii_alphanumeric() || b"._-/:".contains(&b))
                            && v.config_sha256.len() == 64
                            && v.config_sha256
                                .bytes()
                                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                            && v.mac.len() == 17
                            && v.mac.split(':').count() == 6
                            && v.mac.split(':').all(|part| {
                                part.len() == 2 && part.bytes().all(|b| b.is_ascii_hexdigit())
                            })
                    })
            });
        if !valid {
            return Err(invalid());
        }
        Ok(seed)
    }
    /// Missing seed is unavailable. Loading never refreshes the recorded times.
    pub fn load(path: &Path, fixture_id: Uuid) -> io::Result<Option<Self>> {
        let file = match File::open(path) {
            Ok(file) => file,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(e),
        };
        let mut bytes = Vec::new();
        file.take((MAX_CLONE_READ_BYTES + 1) as u64)
            .read_to_end(&mut bytes)?;
        Self::decode(&bytes, fixture_id).map(Some)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn seed() -> FixtureCloneReads {
        FixtureCloneReads {
            version: 1,
            fixture_id: Uuid::from_u128(1),
            node: "fixture-node".into(),
            node_status: SeedRead::Observed {
                observed_unix_ms: 123,
                value: SeedNode {
                    online: true,
                    uptime_seconds: 1,
                },
            },
            storage: SeedRead::Error {
                observed_unix_ms: 124,
                error: SeedReadError::Forbidden,
            },
            bridges: SeedRead::Observed {
                observed_unix_ms: 125,
                value: vec![],
            },
            cluster_inventory: SeedRead::Observed {
                observed_unix_ms: 126,
                value: vec![],
            },
        }
    }
    #[test]
    fn preserves_observation_times_errors_and_identity() {
        let value = seed();
        let bytes = serde_json::to_vec(&value).unwrap();
        assert_eq!(
            FixtureCloneReads::decode(&bytes, value.fixture_id).unwrap(),
            value
        );
        assert!(FixtureCloneReads::decode(&bytes, Uuid::from_u128(2)).is_err());
    }
    #[test]
    fn rejects_unknown_fields_zero_time_duplicates_and_caps() {
        let value = seed();
        for change in [0, 1, 2, 3] {
            let mut json = serde_json::to_value(&value).unwrap();
            match change {
                0 => json["extra"] = true.into(),
                1 => json["node_status"]["observed_unix_ms"] = 0.into(),
                2 => {
                    json["bridges"]["value"] = serde_json::json!([{"name":"vmbr0","active":true},{"name":"vmbr0","active":false}])
                }
                _ => json["node_status"]["value"]["extra"] = true.into(),
            }
            assert!(
                FixtureCloneReads::decode(&serde_json::to_vec(&json).unwrap(), value.fixture_id)
                    .is_err()
            );
        }
        assert!(
            FixtureCloneReads::decode(&vec![b' '; MAX_CLONE_READ_BYTES + 1], value.fixture_id)
                .is_err()
        );
    }
}

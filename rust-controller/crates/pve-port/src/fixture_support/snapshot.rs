//! Supervisor-seeded inventory. Missing seed is unavailable, never VM absence.
use serde::{Deserialize, Serialize};
use std::{
    fs::File,
    io::{self, Read},
    path::Path,
};
use uuid::Uuid;

pub const MAX_SNAPSHOT: usize = 4096;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureInventory {
    pub version: u8,
    pub fixture_id: Uuid,
    pub node: String,
    pub observed_unix_ms: u64,
    /// Complete inventory for this synthetic node, limited to one VM initially.
    pub vms: Vec<FixtureVmConfig>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureVmConfig {
    pub vmid: u32,
    pub name: String,
    pub template: bool,
    pub disk_bytes: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "state", rename_all = "snake_case", deny_unknown_fields)]
pub enum FixtureSnapshot {
    Unavailable {},
    Inventory { inventory: FixtureInventory },
}

fn invalid() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "invalid fixture inventory")
}
fn identity(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 63
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}
impl FixtureSnapshot {
    pub fn decode(bytes: &[u8]) -> io::Result<Self> {
        if bytes.is_empty() || bytes.len() > MAX_SNAPSHOT {
            return Err(invalid());
        }
        let snapshot: Self = serde_json::from_slice(bytes).map_err(|_| invalid())?;
        if let Self::Inventory { inventory } = &snapshot
            && (inventory.version != 1
                || inventory.fixture_id.is_nil()
                || !identity(&inventory.node)
                || inventory.observed_unix_ms == 0
                || inventory.vms.len() > 1
                || inventory
                    .vms
                    .iter()
                    .any(|vm| vm.vmid == 0 || !identity(&vm.name) || vm.disk_bytes == 0))
        {
            return Err(invalid());
        }
        Ok(snapshot)
    }
    pub(super) fn load(path: &Path) -> io::Result<Self> {
        let file = match File::open(path) {
            Ok(file) => file,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(Self::Unavailable {}),
            Err(e) => return Err(e),
        };
        let mut bytes = Vec::new();
        file.take((MAX_SNAPSHOT + 1) as u64)
            .read_to_end(&mut bytes)?;
        Self::decode(&bytes)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn inventory_is_strict_and_bounded() {
        let value = FixtureSnapshot::Inventory {
            inventory: FixtureInventory {
                version: 1,
                fixture_id: Uuid::now_v7(),
                node: "fixture-node".into(),
                observed_unix_ms: 123,
                vms: vec![],
            },
        };
        let bytes = serde_json::to_vec(&value).unwrap();
        assert_eq!(FixtureSnapshot::decode(&bytes).unwrap(), value);
        for bad in [
            br#"{"state":"unavailable","extra":1}"#.as_slice(),
            b"{}",
            &vec![b' '; MAX_SNAPSHOT + 1],
        ] {
            assert!(FixtureSnapshot::decode(bad).is_err());
        }
        let mut json = serde_json::to_value(value).unwrap();
        json["inventory"]["observed_unix_ms"] = 0.into();
        assert!(FixtureSnapshot::decode(&serde_json::to_vec(&json).unwrap()).is_err());
    }
}

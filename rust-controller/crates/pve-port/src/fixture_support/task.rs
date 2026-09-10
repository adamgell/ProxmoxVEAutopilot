//! Historical supervisor observations, never caller-selected completion results.
use serde::{Deserialize, Serialize};
use std::{
    fs::File,
    io::{self, Read},
    path::Path,
};
use uuid::Uuid;

pub const MAX_TASK_BYTES: usize = 4096;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureTaskIdentity {
    pub fixture_id: Uuid,
    pub node: String,
    pub operation: Uuid,
    pub request_sha256: String,
    pub upid: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "state", rename_all = "snake_case", deny_unknown_fields)]
pub enum FixtureTaskState {
    Absent {},
    Running {},
    Succeeded {},
    Failed { reason: String },
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureTaskObservation {
    pub version: u8,
    pub identity: FixtureTaskIdentity,
    pub observed_unix_ms: u64,
    pub result: FixtureTaskState,
}

fn invalid() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        "invalid fixture task observation",
    )
}

impl FixtureTaskIdentity {
    pub fn validate(&self) -> io::Result<()> {
        let node_valid = !self.node.is_empty()
            && self.node.len() <= 63
            && self
                .node
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_');
        if self.fixture_id.is_nil()
            || self.operation.is_nil()
            || !node_valid
            || self.request_sha256.len() != 64
            || !self
                .request_sha256
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            || self.upid.len() > 256
            || !self.upid.starts_with(&format!("UPID:{}:", self.node))
            || !self.upid.ends_with(':')
            || !self.upid.bytes().all(|b| b.is_ascii_graphic())
        {
            return Err(invalid());
        }
        Ok(())
    }
}

impl FixtureTaskObservation {
    pub fn decode(bytes: &[u8]) -> io::Result<Self> {
        if bytes.is_empty() || bytes.len() > MAX_TASK_BYTES {
            return Err(invalid());
        }
        let value: Self = serde_json::from_slice(bytes).map_err(|_| invalid())?;
        value.identity.validate()?;
        if value.version != 1 || value.observed_unix_ms == 0 {
            return Err(invalid());
        }
        if let FixtureTaskState::Failed { reason } = &value.result
            && (reason.is_empty() || reason.len() > 256 || reason.chars().any(char::is_control))
        {
            return Err(invalid());
        }
        Ok(value)
    }

    pub(super) fn load(path: &Path) -> io::Result<Option<Self>> {
        let file = match File::open(path) {
            Ok(file) => file,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(e),
        };
        let mut bytes = Vec::new();
        file.take((MAX_TASK_BYTES + 1) as u64)
            .read_to_end(&mut bytes)?;
        Self::decode(&bytes).map(Some)
    }
}

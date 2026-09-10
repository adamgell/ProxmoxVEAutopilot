//! Local fixture substrate only. The supervisor must own this file exclusively.
//! Available only to local tests and the opt-in fixture support module.
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{File, OpenOptions},
    io::{self, BufRead, BufReader, Read, Write},
    path::Path,
};
use uuid::Uuid;

const MAX_LINE: usize = 4096;

#[derive(Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Attempt {
    version: u8,
    sequence: u64,
    operation: Uuid,
    request_sha256: String,
    duplicate: bool,
}

/// Minimal synthetic VM snapshot, not a production PVE configuration.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VmState {
    pub disk_bytes: u64,
    pub pe_configured: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Effect {
    effect_version: u8,
    sequence: u64,
    attempt_sequence: u64,
    operation: Uuid,
    request_sha256: String,
    vmid: u32,
    before: Option<VmState>,
    after: VmState,
}

#[derive(Serialize, Deserialize)]
#[serde(untagged)]
enum Record {
    Attempt(Attempt),
    Effect(Effect),
}

pub struct FixtureLog {
    file: File,
    records: Vec<Attempt>,
    attempted: BTreeSet<Uuid>,
    effects: Vec<Effect>,
    world: BTreeMap<u32, VmState>,
    poisoned: bool,
}

fn invalid() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "invalid fixture log")
}

fn frame(record: &impl Serialize) -> io::Result<Vec<u8>> {
    let payload = serde_json::to_vec(record).map_err(|_| invalid())?;
    Ok(format!(
        "{:08x}:{}:{:x}\n",
        payload.len(),
        std::str::from_utf8(&payload).map_err(|_| invalid())?,
        Sha256::digest(&payload)
    )
    .into_bytes())
}

impl FixtureLog {
    /// Creates a fresh file, never overwriting an existing fixture.
    pub fn create(path: &Path) -> io::Result<Self> {
        let file = OpenOptions::new()
            .read(true)
            .append(true)
            .create_new(true)
            .open(path)?;
        file.sync_all()?;
        // Persist the new directory entry before acknowledging creation.
        File::open(
            path.parent()
                .filter(|p| !p.as_os_str().is_empty())
                .unwrap_or(Path::new(".")),
        )?
        .sync_all()?;
        Ok(Self {
            file,
            records: Vec::new(),
            attempted: BTreeSet::new(),
            effects: Vec::new(),
            world: BTreeMap::new(),
            poisoned: false,
        })
    }

    pub fn recover(path: &Path) -> io::Result<Self> {
        let file = OpenOptions::new().read(true).append(true).open(path)?;
        let mut reader = BufReader::new(file.try_clone()?);
        let mut records = Vec::new();
        let mut attempted = BTreeSet::new();
        let mut effects = Vec::new();
        let mut world = BTreeMap::new();
        loop {
            let mut line = Vec::new();
            let n = reader
                .by_ref()
                .take((MAX_LINE + 1) as u64)
                .read_until(b'\n', &mut line)?;
            if n == 0 {
                break;
            }
            if n > MAX_LINE || line.last() != Some(&b'\n') || n < 75 || line[8] != b':' {
                return Err(invalid());
            }
            let length =
                usize::from_str_radix(std::str::from_utf8(&line[..8]).map_err(|_| invalid())?, 16)
                    .map_err(|_| invalid())?;
            if length.checked_add(75) != Some(n) {
                return Err(invalid());
            }
            let record: Record =
                serde_json::from_slice(&line[9..9 + length]).map_err(|_| invalid())?;
            // Exact regeneration checks checksum, framing and canonical JSON together.
            if frame(&record)? != line {
                return Err(invalid());
            }
            match record {
                Record::Attempt(record) => {
                    if record.version != 1
                        || record.sequence != records.len() as u64 + 1
                        || record.operation.is_nil()
                        || !valid_digest(&record.request_sha256)
                        || record.duplicate != attempted.contains(&record.operation)
                    {
                        return Err(invalid());
                    }
                    attempted.insert(record.operation);
                    records.push(record);
                }
                Record::Effect(effect) => {
                    validate_effect(&effect, &records, &effects, &world)?;
                    world.insert(effect.vmid, effect.after.clone());
                    effects.push(effect);
                }
            }
        }
        Ok(Self {
            file,
            records,
            attempted,
            effects,
            world,
            poisoned: false,
        })
    }

    /// Returns true for a duplicate, *after* durably recording that attempt.
    /// This is admission bookkeeping, never an accepted-effect acknowledgement.
    pub fn record_attempt(&mut self, operation: Uuid, digest: &str) -> io::Result<bool> {
        if self.poisoned {
            return Err(io::Error::other("fixture log requires recovery"));
        }
        if operation.is_nil() || !valid_digest(digest) {
            return Err(invalid());
        }
        let duplicate = self.attempted.contains(&operation);
        let record = Attempt {
            version: 1,
            sequence: u64::try_from(self.records.len())
                .ok()
                .and_then(|n| n.checked_add(1))
                .ok_or_else(invalid)?,
            operation,
            request_sha256: digest.into(),
            duplicate,
        };
        let bytes = frame(&record)?;
        self.poisoned = true;
        self.file.write_all(&bytes)?;
        // sync_data is Rust's fdatasync-equivalent; no success is returned before it.
        self.file.sync_data()?;
        self.attempted.insert(operation);
        self.records.push(record);
        self.poisoned = false;
        Ok(duplicate)
    }

    pub fn records(&self) -> &[Attempt] {
        &self.records
    }

    /// Atomically persists acceptance and its synthetic world transition.
    /// The referenced attempt must already be durable and nonduplicate.
    pub fn record_effect(
        &mut self,
        attempt_sequence: u64,
        vmid: u32,
        before: Option<VmState>,
        after: VmState,
    ) -> io::Result<()> {
        if self.poisoned {
            return Err(io::Error::other("fixture log requires recovery"));
        }
        let attempt = self
            .records
            .iter()
            .find(|a| a.sequence == attempt_sequence)
            .ok_or_else(invalid)?;
        let effect = Effect {
            effect_version: 1,
            sequence: self.effects.len() as u64 + 1,
            attempt_sequence,
            operation: attempt.operation,
            request_sha256: attempt.request_sha256.clone(),
            vmid,
            before,
            after,
        };
        validate_effect(&effect, &self.records, &self.effects, &self.world)?;
        let bytes = frame(&effect)?;
        self.poisoned = true;
        self.file.write_all(&bytes)?;
        self.file.sync_data()?;
        self.world.insert(vmid, effect.after.clone());
        self.effects.push(effect);
        self.poisoned = false;
        Ok(())
    }

    pub fn world(&self) -> &BTreeMap<u32, VmState> {
        &self.world
    }
    pub fn effects(&self) -> &[Effect] {
        &self.effects
    }

    /// Reads a committed synthetic effect for the exact original request.
    /// An admitted attempt alone never constitutes acceptance. A reused operation
    /// with another digest is a binding error, even before any effect exists.
    pub fn accepted_effect(&self, operation: Uuid, digest: &str) -> io::Result<Option<&Effect>> {
        if operation.is_nil() || !valid_digest(digest) || self.poisoned {
            return Err(invalid());
        }
        if let Some(original) = self.records.iter().find(|a| a.operation == operation)
            && original.request_sha256 != digest
        {
            return Err(invalid());
        }
        Ok(self
            .effects
            .iter()
            .find(|effect| effect.operation == operation))
    }
}

fn validate_effect(
    effect: &Effect,
    attempts: &[Attempt],
    effects: &[Effect],
    world: &BTreeMap<u32, VmState>,
) -> io::Result<()> {
    let attempt = attempts
        .iter()
        .find(|a| a.sequence == effect.attempt_sequence)
        .ok_or_else(invalid)?;
    if effect.effect_version != 1
        || effect.sequence != effects.len() as u64 + 1
        || attempt.duplicate
        || effect.operation != attempt.operation
        || effect.request_sha256 != attempt.request_sha256
        || effects.iter().any(|e| e.operation == effect.operation)
        || effect.vmid == 0
        || effect.after.disk_bytes == 0
        || effect.before.as_ref() != world.get(&effect.vmid)
    {
        return Err(invalid());
    }
    Ok(())
}

fn valid_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

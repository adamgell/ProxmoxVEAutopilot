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

/// Version-two fixture identity. Retry protection is per operation and stage;
/// the remaining fields bind recovery to the original dispatch authority.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FixtureLedgerStage {
    Clone,
    DiskCapacity,
    ConfigurePe,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StageBinding {
    pub stage: FixtureLedgerStage,
    pub attempt: Uuid,
    pub generation: Uuid,
    pub owner: Uuid,
}

impl StageBinding {
    fn valid(self) -> bool {
        !self.attempt.is_nil() && !self.generation.is_nil() && !self.owner.is_nil()
    }
}

#[derive(Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Attempt {
    version: u8,
    sequence: u64,
    operation: Uuid,
    request_sha256: String,
    duplicate: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    stage_binding: Option<StageBinding>,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    receipt: Option<Vec<u8>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    stage_binding: Option<StageBinding>,
}

impl Effect {
    #[allow(dead_code)] // Also compiled directly by legacy ledger-only integration tests.
    pub fn receipt(&self) -> Option<&[u8]> {
        self.receipt.as_deref()
    }
    pub(super) fn matches(&self, operation: Uuid, digest: &str) -> bool {
        self.effect_version == 1
            && self.sequence != 0
            && self.attempt_sequence != 0
            && self.vmid != 0
            && self.operation == operation
            && self.request_sha256 == digest
    }
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
    attempted: BTreeSet<(Uuid, Option<FixtureLedgerStage>)>,
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
                    let key = (record.operation, record.stage_binding.map(|b| b.stage));
                    if !valid_version(record.version, record.stage_binding)
                        || record.sequence != records.len() as u64 + 1
                        || record.operation.is_nil()
                        || !valid_digest(&record.request_sha256)
                        || record.duplicate != attempted.contains(&key)
                        || ambiguous_legacy(&records, record.operation, record.stage_binding)
                    {
                        return Err(invalid());
                    }
                    attempted.insert(key);
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
        self.record_bound_attempt(operation, digest, None)
    }

    /// Additive v2 contract; daemon stage transport remains separately gated.
    #[allow(dead_code)]
    pub fn record_stage_attempt(
        &mut self,
        operation: Uuid,
        digest: &str,
        binding: StageBinding,
    ) -> io::Result<bool> {
        self.record_bound_attempt(operation, digest, Some(binding))
    }

    fn record_bound_attempt(
        &mut self,
        operation: Uuid,
        digest: &str,
        stage_binding: Option<StageBinding>,
    ) -> io::Result<bool> {
        if self.poisoned {
            return Err(io::Error::other("fixture log requires recovery"));
        }
        if operation.is_nil()
            || !valid_digest(digest)
            || stage_binding.is_some_and(|b| !b.valid())
            || ambiguous_legacy(&self.records, operation, stage_binding)
        {
            return Err(invalid());
        }
        let key = (operation, stage_binding.map(|b| b.stage));
        let duplicate = self.attempted.contains(&key);
        let record = Attempt {
            version: if stage_binding.is_some() { 2 } else { 1 },
            sequence: u64::try_from(self.records.len())
                .ok()
                .and_then(|n| n.checked_add(1))
                .ok_or_else(invalid)?,
            operation,
            request_sha256: digest.into(),
            duplicate,
            stage_binding,
        };
        let bytes = frame(&record)?;
        self.poisoned = true;
        self.file.write_all(&bytes)?;
        // sync_data is Rust's fdatasync-equivalent; no success is returned before it.
        self.file.sync_data()?;
        self.attempted.insert(key);
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
        self.record_effect_receipt(attempt_sequence, vmid, before, after, None)
    }

    pub fn record_effect_receipt(
        &mut self,
        attempt_sequence: u64,
        vmid: u32,
        before: Option<VmState>,
        after: VmState,
        receipt: Option<Vec<u8>>,
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
            effect_version: attempt.version,
            sequence: self.effects.len() as u64 + 1,
            attempt_sequence,
            operation: attempt.operation,
            request_sha256: attempt.request_sha256.clone(),
            vmid,
            before,
            after,
            receipt,
            stage_binding: attempt.stage_binding,
        };
        validate_effect(&effect, &self.records, &self.effects, &self.world)?;
        let bytes = frame(&effect)?;
        if bytes.len() > MAX_LINE {
            return Err(invalid());
        }
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
        self.accepted_bound_effect(operation, digest, None)
    }

    #[allow(dead_code)]
    pub fn accepted_stage_effect(
        &self,
        operation: Uuid,
        digest: &str,
        binding: StageBinding,
    ) -> io::Result<Option<&Effect>> {
        self.accepted_bound_effect(operation, digest, Some(binding))
    }

    fn accepted_bound_effect(
        &self,
        operation: Uuid,
        digest: &str,
        binding: Option<StageBinding>,
    ) -> io::Result<Option<&Effect>> {
        if operation.is_nil()
            || !valid_digest(digest)
            || self.poisoned
            || binding.is_some_and(|b| !b.valid())
            || ambiguous_legacy(&self.records, operation, binding)
        {
            return Err(invalid());
        }
        let stage = binding.map(|b| b.stage);
        if let Some(original) = self
            .records
            .iter()
            .find(|a| a.operation == operation && a.stage_binding.map(|b| b.stage) == stage)
            && (original.request_sha256 != digest || original.stage_binding != binding)
        {
            return Err(invalid());
        }
        Ok(self
            .effects
            .iter()
            .find(|effect| effect.operation == operation && effect.stage_binding == binding))
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
    if !valid_version(effect.effect_version, effect.stage_binding)
        || effect.effect_version != attempt.version
        || effect.stage_binding != attempt.stage_binding
        || effect.sequence != effects.len() as u64 + 1
        || attempt.duplicate
        || effect.operation != attempt.operation
        || effect.request_sha256 != attempt.request_sha256
        || effects.iter().any(|e| {
            e.operation == effect.operation
                && e.stage_binding.map(|b| b.stage) == effect.stage_binding.map(|b| b.stage)
        })
        || effect.vmid == 0
        || effect.after.disk_bytes == 0
        || effect.before.as_ref() != world.get(&effect.vmid)
    {
        return Err(invalid());
    }
    Ok(())
}

fn valid_version(version: u8, binding: Option<StageBinding>) -> bool {
    match (version, binding) {
        (1, None) => true,
        (2, Some(binding)) => binding.valid(),
        _ => false,
    }
}

// An unscoped legacy attempt cannot safely be assigned a stage after the fact.
fn ambiguous_legacy(records: &[Attempt], operation: Uuid, binding: Option<StageBinding>) -> bool {
    records
        .iter()
        .any(|a| a.operation == operation && a.stage_binding.is_some() != binding.is_some())
}

fn valid_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

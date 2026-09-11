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

#[cfg(test)]
mod power_tests {
    use super::*;

    #[test]
    fn power_replay_requires_exact_effect_and_fresh_observation() {
        let path = std::env::temp_dir().join(format!("power-ledger-{}", Uuid::now_v7()));
        let mut log = FixtureLog::create(&path).unwrap();
        let operation = Uuid::now_v7();
        let digest = "a".repeat(64);
        let binding = StageBinding {
            stage: FixtureLedgerStage::ConfigurePe,
            attempt: Uuid::now_v7(),
            generation: Uuid::now_v7(),
            owner: Uuid::now_v7(),
        };
        log.record_stage_attempt(operation, &digest, binding)
            .unwrap();
        log.record_effect_receipt(
            1,
            101,
            None,
            VmState {
                disk_bytes: 120,
                pe_configured: true,
            },
            Some(b"exact-receipt".to_vec()),
        )
        .unwrap();
        let generation = Uuid::now_v7();
        assert!(
            log.record_power_observation(
                operation,
                &digest,
                binding,
                b"wrong",
                PowerStateV1::Stopped,
                generation,
                10,
                11
            )
            .is_err()
        );
        assert!(
            log.record_power_observation(
                operation,
                &digest,
                binding,
                b"exact-receipt",
                PowerStateV1::Stopped,
                generation,
                10,
                9
            )
            .is_err()
        );
        assert!(log.power_records().is_empty());
        log.record_power_observation(
            operation,
            &digest,
            binding,
            b"exact-receipt",
            PowerStateV1::Stopped,
            generation,
            10,
            11,
        )
        .unwrap();
        drop(log);
        let mut log = FixtureLog::recover(&path).unwrap();
        assert_eq!(log.power_records().len(), 1);
        assert_eq!(log.power_records()[0].state, PowerStateV1::Stopped);
        let before = std::fs::read(&path).unwrap();
        assert!(
            log.record_power_observation(
                operation,
                &digest,
                binding,
                b"exact-receipt",
                PowerStateV1::Stopped,
                generation,
                10,
                12
            )
            .is_err()
        );
        let mut wrong = binding;
        wrong.owner = Uuid::now_v7();
        assert!(
            log.record_power_observation(
                operation,
                &digest,
                wrong,
                b"exact-receipt",
                PowerStateV1::Running,
                generation,
                10,
                12
            )
            .is_err()
        );
        assert_eq!(before, std::fs::read(&path).unwrap());
        let start_operation = Uuid::now_v7();
        let start_binding = StageBinding {
            stage: FixtureLedgerStage::StartPe,
            ..binding
        };
        log.record_stage_attempt(start_operation, &digest, start_binding)
            .unwrap();
        let state = log.world()[&101].clone();
        log.record_effect_receipt(
            2,
            101,
            Some(state.clone()),
            state,
            Some(b"start-receipt".to_vec()),
        )
        .unwrap();
        // The ledger test supplies an accepted synthetic effect; the IPC StartPe
        // execution path remains closed and cannot produce this effect yet.
        assert!(
            log.record_power_observation(
                start_operation,
                &digest,
                start_binding,
                b"start-receipt",
                PowerStateV1::Running,
                generation,
                11,
                12
            )
            .is_err()
        );
        log.record_power_observation(
            start_operation,
            &digest,
            start_binding,
            b"start-receipt",
            PowerStateV1::Running,
            generation,
            12,
            13,
        )
        .unwrap();
        drop(log);
        let mut log = FixtureLog::recover(&path).unwrap();
        assert_eq!(log.power_records().len(), 2);
        assert_eq!(log.power_records()[1].state, PowerStateV1::Running);
        assert_eq!(log.power_records()[1].predecessor, Some(1));
        assert!(
            log.record_power_observation(
                start_operation,
                &digest,
                start_binding,
                b"start-receipt",
                PowerStateV1::Running,
                Uuid::now_v7(),
                12,
                14
            )
            .is_err()
        );
        std::fs::remove_file(path).unwrap();
    }
}

/// Version-two fixture identity. Retry protection is per operation and stage;
/// the remaining fields bind recovery to the original dispatch authority.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FixtureLedgerStage {
    Clone,
    DiskCapacity,
    ConfigurePe,
    StartPe,
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
    #[allow(dead_code)]
    pub(super) fn has_after(&self, vmid: u32, state: &VmState) -> bool {
        self.vmid == vmid && &self.after == state
    }
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
    Power(PowerObservationV1),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PowerStateV1 {
    Stopped,
    Running,
}

/// Additive record: legacy VM snapshots never imply a known power state.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PowerObservationV1 {
    power_version: u8,
    sequence: u64,
    effect_sequence: u64,
    predecessor: Option<u64>,
    operation: Uuid,
    request_sha256: String,
    binding: StageBinding,
    receipt_sha256: String,
    vmid: u32,
    pub state: PowerStateV1,
    daemon_generation: Uuid,
    accepted_unix_ms: u64,
    observed_unix_ms: u64,
}

pub struct FixtureLog {
    file: File,
    records: Vec<Attempt>,
    attempted: BTreeSet<(Uuid, Option<FixtureLedgerStage>)>,
    effects: Vec<Effect>,
    world: BTreeMap<u32, VmState>,
    power: Vec<PowerObservationV1>,
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
            power: Vec::new(),
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
        let mut power = Vec::new();
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
                Record::Power(observation) => {
                    validate_power(&observation, &effects, &power)?;
                    power.push(observation);
                }
            }
        }
        Ok(Self {
            file,
            records,
            attempted,
            effects,
            world,
            power,
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

    #[allow(dead_code)]
    pub fn power_records(&self) -> &[PowerObservationV1] {
        &self.power
    }

    /// Only the supervisor's validated post-effect observation may call this.
    /// This records evidence, never authorizes or performs a power mutation.
    #[allow(dead_code, clippy::too_many_arguments)]
    pub fn record_power_observation(
        &mut self,
        operation: Uuid,
        digest: &str,
        binding: StageBinding,
        receipt: &[u8],
        state: PowerStateV1,
        daemon_generation: Uuid,
        accepted_unix_ms: u64,
        observed_unix_ms: u64,
    ) -> io::Result<()> {
        let effect = self
            .accepted_stage_effect(operation, digest, binding)?
            .ok_or_else(invalid)?;
        if effect.receipt() != Some(receipt) {
            return Err(invalid());
        }
        let observation = PowerObservationV1 {
            power_version: 1,
            sequence: self.power.len() as u64 + 1,
            effect_sequence: effect.sequence,
            predecessor: self
                .power
                .iter()
                .rev()
                .find(|p| p.vmid == effect.vmid)
                .map(|p| p.sequence),
            operation,
            request_sha256: digest.to_owned(),
            binding,
            receipt_sha256: format!("{:x}", Sha256::digest(receipt)),
            vmid: effect.vmid,
            state,
            daemon_generation,
            accepted_unix_ms,
            observed_unix_ms,
        };
        validate_power(&observation, &self.effects, &self.power)?;
        let bytes = frame(&observation)?;
        if bytes.len() > MAX_LINE {
            return Err(invalid());
        }
        self.poisoned = true;
        self.file.write_all(&bytes)?;
        self.file.sync_data()?;
        self.power.push(observation);
        self.poisoned = false;
        Ok(())
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

fn validate_power(
    p: &PowerObservationV1,
    effects: &[Effect],
    power: &[PowerObservationV1],
) -> io::Result<()> {
    let effect = effects
        .iter()
        .find(|e| e.sequence == p.effect_sequence)
        .ok_or_else(invalid)?;
    let previous = power.iter().rev().find(|prior| prior.vmid == p.vmid);
    if p.power_version != 1
        || p.sequence != power.len() as u64 + 1
        || p.daemon_generation.is_nil()
        || p.accepted_unix_ms == 0
        || p.observed_unix_ms < p.accepted_unix_ms
        || effect.operation != p.operation
        || effect.request_sha256 != p.request_sha256
        || effect.stage_binding != Some(p.binding)
        || effect.vmid != p.vmid
        || p.receipt_sha256
            != format!(
                "{:x}",
                Sha256::digest(effect.receipt().ok_or_else(invalid)?)
            )
        || effects.iter().rev().find(|e| e.vmid == p.vmid) != Some(effect)
        || power
            .iter()
            .any(|prior| prior.effect_sequence == p.effect_sequence)
        || p.predecessor != previous.map(|prior| prior.sequence)
    {
        return Err(invalid());
    }
    match (p.binding.stage, p.state, previous) {
        (FixtureLedgerStage::ConfigurePe, PowerStateV1::Stopped, None)
            if effect.after.pe_configured =>
        {
            Ok(())
        }
        (FixtureLedgerStage::StartPe, PowerStateV1::Running, Some(prior))
            if prior.state == PowerStateV1::Stopped
                && prior.binding.stage == FixtureLedgerStage::ConfigurePe
                && prior.effect_sequence + 1 == effect.sequence
                && prior.observed_unix_ms < p.accepted_unix_ms =>
        {
            Ok(())
        }
        _ => Err(invalid()),
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

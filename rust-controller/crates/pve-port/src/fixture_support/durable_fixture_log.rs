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
    fn stop_admission_requires_completed_start_and_preserves_clocks_on_replay() {
        let path = std::env::temp_dir().join(format!("stop-admission-{}", Uuid::now_v7()));
        let mut log = FixtureLog::create(&path).unwrap();
        let operation = Uuid::now_v7();
        let digest = "a".repeat(64);
        let generation = Uuid::now_v7();
        let configure = StageBinding {
            stage: FixtureLedgerStage::ConfigurePe,
            attempt: Uuid::now_v7(),
            generation: Uuid::now_v7(),
            owner: Uuid::now_v7(),
        };
        log.record_stage_attempt(operation, &digest, configure)
            .unwrap();
        log.record_effect_receipt(
            1,
            101,
            None,
            VmState {
                disk_bytes: 120,
                pe_configured: true,
            },
            Some(b"configure".to_vec()),
        )
        .unwrap();
        log.record_power_observation(
            operation,
            &digest,
            configure,
            b"configure",
            PowerStateV1::Stopped,
            generation,
            10,
            11,
        )
        .unwrap();
        let start = StageBinding {
            stage: FixtureLedgerStage::StartPe,
            ..configure
        };
        let start_operation = Uuid::now_v7();
        let receipt = br#"{"receipt":{"task":"fixture-start-task"}}"#;
        let prior = log.power_records()[0].clone();
        log.record_start_transition(
            start_operation,
            &digest,
            start,
            &prior,
            receipt.to_vec(),
            || Ok(()),
        )
        .unwrap();
        let stop = StageBinding {
            stage: FixtureLedgerStage::PeEnsureStopped,
            attempt: Uuid::now_v7(),
            generation: Uuid::now_v7(),
            owner: Uuid::now_v7(),
        };
        let stop_operation = Uuid::now_v7();
        let authority = FixtureStopAuthorityV1 {
            version: 1,
            grace_operation: Uuid::now_v7(),
            decision_event: Uuid::now_v7(),
            evidence_fence: 2,
            grace_due_unix_ms: 12,
            decision_unix_ms: 13,
            lease_checked_unix_ms: 14,
            lease_expires_unix_ms: 100,
            original_deadline_unix_ms: 200,
        };
        let admit = |log: &mut FixtureLog, binding, authority, generation, at| {
            log.admit_stop(
                stop_operation,
                digest.clone(),
                binding,
                authority,
                start_operation,
                &digest,
                start,
                receipt,
                generation,
                at,
            )
        };
        let before = std::fs::read(&path).unwrap();
        assert!(
            admit(&mut log, stop, authority.clone(), generation, 16).is_err(),
            "receipt alone cannot prove completed StartPe"
        );
        assert_eq!(before, std::fs::read(&path).unwrap());
        log.record_start_observation(
            start_operation,
            &digest,
            start,
            generation,
            12,
            15,
            "fixture-start-task".into(),
            15,
            15,
        )
        .unwrap();
        let before = std::fs::read(&path).unwrap();
        for altered in [
            FixtureStopAuthorityV1 {
                grace_due_unix_ms: 14,
                ..authority.clone()
            },
            FixtureStopAuthorityV1 {
                lease_expires_unix_ms: 16,
                ..authority.clone()
            },
            FixtureStopAuthorityV1 {
                original_deadline_unix_ms: 16,
                ..authority.clone()
            },
            FixtureStopAuthorityV1 {
                lease_checked_unix_ms: 16,
                ..authority.clone()
            },
            FixtureStopAuthorityV1 {
                evidence_fence: 0,
                ..authority.clone()
            },
        ] {
            assert!(admit(&mut log, stop, altered, generation, 16).is_err());
            assert_eq!(before, std::fs::read(&path).unwrap());
        }
        assert!(admit(&mut log, stop, authority.clone(), Uuid::now_v7(), 16).is_err());
        assert!(admit(&mut log, stop, authority.clone(), generation, 5016).is_err());
        admit(&mut log, stop, authority.clone(), generation, 16).unwrap();
        let accepted = std::fs::read(&path).unwrap();
        assert_eq!(log.records().len(), 2);
        assert_eq!(log.effects().len(), 2);
        assert_eq!(
            log.power_records().last().unwrap().state,
            PowerStateV1::Running
        );
        admit(&mut log, stop, authority.clone(), generation, 17).unwrap();
        assert_eq!(accepted, std::fs::read(&path).unwrap());
        assert!(
            admit(
                &mut log,
                StageBinding {
                    owner: Uuid::now_v7(),
                    ..stop
                },
                authority.clone(),
                generation,
                17
            )
            .is_err()
        );
        assert!(
            admit(
                &mut log,
                StageBinding {
                    generation: Uuid::now_v7(),
                    ..stop
                },
                authority.clone(),
                generation,
                17
            )
            .is_err()
        );
        drop(log);
        let mut log = FixtureLog::recover(&path).unwrap();
        assert_eq!(log.stop_admissions.len(), 1);
        assert_eq!(log.stop_admissions[0].admitted_unix_ms, 16);
        admit(&mut log, stop, authority, generation, 18).unwrap();
        assert_eq!(accepted, std::fs::read(&path).unwrap());
        let torn = path.with_extension("torn");
        std::fs::write(&torn, &accepted[..accepted.len() - 1]).unwrap();
        assert!(FixtureLog::recover(&torn).is_err());
        let mut forged = log.stop_admissions[0].clone();
        forged.authority.grace_due_unix_ms = 99;
        std::fs::write(&torn, [before, frame(&forged).unwrap()].concat()).unwrap();
        assert!(FixtureLog::recover(&torn).is_err());
        std::fs::remove_file(torn).unwrap();
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn atomic_start_admission_survives_replay_without_consuming_duplicate_authority() {
        let path = std::env::temp_dir().join(format!("atomic-start-{}", Uuid::now_v7()));
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
            Some(b"configure-receipt".to_vec()),
        )
        .unwrap();
        log.record_power_observation(
            operation,
            &digest,
            binding,
            b"configure-receipt",
            PowerStateV1::Stopped,
            Uuid::now_v7(),
            10,
            11,
        )
        .unwrap();
        let prior = log.power_records()[0].clone();
        let start = StageBinding {
            stage: FixtureLedgerStage::StartPe,
            ..binding
        };
        let start_operation = Uuid::now_v7();
        let before = std::fs::read(&path).unwrap();
        let consumed = std::cell::Cell::new(0);
        let consume = || {
            consumed.set(consumed.get() + 1);
            Ok(())
        };
        let mut wrong = prior.clone();
        wrong.binding.owner = Uuid::now_v7();
        assert!(
            log.record_start_transition(
                start_operation,
                &digest,
                start,
                &wrong,
                b"start-receipt".to_vec(),
                consume
            )
            .is_err()
        );
        assert_eq!(consumed.get(), 0);
        assert!(
            log.record_start_transition(
                start_operation,
                &digest,
                start,
                &prior,
                b"start-receipt".to_vec(),
                || Err(invalid())
            )
            .is_err()
        );
        assert_eq!(before, std::fs::read(&path).unwrap());
        log.record_start_transition(
            start_operation,
            &digest,
            start,
            &prior,
            b"start-receipt".to_vec(),
            consume,
        )
        .unwrap();
        assert_eq!(consumed.get(), 1);
        assert_eq!(log.records().len(), 2);
        assert_eq!(log.effects().len(), 2);
        assert_eq!(
            log.power_records().len(),
            1,
            "acceptance cannot invent an observation"
        );
        let bytes = std::fs::read(&path).unwrap();
        assert_eq!(
            log.effects()[1].start_transition.as_ref().unwrap().after,
            PowerStateV1::Running
        );
        assert_eq!(
            bytes[before.len()..]
                .iter()
                .filter(|byte| **byte == b'\n')
                .count(),
            1
        );
        drop(log);
        let mut log = FixtureLog::recover(&path).unwrap();
        assert_eq!(
            log.accepted_stage_effect(start_operation, &digest, start)
                .unwrap()
                .unwrap()
                .receipt(),
            Some(b"start-receipt".as_slice())
        );
        assert!(
            log.record_start_transition(
                start_operation,
                &digest,
                start,
                &prior,
                b"start-receipt".to_vec(),
                consume
            )
            .is_err()
        );
        assert_eq!(consumed.get(), 1);
        assert_eq!(bytes, std::fs::read(&path).unwrap());
        log.record_power_observation(
            start_operation,
            &digest,
            start,
            b"start-receipt",
            PowerStateV1::Running,
            Uuid::now_v7(),
            12,
            13,
        )
        .unwrap();
        drop(log);
        assert_eq!(FixtureLog::recover(&path).unwrap().power_records().len(), 2);
        // Simulated process death in the single atomic frame cannot expose a
        // recoverable partial receipt/effect. The intact predecessor remains.
        let torn = path.with_extension("torn");
        std::fs::write(&torn, &bytes[..bytes.len() - 1]).unwrap();
        assert!(FixtureLog::recover(&torn).is_err());
        std::fs::write(&torn, &bytes[..before.len()]).unwrap();
        let pre_admission = FixtureLog::recover(&torn).unwrap();
        assert_eq!(pre_admission.records().len(), 1);
        assert!(
            pre_admission
                .accepted_stage_effect(start_operation, &digest, start)
                .unwrap()
                .is_none()
        );
        drop(pre_admission);
        // A checksum-valid substitution must fail semantic replay validation.
        let mut forged: StartAdmissionV1 =
            serde_json::from_slice(&bytes[before.len() + 9..bytes.len() - 66]).unwrap();
        forged
            .effect
            .start_transition
            .as_mut()
            .unwrap()
            .predecessor
            .binding
            .owner = Uuid::now_v7();
        let forged_bytes = [before.as_slice(), frame(&forged).unwrap().as_slice()].concat();
        std::fs::write(&torn, forged_bytes).unwrap();
        assert!(FixtureLog::recover(&torn).is_err());
        std::fs::remove_file(torn).unwrap();
        std::fs::remove_file(path).unwrap();
    }

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
        for at in [11, 5011] {
            assert!(
                log.current_stopped_power(
                    operation,
                    &digest,
                    binding,
                    b"exact-receipt",
                    generation,
                    at
                )
                .is_ok()
            );
            assert!(
                log.revalidate_stopped_token(&log.power_records()[0], generation, at)
                    .is_ok()
            );
        }
        for at in [10, 5012] {
            assert!(
                log.current_stopped_power(
                    operation,
                    &digest,
                    binding,
                    b"exact-receipt",
                    generation,
                    at
                )
                .is_err()
            );
            assert!(
                log.revalidate_stopped_token(&log.power_records()[0], generation, at)
                    .is_err()
            );
        }
        assert!(
            log.current_stopped_power(
                operation,
                &digest,
                binding,
                b"exact-receipt",
                Uuid::now_v7(),
                12
            )
            .is_err()
        );
        let mut forged_token = log.power_records()[0].clone();
        forged_token.observed_unix_ms += 1;
        assert!(
            log.revalidate_stopped_token(&forged_token, generation, 12)
                .is_err()
        );
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
        assert!(
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
            .is_err()
        );
        drop(log);
        let mut log = FixtureLog::recover(&path).unwrap();
        assert_eq!(
            log.power_records().len(),
            1,
            "a non-atomic effect cannot establish a running transition"
        );
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
    /// Message identity only. Stop admission and power observations are gated.
    PeEnsureStopped,
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

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    start_transition: Option<StartTransitionV1>,
}

/// Accepted synthetic state transition, deliberately not a collected observation.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct StartTransitionV1 {
    version: u8,
    predecessor: PowerObservationV1,
    before: PowerStateV1,
    after: PowerStateV1,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct StartAdmissionV1 {
    admission_version: u8,
    attempt: Attempt,
    effect: Effect,
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
    Start(StartAdmissionV1),
    StartObservation(StartObservationV1),
    StopAdmission(StopAdmissionV1),
}

/// Supervisor assertion of the scheduler's guarded grace decision and current
/// lease. This fixture record is bookkeeping, never a stop receipt or power fact.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureStopAuthorityV1 {
    pub version: u8,
    pub grace_operation: Uuid,
    pub decision_event: Uuid,
    pub evidence_fence: u64,
    pub grace_due_unix_ms: u64,
    pub decision_unix_ms: u64,
    pub lease_checked_unix_ms: u64,
    pub lease_expires_unix_ms: u64,
    pub original_deadline_unix_ms: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct StopAdmissionV1 {
    stop_admission_version: u8,
    operation: Uuid,
    request_sha256: String,
    binding: StageBinding,
    authority: FixtureStopAuthorityV1,
    predecessor: StartObservationV1,
    admitted_unix_ms: u64,
}

fn validate_stop_admission(
    admission: &StopAdmissionV1,
    effects: &[Effect],
    power: &[PowerObservationV1],
    starts: &[StartObservationV1],
) -> io::Result<()> {
    let a = &admission.authority;
    let p = &admission.predecessor.power;
    let at = admission.admitted_unix_ms;
    if admission.stop_admission_version != 1
        || admission.operation.is_nil()
        || !valid_digest(&admission.request_sha256)
        || !admission.binding.valid()
        || admission.binding.stage != FixtureLedgerStage::PeEnsureStopped
        || a.version != 1
        || a.grace_operation.is_nil()
        || a.decision_event.is_nil()
        || a.evidence_fence == 0
        || a.grace_due_unix_ms == 0
        || a.grace_due_unix_ms > a.decision_unix_ms
        || a.decision_unix_ms > a.lease_checked_unix_ms
        || a.lease_checked_unix_ms > at
        || at - a.lease_checked_unix_ms > 5000
        || at >= a.lease_expires_unix_ms
        || at >= a.original_deadline_unix_ms
        || p.state != PowerStateV1::Running
        || p.observed_unix_ms < a.lease_checked_unix_ms
        || at < admission.predecessor.published_unix_ms
        || at < p.observed_unix_ms
        || at - p.observed_unix_ms > 5000
        || !starts.contains(&admission.predecessor)
        || power.iter().rev().find(|entry| entry.vmid == p.vmid) != Some(p)
        || effects
            .iter()
            .rev()
            .find(|entry| entry.vmid == p.vmid)
            .is_none_or(|effect| effect.sequence != p.effect_sequence)
    {
        return Err(invalid());
    }
    Ok(())
}

/// Atomic task-success and running-power observation, not task acceptance.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StartObservationV1 {
    observation_version: u8,
    pub power: PowerObservationV1,
    pub task_upid: String,
    pub task_observed_unix_ms: u64,
    pub published_unix_ms: u64,
}

impl StartObservationV1 {
    #[allow(dead_code)]
    pub fn validate_restoration(
        &self,
        operation: Uuid,
        digest: &str,
        binding: StageBinding,
        receipt: &[u8],
        vmid: u32,
    ) -> io::Result<()> {
        let receipt_value: serde_json::Value =
            serde_json::from_slice(receipt).map_err(|_| invalid())?;
        let p = &self.power;
        if self.observation_version != 1
            || p.power_version != 1
            || p.operation != operation
            || p.vmid != vmid
            || p.request_sha256 != digest
            || p.binding != binding
            || !binding.valid()
            || binding.stage != FixtureLedgerStage::StartPe
            || p.state != PowerStateV1::Running
            || p.daemon_generation.is_nil()
            || p.effect_sequence == 0
            || p.predecessor
                .is_none_or(|prior| prior == 0 || prior >= p.sequence)
            || p.receipt_sha256 != format!("{:x}", Sha256::digest(receipt))
            || receipt_value
                .pointer("/receipt/task")
                .and_then(serde_json::Value::as_str)
                != Some(self.task_upid.as_str())
            || p.accepted_unix_ms == 0
            || p.observed_unix_ms <= p.accepted_unix_ms
            || self.task_observed_unix_ms <= p.accepted_unix_ms
            || p.observed_unix_ms > self.published_unix_ms
            || self.task_observed_unix_ms > self.published_unix_ms
        {
            return Err(invalid());
        }
        Ok(())
    }

    #[allow(dead_code)]
    pub(super) fn observation_clocks(&self) -> (Uuid, u64, u64) {
        (
            self.power.daemon_generation,
            self.power.accepted_unix_ms,
            self.power.observed_unix_ms,
        )
    }
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
    start_observations: Vec<StartObservationV1>,
    stop_admissions: Vec<StopAdmissionV1>,
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
    /// Persist admission without recording an attempt, effect, receipt or stopped
    /// state. Replays retain original clocks and cannot renew release authority.
    #[allow(dead_code, clippy::too_many_arguments)]
    pub(super) fn admit_stop(
        &mut self,
        operation: Uuid,
        digest: String,
        binding: StageBinding,
        authority: FixtureStopAuthorityV1,
        start_operation: Uuid,
        start_digest: &str,
        start_binding: StageBinding,
        start_receipt: &[u8],
        daemon_generation: Uuid,
        at: u64,
    ) -> io::Result<()> {
        if self.poisoned || daemon_generation.is_nil() {
            return Err(invalid());
        }
        let effect = self
            .accepted_stage_effect(start_operation, start_digest, start_binding)?
            .ok_or_else(invalid)?;
        if effect.receipt() != Some(start_receipt) {
            return Err(invalid());
        }
        let predecessor = self
            .start_observation(start_operation, start_digest, start_binding)?
            .ok_or_else(invalid)?
            .clone();
        if predecessor.power.daemon_generation != daemon_generation {
            return Err(invalid());
        }
        let admission = StopAdmissionV1 {
            stop_admission_version: 1,
            operation,
            request_sha256: digest,
            binding,
            authority,
            predecessor,
            admitted_unix_ms: at,
        };
        validate_stop_admission(
            &admission,
            &self.effects,
            &self.power,
            &self.start_observations,
        )?;
        if let Some(prior) = self
            .stop_admissions
            .iter()
            .find(|prior| prior.operation == operation)
        {
            let mut replay = admission;
            replay.admitted_unix_ms = prior.admitted_unix_ms;
            return if prior == &replay {
                Ok(())
            } else {
                Err(invalid())
            };
        }
        let bytes = frame(&admission)?;
        if bytes.len() > MAX_LINE {
            return Err(invalid());
        }
        self.poisoned = true;
        self.file.write_all(&bytes)?;
        self.file.sync_data()?;
        self.stop_admissions.push(admission);
        self.poisoned = false;
        Ok(())
    }

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
            start_observations: Vec::new(),
            stop_admissions: Vec::new(),
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
        let mut start_observations = Vec::new();
        let mut stop_admissions: Vec<StopAdmissionV1> = Vec::new();
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
                    if effect.start_transition.is_some() {
                        return Err(invalid());
                    }
                    validate_effect(&effect, &records, &effects, &world)?;
                    world.insert(effect.vmid, effect.after.clone());
                    effects.push(effect);
                }
                Record::Power(observation) => {
                    validate_power(&observation, &effects, &power)?;
                    power.push(observation);
                }
                Record::Start(admission) => {
                    validate_start(&admission, &records, &effects, &world, &power)?;
                    attempted.insert((
                        admission.attempt.operation,
                        Some(FixtureLedgerStage::StartPe),
                    ));
                    records.push(admission.attempt);
                    world.insert(admission.effect.vmid, admission.effect.after.clone());
                    effects.push(admission.effect);
                }
                Record::StartObservation(observation) => {
                    validate_start_observation(&observation, &effects, &power)?;
                    power.push(observation.power.clone());
                    start_observations.push(observation);
                }
                Record::StopAdmission(admission) => {
                    validate_stop_admission(&admission, &effects, &power, &start_observations)?;
                    if stop_admissions
                        .iter()
                        .any(|prior| prior.operation == admission.operation)
                    {
                        return Err(invalid());
                    }
                    stop_admissions.push(admission);
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
            start_observations,
            stop_admissions,
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
            start_transition: None,
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

    /// Private fixture seam. Admission, exact receipt and synthetic transition
    /// share one durable frame. Callers must validate the typed StartPe request
    /// and fresh current-generation authority before invoking this method.
    #[allow(dead_code, clippy::too_many_arguments)]
    pub fn record_start_transition(
        &mut self,
        operation: Uuid,
        digest: &str,
        binding: StageBinding,
        predecessor: &PowerObservationV1,
        receipt: Vec<u8>,
        consume: impl FnOnce() -> io::Result<()>,
    ) -> io::Result<()> {
        if self.poisoned {
            return Err(invalid());
        }
        let before = self
            .world
            .get(&predecessor.vmid)
            .cloned()
            .ok_or_else(invalid)?;
        let attempt = Attempt {
            version: 2,
            sequence: self.records.len() as u64 + 1,
            operation,
            request_sha256: digest.to_owned(),
            duplicate: false,
            stage_binding: Some(binding),
        };
        let effect = Effect {
            effect_version: 2,
            sequence: self.effects.len() as u64 + 1,
            attempt_sequence: attempt.sequence,
            operation,
            request_sha256: digest.to_owned(),
            vmid: predecessor.vmid,
            before: Some(before.clone()),
            after: before,
            receipt: Some(receipt),
            stage_binding: Some(binding),
            start_transition: Some(StartTransitionV1 {
                version: 1,
                predecessor: predecessor.clone(),
                before: PowerStateV1::Stopped,
                after: PowerStateV1::Running,
            }),
        };
        let admission = StartAdmissionV1 {
            admission_version: 1,
            attempt,
            effect,
        };
        validate_start(
            &admission,
            &self.records,
            &self.effects,
            &self.world,
            &self.power,
        )?;
        let bytes = frame(&admission)?;
        if bytes.len() > MAX_LINE {
            return Err(invalid());
        }
        // Refused validations never consume authority. A failed persistence after
        // consumption is ambiguous and cannot safely be automatically retried.
        consume()?;
        self.poisoned = true;
        self.file.write_all(&bytes)?;
        self.file.sync_data()?;
        self.attempted
            .insert((operation, Some(FixtureLedgerStage::StartPe)));
        self.records.push(admission.attempt);
        self.effects.push(admission.effect);
        self.poisoned = false;
        Ok(())
    }

    #[allow(dead_code)]
    pub fn power_records(&self) -> &[PowerObservationV1] {
        &self.power
    }

    #[allow(dead_code)]
    pub fn start_observation(
        &self,
        operation: Uuid,
        digest: &str,
        binding: StageBinding,
    ) -> io::Result<Option<&StartObservationV1>> {
        let effect = self
            .accepted_stage_effect(operation, digest, binding)?
            .ok_or_else(invalid)?;
        Ok(self
            .start_observations
            .iter()
            .find(|observation| observation.power.effect_sequence == effect.sequence))
    }

    #[allow(dead_code, clippy::too_many_arguments)]
    pub fn record_start_observation(
        &mut self,
        operation: Uuid,
        digest: &str,
        binding: StageBinding,
        daemon_generation: Uuid,
        accepted_unix_ms: u64,
        power_observed_unix_ms: u64,
        task_upid: String,
        task_observed_unix_ms: u64,
        published_unix_ms: u64,
    ) -> io::Result<StartObservationV1> {
        let effect = self
            .accepted_stage_effect(operation, digest, binding)?
            .ok_or_else(invalid)?;
        let observation = StartObservationV1 {
            observation_version: 1,
            power: PowerObservationV1 {
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
                receipt_sha256: format!(
                    "{:x}",
                    Sha256::digest(effect.receipt().ok_or_else(invalid)?)
                ),
                vmid: effect.vmid,
                state: PowerStateV1::Running,
                daemon_generation,
                accepted_unix_ms,
                observed_unix_ms: power_observed_unix_ms,
            },
            task_upid,
            task_observed_unix_ms,
            published_unix_ms,
        };
        validate_start_observation(&observation, &self.effects, &self.power)?;
        let bytes = frame(&observation)?;
        if bytes.len() > MAX_LINE {
            return Err(invalid());
        }
        self.poisoned = true;
        self.file.write_all(&bytes)?;
        self.file.sync_data()?;
        self.power.push(observation.power.clone());
        self.start_observations.push(observation.clone());
        self.poisoned = false;
        Ok(observation)
    }

    /// Resolve the exact predecessor for the atomic admission seam. Historical
    /// replay alone cannot create current-generation release authority.
    #[allow(dead_code, clippy::too_many_arguments)]
    pub fn current_stopped_power(
        &self,
        operation: Uuid,
        digest: &str,
        binding: StageBinding,
        receipt: &[u8],
        daemon_generation: Uuid,
        now_unix_ms: u64,
    ) -> io::Result<PowerObservationV1> {
        let effect = self
            .accepted_stage_effect(operation, digest, binding)?
            .ok_or_else(invalid)?;
        let power = self
            .power
            .iter()
            .rev()
            .find(|p| p.vmid == effect.vmid)
            .ok_or_else(invalid)?;
        if binding.stage != FixtureLedgerStage::ConfigurePe
            || effect.receipt() != Some(receipt)
            || power.effect_sequence != effect.sequence
            || power.state != PowerStateV1::Stopped
            || power.daemon_generation != daemon_generation
            || daemon_generation.is_nil()
            || now_unix_ms < power.observed_unix_ms
            || now_unix_ms - power.observed_unix_ms > 5000
            || self.effects.iter().rev().find(|e| e.vmid == effect.vmid) != Some(effect)
            || self.world.get(&effect.vmid) != Some(&effect.after)
        {
            return Err(invalid());
        }
        Ok(power.clone())
    }

    #[allow(dead_code)]
    pub fn revalidate_stopped_token(
        &self,
        token: &PowerObservationV1,
        daemon_generation: Uuid,
        now_unix_ms: u64,
    ) -> io::Result<()> {
        let effect = self
            .accepted_stage_effect(token.operation, &token.request_sha256, token.binding)?
            .ok_or_else(invalid)?;
        let current = self.current_stopped_power(
            token.operation,
            &token.request_sha256,
            token.binding,
            effect.receipt().ok_or_else(invalid)?,
            daemon_generation,
            now_unix_ms,
        )?;
        if &current != token {
            return Err(invalid());
        }
        Ok(())
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
                && effect
                    .start_transition
                    .as_ref()
                    .is_some_and(|transition| &transition.predecessor == prior)
                && prior.binding.stage == FixtureLedgerStage::ConfigurePe
                && prior.effect_sequence + 1 == effect.sequence
                && prior.observed_unix_ms < p.accepted_unix_ms =>
        {
            Ok(())
        }
        _ => Err(invalid()),
    }
}

fn validate_start_observation(
    observation: &StartObservationV1,
    effects: &[Effect],
    power: &[PowerObservationV1],
) -> io::Result<()> {
    validate_power(&observation.power, effects, power)?;
    let effect = effects
        .iter()
        .find(|effect| effect.sequence == observation.power.effect_sequence)
        .ok_or_else(invalid)?;
    let receipt: serde_json::Value =
        serde_json::from_slice(effect.receipt().ok_or_else(invalid)?).map_err(|_| invalid())?;
    if observation.observation_version != 1
        || observation.power.state != PowerStateV1::Running
        || receipt
            .pointer("/receipt/task")
            .and_then(serde_json::Value::as_str)
            != Some(observation.task_upid.as_str())
        || observation.task_observed_unix_ms <= observation.power.accepted_unix_ms
        || observation.power.observed_unix_ms <= observation.power.accepted_unix_ms
        || observation.task_observed_unix_ms > observation.published_unix_ms
        || observation.power.observed_unix_ms > observation.published_unix_ms
    {
        return Err(invalid());
    }
    Ok(())
}

fn validate_start(
    admission: &StartAdmissionV1,
    records: &[Attempt],
    effects: &[Effect],
    world: &BTreeMap<u32, VmState>,
    power: &[PowerObservationV1],
) -> io::Result<()> {
    let attempt = &admission.attempt;
    let effect = &admission.effect;
    let transition = effect.start_transition.as_ref().ok_or_else(invalid)?;
    let prior = &transition.predecessor;
    if admission.admission_version != 1
        || attempt.version != 2
        || attempt.duplicate
        || attempt.sequence != records.len() as u64 + 1
        || attempt.operation.is_nil()
        || !valid_digest(&attempt.request_sha256)
        || !attempt
            .stage_binding
            .is_some_and(|binding| binding.valid() && binding.stage == FixtureLedgerStage::StartPe)
        || records.iter().any(|record| {
            record.operation == attempt.operation
                && record.stage_binding.map(|binding| binding.stage)
                    == Some(FixtureLedgerStage::StartPe)
        })
        || ambiguous_legacy(records, attempt.operation, attempt.stage_binding)
        || transition.version != 1
        || transition.before != PowerStateV1::Stopped
        || transition.after != PowerStateV1::Running
        || prior.state != PowerStateV1::Stopped
        || prior.binding.stage != FixtureLedgerStage::ConfigurePe
        || power.iter().rev().find(|p| p.vmid == prior.vmid) != Some(prior)
        || effects
            .iter()
            .rev()
            .find(|e| e.vmid == prior.vmid)
            .is_none_or(|e| e.sequence != prior.effect_sequence)
        || effect.vmid != prior.vmid
        || effect.before.as_ref() != Some(&effect.after)
        || !effect.after.pe_configured
        || effect.receipt.as_ref().is_none_or(Vec::is_empty)
    {
        return Err(invalid());
    }
    let mut attempts = records.to_vec();
    attempts.push(attempt.clone());
    validate_effect(effect, &attempts, effects, world)
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

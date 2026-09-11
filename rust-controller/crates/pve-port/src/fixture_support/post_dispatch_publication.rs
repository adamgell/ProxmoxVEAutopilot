//! Supervisor publications are tied to effects accepted by this daemon lifetime.
use super::{FixturePostDispatchV1, durable_fixture_log::FixtureLog};
use crate::fixture_ipc::FixtureCloneRequest;
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs,
    io::{self, Write},
    path::Path,
};
use uuid::Uuid;

fn invalid() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        "invalid post-dispatch publication",
    )
}

#[cfg(test)]
mod power_refresh_tests {
    use super::*;

    #[test]
    fn explicit_test_power_refreshes_completed_start_and_fences_restart() {
        use super::super::durable_fixture_log::{
            FixtureLedgerStage, PowerStateV1, StageBinding, VmState,
        };
        let directory =
            std::env::temp_dir().join(format!("power-source-publication-{}", Uuid::now_v7()));
        fs::create_dir(&directory).unwrap();
        let path = directory.join("fixture.log");
        let mut log = FixtureLog::create(&path).unwrap();
        let mut publications = Publications::new();
        let generation = publications.generation();
        let clock = now().unwrap();
        let digest = "a".repeat(64);
        let configure = StageBinding {
            stage: FixtureLedgerStage::ConfigurePe,
            attempt: Uuid::now_v7(),
            generation: Uuid::now_v7(),
            owner: Uuid::now_v7(),
        };
        let operation = Uuid::now_v7();
        log.record_stage_attempt(operation, &digest, configure)
            .unwrap();
        log.record_effect_receipt(
            1,
            109,
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
            clock - 20,
            clock - 19,
        )
        .unwrap();
        let start = StageBinding {
            stage: FixtureLedgerStage::StartPe,
            ..configure
        };
        let operation = Uuid::now_v7();
        let receipt = br#"{"receipt":{"task":"fixture-start-task"}}"#;
        let prior = log.power_records()[0].clone();
        log.record_start_transition(operation, &digest, start, &prior, receipt.to_vec(), || {
            Ok(())
        })
        .unwrap();
        log.record_start_observation(
            operation,
            &digest,
            start,
            generation,
            clock - 18,
            clock - 17,
            "fixture-start-task".into(),
            clock - 17,
            clock - 17,
        )
        .unwrap();
        let identity = super::super::FixtureStageIdentity {
            operation,
            stage: start.stage,
            attempt: start.attempt,
            generation: start.generation,
            owner: start.owner,
            request_sha256: digest,
        };
        let sample = super::super::test_power_source::TestPowerSample {
            version: 1,
            identity: identity.clone(),
            vmid: 109,
            daemon_generation: generation,
            observed_unix_ms: clock,
            lease_expires_unix_ms: clock + 10000,
            power: super::super::SeedPower {
                power: crate::PowerState::Running,
                locked: false,
            },
        };
        let before = fs::read(&path).unwrap();
        assert!(
            publications
                .handle(
                    Command::InstallTestPowerSource {
                        sample: sample.clone()
                    },
                    false,
                    &mut log,
                    &directory
                )
                .is_err()
        );
        publications
            .handle(
                Command::InstallTestPowerSource { sample },
                true,
                &mut log,
                &directory,
            )
            .unwrap();
        assert_eq!(
            before,
            fs::read(&path).unwrap(),
            "installing source does not publish power"
        );
        let response = publications
            .handle(
                Command::ConsumeStopCurrentPower {
                    identity: identity.clone(),
                },
                true,
                &mut log,
                &directory,
            )
            .unwrap();
        let response: serde_json::Value = serde_json::from_slice(&response).unwrap();
        assert_eq!(response["ok"], true);
        assert_eq!(response["sample"]["observed_unix_ms"], clock);
        assert_eq!(log.records().len(), 2);
        assert_eq!(log.effects().len(), 2);
        assert_eq!(
            serde_json::to_value(log.power_records().last().unwrap()).unwrap()["observed_unix_ms"],
            clock
        );
        let published = fs::read(&path).unwrap();
        // A fresh DB lease check after this sample cannot use a re-read to
        // manufacture the required post-check observation. The supervisor
        // must arrange an independent sample after obtaining authority.
        let stop = StageBinding {
            stage: FixtureLedgerStage::PeEnsureStopped,
            attempt: Uuid::now_v7(),
            generation: Uuid::now_v7(),
            owner: Uuid::now_v7(),
        };
        let authority = super::super::FixtureStopAuthorityV1 {
            version: 1,
            grace_operation: Uuid::now_v7(),
            decision_event: Uuid::now_v7(),
            evidence_fence: 1,
            grace_due_unix_ms: clock - 2,
            decision_unix_ms: clock - 1,
            lease_checked_unix_ms: clock + 1,
            lease_expires_unix_ms: clock + 10000,
            original_deadline_unix_ms: clock + 20000,
        };
        for _ in 0..2 {
            let _replay = publications.handle(
                Command::ConsumeStopCurrentPower {
                    identity: identity.clone(),
                },
                true,
                &mut log,
                &directory,
            );
            assert!(
                log.admit_stop(
                    Uuid::now_v7(),
                    "b".repeat(64),
                    stop,
                    authority.clone(),
                    operation,
                    &identity.request_sha256,
                    start,
                    receipt,
                    generation,
                    clock + 2,
                )
                .is_err(),
                "cached sample predates the newer lease check"
            );
            assert_eq!(published, fs::read(&path).unwrap());
            assert_eq!(log.records().len(), 2);
            assert_eq!(log.effects().len(), 2);
            assert_eq!(
                log.power_records().last().unwrap().state,
                PowerStateV1::Running
            );
        }
        drop(log);
        let mut log = FixtureLog::recover(&path).unwrap();
        assert!(
            Publications::new()
                .handle(
                    Command::ConsumeStopCurrentPower { identity },
                    true,
                    &mut log,
                    &directory
                )
                .is_err()
        );
        assert_eq!(published, fs::read(&path).unwrap());
        drop(log);
        for entry in fs::read_dir(&directory).unwrap() {
            fs::remove_file(entry.unwrap().path()).unwrap();
        }
        fs::remove_dir(directory).unwrap();
    }

    #[test]
    fn current_power_consumer_refuses_without_independent_source() {
        let directory = std::env::temp_dir().join(format!("power-consumer-{}", Uuid::now_v7()));
        fs::create_dir(&directory).unwrap();
        let path = directory.join("fixture.log");
        let mut log = FixtureLog::create(&path).unwrap();
        let original = fs::read(&path).unwrap();
        let identity = super::super::FixtureStageIdentity {
            operation: Uuid::now_v7(),
            stage: super::super::FixtureLedgerStage::StartPe,
            attempt: Uuid::now_v7(),
            generation: Uuid::now_v7(),
            owner: Uuid::now_v7(),
            request_sha256: "a".repeat(64),
        };
        let mut publications = Publications::new();
        for supervisor in [false, true, true] {
            let reply = publications.handle(
                Command::ConsumeStopCurrentPower {
                    identity: identity.clone(),
                },
                supervisor,
                &mut log,
                &directory,
            );
            if supervisor {
                let reply: serde_json::Value = serde_json::from_slice(&reply.unwrap()).unwrap();
                assert_eq!(reply["ok"], false);
                assert_eq!(reply["reason"], "current_power_source_unavailable");
                assert_eq!(reply["identity"], serde_json::to_value(&identity).unwrap());
            } else {
                assert_eq!(reply.unwrap_err().kind(), io::ErrorKind::InvalidData);
            }
            assert_eq!(original, fs::read(&path).unwrap());
            assert!(log.records().is_empty());
            assert!(log.effects().is_empty());
            assert!(publications.accepted.is_empty());
            assert!(publications.published.is_empty());
            assert!(publications.synchronous.is_empty());
        }
        // A caller cannot smuggle a cached or asserted observation into the probe.
        let mut wire =
            serde_json::json!({"command":"consume_stop_current_power", "identity":identity});
        wire["power"] = serde_json::json!({"power":"running", "locked":false});
        assert!(serde_json::from_value::<Command>(wire).is_err());
        drop(log);
        fs::remove_file(path).unwrap();
        fs::remove_dir(directory).unwrap();
    }

    #[test]
    fn worker_cannot_publish_running_power() {
        let directory = std::env::temp_dir().join(format!("power-publisher-{}", Uuid::now_v7()));
        fs::create_dir(&directory).unwrap();
        let path = directory.join("fixture.log");
        let mut log = FixtureLog::create(&path).unwrap();
        let original = fs::read(&path).unwrap();
        let mut publications = Publications::new();
        let command = Command::PublishStartPeRunningPower {
            identity: super::super::FixtureStageIdentity {
                operation: Uuid::now_v7(),
                stage: super::super::FixtureLedgerStage::StartPe,
                attempt: Uuid::now_v7(),
                generation: Uuid::now_v7(),
                owner: Uuid::now_v7(),
                request_sha256: "a".repeat(64),
            },
            request: serde_json::Value::Null,
            receipt: Vec::new(),
            daemon_generation: publications.generation(),
            observed_unix_ms: now().unwrap(),
            power: super::super::SeedPower {
                power: crate::PowerState::Running,
                locked: false,
            },
        };
        assert_eq!(
            publications
                .handle(command, false, &mut log, &directory)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidData
        );
        assert_eq!(original, fs::read(&path).unwrap());
        assert!(log.records().is_empty());
        assert!(log.effects().is_empty());
        drop(log);
        fs::remove_file(path).unwrap();
        fs::remove_dir(directory).unwrap();
    }
}

pub(crate) fn now() -> io::Result<u64> {
    u64::try_from(chrono::Utc::now().timestamp_millis()).map_err(|_| invalid())
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixturePostDispatchPublication {
    pub daemon_generation: Uuid,
    pub accepted_unix_ms: u64,
    pub published_unix_ms: u64,
    pub observation: FixturePostDispatchV1,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureSynchronousPublication {
    pub daemon_generation: Uuid,
    pub accepted_unix_ms: u64,
    pub published_unix_ms: u64,
    pub observation: super::FixtureSynchronousPostDispatchV1,
}

#[derive(Deserialize)]
#[allow(clippy::enum_variant_names)] // Wire command names share their protocol namespace.
#[serde(tag = "command", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum Command {
    /// Explicit synthetic input, restricted to the fixture supervisor socket.
    InstallTestPowerSource {
        sample: super::test_power_source::TestPowerSample,
    },
    /// Probe the supervisor-owned live read path. Cached completion publications
    /// and worker-supplied power are deliberately not accepted as a source.
    ConsumeStopCurrentPower {
        identity: super::FixtureStageIdentity,
    },
    /// This is an explicit supervisor observation, never a refreshed cache read.
    PublishStartPeRunningPower {
        identity: super::FixtureStageIdentity,
        request: serde_json::Value,
        receipt: Vec<u8>,
        daemon_generation: Uuid,
        observed_unix_ms: u64,
        power: super::SeedPower,
    },
    PublishStartPeFull {
        identity: super::FixtureStageIdentity,
        request: serde_json::Value,
        observation: Box<super::FixtureStartPeFullV1>,
    },
    StartPeFull {
        identity: super::FixtureStageIdentity,
        request: serde_json::Value,
    },
    PublishStartPeObservation {
        identity: super::FixtureStageIdentity,
        request: serde_json::Value,
        observation: super::FixtureStartPeObservationV1,
    },
    StartPeObservation {
        identity: super::FixtureStageIdentity,
        request: serde_json::Value,
    },
    PublishSynchronousStage {
        identity: super::FixtureStageIdentity,
        request: serde_json::Value,
        observation: Box<super::FixtureSynchronousPostDispatchV1>,
    },
    SynchronousStage {
        identity: super::FixtureStageIdentity,
        request: serde_json::Value,
    },
    PublishPostDispatch {
        request: serde_json::Value,
        observation: Box<FixturePostDispatchV1>,
    },
    PostDispatch {
        request: serde_json::Value,
    },
    PublishStagePostDispatch {
        identity: super::FixtureStageIdentity,
        request: serde_json::Value,
        observation: Box<FixturePostDispatchV1>,
    },
    StagePostDispatch {
        identity: super::FixtureStageIdentity,
        request: serde_json::Value,
    },
}

/// No startup file is loaded: restart invalidates observations and acceptance clocks.
pub(crate) struct Publications {
    generation: Uuid,
    accepted: BTreeMap<(Uuid, String), u64>,
    published: BTreeMap<(Uuid, String), FixturePostDispatchPublication>,
    synchronous: BTreeMap<(Uuid, String), FixtureSynchronousPublication>,
}

impl Publications {
    pub(crate) fn generation(&self) -> Uuid {
        self.generation
    }
    pub(crate) fn accepted_stage(
        &mut self,
        identity: &super::FixtureStageIdentity,
    ) -> io::Result<()> {
        identity.validate()?;
        if self.accepted.len() >= 64 {
            return Err(invalid());
        }
        self.accepted.insert(
            (identity.operation, identity.request_sha256.clone()),
            now()?,
        );
        Ok(())
    }
    pub(crate) fn new() -> Self {
        Self {
            generation: Uuid::now_v7(),
            accepted: BTreeMap::new(),
            published: BTreeMap::new(),
            synchronous: BTreeMap::new(),
        }
    }

    pub(crate) fn accepted(&mut self, bytes: &[u8]) -> io::Result<()> {
        let request = FixtureCloneRequest::decode(bytes).map_err(|_| invalid())?;
        if self.accepted.len() >= 64 {
            return Err(invalid());
        }
        self.accepted.insert(
            (
                request.request().binding().operation_id().as_uuid(),
                request.request_sha256(),
            ),
            now()?,
        );
        Ok(())
    }

    pub(crate) fn handle(
        &mut self,
        command: Command,
        supervisor: bool,
        log: &mut FixtureLog,
        directory: &Path,
    ) -> io::Result<Vec<u8>> {
        let (value, observation, stage) = match command {
            Command::InstallTestPowerSource { sample } if supervisor => {
                sample.install(directory, self.generation, now()?)?;
                return Ok(serde_json::to_vec(&serde_json::json!({"ok":true}))?);
            }
            Command::ConsumeStopCurrentPower { identity } if supervisor => {
                identity.validate()?;
                if identity.stage != super::FixtureLedgerStage::StartPe {
                    return Err(invalid());
                }
                match super::test_power_source::TestPowerSample::read(
                    directory,
                    &identity,
                    self.generation,
                    now()?,
                ) {
                    Ok(sample) => {
                        let published = if sample.power.power == crate::PowerState::Running {
                            let receipt = log
                                .accepted_stage_effect(
                                    identity.operation,
                                    &identity.request_sha256,
                                    identity.ledger_binding(),
                                )?
                                .and_then(|effect| effect.receipt())
                                .ok_or_else(invalid)?
                                .to_vec();
                            Some(log.refresh_start_power(
                                identity.operation,
                                &identity.request_sha256,
                                identity.ledger_binding(),
                                &receipt,
                                self.generation,
                                sample.daemon_generation,
                                sample.vmid,
                                sample.observed_unix_ms,
                                now()?,
                            )?)
                        } else {
                            None
                        };
                        return Ok(serde_json::to_vec(&serde_json::json!({
                            "ok":true, "source":"explicit_test_power", "sample":sample,
                            "running_publication": published,
                        }))?);
                    }
                    Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                    Err(error) => return Err(error),
                }
                return Ok(serde_json::to_vec(&serde_json::json!({
                    "ok": false,
                    "reason": "current_power_source_unavailable",
                    "daemon_generation": self.generation,
                    "identity": identity,
                }))?);
            }
            Command::PublishStartPeRunningPower {
                identity,
                request,
                receipt,
                daemon_generation,
                observed_unix_ms,
                power,
            } if supervisor => {
                let request =
                    crate::fixture_ipc::FixtureStageRequest::decode(&serde_json::to_vec(&request)?)
                        .map_err(|_| invalid())?;
                identity.validate_request(&request)?;
                request.decode_receipt(&receipt).map_err(|_| invalid())?;
                if identity.stage != super::FixtureLedgerStage::StartPe
                    || power.power != crate::PowerState::Running
                    || power.locked
                {
                    return Err(invalid());
                }
                return Ok(serde_json::to_vec(&log.refresh_start_power(
                    identity.operation,
                    &identity.request_sha256,
                    identity.ledger_binding(),
                    &receipt,
                    self.generation,
                    daemon_generation,
                    request.request().plan().expected().vm().target_vmid().get(),
                    observed_unix_ms,
                    now()?,
                )?)?);
            }
            Command::PublishStartPeFull {
                identity,
                request,
                observation,
            } if supervisor => {
                let accepted = self
                    .accepted
                    .get(&(identity.operation, identity.request_sha256.clone()))
                    .copied()
                    .ok_or_else(invalid)?;
                return super::start_full::handle(
                    directory,
                    log,
                    identity,
                    request,
                    Some((*observation, self.generation, accepted)),
                );
            }
            Command::StartPeFull { identity, request } if !supervisor => {
                return super::start_full::handle(directory, log, identity, request, None);
            }
            Command::PublishStartPeObservation {
                identity,
                request,
                observation,
            } if supervisor => {
                let request =
                    crate::fixture_ipc::FixtureStageRequest::decode(&serde_json::to_vec(&request)?)
                        .map_err(|_| invalid())?;
                let accepted = *self
                    .accepted
                    .get(&(identity.operation, identity.request_sha256.clone()))
                    .ok_or_else(invalid)?;
                return Ok(serde_json::to_vec(&observation.publish(
                    &identity,
                    &request,
                    log,
                    self.generation,
                    accepted,
                )?)?);
            }
            Command::StartPeObservation { identity, request } if !supervisor => {
                let request =
                    crate::fixture_ipc::FixtureStageRequest::decode(&serde_json::to_vec(&request)?)
                        .map_err(|_| invalid())?;
                identity.validate_request(&request)?;
                if identity.stage != super::FixtureLedgerStage::StartPe {
                    return Err(invalid());
                }
                return Ok(serde_json::to_vec(&log.start_observation(
                    identity.operation,
                    &identity.request_sha256,
                    identity.ledger_binding(),
                )?)?);
            }
            Command::PublishSynchronousStage {
                identity,
                request,
                observation,
            } if supervisor => {
                return self.synchronous(identity, request, Some(*observation), log, directory);
            }
            Command::SynchronousStage { identity, request } if !supervisor => {
                return self.synchronous(identity, request, None, log, directory);
            }
            Command::PublishPostDispatch {
                request,
                observation,
            } if supervisor => (request, Some(observation), None),
            Command::PostDispatch { request } if !supervisor => (request, None, None),
            Command::PublishStagePostDispatch {
                identity,
                request,
                observation,
            } if supervisor => (request, Some(observation), Some(identity)),
            Command::StagePostDispatch { identity, request } if !supervisor => {
                (request, None, Some(identity))
            }
            _ => return Err(invalid()),
        };
        if let Some(identity) = stage {
            // Atomic fixture admission alone is not independent running/task
            // evidence. Keep publication closed until its durable contract exists.
            if identity.stage == super::FixtureLedgerStage::StartPe {
                return Err(invalid());
            }
            let request =
                crate::fixture_ipc::FixtureStageRequest::decode(&serde_json::to_vec(&value)?)
                    .map_err(|_| invalid())?;
            identity.validate_request(&request)?;
            let key = (identity.operation, identity.request_sha256.clone());
            let effect = log
                .accepted_stage_effect(key.0, &key.1, identity.ledger_binding())?
                .ok_or_else(invalid)?;
            if let Some(observation) = observation {
                let accepted = *self.accepted.get(&key).ok_or_else(invalid)?;
                let published = now()?;
                let observation = FixturePostDispatchV1::decode_stage(
                    &serde_json::to_vec(&observation)?,
                    &request,
                    effect.receipt().ok_or_else(invalid)?,
                    accepted,
                    published,
                )?;
                if self.published.contains_key(&key) {
                    return Err(invalid());
                }
                let publication = FixturePostDispatchPublication {
                    daemon_generation: self.generation,
                    accepted_unix_ms: accepted,
                    published_unix_ms: published,
                    observation,
                };
                let bytes = serde_json::to_vec(
                    &serde_json::json!({"identity":identity,"request":value,"receipt":effect.receipt(),"publication":publication}),
                )?;
                let destination = directory.join(format!(
                    "post-dispatch-{}-{}-{}.json",
                    self.generation, key.0, key.1
                ));
                let temporary = directory.join(format!("post-dispatch-{}.tmp", Uuid::now_v7()));
                let mut file = fs::OpenOptions::new()
                    .create_new(true)
                    .write(true)
                    .open(&temporary)?;
                file.write_all(&bytes)?;
                file.sync_all()?;
                fs::rename(&temporary, &destination)?;
                fs::File::open(directory)?.sync_all()?;
                self.published.insert(key.clone(), publication);
            }
            return Ok(serde_json::to_vec(&self.published.get(&key))?);
        }
        let request =
            FixtureCloneRequest::decode(&serde_json::to_vec(&value)?).map_err(|_| invalid())?;
        let key = (
            request.request().binding().operation_id().as_uuid(),
            request.request_sha256(),
        );
        if let Some(observation) = observation {
            let accepted = *self.accepted.get(&key).ok_or_else(invalid)?;
            let effect = log.accepted_effect(key.0, &key.1)?.ok_or_else(invalid)?;
            let receipt = effect.receipt().ok_or_else(invalid)?;
            let published = now()?;
            let observation = FixturePostDispatchV1::decode(
                &serde_json::to_vec(&observation)?,
                &request,
                receipt,
                accepted,
                published,
            )?;
            // One immutable publication per accepted effect prevents observation rollback.
            if self.published.contains_key(&key) {
                return Err(invalid());
            }
            let publication = FixturePostDispatchPublication {
                daemon_generation: self.generation,
                accepted_unix_ms: accepted,
                published_unix_ms: published,
                observation,
            };
            let bytes = serde_json::to_vec(
                &serde_json::json!({"request":value,"receipt":receipt,"publication":publication}),
            )?;
            let destination =
                directory.join(format!("post-dispatch-{}-{}.json", self.generation, key.0));
            let temporary = directory.join(format!("post-dispatch-{}.tmp", Uuid::now_v7()));
            let mut file = fs::OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&temporary)?;
            file.write_all(&bytes)?;
            file.sync_all()?;
            fs::rename(&temporary, &destination)?;
            fs::File::open(directory)?.sync_all()?;
            self.published.insert(key.clone(), publication);
        }
        Ok(serde_json::to_vec(&self.published.get(&key))?)
    }

    fn synchronous(
        &mut self,
        identity: super::FixtureStageIdentity,
        value: serde_json::Value,
        observation: Option<super::FixtureSynchronousPostDispatchV1>,
        log: &mut FixtureLog,
        directory: &Path,
    ) -> io::Result<Vec<u8>> {
        let request = crate::fixture_ipc::FixtureStageRequest::decode(&serde_json::to_vec(&value)?)
            .map_err(|_| invalid())?;
        identity.validate_request(&request)?;
        if identity.stage != super::FixtureLedgerStage::ConfigurePe {
            return Err(invalid());
        }
        let key = (identity.operation, identity.request_sha256.clone());
        let effect = log
            .accepted_stage_effect(key.0, &key.1, identity.ledger_binding())?
            .ok_or_else(invalid)?;
        if let Some(observation) = observation {
            let accepted = *self.accepted.get(&key).ok_or_else(invalid)?;
            let published = now()?;
            let observation = super::FixtureSynchronousPostDispatchV1::decode(
                &serde_json::to_vec(&observation)?,
                &request,
                effect.receipt().ok_or_else(invalid)?,
                accepted,
                published,
            )?;
            if self.synchronous.contains_key(&key) || self.published.contains_key(&key) {
                return Err(invalid());
            }
            let super::SeedRead::Observed {
                observed_unix_ms,
                value: power,
            } = &observation.provisioning.target_power
            else {
                return Err(invalid());
            };
            if power.power != crate::PowerState::Stopped || power.locked {
                return Err(invalid());
            }
            let receipt = effect.receipt().ok_or_else(invalid)?.to_vec();
            log.record_power_observation(
                identity.operation,
                &identity.request_sha256,
                identity.ledger_binding(),
                &receipt,
                super::durable_fixture_log::PowerStateV1::Stopped,
                self.generation,
                accepted,
                *observed_unix_ms,
            )?;
            let publication = FixtureSynchronousPublication {
                daemon_generation: self.generation,
                accepted_unix_ms: accepted,
                published_unix_ms: published,
                observation,
            };
            let bytes = serde_json::to_vec(
                &serde_json::json!({"identity":identity,"request":value,"receipt":receipt,"publication":publication}),
            )?;
            let destination = directory.join(format!(
                "synchronous-{}-{}-{}.json",
                self.generation, key.0, key.1
            ));
            let temporary = directory.join(format!("synchronous-{}.tmp", Uuid::now_v7()));
            let mut file = fs::OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&temporary)?;
            file.write_all(&bytes)?;
            file.sync_all()?;
            fs::rename(&temporary, destination)?;
            fs::File::open(directory)?.sync_all()?;
            self.synchronous.insert(key.clone(), publication);
        }
        Ok(serde_json::to_vec(&self.synchronous.get(&key))?)
    }
}

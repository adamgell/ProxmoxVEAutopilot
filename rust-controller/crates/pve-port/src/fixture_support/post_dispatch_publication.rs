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
        log: &FixtureLog,
        directory: &Path,
    ) -> io::Result<Vec<u8>> {
        let (value, observation, stage) = match command {
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
        log: &FixtureLog,
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
            let publication = FixtureSynchronousPublication {
                daemon_generation: self.generation,
                accepted_unix_ms: accepted,
                published_unix_ms: published,
                observation,
            };
            let bytes = serde_json::to_vec(
                &serde_json::json!({"identity":identity,"request":value,"receipt":effect.receipt(),"publication":publication}),
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

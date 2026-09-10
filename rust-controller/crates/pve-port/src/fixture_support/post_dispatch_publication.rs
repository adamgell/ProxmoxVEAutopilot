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

#[derive(Deserialize)]
#[serde(tag = "command", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum Command {
    PublishPostDispatch {
        request: serde_json::Value,
        observation: Box<FixturePostDispatchV1>,
    },
    PostDispatch {
        request: serde_json::Value,
    },
}

/// No startup file is loaded: restart invalidates observations and acceptance clocks.
pub(crate) struct Publications {
    generation: Uuid,
    accepted: BTreeMap<(Uuid, String), u64>,
    published: BTreeMap<(Uuid, String), FixturePostDispatchPublication>,
}

impl Publications {
    pub(crate) fn new() -> Self {
        Self {
            generation: Uuid::now_v7(),
            accepted: BTreeMap::new(),
            published: BTreeMap::new(),
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
        let (value, observation) = match command {
            Command::PublishPostDispatch {
                request,
                observation,
            } if supervisor => (request, Some(observation)),
            Command::PostDispatch { request } if !supervisor => (request, None),
            _ => return Err(invalid()),
        };
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
}

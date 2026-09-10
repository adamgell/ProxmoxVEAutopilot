//! Stage barriers never share release authority with the legacy Clone barrier.
use super::{FixtureStageIdentity, VmState, stage_identity::invalid};
use crate::fixture_ipc::FixtureStageRequest;
use serde::{Deserialize, Serialize};
use std::{
    fs,
    io::{self, Write},
    path::{Path, PathBuf},
    time::{Duration, Instant},
};
use uuid::Uuid;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum StageCheckpointRequest {
    Status,
    Arm {
        identity: FixtureStageIdentity,
        timeout_ms: u64,
    },
    Enter {
        identity: FixtureStageIdentity,
    },
    AuthorizeRelease {
        identity: FixtureStageIdentity,
        request: Vec<u8>,
        committed_request: Vec<u8>,
        after: VmState,
    },
    Poll {
        identity: FixtureStageIdentity,
    },
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StageCheckpointReply {
    pub ok: bool,
    pub generation: Uuid,
    pub identity: Option<FixtureStageIdentity>,
    pub phase: super::CheckpointPhase,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Authorization {
    request: Vec<u8>,
    after: VmState,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct StageBarrier {
    state: StageCheckpointReply,
    authorization: Option<Authorization>,
    #[serde(skip)]
    path: PathBuf,
    #[serde(skip)]
    deadline: Option<Instant>,
}
impl StageBarrier {
    pub(crate) fn new(directory: &Path) -> io::Result<Self> {
        let path = directory.join("stage-checkpoint.json");
        if path.exists() {
            let metadata = fs::symlink_metadata(&path)?;
            if !metadata.is_file() || metadata.len() > 196_608 {
                return Err(invalid());
            }
            let _: Self = serde_json::from_slice(&fs::read(&path)?)?;
        }
        let result = Self {
            state: StageCheckpointReply {
                ok: false,
                generation: Uuid::now_v7(),
                identity: None,
                phase: super::CheckpointPhase::Idle,
            },
            authorization: None,
            path,
            deadline: None,
        };
        result.persist()?;
        Ok(result)
    }
    fn persist(&self) -> io::Result<()> {
        let temporary = self.path.with_extension(format!("{}.tmp", Uuid::now_v7()));
        let mut file = fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temporary)?;
        file.write_all(&serde_json::to_vec(self)?)?;
        file.sync_all()?;
        fs::rename(temporary, &self.path)?;
        fs::File::open(self.path.parent().ok_or_else(invalid)?)?.sync_all()
    }
    fn bound(&self, identity: &FixtureStageIdentity) -> bool {
        identity.validate().is_ok()
            && identity.generation == self.state.generation
            && self.state.identity.as_ref() == Some(identity)
            && self.deadline.is_some_and(|d| Instant::now() < d)
    }
    pub(crate) fn handle(
        &mut self,
        request: StageCheckpointRequest,
        supervisor: bool,
    ) -> io::Result<StageCheckpointReply> {
        use super::CheckpointPhase::*;
        let ok = match request {
            StageCheckpointRequest::Status => supervisor,
            StageCheckpointRequest::Arm {
                identity,
                timeout_ms,
            } if supervisor
                && self.state.phase == Idle
                && identity.validate().is_ok()
                && identity.generation == self.state.generation
                && (1..=5000).contains(&timeout_ms) =>
            {
                self.state.identity = Some(identity);
                self.state.phase = Armed;
                self.deadline = Some(Instant::now() + Duration::from_millis(timeout_ms));
                true
            }
            StageCheckpointRequest::Enter { identity }
                if !supervisor && self.bound(&identity) && self.state.phase == Armed =>
            {
                self.state.phase = Entered;
                true
            }
            StageCheckpointRequest::AuthorizeRelease {
                identity,
                request,
                committed_request,
                after,
            } if supervisor
                && self.bound(&identity)
                && self.state.phase == Entered
                && self.authorization.is_none() =>
            {
                let valid = FixtureStageRequest::decode(&request)
                    .ok()
                    .is_some_and(|r| identity.validate_request(&r).is_ok())
                    && request == committed_request
                    && after.disk_bytes > 0;
                if valid {
                    self.authorization = Some(Authorization { request, after });
                    self.state.phase = Released;
                }
                valid
            }
            StageCheckpointRequest::Poll { identity } if !supervisor && self.bound(&identity) => {
                matches!(self.state.phase, Entered | Released)
            }
            _ => false,
        };
        if ok && !matches!(self.state.phase, Idle) {
            self.persist()?;
        }
        let mut reply = self.state.clone();
        reply.ok = ok;
        Ok(reply)
    }
    pub(crate) fn validate_submission(
        &self,
        identity: &FixtureStageIdentity,
        request: &FixtureStageRequest,
    ) -> io::Result<()> {
        identity.validate_request(request)?;
        if !self.bound(identity)
            || self.state.phase != super::CheckpointPhase::Released
            || self
                .authorization
                .as_ref()
                .is_none_or(|a| request.encode().ok().as_ref() != Some(&a.request))
        {
            return Err(invalid());
        }
        Ok(())
    }
    pub(crate) fn authorized_after(
        &self,
        identity: &FixtureStageIdentity,
        request: &FixtureStageRequest,
    ) -> io::Result<VmState> {
        self.validate_submission(identity, request)?;
        Ok(self
            .authorization
            .as_ref()
            .ok_or_else(invalid)?
            .after
            .clone())
    }
    pub(crate) fn consume(
        &mut self,
        identity: &FixtureStageIdentity,
        request: &FixtureStageRequest,
    ) -> io::Result<VmState> {
        self.validate_submission(identity, request)?;
        let authorization = self.authorization.take().ok_or_else(invalid)?;
        self.state.phase = super::CheckpointPhase::Idle;
        self.state.identity = None;
        self.deadline = None;
        self.persist()?;
        Ok(authorization.after)
    }
}

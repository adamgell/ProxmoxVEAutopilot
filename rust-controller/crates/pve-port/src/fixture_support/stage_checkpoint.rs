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
    AuthorizeStartPe {
        identity: FixtureStageIdentity,
        request: Vec<u8>,
        committed_request: Vec<u8>,
        power: super::StartPePowerAuthorizationV1,
    },
    AdmitStop {
        identity: FixtureStageIdentity,
        request: Vec<u8>,
        committed_request: Vec<u8>,
        predecessor: FixtureStageIdentity,
        predecessor_request: Vec<u8>,
        predecessor_receipt: Vec<u8>,
        authority: super::FixtureStopAuthorityV1,
    },
    Poll {
        identity: FixtureStageIdentity,
    },
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StageCheckpointReply {
    pub ok: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub refusal: Option<StageCheckpointRefusal>,
    pub generation: Uuid,
    pub identity: Option<FixtureStageIdentity>,
    pub phase: super::CheckpointPhase,
}
/// A successful admission has no authority to release a physical stop.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StageCheckpointRefusal {
    StopReleaseAuthorityUnavailable,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Authorization {
    request: Vec<u8>,
    after: VmState,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    start_power: Option<super::durable_fixture_log::PowerObservationV1>,
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
                refusal: None,
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
        log: &mut super::durable_fixture_log::FixtureLog,
        daemon_generation: Uuid,
    ) -> io::Result<StageCheckpointReply> {
        use super::CheckpointPhase::*;
        let mut refusal = None;
        let ok = match request {
            StageCheckpointRequest::AdmitStop {
                identity,
                request,
                committed_request,
                predecessor,
                predecessor_request,
                predecessor_receipt,
                authority,
            } if supervisor
                && self.bound(&identity)
                && self.state.phase == Entered
                && self.authorization.is_none() =>
            {
                let checked = (|| -> io::Result<()> {
                    let decoded = FixtureStageRequest::decode(&request).map_err(|_| invalid())?;
                    let prior =
                        FixtureStageRequest::decode(&predecessor_request).map_err(|_| invalid())?;
                    identity.validate_request(&decoded)?;
                    predecessor.validate_request(&prior)?;
                    decoded
                        .validate_ensure_stopped_physical_predecessor(&prior, &predecessor_receipt)
                        .map_err(|_| invalid())?;
                    if request != committed_request
                        || authority.evidence_fence != decoded.request().binding().evidence_fence()
                    {
                        return Err(invalid());
                    }
                    log.admit_stop(
                        identity.operation,
                        identity.request_sha256.clone(),
                        identity.ledger_binding(),
                        authority,
                        predecessor.operation,
                        &predecessor.request_sha256,
                        predecessor.ledger_binding(),
                        &predecessor_receipt,
                        daemon_generation,
                        super::post_dispatch_publication::now()?,
                    )
                })();
                // Admission deliberately does not release the barrier. Stop task
                // publication and reconciliation are separate unfinished seams.
                checked.is_ok()
            }
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
                let valid = FixtureStageRequest::decode(&request).ok().is_some_and(|r| {
                    if identity.validate_request(&r).is_err() {
                        return false;
                    }
                    if r.request().plan().action() == crate::ProvisioningActionV1::EnsureStopped {
                        // Point-in-time DB checks and a durable admission record
                        // do not bridge cancellation/lease changes into this IPC
                        // release. No generic disk-state authorization can do so.
                        refusal = Some(StageCheckpointRefusal::StopReleaseAuthorityUnavailable);
                        return false;
                    }
                    true
                }) && request == committed_request
                    && after.disk_bytes > 0;
                if valid {
                    self.authorization = Some(Authorization {
                        request,
                        after,
                        start_power: None,
                    });
                    self.state.phase = Released;
                }
                valid
            }
            StageCheckpointRequest::AuthorizeStartPe {
                identity,
                request,
                committed_request,
                power,
            } if supervisor
                && self.bound(&identity)
                && self.state.phase == Entered
                && self.authorization.is_none() =>
            {
                let evidence = FixtureStageRequest::decode(&request)
                    .ok()
                    .and_then(|decoded| {
                        if identity.validate_request(&decoded).is_err()
                            || request != committed_request
                        {
                            return None;
                        }
                        power.validate(&decoded, log, daemon_generation).ok()
                    });
                if let Some((after, start_power)) = evidence {
                    self.authorization = Some(Authorization {
                        request,
                        after,
                        start_power: Some(start_power),
                    });
                    self.state.phase = Released;
                    true
                } else {
                    false
                }
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
        reply.refusal = refusal;
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

    pub(crate) fn authorized_start_power(
        &self,
        identity: &FixtureStageIdentity,
        request: &FixtureStageRequest,
        log: &super::durable_fixture_log::FixtureLog,
        daemon_generation: Uuid,
    ) -> io::Result<super::durable_fixture_log::PowerObservationV1> {
        self.validate_submission(identity, request)?;
        let token = self
            .authorization
            .as_ref()
            .and_then(|a| a.start_power.as_ref())
            .ok_or_else(invalid)?;
        log.revalidate_stopped_token(
            token,
            daemon_generation,
            super::post_dispatch_publication::now()?,
        )?;
        Ok(token.clone())
    }
}

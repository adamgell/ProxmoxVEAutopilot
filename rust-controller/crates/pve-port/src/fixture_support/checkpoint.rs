//! Supervisor-owned barriers. A daemon restart always invalidates old releases.
use crate::fixture_ipc::CheckpointError;
use serde::{Deserialize, Serialize};
use std::{
    fs::{self, OpenOptions},
    io::{self, Write},
    path::{Path, PathBuf},
    time::{Duration, Instant},
};
use uuid::Uuid;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CheckpointPoint {
    DispatchCommitted,
}

#[cfg(test)]
mod stop_transport_tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    #[test]
    fn shared_history_provenance_requires_exact_channel_and_binding() {
        let operation = Uuid::now_v7();
        let binding = CheckpointBinding {
            generation: Uuid::now_v7(),
            owner: Uuid::now_v7(),
            operation,
            point: CheckpointPoint::DispatchCommitted,
        };
        let a = FixtureCheckpointClient::new(
            std::path::PathBuf::from("/tmp/provenance-a.sock"),
            Duration::from_secs(1),
        )
        .unwrap();
        let b = FixtureCheckpointClient::new(
            std::path::PathBuf::from("/tmp/provenance-b.sock"),
            Duration::from_secs(1),
        )
        .unwrap();
        assert_eq!(
            a.shared_history_provenance(&binding),
            a.shared_history_provenance(&binding)
        );
        assert_ne!(
            a.shared_history_provenance(&binding),
            b.shared_history_provenance(&binding)
        );
        let mut other = binding;
        other.operation = Uuid::now_v7();
        assert_ne!(
            a.shared_history_provenance(&binding),
            a.shared_history_provenance(&other)
        );
    }

    fn sample() -> super::super::VersionedTestPowerSample {
        use super::super::*;
        let identity = FixtureStageIdentity {
            operation: Uuid::now_v7(),
            stage: FixtureLedgerStage::StartPe,
            attempt: Uuid::now_v7(),
            generation: Uuid::now_v7(),
            owner: Uuid::now_v7(),
            request_sha256: "a".repeat(64),
        };
        VersionedTestPowerSample {
            version: 2,
            sequence: 1,
            previous_sha256: None,
            stop: FixtureStageIdentity {
                stage: FixtureLedgerStage::PeEnsureStopped,
                operation: Uuid::now_v7(),
                ..identity.clone()
            },
            authority: FixtureStopAuthorityV1 {
                version: 1,
                grace_operation: Uuid::now_v7(),
                decision_event: Uuid::now_v7(),
                evidence_fence: 1,
                grace_due_unix_ms: 90,
                decision_unix_ms: 95,
                lease_checked_unix_ms: 99,
                lease_expires_unix_ms: 6000,
                original_deadline_unix_ms: 7000,
            },
            sample: TestPowerSample {
                version: 1,
                identity,
                vmid: 109,
                daemon_generation: Uuid::now_v7(),
                observed_unix_ms: 100,
                lease_expires_unix_ms: 6000,
                power: SeedPower {
                    power: crate::PowerState::Running,
                    locked: false,
                },
            },
        }
    }

    #[tokio::test]
    async fn stop_power_transport_binds_reply_and_refuses_missing_or_malformed_source() {
        let directory = std::env::temp_dir().join(format!("stop-client-{}", Uuid::now_v7()));
        fs::create_dir(&directory).unwrap();
        let socket = directory.join("s");
        let client =
            FixtureCheckpointClient::new(socket.clone(), Duration::from_millis(100)).unwrap();
        let sample = sample();
        assert!(client.consume_versioned_stop_power(&sample).await.is_err());
        let listener = tokio::net::UnixListener::bind(&socket).unwrap();
        for case in 0..7 {
            let expected = sample.clone();
            let serve = async {
                let (mut stream, _) = listener.accept().await.unwrap();
                let size = stream.read_u32().await.unwrap() as usize;
                let mut bytes = vec![0; size];
                stream.read_exact(&mut bytes).await.unwrap();
                let request: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
                assert_eq!(request["command"], "consume_versioned_stop_current_power");
                assert_eq!(request["sample"], serde_json::to_value(&expected).unwrap());
                let mut reply = serde_json::json!({"ok":true,
                    "source":"versioned_explicit_test_power", "sample":expected,
                    "running_publication":{}});
                match case {
                    1 => reply["sample"]["sequence"] = 2.into(),
                    2 => reply["ok"] = false.into(),
                    3 => reply["source"] = "cached".into(),
                    4 => reply["running_publication"] = serde_json::Value::Null,
                    5 => {
                        stream.write_u32(32_769).await.unwrap();
                        return;
                    }
                    6 => {
                        tokio::time::sleep(Duration::from_millis(150)).await;
                        return;
                    }
                    _ => {}
                }
                let reply = serde_json::to_vec(&reply).unwrap();
                stream.write_u32(reply.len() as u32).await.unwrap();
                stream.write_all(&reply).await.unwrap();
            };
            let (result, ()) = tokio::join!(client.consume_versioned_stop_power(&sample), serve);
            assert_eq!(result.is_ok(), case == 0);
        }
        drop(listener);
        fs::remove_file(socket).unwrap();
        fs::remove_dir(directory).unwrap();
    }
}
impl From<crate::FakeControllerCheckpoint> for CheckpointPoint {
    fn from(value: crate::FakeControllerCheckpoint) -> Self {
        match value {
            crate::FakeControllerCheckpoint::DispatchCommitted => Self::DispatchCommitted,
        }
    }
}
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CheckpointBinding {
    pub generation: Uuid,
    pub owner: Uuid,
    pub operation: Uuid,
    pub point: CheckpointPoint,
}
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CheckpointPhase {
    Idle,
    Armed,
    Entered,
    Released,
    Expired,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CheckpointState {
    pub generation: Uuid,
    pub binding: Option<CheckpointBinding>,
    pub phase: CheckpointPhase,
}
#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum CheckpointRequest {
    Status,
    ArmLate {
        binding: CheckpointBinding,
        timeout_ms: u64,
        identity: super::FixtureReadIdentity,
    },
    AuthorizeRelease {
        proposal: super::LateCloneAuthorizationV1,
        committed_request: Vec<u8>,
    },
    Arm {
        binding: CheckpointBinding,
        timeout_ms: u64,
    },
    Enter {
        binding: CheckpointBinding,
    },
    Poll {
        binding: CheckpointBinding,
    },
    Release {
        binding: CheckpointBinding,
    },
}
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CheckpointReply {
    pub ok: bool,
    pub state: CheckpointState,
}
pub(crate) struct Barrier {
    state: CheckpointState,
    path: PathBuf,
    deadline: Option<Instant>,
    late_identity: Option<super::FixtureReadIdentity>,
    authorization: Option<super::LateCloneAuthorizationV1>,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct PersistedBarrier {
    state: CheckpointState,
    #[serde(default)]
    late_identity: Option<super::FixtureReadIdentity>,
    #[serde(default)]
    authorization: Option<super::LateCloneAuthorizationV1>,
}
impl Barrier {
    pub(crate) fn consume_clone(
        &mut self,
        binding: CheckpointBinding,
        request: &crate::fixture_ipc::FixtureCloneRequest,
    ) -> io::Result<super::FixtureCloneSeed> {
        let authorization = self.authorization.as_ref().ok_or_else(invalid)?;
        if self.state.phase != CheckpointPhase::Released
            || self.state.binding != Some(binding)
            || self.state.generation != binding.generation
            || self
                .deadline
                .is_none_or(|deadline| Instant::now() >= deadline)
            || authorization.binding != binding
            || authorization.request != request.encode().map_err(|_| invalid())?
            || authorization.request_sha256 != request.request_sha256()
        {
            return Err(invalid());
        }
        let seed = super::FixtureCloneSeed::new(request, authorization.after.clone())?;
        // Persist consumption before granting the in-process effect capability.
        // A crash between consumption and effect remains fail-closed.
        self.authorization = None;
        self.persist()?;
        Ok(seed)
    }
    pub(crate) fn new(directory: &Path) -> io::Result<Self> {
        let path = directory.join("checkpoint.json");
        // Reject corrupt prior state; old state can never authorize a new process.
        if path.exists() {
            let metadata = fs::symlink_metadata(&path)?;
            if !metadata.is_file() || metadata.len() > 131_072 {
                return Err(invalid());
            }
            let bytes = fs::read(&path)?;
            if serde_json::from_slice::<PersistedBarrier>(&bytes).is_err() {
                let _: CheckpointState = serde_json::from_slice(&bytes)?;
            }
        }
        let barrier = Self {
            state: CheckpointState {
                generation: Uuid::now_v7(),
                binding: None,
                phase: CheckpointPhase::Idle,
            },
            path,
            deadline: None,
            late_identity: None,
            authorization: None,
        };
        barrier.persist()?;
        Ok(barrier)
    }
    fn persist(&self) -> io::Result<()> {
        let temporary = self.path.with_extension(format!("{}.tmp", Uuid::now_v7()));
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)?;
        file.write_all(&serde_json::to_vec(&PersistedBarrier {
            state: self.state.clone(),
            late_identity: self.late_identity.clone(),
            authorization: self.authorization.clone(),
        })?)?;
        file.sync_all()?;
        fs::rename(&temporary, &self.path)?;
        fs::File::open(self.path.parent().ok_or_else(invalid)?)?.sync_all()
    }
    pub(crate) fn handle(
        &mut self,
        request: CheckpointRequest,
        supervisor: bool,
    ) -> io::Result<CheckpointReply> {
        if self.deadline.is_some_and(|d| Instant::now() >= d)
            && matches!(
                self.state.phase,
                CheckpointPhase::Armed | CheckpointPhase::Entered
            )
        {
            self.state.phase = CheckpointPhase::Expired;
            self.persist()?;
        }
        let ok = match request {
            CheckpointRequest::Status => supervisor,
            CheckpointRequest::ArmLate {
                binding,
                timeout_ms,
                identity,
            } if supervisor
                && self.state.phase == CheckpointPhase::Idle
                && binding.generation == self.state.generation
                && !binding.owner.is_nil()
                && binding.operation == identity.operation
                && identity.validate().is_ok()
                && (1..=5000).contains(&timeout_ms) =>
            {
                self.state.binding = Some(binding);
                self.state.phase = CheckpointPhase::Armed;
                self.late_identity = Some(identity);
                self.deadline = Some(Instant::now() + Duration::from_millis(timeout_ms));
                true
            }
            CheckpointRequest::AuthorizeRelease {
                proposal,
                committed_request,
            } if supervisor && self.authorization.is_none() => {
                let valid = self
                    .late_identity
                    .as_ref()
                    .zip(self.state.binding)
                    .and_then(|(identity, binding)| {
                        let committed =
                            crate::fixture_ipc::FixtureCloneRequest::decode(&committed_request)
                                .ok()?;
                        proposal
                            .validate_candidate(identity, binding, &self.state, &committed)
                            .ok()
                    })
                    .is_some();
                if valid {
                    self.authorization = Some(proposal);
                    self.state.phase = CheckpointPhase::Released;
                }
                valid
            }
            CheckpointRequest::Arm {
                binding,
                timeout_ms,
            } if supervisor
                && self.state.phase == CheckpointPhase::Idle
                && binding.generation == self.state.generation
                && !binding.owner.is_nil()
                && !binding.operation.is_nil()
                && (1..=5000).contains(&timeout_ms) =>
            {
                self.state.binding = Some(binding);
                self.state.phase = CheckpointPhase::Armed;
                self.deadline = Some(Instant::now() + Duration::from_millis(timeout_ms));
                true
            }
            CheckpointRequest::Enter { binding }
                if !supervisor
                    && self.state.binding == Some(binding)
                    && self.state.phase == CheckpointPhase::Armed =>
            {
                self.state.phase = CheckpointPhase::Entered;
                true
            }
            CheckpointRequest::Poll { binding }
                if !supervisor && self.state.binding == Some(binding) =>
            {
                matches!(
                    self.state.phase,
                    CheckpointPhase::Entered | CheckpointPhase::Released
                )
            }
            CheckpointRequest::Release { binding }
                if supervisor
                    && self.late_identity.is_none()
                    && self.state.binding == Some(binding)
                    && self.state.phase == CheckpointPhase::Entered =>
            {
                self.state.phase = CheckpointPhase::Released;
                true
            }
            _ => false,
        };
        if ok {
            self.persist()?;
        }
        Ok(CheckpointReply {
            ok,
            state: self.state.clone(),
        })
    }
}
fn invalid() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "checkpoint protocol rejected")
}

/// Explicit fallible checkpoint transport. Filesystem access defines the local trust boundary.
pub struct FixtureCheckpointClient {
    socket: PathBuf,
    timeout: Duration,
}
impl FixtureCheckpointClient {
    /// Construct the only provenance value a local checkpoint client can
    /// provide. Equality requires the exact operation binding and socket path;
    /// a decoded receipt or copied identity cannot produce this value.
    pub fn shared_history_provenance(
        &self,
        binding: &CheckpointBinding,
    ) -> Result<crate::fixture_ipc::FixtureSharedHistoryProvenanceV1, CheckpointError> {
        crate::fixture_ipc::FixtureSharedHistoryProvenanceV1::new(
            binding.operation,
            binding.generation,
            binding.owner,
            self.socket.to_string_lossy().into_owned(),
        )
    }

    /// Consume an already installed, independently sampled fixture power value.
    /// This never installs a source, refreshes its clock, or releases a barrier.
    pub async fn consume_versioned_stop_power(
        &self,
        sample: &super::VersionedTestPowerSample,
    ) -> io::Result<()> {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Reply {
            ok: bool,
            source: String,
            sample: super::VersionedTestPowerSample,
            running_publication: serde_json::Value,
        }
        tokio::time::timeout(self.timeout, async {
            let mut stream = tokio::net::UnixStream::connect(&self.socket).await?;
            let payload = serde_json::to_vec(&serde_json::json!({
                "command":"consume_versioned_stop_current_power", "sample":sample
            }))?;
            if payload.len() > 16_384 {
                return Err(invalid());
            }
            stream.write_u32(payload.len() as u32).await?;
            stream.write_all(&payload).await?;
            let size = stream.read_u32().await? as usize;
            if size == 0 || size > 32_768 {
                return Err(invalid());
            }
            let mut bytes = vec![0; size];
            stream.read_exact(&mut bytes).await?;
            let reply: Reply = serde_json::from_slice(&bytes)?;
            if !reply.ok
                || reply.source != "versioned_explicit_test_power"
                || serde_json::to_vec(&reply.sample)? != serde_json::to_vec(sample)?
                || !reply.running_publication.is_object()
            {
                return Err(invalid());
            }
            Ok(())
        })
        .await
        .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "stop power deadline"))?
    }

    pub async fn stage_request(
        &self,
        request: super::StageCheckpointRequest,
    ) -> io::Result<super::StageCheckpointReply> {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        tokio::time::timeout(self.timeout, async {
            let mut stream = tokio::net::UnixStream::connect(&self.socket).await?;
            let payload = serde_json::to_vec(
                &serde_json::json!({"command":"stage_checkpoint","request":request}),
            )?;
            stream.write_u32(payload.len() as u32).await?;
            stream.write_all(&payload).await?;
            let size = stream.read_u32().await? as usize;
            if size == 0 || size > 4096 {
                return Err(invalid());
            }
            let mut bytes = vec![0; size];
            stream.read_exact(&mut bytes).await?;
            serde_json::from_slice(&bytes).map_err(Into::into)
        })
        .await
        .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "stage checkpoint deadline"))?
    }
    /// Enter and await the exact stage barrier; dropping this future never submits.
    pub async fn stage_checkpoint(&self, identity: &super::FixtureStageIdentity) -> io::Result<()> {
        identity.validate()?;
        tokio::time::timeout(self.timeout, async {
            // A fresh controller creates the exact attempt before the supervisor
            // can observe its committed dispatch and arm that identity.
            loop {
                let state = self
                    .stage_request(super::StageCheckpointRequest::Status)
                    .await?;
                if state.generation != identity.generation {
                    return Err(invalid());
                }
                match state.phase {
                    CheckpointPhase::Idle if state.identity.is_none() => {
                        tokio::time::sleep(Duration::from_millis(5)).await
                    }
                    CheckpointPhase::Armed if state.identity.as_ref() == Some(identity) => break,
                    _ => return Err(invalid()),
                }
            }
            let entered = self
                .stage_request(super::StageCheckpointRequest::Enter {
                    identity: identity.clone(),
                })
                .await?;
            if !entered.ok
                || entered.generation != identity.generation
                || entered.identity.as_ref() != Some(identity)
                || entered.phase != CheckpointPhase::Entered
            {
                return Err(invalid());
            }
            loop {
                let reply = self
                    .stage_request(super::StageCheckpointRequest::Poll {
                        identity: identity.clone(),
                    })
                    .await?;
                if !reply.ok
                    || reply.generation != identity.generation
                    || reply.identity.as_ref() != Some(identity)
                {
                    return Err(invalid());
                }
                match reply.phase {
                    CheckpointPhase::Released => return Ok(()),
                    CheckpointPhase::Entered => tokio::time::sleep(Duration::from_millis(5)).await,
                    _ => return Err(invalid()),
                }
            }
        })
        .await
        .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "stage checkpoint release deadline"))?
    }
    /// Preserve checkpoint failure categories across the sealed controller seam.
    pub async fn controller_checkpoint(
        &self,
        binding: CheckpointBinding,
    ) -> Result<(), crate::fixture_ipc::CheckpointError> {
        self.checkpoint(binding).await.map_err(Into::into)
    }
    pub fn new(socket: PathBuf, timeout: Duration) -> io::Result<Self> {
        if timeout.is_zero() || timeout > Duration::from_secs(5) {
            return Err(invalid());
        }
        Ok(Self { socket, timeout })
    }
    pub async fn request(&self, request: CheckpointRequest) -> io::Result<CheckpointReply> {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        tokio::time::timeout(self.timeout, async {
            let mut stream = tokio::net::UnixStream::connect(&self.socket).await?;
            let payload = serde_json::to_vec(
                &serde_json::json!({"command":"checkpoint", "request":request}),
            )?;
            stream.write_u32(payload.len() as u32).await?;
            stream.write_all(&payload).await?;
            let size = stream.read_u32().await? as usize;
            if size == 0 || size > 4096 {
                return Err(invalid());
            }
            let mut bytes = vec![0; size];
            stream.read_exact(&mut bytes).await?;
            serde_json::from_slice(&bytes).map_err(Into::into)
        })
        .await
        .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "checkpoint deadline"))?
    }
    pub async fn checkpoint(&self, binding: CheckpointBinding) -> io::Result<()> {
        tokio::time::timeout(self.timeout, async {
            let entered = self.request(CheckpointRequest::Enter { binding }).await?;
            if !entered.ok
                || entered.state.binding != Some(binding)
                || entered.state.phase != CheckpointPhase::Entered
            {
                return Err(invalid());
            }
            loop {
                let reply = self.request(CheckpointRequest::Poll { binding }).await?;
                if !reply.ok || reply.state.binding != Some(binding) {
                    return Err(invalid());
                }
                match reply.state.phase {
                    CheckpointPhase::Released => return Ok(()),
                    CheckpointPhase::Entered => tokio::time::sleep(Duration::from_millis(5)).await,
                    _ => return Err(invalid()),
                }
            }
        })
        .await
        .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "checkpoint release deadline"))?
    }
}

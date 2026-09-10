//! Supervisor-owned barriers. A daemon restart always invalidates old releases.
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
}
impl Barrier {
    pub(crate) fn new(directory: &Path) -> io::Result<Self> {
        let path = directory.join("checkpoint.json");
        // Reject corrupt prior state; old state can never authorize a new process.
        if path.exists() {
            let metadata = fs::symlink_metadata(&path)?;
            if !metadata.is_file() || metadata.len() > 4096 {
                return Err(invalid());
            }
            let _: CheckpointState = serde_json::from_slice(&fs::read(&path)?)?;
        }
        let barrier = Self {
            state: CheckpointState {
                generation: Uuid::now_v7(),
                binding: None,
                phase: CheckpointPhase::Idle,
            },
            path,
            deadline: None,
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
        file.write_all(&serde_json::to_vec(&self.state)?)?;
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

//! Bounded observations of a supervisor-owned local fixture socket.
use super::{Effect, Reply, VmState};
use serde::Serialize;
use std::{io, path::PathBuf, time::Duration};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::UnixStream,
};
use uuid::Uuid;

const MAX_FRAME: usize = 4096;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FixtureStatus {
    pub attempts: usize,
    pub effects: usize,
}

/// Read-only protocol surface; filesystem ownership supplies the trust boundary.
/// Every call opens one connection, with a single deadline including connect.
#[derive(Debug, Clone)]
pub struct FixtureReadClient {
    socket: PathBuf,
    timeout: Duration,
}

#[derive(Serialize)]
#[serde(tag = "command", rename_all = "snake_case")]
enum Request<'a> {
    Status {},
    World {
        vmid: u32,
    },
    AcceptedEffect {
        operation: Uuid,
        request_sha256: &'a str,
    },
}

fn invalid() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        "invalid fixture observation reply",
    )
}

impl FixtureReadClient {
    pub fn new(socket: PathBuf, timeout: Duration) -> io::Result<Self> {
        if timeout.is_zero() || timeout > Duration::from_secs(10) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "fixture deadline must be within 10 seconds",
            ));
        }
        Ok(Self { socket, timeout })
    }

    async fn query(&self, request: Request<'_>) -> io::Result<Reply> {
        tokio::time::timeout(self.timeout, async {
            let payload = serde_json::to_vec(&request)?;
            let mut stream = UnixStream::connect(&self.socket).await?;
            stream
                .write_all(&(payload.len() as u32).to_be_bytes())
                .await?;
            stream.write_all(&payload).await?;
            let length = stream.read_u32().await? as usize;
            if length == 0 || length > MAX_FRAME {
                return Err(invalid());
            }
            let mut bytes = vec![0; length];
            stream.read_exact(&mut bytes).await?;
            let reply: Reply = serde_json::from_slice(&bytes).map_err(|_| invalid())?;
            if !reply.ok {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "fixture observation rejected",
                ));
            }
            if reply.duplicate.is_some() || reply.effects > reply.attempts {
                return Err(invalid());
            }
            match request {
                Request::Status {} if reply.vm.is_some() || reply.accepted_effect.is_some() => {
                    return Err(invalid());
                }
                Request::World { .. } if reply.accepted_effect.is_some() => return Err(invalid()),
                Request::AcceptedEffect {
                    operation,
                    request_sha256,
                } => {
                    if reply.vm.is_some()
                        || reply
                            .accepted_effect
                            .as_ref()
                            .is_some_and(|effect| !effect.matches(operation, request_sha256))
                    {
                        return Err(invalid());
                    }
                }
                _ => {}
            }
            Ok(reply)
        })
        .await
        .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "fixture observation deadline"))?
    }

    pub async fn status(&self) -> io::Result<FixtureStatus> {
        let reply = self.query(Request::Status {}).await?;
        Ok(FixtureStatus {
            attempts: reply.attempts,
            effects: reply.effects,
        })
    }

    /// Absence is a successful observation, distinct from transport/rejection errors.
    pub async fn world(&self, vmid: u32) -> io::Result<Option<VmState>> {
        if vmid == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "zero fixture VM ID",
            ));
        }
        Ok(self.query(Request::World { vmid }).await?.vm)
    }

    pub async fn accepted_effect(
        &self,
        operation: Uuid,
        request_sha256: &str,
    ) -> io::Result<Option<Effect>> {
        if operation.is_nil()
            || request_sha256.len() != 64
            || !request_sha256
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid fixture request identity",
            ));
        }
        Ok(self
            .query(Request::AcceptedEffect {
                operation,
                request_sha256,
            })
            .await?
            .accepted_effect)
    }
}

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
///
/// Observing a durable effect does not grant the sealed submission capability.
/// ```compile_fail
/// use pve_port::{fixture_support::FixtureReadClient, ProvisioningFakePort};
/// fn cannot_submit(client: &FixtureReadClient) {
///     let _: &dyn ProvisioningFakePort = client;
/// }
/// ```
/// Nor does it provide the controller's dispatch checkpoint capability.
/// ```compile_fail
/// use pve_port::{fixture_support::FixtureReadClient, fixture_ipc::ControllerFixturePort};
/// fn cannot_drive_controller(client: &FixtureReadClient) {
///     let _: &dyn ControllerFixturePort = client;
/// }
/// ```
/// The partial world snapshot cannot stand in for the preflight inventory.
/// ```compile_fail
/// use pve_port::{fixture_support::FixtureReadClient, PvePreflightReadPort};
/// fn cannot_certify_absence(client: &FixtureReadClient) {
///     let _: &dyn PvePreflightReadPort = client;
/// }
/// ```
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
    /// Version two reads bind stable collection identity before an attempt exists.
    pub async fn provisioning_reads_v2(
        &self,
        identity: &super::FixtureReadIdentity,
    ) -> io::Result<Option<super::FixtureProvisioningReadsV2>> {
        identity.validate()?;
        tokio::time::timeout(self.timeout, async {
            let payload = serde_json::to_vec(
                &serde_json::json!({"command":"provisioning_reads_v2","identity":identity}),
            )?;
            let mut stream = UnixStream::connect(&self.socket).await?;
            stream.write_u32(payload.len() as u32).await?;
            stream.write_all(&payload).await?;
            let length = stream.read_u32().await? as usize;
            if length == 0 || length > super::provisioning_reads::MAX_PROVISIONING_READ_BYTES {
                return Err(invalid());
            }
            let mut bytes = vec![0; length];
            stream.read_exact(&mut bytes).await?;
            if bytes == b"null" {
                return Ok(None);
            }
            super::FixtureProvisioningReadsV2::decode_v2(&bytes, identity).map(Some)
        })
        .await
        .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "fixture provisioning v2 deadline"))?
    }
    /// Reads immutable supervisor facts bound to the complete expected request identity.
    pub async fn provisioning_reads(
        &self,
        identity: &super::FixtureProvisioningIdentity,
    ) -> io::Result<Option<super::FixtureProvisioningReads>> {
        identity.validate()?;
        tokio::time::timeout(self.timeout, async {
            let payload = serde_json::to_vec(
                &serde_json::json!({"command":"provisioning_reads","identity":identity}),
            )?;
            let mut stream = UnixStream::connect(&self.socket).await?;
            stream.write_u32(payload.len() as u32).await?;
            stream.write_all(&payload).await?;
            let length = stream.read_u32().await? as usize;
            if length == 0 || length > super::provisioning_reads::MAX_PROVISIONING_READ_BYTES {
                return Err(invalid());
            }
            let mut bytes = vec![0; length];
            stream.read_exact(&mut bytes).await?;
            if bytes == b"null" {
                return Ok(None);
            }
            super::FixtureProvisioningReads::decode(&bytes, identity).map(Some)
        })
        .await
        .map_err(|_| {
            io::Error::new(
                io::ErrorKind::TimedOut,
                "fixture provisioning reads deadline",
            )
        })?
    }
    /// Returns historical supervisor facts, or `None` when the seed is unavailable.
    /// The caller must supply the expected fixture identity; times are never refreshed.
    pub async fn clone_reads(
        &self,
        fixture_id: Uuid,
    ) -> io::Result<Option<super::FixtureCloneReads>> {
        if fixture_id.is_nil() {
            return Err(invalid());
        }
        tokio::time::timeout(self.timeout, async {
            let payload = serde_json::to_vec(
                &serde_json::json!({"command":"clone_reads", "fixture_id":fixture_id}),
            )?;
            let mut stream = UnixStream::connect(&self.socket).await?;
            stream.write_u32(payload.len() as u32).await?;
            stream.write_all(&payload).await?;
            let length = stream.read_u32().await? as usize;
            if length == 0 || length > super::clone_reads::MAX_CLONE_READ_BYTES {
                return Err(invalid());
            }
            let mut bytes = vec![0; length];
            stream.read_exact(&mut bytes).await?;
            if bytes == b"null" {
                return Ok(None);
            }
            super::FixtureCloneReads::decode(&bytes, fixture_id).map(Some)
        })
        .await
        .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "fixture Clone reads deadline"))?
    }
    /// Reads an explicitly recorded historical task observation. Missing or
    /// mismatched records are errors, never inferred absence or success.
    pub async fn task(
        &self,
        identity: &super::FixtureTaskIdentity,
    ) -> io::Result<super::FixtureTaskObservation> {
        identity.validate()?;
        tokio::time::timeout(self.timeout, async {
            let payload =
                serde_json::to_vec(&serde_json::json!({"command":"task", "identity":identity}))?;
            let mut stream = UnixStream::connect(&self.socket).await?;
            stream.write_u32(payload.len() as u32).await?;
            stream.write_all(&payload).await?;
            let length = stream.read_u32().await? as usize;
            if length == 0 || length > super::task::MAX_TASK_BYTES {
                return Err(invalid());
            }
            let mut bytes = vec![0; length];
            stream.read_exact(&mut bytes).await?;
            let observation = super::FixtureTaskObservation::decode(&bytes)?;
            if observation.identity != *identity {
                return Err(invalid());
            }
            Ok(observation)
        })
        .await
        .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "fixture task deadline"))?
    }
    /// Reads daemon-owned startup inventory. An empty inventory proves absence
    /// only at its recorded observation time; unavailable proves nothing.
    pub async fn snapshot(&self) -> io::Result<super::FixtureSnapshot> {
        tokio::time::timeout(self.timeout, async {
            let mut stream = UnixStream::connect(&self.socket).await?;
            let payload = br#"{"command":"snapshot"}"#;
            stream.write_u32(payload.len() as u32).await?;
            stream.write_all(payload).await?;
            let length = stream.read_u32().await? as usize;
            if length == 0 || length > super::fixture_daemon::snapshot::MAX_SNAPSHOT {
                return Err(invalid());
            }
            let mut bytes = vec![0; length];
            stream.read_exact(&mut bytes).await?;
            super::FixtureSnapshot::decode(&bytes)
        })
        .await
        .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "fixture snapshot deadline"))?
    }
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

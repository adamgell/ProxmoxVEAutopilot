//! Exact-request supervisor authorization for a local synthetic Clone effect.
use super::{VmState, durable_fixture_log::FixtureLog};
use crate::{
    Upid,
    fixture_ipc::{FixtureCloneReceipt, FixtureCloneRequest},
};
use serde::{Deserialize, Serialize};
use std::{
    io,
    path::{Path, PathBuf},
    time::Duration,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

pub(crate) const MAX_CLONE_FRAME: usize = 65_536;

/// Supervisor-created seed. The complete request is pinned, including before
/// observations and generation fence. This is synthetic authorization, not a
/// claim that the daemon can discover real Proxmox configuration.
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureCloneSeed {
    version: u8,
    request: Vec<u8>,
    after: VmState,
}

fn invalid() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "invalid fixture clone")
}

impl FixtureCloneSeed {
    pub fn new(request: &FixtureCloneRequest, after: VmState) -> io::Result<Self> {
        if after.disk_bytes == 0 || after.pe_configured {
            return Err(invalid());
        }
        Ok(Self {
            version: 1,
            request: request.encode().map_err(|_| invalid())?,
            after,
        })
    }
    pub fn encode(&self) -> io::Result<Vec<u8>> {
        Ok(serde_json::to_vec(self)?)
    }
    pub(crate) fn load(path: &Path) -> io::Result<Option<Self>> {
        use std::io::Read;
        let file = match std::fs::File::open(path) {
            Ok(file) => file,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(e),
        };
        let mut bytes = Vec::new();
        file.take((MAX_CLONE_FRAME + 1) as u64)
            .read_to_end(&mut bytes)?;
        if bytes.len() > MAX_CLONE_FRAME {
            return Err(invalid());
        }
        let seed: Self = serde_json::from_slice(&bytes)?;
        let request = FixtureCloneRequest::decode(&seed.request).map_err(|_| invalid())?;
        if seed.version != 1 || Self::new(&request, seed.after.clone())?.request != seed.request {
            return Err(invalid());
        }
        Ok(Some(seed))
    }
    pub(crate) fn submit(&self, bytes: &[u8], log: &mut FixtureLog) -> io::Result<Vec<u8>> {
        let request = FixtureCloneRequest::decode(bytes).map_err(|_| invalid())?;
        let operation = request.request().binding().operation_id().as_uuid();
        let duplicate = log.record_attempt(operation, &request.request_sha256())?;
        if duplicate || request.encode().map_err(|_| invalid())? != self.request {
            return Err(invalid());
        }
        let vm = request.request().clone_request().vm();
        let sequence = log.records().len() as u64;
        let upid = Upid::parse(format!(
            "UPID:{}:{:08X}:00000001:00000001:qmclone:{}:fake@pve:",
            vm.node(),
            sequence,
            vm.source_vmid()
        ))
        .map_err(|_| invalid())?;
        let receipt = request
            .encode_receipt(sequence, upid)
            .map_err(|_| invalid())?;
        log.record_effect_receipt(
            sequence,
            vm.target_vmid().get(),
            None,
            self.after.clone(),
            Some(receipt.clone()),
        )?;
        Ok(receipt)
    }
}

/// Explicit local mutation client. It has no production provisioning trait.
pub struct FixtureMutationClient {
    socket: PathBuf,
    timeout: Duration,
}
impl FixtureMutationClient {
    /// Reserved three-stage transport. The daemon rejects this command until
    /// stage-scoped durable authorization and attempt identity are implemented.
    /// A valid message does not grant permission to submit a mutation.
    pub async fn stage_late(
        &self,
        binding: super::CheckpointBinding,
        request: &crate::fixture_ipc::FixtureStageRequest,
    ) -> io::Result<crate::fixture_ipc::FixtureStageReceipt> {
        tokio::time::timeout(self.timeout, async {
            let payload = serde_json::to_vec(&serde_json::json!({
                "command": "stage_late",
                "binding": binding,
                "request": serde_json::from_slice::<serde_json::Value>(
                    &request.encode().map_err(|_| invalid())?
                )?,
            }))?;
            if payload.len() > MAX_CLONE_FRAME {
                return Err(invalid());
            }
            let mut stream = tokio::net::UnixStream::connect(&self.socket).await?;
            stream.write_u32(payload.len() as u32).await?;
            stream.write_all(&payload).await?;
            let length = stream.read_u32().await? as usize;
            if length == 0 || length > MAX_CLONE_FRAME {
                return Err(invalid());
            }
            let mut bytes = vec![0; length];
            stream.read_exact(&mut bytes).await?;
            request.decode_receipt(&bytes).map_err(|_| invalid())
        })
        .await
        .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "fixture stage deadline"))?
    }
    pub fn new(socket: PathBuf, timeout: Duration) -> io::Result<Self> {
        if timeout.is_zero() || timeout > Duration::from_secs(5) {
            return Err(invalid());
        }
        Ok(Self { socket, timeout })
    }
    pub async fn clone_vm(&self, request: &FixtureCloneRequest) -> io::Result<FixtureCloneReceipt> {
        self.send_clone(request, None).await
    }
    pub async fn clone_vm_late(
        &self,
        binding: super::CheckpointBinding,
        request: &FixtureCloneRequest,
    ) -> io::Result<FixtureCloneReceipt> {
        self.send_clone(request, Some(binding)).await
    }
    async fn send_clone(
        &self,
        request: &FixtureCloneRequest,
        binding: Option<super::CheckpointBinding>,
    ) -> io::Result<FixtureCloneReceipt> {
        tokio::time::timeout(self.timeout, async {
            let request_bytes = request.encode().map_err(|_| invalid())?;
            let mut message = serde_json::json!({"command":"clone", "request": serde_json::from_slice::<serde_json::Value>(&request_bytes)?});
            if let Some(binding) = binding {
                message["command"] = serde_json::json!("clone_late");
                message["binding"] = serde_json::to_value(binding)?;
            }
            let payload = serde_json::to_vec(&message)?;
            if payload.len() > MAX_CLONE_FRAME { return Err(invalid()); }
            let mut stream = tokio::net::UnixStream::connect(&self.socket).await?;
            stream.write_u32(payload.len() as u32).await?;
            stream.write_all(&payload).await?;
            let length = stream.read_u32().await? as usize;
            if length == 0 || length > MAX_CLONE_FRAME { return Err(invalid()); }
            let mut bytes = vec![0; length];
            stream.read_exact(&mut bytes).await?;
            request.decode_receipt(&bytes).map_err(|_| invalid())
        }).await.map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "fixture clone deadline"))?
    }
}

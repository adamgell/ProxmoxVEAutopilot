//! Local fixture daemon. Separate sockets separate protocol capabilities;
//! filesystem ownership is the trust boundary, not an authentication claim.
use super::durable_fixture_log::{Effect, FixtureLog, VmState};
use serde::{Deserialize, Serialize};
use std::{
    fs,
    io::{self, Read, Write},
    os::unix::{
        fs::PermissionsExt,
        net::{UnixListener, UnixStream},
    },
    path::Path,
    thread,
    time::{Duration, Instant},
};
use uuid::Uuid;

#[path = "snapshot.rs"]
pub mod snapshot;

const MAX_REQUEST: usize = 65_536;
const IO_BOUND: Duration = Duration::from_millis(100);

#[derive(Deserialize)]
#[serde(tag = "command", rename_all = "snake_case", deny_unknown_fields)]
enum ClientRequest {
    #[cfg(feature = "fixture-ipc")]
    ProvisioningReads {
        identity: super::FixtureProvisioningIdentity,
    },
    #[cfg(feature = "fixture-ipc")]
    CloneReads {
        fixture_id: Uuid,
    },
    #[cfg(feature = "fixture-ipc")]
    Clone {
        request: serde_json::Value,
    },
    #[cfg(feature = "fixture-ipc")]
    Task {
        identity: super::FixtureTaskIdentity,
    },
    Snapshot {},
    Status {},
    AcceptedEffect {
        operation: Uuid,
        request_sha256: String,
    },
    World {
        vmid: u32,
    },
    Effect {
        attempt_sequence: u64,
        vmid: u32,
        before: Option<VmState>,
        after: VmState,
    },
    Attempt {
        operation: Uuid,
        request_sha256: String,
    },
}

#[derive(Deserialize)]
#[serde(tag = "command", rename_all = "snake_case", deny_unknown_fields)]
enum SupervisorRequest {
    Shutdown {},
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Reply {
    pub ok: bool,
    pub attempts: usize,
    pub duplicate: Option<bool>,
    pub effects: usize,
    pub vm: Option<VmState>,
    pub accepted_effect: Option<Effect>,
}

fn read_frame(stream: &mut UnixStream) -> io::Result<Vec<u8>> {
    let deadline = Instant::now() + IO_BOUND;
    stream.set_write_timeout(Some(IO_BOUND))?;
    let mut header = [0; 4];
    read_bounded(stream, &mut header, deadline)?;
    let length = u32::from_be_bytes(header) as usize;
    if length == 0 || length > MAX_REQUEST {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "invalid request length",
        ));
    }
    let mut payload = vec![0; length];
    read_bounded(stream, &mut payload, deadline)?;
    Ok(payload)
}

fn read_bounded(
    stream: &mut UnixStream,
    mut bytes: &mut [u8],
    deadline: Instant,
) -> io::Result<()> {
    while !bytes.is_empty() {
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .filter(|d| !d.is_zero())
            .ok_or_else(|| io::Error::new(io::ErrorKind::TimedOut, "fixture request deadline"))?;
        stream.set_read_timeout(Some(remaining))?;
        let count = stream.read(bytes)?;
        if count == 0 {
            return Err(io::Error::from(io::ErrorKind::UnexpectedEof));
        }
        bytes = &mut bytes[count..];
    }
    Ok(())
}

fn reply(stream: &mut UnixStream, response: &Reply) -> io::Result<()> {
    let bytes = serde_json::to_vec(response)?;
    stream.write_all(&(bytes.len() as u32).to_be_bytes())?;
    stream.write_all(&bytes)
}

/// One bounded request per connection. Invalid framing is closed without reply.
/// The supervisor supplies an existing private directory and finite lifetime.
pub fn run(directory: &Path, lifetime: Duration) -> io::Result<()> {
    let metadata = fs::symlink_metadata(directory)?;
    if !metadata.is_dir()
        || metadata.permissions().mode() & 0o077 != 0
        || lifetime > Duration::from_secs(10)
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "private fixture directory and bounded lifetime required",
        ));
    }
    let ledger = directory.join("fixture.log");
    #[cfg(feature = "fixture-ipc")]
    let provisioning_reads =
        super::FixtureProvisioningReads::load_startup(&directory.join("provisioning_reads.json"))?;
    #[cfg(feature = "fixture-ipc")]
    let clone_reads = super::FixtureCloneReads::load_startup(&directory.join("clone_reads.json"))?;
    #[cfg(feature = "fixture-ipc")]
    let clone_seed = super::clone_mutation::FixtureCloneSeed::load(&directory.join("clone.json"))?;
    let inventory = snapshot::FixtureSnapshot::load(&directory.join("inventory.json"))?;
    #[cfg(feature = "fixture-ipc")]
    let task = super::FixtureTaskObservation::load(&directory.join("task.json"))?;
    let mut log = if ledger.exists() {
        FixtureLog::recover(&ledger)?
    } else {
        FixtureLog::create(&ledger)?
    };
    let client = UnixListener::bind(directory.join("client.sock"))?;
    let control = UnixListener::bind(directory.join("supervisor.sock"))?;
    client.set_nonblocking(true)?;
    control.set_nonblocking(true)?;
    let deadline = Instant::now() + lifetime;
    while Instant::now() < deadline {
        for (listener, supervisor) in [(&control, true), (&client, false)] {
            let mut stream = match listener.accept() {
                Ok((stream, _)) => stream,
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => continue,
                Err(e) => return Err(e),
            };
            // Accepted sockets inherit O_NONBLOCK on macOS. Framing uses
            // blocking reads with a deadline, so normalize the stream mode
            // before a client can race its payload against our first read.
            stream.set_nonblocking(false)?;
            let Ok(bytes) = read_frame(&mut stream) else {
                continue;
            };
            let mut response = Reply {
                ok: false,
                attempts: log.records().len(),
                duplicate: None,
                effects: log.effects().len(),
                vm: None,
                accepted_effect: None,
            };
            let mut shutdown = false;
            if supervisor {
                if serde_json::from_slice::<SupervisorRequest>(&bytes).is_ok() {
                    response.ok = true;
                    shutdown = true;
                }
            } else {
                match serde_json::from_slice::<ClientRequest>(&bytes) {
                    #[cfg(feature = "fixture-ipc")]
                    Ok(ClientRequest::ProvisioningReads { identity }) => {
                        if identity.validate().is_ok()
                            && provisioning_reads
                                .as_ref()
                                .is_none_or(|seed| seed.identity == identity)
                        {
                            let payload = serde_json::to_vec(&provisioning_reads)?;
                            let _ = stream
                                .write_all(&(payload.len() as u32).to_be_bytes())
                                .and_then(|()| stream.write_all(&payload));
                            continue;
                        }
                    }
                    #[cfg(feature = "fixture-ipc")]
                    Ok(ClientRequest::CloneReads { fixture_id }) => {
                        if !fixture_id.is_nil()
                            && clone_reads
                                .as_ref()
                                .is_none_or(|seed| seed.fixture_id == fixture_id)
                        {
                            let payload = serde_json::to_vec(&clone_reads)?;
                            let _ = stream
                                .write_all(&(payload.len() as u32).to_be_bytes())
                                .and_then(|()| stream.write_all(&payload));
                            continue;
                        }
                    }
                    #[cfg(feature = "fixture-ipc")]
                    Ok(ClientRequest::Clone { request }) => {
                        if let Some(seed) = &clone_seed {
                            match seed.submit(&serde_json::to_vec(&request)?, &mut log) {
                                Ok(payload) => {
                                    let _ = stream
                                        .write_all(&(payload.len() as u32).to_be_bytes())
                                        .and_then(|()| stream.write_all(&payload));
                                    continue;
                                }
                                Err(e) if e.kind() == io::ErrorKind::InvalidData => {}
                                Err(e) => return Err(e),
                            }
                        }
                    }
                    #[cfg(feature = "fixture-ipc")]
                    Ok(ClientRequest::Task { identity }) => {
                        if identity.validate().is_ok()
                            && let Some(observation) =
                                task.as_ref().filter(|task| task.identity == identity)
                        {
                            let payload = serde_json::to_vec(observation)?;
                            let _ = stream
                                .write_all(&(payload.len() as u32).to_be_bytes())
                                .and_then(|()| stream.write_all(&payload));
                            continue;
                        }
                    }
                    Ok(ClientRequest::Snapshot {}) => {
                        let payload = serde_json::to_vec(&inventory)?;
                        let _ = stream
                            .write_all(&(payload.len() as u32).to_be_bytes())
                            .and_then(|()| stream.write_all(&payload));
                        continue;
                    }
                    Ok(ClientRequest::Status {}) => response.ok = true,
                    Ok(ClientRequest::AcceptedEffect {
                        operation,
                        request_sha256,
                    }) => {
                        if let Ok(effect) = log.accepted_effect(operation, &request_sha256) {
                            response.ok = true;
                            response.accepted_effect = effect.cloned();
                        }
                    }
                    Ok(ClientRequest::World { vmid }) => {
                        response.ok = vmid != 0;
                        response.vm = log.world().get(&vmid).cloned();
                    }
                    Ok(ClientRequest::Effect {
                        attempt_sequence,
                        vmid,
                        before,
                        after,
                    }) => {
                        match log.record_effect(attempt_sequence, vmid, before, after) {
                            Ok(()) => {
                                response.ok = true;
                                response.effects = log.effects().len();
                                response.vm = log.world().get(&vmid).cloned();
                            }
                            // A rejected precondition has not written anything. Storage
                            // errors retain the poisoned-writer shutdown behavior.
                            Err(e) if e.kind() == io::ErrorKind::InvalidData => {}
                            Err(e) => return Err(e),
                        }
                    }
                    Ok(ClientRequest::Attempt {
                        operation,
                        request_sha256,
                    }) => {
                        // Any log error stops the daemon rather than accepting further writes.
                        response.duplicate = Some(log.record_attempt(operation, &request_sha256)?);
                        response.attempts = log.records().len();
                        response.ok = true;
                    }
                    Err(_) => {}
                }
            }
            // Response loss cannot undo a synced ledger append.
            let _ = reply(&mut stream, &response);
            if shutdown {
                return Ok(());
            }
        }
        thread::sleep(Duration::from_millis(2));
    }
    Ok(())
}

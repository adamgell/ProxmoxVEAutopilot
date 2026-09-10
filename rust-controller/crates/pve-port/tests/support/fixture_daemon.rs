//! Local test daemon skeleton. Separate sockets separate protocol capabilities;
//! filesystem ownership is the trust boundary, not an authentication claim.
use super::durable_fixture_log::{FixtureLog, VmState};
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

const MAX_REQUEST: usize = 1024;
const IO_BOUND: Duration = Duration::from_millis(100);

#[derive(Deserialize)]
#[serde(tag = "command", rename_all = "snake_case", deny_unknown_fields)]
enum ClientRequest {
    Status {},
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
            let Ok(bytes) = read_frame(&mut stream) else {
                continue;
            };
            let mut response = Reply {
                ok: false,
                attempts: log.records().len(),
                duplicate: None,
                effects: log.effects().len(),
                vm: None,
            };
            let mut shutdown = false;
            if supervisor {
                if serde_json::from_slice::<SupervisorRequest>(&bytes).is_ok() {
                    response.ok = true;
                    shutdown = true;
                }
            } else {
                match serde_json::from_slice::<ClientRequest>(&bytes) {
                    Ok(ClientRequest::Status {}) => response.ok = true,
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

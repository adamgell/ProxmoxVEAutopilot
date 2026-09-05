//! Private Unix subprocess lifecycle for this fixed local proof.
use std::{
    io::{self, Read},
    os::{fd::OwnedFd, unix::net::UnixStream},
    process::{Child, Command, Output, Stdio},
    time::{Duration, Instant},
};

const OUTPUT_CAP: usize = 8192;
const POLL_INTERVAL: Duration = Duration::from_millis(10);
pub(super) const REAP_GRACE: Duration = Duration::from_millis(200);
const COMMAND_BOUND: Duration = Duration::from_secs(3);

struct ManagedChild {
    child: Child,
    stdout: UnixStream,
    captured: Vec<u8>,
    output_cap: usize,
    reaped: bool,
    cleanup_deadline: Option<Instant>,
}
impl ManagedChild {
    #[cfg(test)]
    fn spawn_logs(command: Command) -> io::Result<Self> {
        Self::spawn_captured(command, true, 262_144)
    }
    fn spawn(command: Command) -> io::Result<Self> {
        Self::spawn_captured(command, false, OUTPUT_CAP)
    }
    fn spawn_captured(
        mut command: Command,
        combine_stderr: bool,
        output_cap: usize,
    ) -> io::Result<Self> {
        let (stdout, writer) = UnixStream::pair()?;
        stdout.set_nonblocking(true)?;
        let stderr = if combine_stderr {
            Stdio::from(OwnedFd::from(writer.try_clone()?))
        } else {
            Stdio::null()
        };
        let writer: OwnedFd = writer.into();
        let child = command
            .stdin(Stdio::null())
            .stdout(Stdio::from(writer))
            .stderr(stderr)
            .spawn()?;
        Ok(Self {
            child,
            stdout,
            captured: Vec::new(),
            output_cap,
            reaped: false,
            cleanup_deadline: None,
        })
    }
    fn drain(&mut self) -> io::Result<()> {
        let mut buffer = [0; 1024];
        loop {
            match self.stdout.read(&mut buffer) {
                Ok(0) => return Ok(()),
                Ok(count) => {
                    if self.captured.len() + count > self.output_cap {
                        return Err(io::Error::other("local_output_limit"));
                    }
                    self.captured.extend_from_slice(&buffer[..count]);
                }
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => return Ok(()),
                Err(e) => return Err(e),
            }
        }
    }
    fn poll(&mut self) -> io::Result<Option<Output>> {
        self.drain()?;
        if let Some(status) = self.child.try_wait()? {
            self.reaped = true;
            self.drain()?;
            return Ok(Some(Output {
                status,
                stdout: std::mem::take(&mut self.captured),
                stderr: vec![],
            }));
        }
        Ok(None)
    }
    async fn output(mut self, bound: Duration) -> io::Result<Output> {
        let deadline = Instant::now() + bound;
        loop {
            if Instant::now() >= deadline {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "local_process_timeout",
                ));
            }
            if let Some(output) = self.poll()? {
                return Ok(output);
            }
            tokio::time::sleep(
                POLL_INTERVAL.min(deadline.saturating_duration_since(Instant::now())),
            )
            .await;
        }
    }
    fn output_blocking(mut self, deadline: Instant) -> io::Result<Output> {
        // Reserve kill/reap time inside the caller's ONE cleanup budget.
        self.cleanup_deadline = Some(deadline);
        let work_deadline = deadline.checked_sub(REAP_GRACE).unwrap_or(deadline);
        loop {
            if Instant::now() >= work_deadline {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "local_cleanup_timeout",
                ));
            }
            if let Some(output) = self.poll()? {
                return Ok(output);
            }
            std::thread::sleep(
                POLL_INTERVAL.min(work_deadline.saturating_duration_since(Instant::now())),
            );
        }
    }
    #[cfg(test)]
    fn test_sleep() -> Self {
        let mut command = Command::new("/bin/sleep");
        command.arg("1");
        Self::spawn(command).unwrap()
    }
    #[cfg(test)]
    fn id(&self) -> u32 {
        self.child.id()
    }
}
impl Drop for ManagedChild {
    fn drop(&mut self) {
        if self.reaped {
            return;
        }
        let deadline = self
            .cleanup_deadline
            .unwrap_or_else(|| Instant::now() + REAP_GRACE);
        let _ = self.child.kill();
        loop {
            if matches!(self.child.try_wait(), Ok(Some(_))) {
                self.reaped = true;
                return;
            }
            if Instant::now() >= deadline {
                eprintln!("native_fake_child_reap_unconfirmed");
                return;
            }
            std::thread::sleep(
                POLL_INTERVAL.min(deadline.saturating_duration_since(Instant::now())),
            );
        }
    }
}
pub(super) async fn docker(args: &[&str]) -> io::Result<Output> {
    let mut command = Command::new("docker");
    command.args(args);
    ManagedChild::spawn(command)?.output(COMMAND_BOUND).await
}
#[cfg(test)]
#[allow(
    dead_code,
    reason = "only the authenticated service fixture audits complete owned logs"
)]
pub(super) async fn docker_logs(endpoint: &str, id: &str, bound: Duration) -> io::Result<Output> {
    let mut command = Command::new("docker");
    command.args(["--host", endpoint, "logs", id]);
    ManagedChild::spawn_logs(command)?.output(bound).await
}
pub(super) fn docker_cleanup(args: &[&str], deadline: Instant) -> io::Result<Output> {
    if deadline.saturating_duration_since(Instant::now()) <= REAP_GRACE {
        return Err(io::Error::new(
            io::ErrorKind::TimedOut,
            "local_cleanup_timeout",
        ));
    }
    let mut command = Command::new("docker");
    command.args(args);
    ManagedChild::spawn(command)?.output_blocking(deadline)
}

#[cfg(test)]
pub(super) enum LinuxCommand {
    Admit,
    Create,
    Cleanup,
}
#[cfg(test)]
fn linux_command(op: LinuxCommand, request: &str, deadline: Instant) -> io::Result<Command> {
    let work = deadline
        .saturating_duration_since(Instant::now())
        .saturating_sub(REAP_GRACE);
    let budget_ms = work.as_millis().min(3000);
    if budget_ms == 0 {
        return Err(io::Error::new(
            io::ErrorKind::TimedOut,
            "local_cleanup_timeout",
        ));
    }
    let mut value = super::linux_postgres::canonical(request.as_bytes())
        .map_err(|_| io::Error::other("local_linux_request_invalid"))?;
    let object = value
        .as_object_mut()
        .ok_or_else(|| io::Error::other("local_linux_request_invalid"))?;
    if object.contains_key("budget_ms") {
        return Err(io::Error::other("local_linux_request_invalid"));
    }
    object.insert(
        "budget_ms".into(),
        serde_json::Value::from(budget_ms as u64),
    );
    let op = match op {
        LinuxCommand::Admit => "admit",
        LinuxCommand::Create => "create",
        LinuxCommand::Cleanup => "cleanup",
    };
    let mut command = Command::new("/usr/bin/python3");
    command.env_clear().args([
        "-I",
        "-B",
        "-c",
        include_str!("linux_postgres.py"),
        op,
        &value.to_string(),
    ]);
    Ok(command)
}
#[cfg(test)]
pub(super) async fn linux_python(
    op: LinuxCommand,
    request: &str,
    bound: Duration,
) -> io::Result<Output> {
    let deadline = Instant::now() + bound.min(COMMAND_BOUND);
    let command = linux_command(op, request, deadline)?;
    let mut child = ManagedChild::spawn(command)?;
    child.cleanup_deadline = Some(deadline);
    child
        .output(
            deadline
                .saturating_duration_since(Instant::now())
                .saturating_sub(REAP_GRACE),
        )
        .await
}
#[cfg(test)]
pub(super) fn linux_python_cleanup(request: &str, deadline: Instant) -> io::Result<Output> {
    let command = linux_command(LinuxCommand::Cleanup, request, deadline)?;
    ManagedChild::spawn(command)?.output_blocking(deadline)
}

// Closed failure points exist only in owned Linux tests, never protocol argv.
#[cfg(all(test, target_os = "linux"))]
pub(super) enum LinuxFault {
    BeforeCreate,
    Unmarked,
    Stamped,
    Locked,
}
#[cfg(all(test, target_os = "linux"))]
pub(super) struct FaultChild {
    child: ManagedChild,
    deadline: Instant,
    stage: &'static str,
}
#[cfg(all(test, target_os = "linux"))]
pub(super) fn linux_fault(request: &str, fault: LinuxFault) -> io::Result<FaultChild> {
    let deadline = Instant::now() + COMMAND_BOUND;
    let stage = match fault {
        LinuxFault::BeforeCreate => "before_create",
        LinuxFault::Unmarked => "unmarked",
        LinuxFault::Stamped => "stamped",
        LinuxFault::Locked => "locked",
    };
    let standard = linux_command(LinuxCommand::Create, request, deadline)?;
    let encoded = standard
        .get_args()
        .last()
        .ok_or_else(|| io::Error::other("local_linux_request_invalid"))?;
    // The normal program is imported without invoking its entry point. All its
    // receipt/instance validation still runs before any selected checkpoint.
    let source = format!(
        "__name__ = 'owned_fixture_fault'\n{}\ntry:\n    value = request('create', sys.argv[2])\n    work_deadline = time.monotonic() + value['budget_ms'] / 1000\n    run('create', value, lambda stage: _fault_checkpoint(stage, '{stage}', work_deadline))\nexcept Exception:\n    print('local_linux_fixture_refused', file=sys.stderr)\n    sys.exit(1)\n",
        include_str!("linux_postgres.py")
    );
    let mut command = Command::new("/usr/bin/python3");
    command.env_clear().args([
        std::ffi::OsStr::new("-I"),
        std::ffi::OsStr::new("-B"),
        std::ffi::OsStr::new("-c"),
        std::ffi::OsStr::new(&source),
        std::ffi::OsStr::new("create"),
        encoded,
    ]);
    let mut child = ManagedChild::spawn(command)?;
    child.cleanup_deadline = Some(deadline);
    Ok(FaultChild {
        child,
        deadline,
        stage,
    })
}
#[cfg(all(test, target_os = "linux"))]
impl FaultChild {
    pub(super) fn id(&self) -> u32 {
        self.child.id()
    }
    pub(super) async fn ready(&mut self) -> io::Result<()> {
        let expected = format!("{{\"stage\":\"{}\",\"version\":1}}\n", self.stage);
        loop {
            if Instant::now() + REAP_GRACE >= self.deadline {
                return Err(io::Error::other("local_linux_fault_timeout"));
            }
            if self.child.poll()?.is_some() {
                return Err(io::Error::other("local_linux_fault_early_exit"));
            }
            if self.child.captured == expected.as_bytes() {
                return Ok(());
            }
            tokio::time::sleep(POLL_INTERVAL).await;
        }
    }
}

#[cfg(test)]
pub(super) enum TestCleanupCommand {
    Inspect(String),
    Missing,
    Hang,
    Remove,
}
#[cfg(test)]
pub(super) fn scripted_cleanup(
    command: TestCleanupCommand,
    deadline: Instant,
    pids: &std::sync::Mutex<Vec<u32>>,
) -> io::Result<Output> {
    let mut command = match command {
        TestCleanupCommand::Inspect(text) => {
            let mut c = Command::new("/bin/echo");
            c.arg(text);
            c
        }
        TestCleanupCommand::Missing => Command::new("/usr/bin/false"),
        TestCleanupCommand::Hang => {
            let mut c = Command::new("/bin/sleep");
            c.arg("60");
            c
        }
        TestCleanupCommand::Remove => Command::new("/usr/bin/true"),
    };
    command.env_clear();
    let child = ManagedChild::spawn(command)?;
    pids.lock().unwrap().push(child.id());
    child.output_blocking(deadline)
}
#[cfg(test)]
pub(super) fn assert_child_reaped(pid: u32) {
    let mut command = Command::new("/bin/ps");
    command.args(["-p", &pid.to_string(), "-o", "pid="]);
    let found = ManagedChild::spawn(command)
        .unwrap()
        .output_blocking(Instant::now() + Duration::from_millis(500))
        .unwrap();
    assert!(
        !found.status.success(),
        "owned child must be absent, including zombies"
    );
}
#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Instant;

    #[tokio::test(flavor = "current_thread")]
    async fn linux_invocation_rejects_timeout_widening_and_invalid_protocol_without_admission() {
        for op in [
            LinuxCommand::Admit,
            LinuxCommand::Create,
            LinuxCommand::Cleanup,
        ] {
            let output = linux_python(op, r#"{"version":2}"#, Duration::from_secs(3))
                .await
                .unwrap();
            assert!(!output.status.success());
            assert!(output.stdout.is_empty());
        }
        assert!(
            linux_python(
                LinuxCommand::Admit,
                r#"{"budget_ms":3000,"version":1}"#,
                Duration::from_secs(3)
            )
            .await
            .is_err()
        );
        assert!(
            linux_python(LinuxCommand::Admit, r#"{"version":1}"#, REAP_GRACE)
                .await
                .is_err()
        );
        assert!(linux_python_cleanup(r#"{"version":1}"#, Instant::now()).is_err());
        let command = linux_command(
            LinuxCommand::Admit,
            r#"{"version":1}"#,
            Instant::now() + Duration::from_millis(600),
        )
        .unwrap();
        let args: Vec<_> = command.get_args().collect();
        assert_eq!(command.get_program(), "/usr/bin/python3");
        assert_eq!(args[0], "-I");
        assert_eq!(args[1], "-B");
        assert_eq!(args[2], "-c");
        assert_eq!(args[4], "admit");
        let request: serde_json::Value = serde_json::from_str(args[5].to_str().unwrap()).unwrap();
        assert!((1..=400).contains(&request["budget_ms"].as_u64().unwrap()));
    }

    #[tokio::test(flavor = "current_thread")]
    async fn owned_log_capture_combines_streams_and_bounds_overflow_and_cancellation() {
        let mut command = Command::new("/usr/bin/python3");
        command.args(["-c", "import sys; sys.stdout.write('stdout-canary'); sys.stdout.flush(); sys.stderr.write('stderr-canary')"]);
        let child = ManagedChild::spawn_logs(command).unwrap();
        let pid = child.id();
        let result = child.output(Duration::from_secs(2)).await.unwrap();
        assert!(result.status.success());
        assert_eq!(
            String::from_utf8(result.stdout).unwrap(),
            "stdout-canarystderr-canary"
        );
        assert_reaped(pid);

        let mut command = Command::new("/usr/bin/python3");
        command.args(["-c", "import sys; sys.stderr.write('x' * 262145)"]);
        let child = ManagedChild::spawn_logs(command).unwrap();
        let pid = child.id();
        assert!(child.output(Duration::from_secs(2)).await.is_err());
        assert_reaped(pid);

        let mut command = Command::new("/bin/sleep");
        command.arg("60");
        let child = ManagedChild::spawn_logs(command).unwrap();
        let pid = child.id();
        let start = Instant::now();
        assert!(
            tokio::time::timeout(
                Duration::from_millis(40),
                child.output(Duration::from_secs(2))
            )
            .await
            .is_err()
        );
        assert!(start.elapsed() < Duration::from_millis(600));
        assert_reaped(pid);
    }

    // Regression: synchronous output collection can ignore the runtime timer.
    #[tokio::test(flavor = "current_thread")]
    async fn child_hang_obeys_timeout_and_is_reaped() {
        let child = ManagedChild::test_sleep();
        let pid = child.id();
        let start = Instant::now();
        let result = child.output(Duration::from_millis(40)).await;
        assert!(
            result.is_err(),
            "child timeout must reject incomplete output"
        );
        assert!(start.elapsed() < Duration::from_millis(600));
        assert_reaped(pid);
    }

    // Cancellation must drop the guard and terminate/reap the actual child.
    #[tokio::test(flavor = "current_thread")]
    async fn stalled_setup_child_obeys_current_thread_timeout() {
        let child = ManagedChild::test_sleep();
        let pid = child.id();
        let start = Instant::now();
        let result = tokio::time::timeout(
            Duration::from_millis(40),
            child.output(Duration::from_secs(2)),
        )
        .await;
        assert!(
            result.is_err(),
            "outer cancellation must interrupt output collection"
        );
        assert!(start.elapsed() < Duration::from_millis(600));
        assert_reaped(pid);
    }

    #[test]
    fn direct_drop_terminates_and_reaps_child() {
        let child = ManagedChild::test_sleep();
        let pid = child.id();
        let start = Instant::now();
        drop(child);
        assert!(start.elapsed() < Duration::from_millis(600));
        assert_reaped(pid);
    }

    #[test]
    fn cleanup_child_hang_shares_one_budget_with_reaping() {
        let child = ManagedChild::test_sleep();
        let pid = child.id();
        let start = Instant::now();
        assert!(
            child
                .output_blocking(start + Duration::from_millis(300))
                .is_err()
        );
        assert!(start.elapsed() < Duration::from_millis(600));
        assert_reaped(pid);
    }

    #[tokio::test]
    async fn output_is_size_bounded_and_overflow_reaps_child() {
        let mut command = Command::new("/usr/bin/yes");
        command.arg("fixed-local-proof-output");
        let child = ManagedChild::spawn(command).unwrap();
        let pid = child.id();
        assert!(child.output(Duration::from_secs(1)).await.is_err());
        assert_reaped(pid);
    }

    fn assert_reaped(pid: u32) {
        assert_child_reaped(pid);
    }
}

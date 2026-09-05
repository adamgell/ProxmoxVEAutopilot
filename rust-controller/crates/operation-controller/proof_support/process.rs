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
    reaped: bool,
    cleanup_deadline: Option<Instant>,
}
impl ManagedChild {
    fn spawn(mut command: Command) -> io::Result<Self> {
        let (stdout, writer) = UnixStream::pair()?;
        stdout.set_nonblocking(true)?;
        let writer: OwnedFd = writer.into();
        let child = command
            .stdin(Stdio::null())
            .stdout(Stdio::from(writer))
            .stderr(Stdio::null())
            .spawn()?;
        Ok(Self {
            child,
            stdout,
            captured: Vec::new(),
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
                    if self.captured.len() + count > OUTPUT_CAP {
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
    async fn cancellation_drops_and_reaps_child_within_grace() {
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

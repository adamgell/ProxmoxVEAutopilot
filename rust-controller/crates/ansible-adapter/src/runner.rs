use crate::{AdapterError, ValidatedInvocation};
use async_trait::async_trait;
use controller_domain::ExecutionState;
use scheduler::{ExecutorKind, LeaseGrant, Scheduler, SchedulerError};
use std::{fs, process::Stdio, time::Duration};
use tokio::{
    io::{AsyncRead, AsyncReadExt},
    sync::{mpsc, watch},
    time::{Instant, MissedTickBehavior},
};

const HEARTBEAT: Duration = Duration::from_secs(5);
const FENCE_TIMEOUT: Duration = Duration::from_secs(2);
const OUTPUT_BYTES_PER_STREAM: usize = 32 * 1024;
const LINE_BYTES: usize = 1024;
const MAX_LOG_EVENTS: usize = 64;

/// Positive allowlist projection: arbitrary child text never reaches evidence.
/// Task/play names, paths, variables and diagnostic bodies are discarded.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SanitizedLine(&'static str);
impl SanitizedLine {
    pub fn as_str(&self) -> &'static str {
        self.0
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum AdapterEvent {
    Started,
    Heartbeat,
    Log(SanitizedLine),
    Exited { code: Option<i32> },
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CompletionReason {
    Exited,
    Cancelled,
    TimedOut,
    AuthorityLost,
    SpawnFailed,
    ProcessError,
}
#[derive(Debug)]
pub struct AdapterReport {
    pub state: ExecutionState,
    /// False means only the current-authority reaper may persist recovery.
    pub persisted: bool,
    pub reason: CompletionReason,
    pub events: Vec<AdapterEvent>,
}

#[derive(Clone, Copy, Debug)]
pub(crate) enum FenceError {
    Lost,
    Cancelled,
}

// Private capabilities: test doubles do not introduce a public command escape.
#[async_trait]
pub(crate) trait Control: Sync {
    type Grant: Send + Sync;
    async fn start(&self, grant: &Self::Grant, fingerprint: &str) -> Result<(), FenceError>;
    async fn heartbeat(&self, grant: &Self::Grant) -> Result<Self::Grant, FenceError>;
    async fn continuation(&self, grant: &Self::Grant) -> Result<(), FenceError>;
    async fn cancel(&self, grant: &Self::Grant) -> Result<(), FenceError>;
    async fn finalize(&self, grant: &Self::Grant, state: ExecutionState) -> Result<(), FenceError>;
}
#[async_trait]
pub(crate) trait ManagedProcess: Send {
    async fn wait(&mut self) -> Result<Option<i32>, ()>;
    async fn terminate(&mut self);
}

async fn bounded<T>(
    future: impl std::future::Future<Output = Result<T, FenceError>>,
) -> Result<T, FenceError> {
    tokio::time::timeout(FENCE_TIMEOUT, future)
        .await
        .unwrap_or(Err(FenceError::Lost))
}

pub(crate) async fn drive<C, P, F>(
    control: &C,
    mut grant: C::Grant,
    fingerprint: &str,
    spawn: F,
    mut cancel: watch::Receiver<bool>,
) -> AdapterReport
where
    C: Control,
    P: ManagedProcess,
    F: FnOnce() -> Result<(P, mpsc::Receiver<SanitizedLine>), ()>,
{
    let mut report = AdapterReport {
        state: ExecutionState::Unknown,
        persisted: false,
        reason: CompletionReason::AuthorityLost,
        events: Vec::new(),
    };
    if let Err(error) = bounded(control.start(&grant, fingerprint)).await {
        if matches!(error, FenceError::Cancelled) {
            report.reason = CompletionReason::Cancelled;
            report.persisted = bounded(control.finalize(&grant, ExecutionState::Unknown))
                .await
                .is_ok();
        }
        return report;
    }
    if let Err(error) = bounded(control.continuation(&grant)).await {
        if matches!(error, FenceError::Cancelled) {
            report.reason = CompletionReason::Cancelled;
            report.persisted = bounded(control.finalize(&grant, ExecutionState::Unknown))
                .await
                .is_ok();
        }
        return report;
    }
    // Bind the plan before cancellation can mutate its operation, and check
    // again after asynchronous start/continuation before launching anything.
    if *cancel.borrow() {
        if bounded(control.cancel(&grant)).await.is_ok() {
            report.reason = CompletionReason::Cancelled;
            report.persisted = bounded(control.finalize(&grant, ExecutionState::Unknown))
                .await
                .is_ok();
        }
        return report;
    }
    let Ok((mut process, mut output)) = spawn() else {
        report.reason = CompletionReason::SpawnFailed;
        report.persisted = bounded(control.finalize(&grant, ExecutionState::Unknown))
            .await
            .is_ok();
        return report;
    };
    report.events.push(AdapterEvent::Started);
    let deadline =
        Instant::now() + Duration::from_secs(crate::SYNTHETIC_LONG_SLEEP_V1.timeout_seconds);
    let mut heartbeat = tokio::time::interval_at(Instant::now() + HEARTBEAT, HEARTBEAT);
    heartbeat.set_missed_tick_behavior(MissedTickBehavior::Delay);
    let mut cancel_open = true;
    let mut output_open = true;
    let mut log_count = 0;
    let mut authority_lost = false;
    loop {
        tokio::select! {
            biased;
            _ = tokio::time::sleep_until(deadline) => { report.reason = CompletionReason::TimedOut; break; }
            changed = cancel.changed(), if cancel_open => {
                cancel_open = changed.is_ok();
                if cancel_open && *cancel.borrow() {
                    match bounded(control.cancel(&grant)).await {
                        Ok(()) => report.reason = CompletionReason::Cancelled,
                        Err(_) => authority_lost = true,
                    }
                    break;
                }
            }
            _ = heartbeat.tick() => {
                match bounded(control.heartbeat(&grant)).await {
                    Ok(renewed) => { grant = renewed; report.events.push(AdapterEvent::Heartbeat); }
                    Err(FenceError::Cancelled) => { report.reason = CompletionReason::Cancelled; break; }
                    Err(FenceError::Lost) => { authority_lost = true; break; }
                }
            }
            status = process.wait() => {
                match status {
                    Ok(code) => {
                        report.events.push(AdapterEvent::Exited { code });
                        // This result applies only to the pinned synthetic sleep.
                        // It says nothing about deployment/readiness postconditions.
                        report.state = match code { Some(0) => ExecutionState::Satisfied,
                            Some(_) => ExecutionState::Failed, None => ExecutionState::Unknown };
                        report.reason = CompletionReason::Exited;
                    }
                    Err(()) => report.reason = CompletionReason::ProcessError,
                }
                break;
            }
            line = output.recv(), if output_open => {
                match line {
                    Some(line) if log_count < MAX_LOG_EVENTS => { report.events.push(AdapterEvent::Log(line)); log_count += 1; }
                    Some(_) => {},
                    None => output_open = false,
                }
            }
        }
    }
    // Always clean the group, including descendants whose leader already exited.
    process.terminate().await;
    while log_count < MAX_LOG_EVENTS {
        let Ok(line) = output.try_recv() else {
            break;
        };
        report.events.push(AdapterEvent::Log(line));
        log_count += 1;
    }
    if !authority_lost {
        match bounded(control.continuation(&grant)).await {
            Ok(()) => {}
            Err(FenceError::Cancelled) => {
                report.state = ExecutionState::Unknown;
                report.reason = CompletionReason::Cancelled;
            }
            Err(FenceError::Lost) => authority_lost = true,
        }
    }
    if !authority_lost {
        report.persisted = match bounded(control.finalize(&grant, report.state)).await {
            Ok(()) => true,
            Err(FenceError::Cancelled) => {
                report.state = ExecutionState::Unknown;
                report.reason = CompletionReason::Cancelled;
                bounded(control.finalize(&grant, ExecutionState::Unknown))
                    .await
                    .is_ok()
            }
            Err(FenceError::Lost) => false,
        };
        authority_lost = !report.persisted;
    }
    if authority_lost {
        report.state = ExecutionState::Unknown;
        report.reason = CompletionReason::AuthorityLost;
    }
    report
}

pub(crate) async fn read_sanitized(
    mut reader: impl AsyncRead + Unpin,
    sender: mpsc::Sender<SanitizedLine>,
) {
    let mut buffer = [0u8; 1024];
    let mut line = Vec::with_capacity(LINE_BYTES);
    let mut used = 0;
    let mut overlong = false;
    loop {
        let Ok(length) = reader.read(&mut buffer).await else {
            break;
        };
        if length == 0 {
            break;
        }
        // Drain the pipe after the byte budget, without buffering or parsing it.
        let admitted = length.min(OUTPUT_BYTES_PER_STREAM - used);
        used += admitted;
        for byte in &buffer[..admitted] {
            if *byte == b'\n' {
                let sanitized = if overlong {
                    SanitizedLine("output omitted")
                } else {
                    sanitize(&line)
                };
                // Bounded channel and dropping on backpressure preserve supervision.
                let _ = sender.try_send(sanitized);
                line.clear();
                overlong = false;
            } else if line.len() < LINE_BYTES && !overlong {
                line.push(*byte);
            } else {
                line.clear();
                overlong = true;
            }
        }
    }
    if overlong || !line.is_empty() {
        let _ = sender.try_send(SanitizedLine("output omitted"));
    }
}

fn sanitize(line: &[u8]) -> SanitizedLine {
    let label = if line.starts_with(b"PLAY [") {
        "play progress"
    } else if line.starts_with(b"TASK [") {
        "task progress"
    } else if line.starts_with(b"PLAY RECAP") {
        "play recap"
    } else if line.starts_with(b"ok: [localhost]") {
        "localhost task ok"
    } else if line.starts_with(b"changed: [localhost]") {
        "localhost task changed"
    } else {
        "output omitted"
    };
    SanitizedLine(label)
}

/// Local execution requires a Rust scheduler and a registry-made invocation.
/// No command, environment, shell, or pluggable process capability is public.
pub struct AdapterRunner {
    scheduler: Scheduler,
}
impl AdapterRunner {
    pub fn new(scheduler: Scheduler) -> Self {
        Self { scheduler }
    }

    pub async fn run(
        self,
        invocation: ValidatedInvocation,
        grant: LeaseGrant,
        cancel: watch::Receiver<bool>,
    ) -> Result<AdapterReport, AdapterError> {
        if grant.executor_kind() != ExecutorKind::Rust {
            return Err(AdapterError::Contract);
        }
        invocation.registry.verify()?;
        let prepared = Prepared::new(&invocation)?;
        let report = drive(
            &self.scheduler,
            grant,
            &invocation.fingerprint,
            || NativeProcess::spawn(&invocation, prepared),
            cancel,
        )
        .await;
        Ok(report)
    }
}

fn fence(error: SchedulerError) -> FenceError {
    match error {
        SchedulerError::CancellationRequested | SchedulerError::CancellationRequiresUnknown => {
            FenceError::Cancelled
        }
        _ => FenceError::Lost,
    }
}
#[async_trait]
impl Control for Scheduler {
    type Grant = LeaseGrant;
    async fn start(&self, grant: &LeaseGrant, fingerprint: &str) -> Result<(), FenceError> {
        self.start_bound(
            grant,
            controller_domain::WorkflowKind::SyntheticLongSleep,
            1,
            fingerprint,
        )
        .await
        .map(|_| ())
        .map_err(fence)
    }
    async fn heartbeat(&self, grant: &LeaseGrant) -> Result<LeaseGrant, FenceError> {
        Scheduler::heartbeat(self, grant).await.map_err(fence)
    }
    async fn continuation(&self, grant: &LeaseGrant) -> Result<(), FenceError> {
        Scheduler::continuation(self, grant)
            .await
            .map(|_| ())
            .map_err(fence)
    }
    async fn cancel(&self, grant: &LeaseGrant) -> Result<(), FenceError> {
        self.request_cancel_bound(grant)
            .await
            .map(|_| ())
            .map_err(fence)
    }
    async fn finalize(&self, grant: &LeaseGrant, state: ExecutionState) -> Result<(), FenceError> {
        Scheduler::finalize(self, grant, state)
            .await
            .map(|_| ())
            .map_err(fence)
    }
}

pub(crate) struct Prepared {
    pub(crate) directory: tempfile::TempDir,
}
impl Prepared {
    pub(crate) fn new(invocation: &ValidatedInvocation) -> Result<Self, AdapterError> {
        invocation.registry.verify()?;
        let directory = tempfile::Builder::new()
            .prefix("rust-controller-ansible-")
            .tempdir_in(if cfg!(target_os = "macos") {
                "/private/tmp"
            } else {
                "/tmp"
            })?;
        for child in ["home", "tmp", "local", "remote", "empty"] {
            fs::create_dir(directory.path().join(child))?;
        }
        fs::write(
            directory.path().join("playbook.yml"),
            crate::contract::PLAYBOOK_BYTES,
        )?;
        fs::write(
            directory.path().join("inventory"),
            "localhost ansible_connection=local\n",
        )?;
        let path = directory.path().to_str().ok_or(AdapterError::TrustedPath)?;
        // Private copy avoids adjacent worktree group_vars, action/filter plugins,
        // ansible.cfg and roles. Only identical, compile-time approved YAML runs.
        fs::write(
            directory.path().join("ansible.cfg"),
            format!(
                "[defaults]\ninventory = {path}/inventory\nlocal_tmp = {path}/local\nremote_tmp = {path}/remote\nroles_path = {path}/empty\nlibrary = {path}/empty\ncollections_path = {path}/empty\naction_plugins = {path}/empty\nfilter_plugins = {path}/empty\nlookup_plugins = {path}/empty\ncallback_plugins = {path}/empty\nstdout_callback = default\nretry_files_enabled = False\nhost_key_checking = True\nforks = 1\n[inventory]\nenable_plugins = ini\n"
            ),
        )?;
        Ok(Self { directory })
    }

    pub(crate) fn command(
        &self,
        invocation: &ValidatedInvocation,
    ) -> Result<tokio::process::Command, AdapterError> {
        invocation.registry.verify()?;
        let root = self.directory.path();
        let mut command = tokio::process::Command::new(invocation.registry.executable());
        command
            .env_clear()
            .env("PATH", "/usr/bin:/bin")
            .env("HOME", root.join("home"))
            .env("TMPDIR", root.join("tmp"))
            .env("TMP", root.join("tmp"))
            .env("TEMP", root.join("tmp"))
            .env("ANSIBLE_CONFIG", root.join("ansible.cfg"))
            .env(
                "ANSIBLE_PYTHON_INTERPRETER",
                invocation.registry.interpreter(),
            )
            .env("PYTHONNOUSERSITE", "1")
            .env("PYTHONSAFEPATH", "1")
            .env("ANSIBLE_NOCOLOR", "1")
            .env("LC_ALL", "C.UTF-8")
            .current_dir(root)
            .arg(root.join("playbook.yml"))
            .arg("--inventory")
            .arg(root.join("inventory"))
            .args(["--connection", "local", "-e"])
            .arg(format!("{{\"duration\":{}}}", invocation.duration))
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .process_group(0);
        Ok(command)
    }
}

pub(crate) struct NativeProcess {
    child: tokio::process::Child,
    group: i32,
    armed: bool,
    tree: crate::process_tree::ProcessTree,
    readers: Vec<tokio::task::JoinHandle<()>>,
    _prepared: Prepared,
}
impl NativeProcess {
    pub(crate) fn spawn(
        invocation: &ValidatedInvocation,
        prepared: Prepared,
    ) -> Result<(Self, mpsc::Receiver<SanitizedLine>), ()> {
        let mut child = prepared
            .command(invocation)
            .map_err(|_| ())?
            .spawn()
            .map_err(|_| ())?;
        let group = child
            .id()
            .and_then(|pid| i32::try_from(pid).ok())
            .ok_or(())?;
        let (sender, receiver) = mpsc::channel(32);
        let stdout = child.stdout.take().ok_or(())?;
        let stderr = child.stderr.take().ok_or(())?;
        let readers = vec![
            tokio::spawn(read_sanitized(stdout, sender.clone())),
            tokio::spawn(read_sanitized(stderr, sender)),
        ];
        Ok((
            Self {
                child,
                group,
                armed: true,
                tree: crate::process_tree::ProcessTree::new(group),
                readers,
                _prepared: prepared,
            },
            receiver,
        ))
    }
    pub(crate) fn group_id(&self) -> i32 {
        self.group
    }
    fn group_exists(&self) -> bool {
        unsafe { libc::kill(-self.group_id(), 0) == 0 }
    }
}
#[async_trait]
impl ManagedProcess for NativeProcess {
    async fn wait(&mut self) -> Result<Option<i32>, ()> {
        loop {
            self.tree.refresh(false);
            tokio::select! {
                status = self.child.wait() => return status.map(|status| status.code()).map_err(|_| ()),
                _ = tokio::time::sleep(Duration::from_millis(50)) => {},
            }
        }
    }
    async fn terminate(&mut self) {
        self.tree.refresh(true);
        self.tree.signal(libc::SIGTERM);
        self.tree.signal(libc::SIGCONT);
        let deadline = Instant::now() + Duration::from_secs(2);
        // Reap the leader while allowing its whole group a bounded TERM grace.
        while (self.group_exists() || self.tree.any_live()) && Instant::now() < deadline {
            let _ = self.child.try_wait();
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        self.tree.signal(libc::SIGKILL);
        let _ = tokio::time::timeout(Duration::from_secs(1), self.child.wait()).await;
        self.armed = false;
        for reader in &mut self.readers {
            if tokio::time::timeout(Duration::from_millis(100), &mut *reader)
                .await
                .is_err()
            {
                reader.abort();
            }
        }
    }
}
impl Drop for NativeProcess {
    fn drop(&mut self) {
        // Aborting the runner future also kills descendants, not only Child.
        if self.armed {
            self.tree.refresh(true);
            self.tree.signal(libc::SIGKILL);
        }
        for reader in &self.readers {
            reader.abort();
        }
    }
}

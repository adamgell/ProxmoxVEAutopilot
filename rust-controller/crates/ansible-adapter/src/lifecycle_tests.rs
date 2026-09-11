use crate::runner::*;
use async_trait::async_trait;
use controller_domain::ExecutionState;
use std::{
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};
use tokio::sync::{mpsc, watch};

#[derive(Default)]
struct AuthorityState {
    started: bool,
    generation: u32,
    heartbeats: usize,
    cancelled: bool,
    finalized: Option<ExecutionState>,
    stale: bool,
    reject_start: bool,
    reject_final: bool,
    cancel_at_final: bool,
}
#[derive(Clone, Default)]
struct Authority(Arc<Mutex<AuthorityState>>);
#[async_trait]
impl Control for Authority {
    type Grant = u32;
    async fn start(&self, _: &u32, _: &str) -> Result<(), FenceError> {
        let mut state = self.0.lock().unwrap();
        if state.reject_start {
            return Err(FenceError::Lost);
        }
        state.started = true;
        Ok(())
    }
    async fn heartbeat(&self, grant: &u32) -> Result<u32, FenceError> {
        let mut state = self.0.lock().unwrap();
        if state.stale || *grant != state.generation {
            return Err(FenceError::Lost);
        }
        if state.cancelled {
            return Err(FenceError::Cancelled);
        }
        state.heartbeats += 1;
        state.generation += 1;
        Ok(state.generation)
    }
    async fn continuation(&self, grant: &u32) -> Result<(), FenceError> {
        let state = self.0.lock().unwrap();
        if state.stale || *grant != state.generation {
            return Err(FenceError::Lost);
        }
        if state.cancelled {
            return Err(FenceError::Cancelled);
        }
        Ok(())
    }
    async fn cancel(&self, _: &u32) -> Result<(), FenceError> {
        let mut state = self.0.lock().unwrap();
        if state.stale {
            return Err(FenceError::Lost);
        }
        state.cancelled = true;
        Ok(())
    }
    async fn finalize(&self, grant: &u32, result: ExecutionState) -> Result<(), FenceError> {
        let mut state = self.0.lock().unwrap();
        if state.cancel_at_final && result != ExecutionState::Unknown {
            return Err(FenceError::Cancelled);
        }
        if state.stale || state.reject_final || *grant != state.generation {
            return Err(FenceError::Lost);
        }
        state.finalized = Some(result);
        Ok(())
    }
}

#[tokio::test(start_paused = true)]
async fn cancellation_winning_finalization_race_is_persisted_unknown() {
    // Break caught: a concurrent cancel misreported as authority loss and left running.
    let authority = Authority::default();
    authority.0.lock().unwrap().cancel_at_final = true;
    let killed = Arc::new(AtomicBool::new(false));
    let (_cancel, receiver) = watch::channel(false);
    let report = drive(
        &authority,
        0,
        "fingerprint",
        spawn(&authority, 1, Some(0), &killed),
        receiver,
    )
    .await;
    assert_eq!(report.reason, CompletionReason::Cancelled);
    assert_eq!(report.state, ExecutionState::Unknown);
    assert!(report.persisted);
}

struct Process {
    exit_at: tokio::time::Instant,
    code: Option<i32>,
    killed: Arc<AtomicBool>,
}
#[async_trait]
impl ManagedProcess for Process {
    async fn wait(&mut self) -> Result<Option<i32>, ()> {
        tokio::time::sleep_until(self.exit_at).await;
        Ok(self.code)
    }
    async fn terminate(&mut self) {
        self.killed.store(true, Ordering::SeqCst);
    }
}
fn spawn(
    authority: &Authority,
    seconds: u64,
    code: Option<i32>,
    killed: &Arc<AtomicBool>,
) -> impl FnOnce() -> Result<(Process, mpsc::Receiver<SanitizedLine>), ()> + use<> {
    let authority = authority.clone();
    let killed = killed.clone();
    move || {
        assert!(
            authority.0.lock().unwrap().started,
            "spawn before atomic start"
        );
        let (_, receiver) = mpsc::channel(1);
        Ok((
            Process {
                exit_at: tokio::time::Instant::now() + Duration::from_secs(seconds),
                code,
                killed,
            },
            receiver,
        ))
    }
}

#[tokio::test(start_paused = true)]
async fn success_and_nonzero_exit_are_distinct_and_both_cleanup() {
    // Break caught: treating nonzero as success or leaving descendants after exit.
    for (code, expected) in [
        (Some(0), ExecutionState::Satisfied),
        (Some(2), ExecutionState::Failed),
        (None, ExecutionState::Unknown),
    ] {
        let authority = Authority::default();
        let killed = Arc::new(AtomicBool::new(false));
        let (_cancel, receiver) = watch::channel(false);
        let report = drive(
            &authority,
            0,
            "fingerprint",
            spawn(&authority, 1, code, &killed),
            receiver,
        )
        .await;
        assert_eq!(report.state, expected);
        assert!(report.persisted);
        assert_eq!(authority.0.lock().unwrap().finalized, Some(expected));
        assert!(killed.load(Ordering::SeqCst));
    }
}

#[tokio::test(start_paused = true)]
async fn heartbeats_run_every_five_seconds_and_keep_renewed_grant() {
    // Break caught: immediate/missing heartbeat or discarding renewed lease.
    let authority = Authority::default();
    let killed = Arc::new(AtomicBool::new(false));
    let (_cancel, receiver) = watch::channel(false);
    let report = drive(
        &authority,
        0,
        "fingerprint",
        spawn(&authority, 12, Some(0), &killed),
        receiver,
    )
    .await;
    assert_eq!(report.state, ExecutionState::Satisfied);
    assert_eq!(authority.0.lock().unwrap().heartbeats, 2);
    assert!(report.persisted);
}

#[tokio::test(start_paused = true)]
async fn deadline_is_unknown_with_no_retry() {
    // Break caught: timeout reported failed/success or hanging past contract.
    let authority = Authority::default();
    let killed = Arc::new(AtomicBool::new(false));
    let (_cancel, receiver) = watch::channel(false);
    let began = tokio::time::Instant::now();
    let report = drive(
        &authority,
        0,
        "fingerprint",
        spawn(&authority, 100, Some(0), &killed),
        receiver,
    )
    .await;
    assert_eq!(report.state, ExecutionState::Unknown);
    assert_eq!(report.reason, CompletionReason::TimedOut);
    assert!(report.persisted && killed.load(Ordering::SeqCst));
    assert_eq!(began.elapsed(), Duration::from_secs(30));
}

#[tokio::test(start_paused = true)]
async fn cancellation_terminates_and_fenced_finalization_is_unknown() {
    // Break caught: cancellation ignored or completion allowed after cancelling.
    let authority = Authority::default();
    let killed = Arc::new(AtomicBool::new(false));
    let (cancel, receiver) = watch::channel(false);
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_secs(1)).await;
        cancel.send(true).unwrap();
    });
    let began = tokio::time::Instant::now();
    let report = drive(
        &authority,
        0,
        "fingerprint",
        spawn(&authority, 100, Some(0), &killed),
        receiver,
    )
    .await;
    assert_eq!(report.reason, CompletionReason::Cancelled);
    assert_eq!(report.state, ExecutionState::Unknown);
    assert!(report.persisted && killed.load(Ordering::SeqCst));
    assert!(began.elapsed() < Duration::from_secs(10));
}

#[tokio::test(start_paused = true)]
async fn authority_loss_kills_and_never_persists_stale_result() {
    // Break caught: finalization bypasses stale generation after heartbeat fails.
    let authority = Authority::default();
    let killed = Arc::new(AtomicBool::new(false));
    let (_cancel, receiver) = watch::channel(false);
    let flipper = authority.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_secs(1)).await;
        flipper.0.lock().unwrap().stale = true;
    });
    let report = drive(
        &authority,
        0,
        "fingerprint",
        spawn(&authority, 100, Some(0), &killed),
        receiver,
    )
    .await;
    assert_eq!(report.reason, CompletionReason::AuthorityLost);
    assert_eq!(report.state, ExecutionState::Unknown);
    assert!(!report.persisted);
    assert_eq!(authority.0.lock().unwrap().finalized, None);
    assert!(killed.load(Ordering::SeqCst));
}

#[tokio::test(start_paused = true)]
async fn start_rejection_cannot_spawn_and_finalization_race_returns_unknown() {
    // Break caught: checking authority after spawn or returning success when fenced.
    let authority = Authority::default();
    authority.0.lock().unwrap().reject_start = true;
    let (_cancel, receiver) = watch::channel(false);
    let report = drive::<_, Process, _>(
        &authority,
        0,
        "fingerprint",
        || panic!("must never spawn"),
        receiver,
    )
    .await;
    assert!(!report.persisted);
    assert_eq!(report.state, ExecutionState::Unknown);
    authority.0.lock().unwrap().reject_start = false;
    authority.0.lock().unwrap().reject_final = true;
    let killed = Arc::new(AtomicBool::new(false));
    let (_cancel, receiver) = watch::channel(false);
    let report = drive(
        &authority,
        0,
        "fingerprint",
        spawn(&authority, 1, Some(0), &killed),
        receiver,
    )
    .await;
    assert_eq!(report.state, ExecutionState::Unknown);
    assert!(!report.persisted);
}

#[tokio::test]
async fn unterminated_megabyte_and_secret_lines_never_enter_evidence() {
    // Break caught: unbounded read_line or passing raw/redaction-bypass data to persistence.
    let (mut writer, reader) = tokio::io::duplex(2048);
    let (sender, mut receiver) = mpsc::channel(32);
    let task = tokio::spawn(read_sanitized(reader, sender));
    let producer = tokio::spawn(async move {
        use tokio::io::AsyncWriteExt;
        writer
            .write_all(b"TASK [Bearer secret-canary]\nsecret-canary\n")
            .await
            .unwrap();
        writer.write_all(&vec![b'x'; 1_000_000]).await.unwrap();
    });
    let mut lines = Vec::new();
    while let Some(line) = receiver.recv().await {
        lines.push(line);
    }
    producer.await.unwrap();
    task.await.unwrap();
    assert!(lines.len() <= 32);
    assert!(!format!("{lines:?}").contains("secret-canary"));
    assert!(!format!("{lines:?}").contains("Bearer"));
    assert!(format!("{lines:?}").len() < 2048);
}

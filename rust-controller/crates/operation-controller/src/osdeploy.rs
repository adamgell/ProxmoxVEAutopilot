//! Owned, bounded orchestration for durable OSDeploy operations.
mod admission;
mod collect;
mod send;

use admission::OsDeploySendAdmission;
use controller_domain::{EventId, ExecutionState, OperationId};
use postgres_store::{
    LeaseGrant, OsDeployDue, OsDeployDueKind, OsDeployExecutionError, OsDeployProgress, PgStore,
    Scheduler,
};
use pve_port::{
    FakeControllerCheckpoint, NativeFakePve, ProvisioningActionV1, ProvisioningEvaluationModeV1,
};
use std::{future::Future, sync::Arc, time::Duration};
use tokio::sync::{Semaphore, watch};
use tokio::time::{Instant, timeout_at};

const CALL_BOUND: Duration = Duration::from_secs(24);
const READ_BOUND: Duration = Duration::from_secs(2);
const COLLECTION_BOUND: Duration = Duration::from_secs(6);
type RecordedObservation = (EventId, i64, ProvisioningEvaluationModeV1);

#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum OsDeployControllerError {
    #[error("osdeploy_storage_unavailable")]
    Storage,
    #[error("osdeploy_validation_failed")]
    Validation,
    #[error("osdeploy_conflict")]
    Conflict,
    #[error("osdeploy_fence_lost")]
    FenceLost,
    #[error("osdeploy_capability_unavailable")]
    CapabilityUnavailable,
    #[error("osdeploy_call_timed_out")]
    TimedOut,
    #[error("osdeploy_admission_closed")]
    AdmissionClosed,
}
use OsDeployControllerError as Error;
impl From<OsDeployExecutionError> for Error {
    fn from(value: OsDeployExecutionError) -> Self {
        match value {
            OsDeployExecutionError::Validation => Self::Validation,
            OsDeployExecutionError::Conflict => Self::Conflict,
            OsDeployExecutionError::FenceLost => Self::FenceLost,
            OsDeployExecutionError::CapabilityUnavailable => Self::CapabilityUnavailable,
            OsDeployExecutionError::StorageUnavailable => Self::Storage,
        }
    }
}

/// One service instance owns admission and directly awaited send futures.
pub struct OsDeployController {
    store: PgStore,
    scheduler: Scheduler,
    fake: Arc<NativeFakePve>,
    admission: OsDeploySendAdmission,
    workers: Semaphore,
    cap: u32,
}
impl OsDeployController {
    pub fn new(
        store: PgStore,
        scheduler: Scheduler,
        fake: Arc<NativeFakePve>,
        cap: u32,
    ) -> Result<Self, Error> {
        if cap == 0 || cap as usize > Semaphore::MAX_PERMITS {
            return Err(Error::Validation);
        }
        Ok(Self {
            store,
            scheduler,
            fake,
            admission: OsDeploySendAdmission::new(),
            workers: Semaphore::new(cap as usize),
            cap,
        })
    }
    pub async fn open_send_admission(&self) -> Result<(), Error> {
        self.admission.open(&self.scheduler).await
    }
    pub async fn close_and_drain(&self, bound: Duration) -> Result<(), Error> {
        self.admission.close_and_drain(bound).await
    }
    pub async fn run_osdeploy_once(
        &self,
        operation: OperationId,
    ) -> Result<OsDeployProgress, Error> {
        self.run(operation, None).await
    }
    pub async fn run_due_once(&self, due: &OsDeployDue) -> Result<OsDeployProgress, Error> {
        self.run(due.operation_id(), Some(due)).await
    }
    async fn run(
        &self,
        operation: OperationId,
        due: Option<&OsDeployDue>,
    ) -> Result<OsDeployProgress, Error> {
        let deadline = Instant::now() + CALL_BOUND;
        let _activity = self.admission.try_enter()?;
        let Ok(_worker) = self.workers.try_acquire() else {
            return Ok(OsDeployProgress::Idle);
        };
        let mut closed = self.admission.subscribe();
        timeout_at(
            deadline,
            Box::pin(async {
                let snapshot =
                    before_close(&mut closed, self.store.load_osdeploy_operation(operation))
                        .await?;
                if !snapshot.plan().pve().is_some_and(|plan| {
                    matches!(
                        plan.action(),
                        ProvisioningActionV1::Clone
                            | ProvisioningActionV1::EnsureCapacity
                            | ProvisioningActionV1::ConfigurePe
                    )
                }) {
                    return Err(Error::CapabilityUnavailable);
                }
                let hash = snapshot.plan().workflow_sha256();
                let actual_due;
                let due = if snapshot.state() == ExecutionState::Waiting || due.is_some() {
                    let candidates =
                        before_close(&mut closed, self.scheduler.discover_osdeploy_due()).await?;
                    actual_due = candidates.into_iter().find(|candidate| {
                        candidate.operation_id() == operation
                            && due.is_none_or(|expected| {
                                candidate.attempt_id() == expected.attempt_id()
                                    && candidate.revision() == expected.revision()
                                    && candidate.basis_event_id() == expected.basis_event_id()
                                    && candidate.kind() == expected.kind()
                                    && candidate.workflow_sha256() == expected.workflow_sha256()
                            })
                    });
                    let Some(found) = actual_due.as_ref() else {
                        return Ok(OsDeployProgress::Idle);
                    };
                    Some(found)
                } else {
                    None
                };
                if let Some(due) =
                    due.filter(|value| value.kind() == OsDeployDueKind::UnknownReconciliation)
                {
                    return Box::pin(self.reconcile(due, deadline, &mut closed)).await;
                }
                if snapshot.state().is_terminal() {
                    return Ok(OsDeployProgress::Decided(snapshot.state()));
                }
                let grant = if let Some(due) = due {
                    before_close(
                        &mut closed,
                        self.scheduler.resume_osdeploy_bound(
                            operation,
                            due.attempt_id(),
                            due.revision(),
                            hash,
                            self.cap,
                        ),
                    )
                    .await?
                } else {
                    before_close(
                        &mut closed,
                        self.scheduler
                            .claim_osdeploy_bound(operation, hash, self.cap),
                    )
                    .await?
                };
                let Some(grant) = grant else {
                    return Ok(OsDeployProgress::Idle);
                };
                before_close(
                    &mut closed,
                    self.scheduler.start_osdeploy_bound(&grant, hash),
                )
                .await?;
                Box::pin(self.run_grant(&grant, hash, deadline, &mut closed)).await
            }),
        )
        .await
        .map_err(|_| Error::TimedOut)?
    }

    async fn reconcile(
        &self,
        due: &OsDeployDue,
        whole: Instant,
        closed: &mut watch::Receiver<bool>,
    ) -> Result<OsDeployProgress, Error> {
        let check_start = Instant::now();
        let observation = before_close(
            closed,
            self.store.load_osdeploy_pve_context_with_budget(
                due.operation_id(),
                due.revision(),
                ProvisioningEvaluationModeV1::Reconciliation,
            ),
        )
        .await?;
        let deadline = budget_deadline(check_start, observation.remaining(), whole);
        if Instant::now() >= deadline {
            return Err(Error::TimedOut);
        }
        let evidence = before_close(
            closed,
            Box::pin(collect::collect(
                &self.fake,
                observation.context(),
                deadline.min(Instant::now() + COLLECTION_BOUND),
            )),
        )
        .await?;
        let event = before_close(
            closed,
            self.store.record_osdeploy_pve_evidence(
                due.operation_id(),
                due.attempt_id(),
                due.revision(),
                &evidence,
            ),
        )
        .await?;
        let snapshot = before_close(
            closed,
            self.store.load_osdeploy_operation(due.operation_id()),
        )
        .await?;
        before_close(
            closed,
            self.scheduler.reconcile_osdeploy_unknown(
                due.operation_id(),
                due.attempt_id(),
                snapshot.revision(),
                event,
                due.workflow_sha256(),
            ),
        )
        .await
    }

    async fn run_grant(
        &self,
        grant: &LeaseGrant,
        hash: &str,
        whole: Instant,
        closed: &mut watch::Receiver<bool>,
    ) -> Result<OsDeployProgress, Error> {
        loop {
            // Each owned phase is dropped before constructing the next. Only
            // durable identifiers cross the observation/decision boundary.
            let observed = self.observe(grant, hash, whole, closed).await;
            let result = match observed {
                Ok(observation) => self.advance(grant, hash, whole, observation, closed).await,
                Err(error) => Err(error),
            };
            match result {
                // A control event may invalidate the original context. Reload
                // and recollect; never relabel already captured evidence.
                Err(Error::FenceLost) => {
                    before_close(
                        closed,
                        Box::pin(self.scheduler.continuation_osdeploy_bound(grant, hash)),
                    )
                    .await?;
                    tokio::task::yield_now().await;
                }
                Ok(None) => {}
                Ok(Some(progress)) => return Ok(progress),
                Err(error) => return Err(error),
            }
        }
    }

    fn observe<'a>(
        &'a self,
        grant: &'a LeaseGrant,
        hash: &'a str,
        whole: Instant,
        closed: &'a mut watch::Receiver<bool>,
    ) -> std::pin::Pin<Box<impl Future<Output = Result<RecordedObservation, Error>> + 'a>> {
        Box::pin(async move {
            let check_start = Instant::now();
            let status = before_close(
                closed,
                self.scheduler.continuation_osdeploy_bound(grant, hash),
            )
            .await?;
            let deadline = budget_deadline(check_start, status.remaining(), whole);
            if Instant::now() >= deadline {
                return Err(Error::TimedOut);
            }
            let snapshot = before_close(
                closed,
                self.store.load_osdeploy_operation(grant.operation_id()),
            )
            .await?;
            let mode = if snapshot.dispatch().is_some() {
                ProvisioningEvaluationModeV1::Outcome
            } else {
                ProvisioningEvaluationModeV1::Preflight
            };
            let context = before_close(
                closed,
                self.store.load_osdeploy_pve_context(
                    grant.operation_id(),
                    snapshot.revision(),
                    mode,
                ),
            )
            .await?;
            let evidence = before_close(
                closed,
                Box::pin(collect::collect(
                    &self.fake,
                    &context,
                    deadline.min(Instant::now() + COLLECTION_BOUND),
                )),
            )
            .await?;
            let event = before_close(
                closed,
                self.store.record_osdeploy_pve_evidence(
                    grant.operation_id(),
                    grant.attempt_id(),
                    snapshot.revision(),
                    &evidence,
                ),
            )
            .await?;
            let current = before_close(
                closed,
                self.store.load_osdeploy_operation(grant.operation_id()),
            )
            .await?;
            Ok((event, current.revision(), mode))
        })
    }

    fn advance<'a>(
        &'a self,
        grant: &'a LeaseGrant,
        hash: &'a str,
        whole: Instant,
        observation: RecordedObservation,
        closed: &'a mut watch::Receiver<bool>,
    ) -> std::pin::Pin<Box<impl Future<Output = Result<Option<OsDeployProgress>, Error>> + 'a>>
    {
        Box::pin(async move {
            let (event, revision, mode) = observation;
            let request = if mode == ProvisioningEvaluationModeV1::Preflight {
                match before_close(
                    closed,
                    self.store
                        .prepare_osdeploy_pve_request(grant.operation_id(), revision, event),
                )
                .await
                {
                    Ok(request) => Some(request),
                    // Only the locked evaluator can classify a non-Ready result.
                    // Invalid history still fails there; this is not a success.
                    Err(Error::Validation) => None,
                    Err(error) => return Err(error),
                }
            } else {
                None
            };
            if let Some(request) = request {
                let (permit, capture) = before_close(
                    closed,
                    self.scheduler
                        .begin_osdeploy_pve_dispatch(grant, revision, event, &request),
                )
                .await?;
                before_close(closed, async {
                    self.fake
                        .controller_checkpoint(FakeControllerCheckpoint::DispatchCommitted)
                        .await;
                    Ok::<_, Error>(())
                })
                .await?;
                send::submit_and_capture_once(
                    &self.admission,
                    &self.scheduler,
                    &self.fake,
                    grant,
                    hash,
                    permit,
                    capture,
                    whole,
                )
                .await?;
                // Fresh context captures the actual original receipt (or its absence).
                return Ok(None);
            }
            before_close(
                closed,
                self.scheduler.decide_osdeploy_pve(grant, revision, event),
            )
            .await
            .map(Some)
        })
    }
}

/// A returned DB interval is static. Include the full awaited round trip before
/// intersecting with the already-pinned whole-call endpoint.
fn remaining_budget(interval: Duration, elapsed: Duration) -> Duration {
    interval.saturating_sub(elapsed)
}
fn budget_deadline(started: Instant, interval: Duration, whole: Instant) -> Instant {
    let now = Instant::now();
    whole.min(now + remaining_budget(interval, now.saturating_duration_since(started)))
}
async fn before_close<T, E>(
    closed: &mut watch::Receiver<bool>,
    future: impl Future<Output = Result<T, E>>,
) -> Result<T, Error>
where
    Error: From<E>,
{
    if *closed.borrow() || !matches!(closed.has_changed(), Ok(false)) {
        return Err(Error::AdmissionClosed);
    }
    tokio::select! {
        biased;
        _ = closed.changed() => Err(Error::AdmissionClosed),
        result = future => result.map_err(Error::from),
    }
}

#[cfg(test)]
mod budget_tests {
    use super::*;
    #[test]
    fn lease_status_roundtrip_delay_consumes_budget() {
        assert_eq!(
            remaining_budget(Duration::from_secs(2), Duration::from_millis(750)),
            Duration::from_millis(1250)
        );
        assert_eq!(
            remaining_budget(Duration::from_secs(2), Duration::from_secs(3)),
            Duration::ZERO
        );
    }
    #[test]
    fn client_wall_clock_shift_cannot_extend_local_deadline() {
        let interval = Duration::from_secs(6);
        let elapsed = Duration::from_secs(2);
        let wall = chrono::Utc::now();
        for observation in [
            wall - chrono::Duration::days(365),
            wall,
            wall + chrono::Duration::days(365),
        ] {
            // Wall observations are deliberately independent of the only two
            // inputs accepted by the production monotonic conversion.
            assert_ne!(observation.timestamp(), 0);
            assert_eq!(remaining_budget(interval, elapsed), Duration::from_secs(4));
        }
        let started = Instant::now();
        let whole = started + Duration::from_millis(100);
        assert!(budget_deadline(started, interval, whole) <= whole);
        assert!(budget_deadline(started, Duration::ZERO, whole) <= Instant::now());
    }
}

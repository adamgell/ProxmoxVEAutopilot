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
#[cfg(not(feature = "fixture-ipc"))]
use pve_port::FakeControllerCheckpoint;
use pve_port::{NativeFakePve, ProvisioningActionV1, ProvisioningEvaluationModeV1};
use std::{future::Future, sync::Arc, time::Duration};
use tokio::sync::{Semaphore, watch};
use tokio::time::{Instant, timeout_at};

const CALL_BOUND: Duration = Duration::from_secs(24);
const READ_BOUND: Duration = Duration::from_secs(2);
const COLLECTION_BOUND: Duration = Duration::from_secs(6);
type RecordedObservation = (EventId, i64, ProvisioningEvaluationModeV1);

#[cfg(not(feature = "fixture-ipc"))]
type ControllerPort = NativeFakePve;
#[cfg(feature = "fixture-ipc")]
type ControllerPort = dyn pve_port::fixture_ipc::ControllerFixturePort;

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
    #[cfg(feature = "fixture-ipc")]
    #[error("osdeploy_checkpoint_failed: {0}")]
    Checkpoint(pve_port::fixture_ipc::CheckpointError),
}
#[cfg(feature = "fixture-ipc")]
impl From<pve_port::fixture_ipc::CheckpointError> for OsDeployControllerError {
    fn from(error: pve_port::fixture_ipc::CheckpointError) -> Self {
        Self::Checkpoint(error)
    }
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
    fake: Arc<ControllerPort>,
    #[cfg(feature = "fixture-ipc")]
    operation_ports: Option<std::collections::HashMap<OperationId, Arc<ControllerPort>>>,
    admission: OsDeploySendAdmission,
    workers: Semaphore,
    cap: u32,
    #[cfg(feature = "fixture-ipc")]
    credential_delivery: Option<FixtureDeliveryConfig>,
}
#[cfg(feature = "fixture-ipc")]
struct FixtureDeliveryConfig {
    secret: Vec<u8>,
    sink: postgres_store::FixtureCredentialSink,
}
impl OsDeployController {
    /// Explicitly opt into local fixture credential delivery. No signing key or
    /// private sink is inferred from environment variables or production config.
    #[cfg(feature = "fixture-ipc")]
    pub fn with_fixture_credential_delivery(
        mut self,
        secret: Vec<u8>,
        sink: postgres_store::FixtureCredentialSink,
    ) -> Result<Self, Error> {
        if secret.is_empty() {
            return Err(Error::Validation);
        }
        self.scheduler = self.scheduler.with_fixture_credential_delivery();
        self.credential_delivery = Some(FixtureDeliveryConfig { secret, sink });
        Ok(self)
    }
    pub fn new(
        store: PgStore,
        scheduler: Scheduler,
        fake: Arc<NativeFakePve>,
        cap: u32,
    ) -> Result<Self, Error> {
        Self::with_port(store, scheduler, fake, cap)
    }

    /// Construct a controller with a sealed local fixture capability.
    ///
    /// This opt-in seam preserves the same admission, collection, consuming
    /// dispatch permit and response persistence path as the native fixture.
    /// It does not grant a production observer mutation authority.
    ///
    /// ```no_run
    /// use std::sync::Arc;
    /// use operation_controller::OsDeployController;
    /// use postgres_store::{PgStore, Scheduler};
    /// use pve_port::fixture_support::{FixtureProvisioningPort, FixtureCheckpointClient, CheckpointBinding};
    /// fn construct(store: PgStore, scheduler: Scheduler, fixture: FixtureProvisioningPort,
    ///              checkpoint: FixtureCheckpointClient, binding: CheckpointBinding) {
    ///     let fixture = fixture.with_checkpoint(checkpoint, binding).unwrap();
    ///     assert!(OsDeployController::new_fixture(store, scheduler, Arc::new(fixture), 1).is_ok());
    /// }
    /// ```
    ///
    /// ```compile_fail
    /// use std::sync::Arc;
    /// use operation_controller::OsDeployController;
    /// use postgres_store::{PgStore, Scheduler};
    /// use pve_port::ReqwestPveObserver;
    /// fn reject_observer(store: PgStore, scheduler: Scheduler, observer: Arc<ReqwestPveObserver>) {
    ///     let _ = OsDeployController::new_fixture(store, scheduler, observer, 1);
    /// }
    /// ```
    #[cfg(feature = "fixture-ipc")]
    pub fn new_fixture(
        store: PgStore,
        scheduler: Scheduler,
        fixture: Arc<dyn pve_port::fixture_ipc::ControllerFixturePort>,
        cap: u32,
    ) -> Result<Self, Error> {
        Self::with_port(store, scheduler, fixture, cap)
    }

    /// Bind a fixture port once per admitted operation invocation. The returned
    /// capability is retained across every observation, checkpoint and dispatch;
    /// concurrent invocations never share a mutable current-operation selector.
    /// Missing bindings fail closed before any fixture access.
    #[cfg(feature = "fixture-ipc")]
    pub fn with_fixture_ports(
        mut self,
        ports: impl IntoIterator<
            Item = (
                OperationId,
                Arc<dyn pve_port::fixture_ipc::ControllerFixturePort>,
            ),
        >,
    ) -> Result<Self, Error> {
        let mut bindings = std::collections::HashMap::new();
        for (operation, port) in ports {
            if bindings.insert(operation, port).is_some() {
                return Err(Error::Validation);
            }
        }
        self.operation_ports = Some(bindings);
        Ok(self)
    }

    fn resolve_port(&self, operation: OperationId) -> Result<Arc<ControllerPort>, Error> {
        #[cfg(feature = "fixture-ipc")]
        if let Some(ports) = &self.operation_ports {
            return ports
                .get(&operation)
                .cloned()
                .ok_or(Error::CapabilityUnavailable);
        }
        let _ = operation;
        Ok(self.fake.clone())
    }

    fn with_port(
        store: PgStore,
        scheduler: Scheduler,
        fake: Arc<ControllerPort>,
        cap: u32,
    ) -> Result<Self, Error> {
        if cap == 0 || cap as usize > Semaphore::MAX_PERMITS {
            return Err(Error::Validation);
        }
        Ok(Self {
            store,
            scheduler,
            fake,
            #[cfg(feature = "fixture-ipc")]
            operation_ports: None,
            admission: OsDeploySendAdmission::new(),
            workers: Semaphore::new(cap as usize),
            cap,
            #[cfg(feature = "fixture-ipc")]
            credential_delivery: None,
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
        let port = self.resolve_port(operation)?;
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
                    ) || (plan.action() == ProvisioningActionV1::StartPe
                        && self.scheduler.fixture_start_pe_enabled())
                }) {
                    return Err(Error::CapabilityUnavailable);
                }
                let hash = snapshot.plan().workflow_sha256();
                #[cfg(feature = "fixture-ipc")]
                if before_close(
                    &mut closed,
                    self.scheduler.fixture_start_pe_requires_delivery(operation),
                )
                .await?
                    && self.credential_delivery.is_none()
                {
                    return Err(Error::CapabilityUnavailable);
                }
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
                    return Box::pin(self.reconcile(port.as_ref(), due, deadline, &mut closed))
                        .await;
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
                // Credential recovery can transfer an armed dispatch onto a
                // replacement lease already in Running. Re-read after claim;
                // the original snapshot may still describe Pending recovery.
                #[cfg(feature = "fixture-ipc")]
                let already_running_delivery = {
                    let claimed =
                        before_close(&mut closed, self.store.load_osdeploy_operation(operation))
                            .await?;
                    claimed.state() == ExecutionState::Running
                        && claimed.dispatch().is_some()
                        && before_close(
                            &mut closed,
                            self.scheduler.fixture_start_pe_requires_delivery(operation),
                        )
                        .await?
                };
                #[cfg(not(feature = "fixture-ipc"))]
                let already_running_delivery = false;
                if !already_running_delivery {
                    before_close(
                        &mut closed,
                        self.scheduler.start_osdeploy_bound(&grant, hash),
                    )
                    .await?;
                }
                Box::pin(self.run_grant(port.as_ref(), &grant, hash, deadline, &mut closed)).await
            }),
        )
        .await
        .map_err(|_| Error::TimedOut)?
    }

    async fn reconcile(
        &self,
        port: &ControllerPort,
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
                port,
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
        port: &ControllerPort,
        grant: &LeaseGrant,
        hash: &str,
        whole: Instant,
        closed: &mut watch::Receiver<bool>,
    ) -> Result<OsDeployProgress, Error> {
        loop {
            #[cfg(feature = "fixture-ipc")]
            self.resume_fixture_delivery(port, grant, hash, whole, closed)
                .await?;
            // Each owned phase is dropped before constructing the next. Only
            // durable identifiers cross the observation/decision boundary.
            let observed = self.observe(port, grant, hash, whole, closed).await;
            let result = match observed {
                Ok(observation) => {
                    self.advance(port, grant, hash, whole, observation, closed)
                        .await
                }
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
        port: &'a ControllerPort,
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
                    port,
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
        port: &'a ControllerPort,
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
                #[cfg(feature = "fixture-ipc")]
                if before_close(
                    closed,
                    self.scheduler
                        .fixture_start_pe_requires_delivery(grant.operation_id()),
                )
                .await?
                {
                    let config = self
                        .credential_delivery
                        .as_ref()
                        .ok_or(Error::CapabilityUnavailable)?;
                    let envelope = before_close(
                        closed,
                        self.scheduler.arm_fixture_start_pe(
                            grant,
                            revision,
                            event,
                            &request,
                            &config.secret,
                        ),
                    )
                    .await?;
                    let ack =
                        before_close(closed, async { envelope.deliver(&config.sink) }).await?;
                    before_close(
                        closed,
                        self.scheduler.record_fixture_delivery_ack(grant, &ack),
                    )
                    .await?;
                    self.resume_fixture_delivery(port, grant, hash, whole, closed)
                        .await?;
                    return Ok(None);
                }
                let (permit, capture) = before_close(
                    closed,
                    self.scheduler
                        .begin_osdeploy_pve_dispatch(grant, revision, event, &request),
                )
                .await?;
                before_close(closed, async {
                    #[cfg(feature = "fixture-ipc")]
                    port.provisioning_checkpoint(&request).await?;
                    #[cfg(not(feature = "fixture-ipc"))]
                    port.controller_checkpoint(FakeControllerCheckpoint::DispatchCommitted)
                        .await;
                    Ok::<_, Error>(())
                })
                .await?;
                send::submit_and_capture_once(
                    &self.admission,
                    &self.scheduler,
                    port,
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

    /// Recover an armed delivery before collecting physical outcome evidence.
    /// This requires the current live lease; it does not reclaim expired work.
    #[cfg(feature = "fixture-ipc")]
    async fn resume_fixture_delivery(
        &self,
        port: &ControllerPort,
        grant: &LeaseGrant,
        hash: &str,
        whole: Instant,
        closed: &mut watch::Receiver<bool>,
    ) -> Result<(), Error> {
        use postgres_store::FixtureDeliveryRecovery;
        if !before_close(
            closed,
            self.scheduler
                .fixture_start_pe_requires_delivery(grant.operation_id()),
        )
        .await?
        {
            return Ok(());
        }
        let config = self
            .credential_delivery
            .as_ref()
            .ok_or(Error::CapabilityUnavailable)?;
        let snapshot = before_close(
            closed,
            self.store.load_osdeploy_operation(grant.operation_id()),
        )
        .await?;
        if snapshot.dispatch().is_none() {
            return Ok(());
        }
        match before_close(
            closed,
            self.scheduler
                .recover_fixture_start_pe(grant, &config.secret),
        )
        .await?
        {
            FixtureDeliveryRecovery::Physical => return Err(Error::Validation),
            FixtureDeliveryRecovery::Exposed => return Ok(()),
            FixtureDeliveryRecovery::NeedsDelivery(envelope) => {
                let ack = before_close(closed, async { envelope.deliver(&config.sink) }).await?;
                before_close(
                    closed,
                    self.scheduler.record_fixture_delivery_ack(grant, &ack),
                )
                .await?;
            }
            FixtureDeliveryRecovery::Acknowledged => {}
        }
        if let Some((permit, capture)) =
            before_close(closed, self.scheduler.expose_fixture_start_pe_once(grant)).await?
        {
            send::submit_and_capture_once(
                &self.admission,
                &self.scheduler,
                port,
                grant,
                hash,
                permit,
                capture,
                whole,
            )
            .await?;
        }
        Ok(())
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
    #[cfg(feature = "fixture-ipc")]
    #[tokio::test]
    async fn checkpoint_failure_and_cancellation_stop_dispatch_continuation() {
        use pve_port::fixture_ipc::CheckpointError;
        use std::sync::atomic::{AtomicBool, Ordering};
        for failure in [
            CheckpointError::Rejected,
            CheckpointError::TimedOut,
            CheckpointError::Unavailable,
        ] {
            let (_sender, mut closed) = watch::channel(false);
            let submitted = AtomicBool::new(false);
            let result = async {
                before_close(&mut closed, async { Err::<(), _>(failure) }).await?;
                submitted.store(true, Ordering::SeqCst);
                Ok::<(), Error>(())
            }
            .await;
            assert_eq!(result, Err(Error::Checkpoint(failure)));
            assert!(!submitted.load(Ordering::SeqCst));
        }
        let (sender, mut closed) = watch::channel(false);
        let submitted = AtomicBool::new(false);
        let dispatch = async {
            before_close(
                &mut closed,
                std::future::pending::<Result<(), CheckpointError>>(),
            )
            .await?;
            submitted.store(true, Ordering::SeqCst);
            Ok::<(), Error>(())
        };
        let close = async {
            tokio::task::yield_now().await;
            sender.send(true).unwrap();
        };
        let (result, ()) = tokio::join!(dispatch, close);
        assert_eq!(result, Err(Error::AdmissionClosed));
        assert!(!submitted.load(Ordering::SeqCst));
    }
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

//! Local ownership accounting; no gate value grants database authority.
use super::{CALL_BOUND, OsDeployControllerError as Error};
use postgres_store::Scheduler;
use std::{sync::Mutex, time::Duration};
use tokio::sync::{Notify, watch};
use tokio::time::{Instant, timeout_at};

pub(super) struct OsDeploySendAdmission {
    lifecycle: tokio::sync::Mutex<()>,
    state: Mutex<AdmissionState>,
    drained: Notify,
    closed: watch::Sender<bool>,
}
struct AdmissionState {
    open: bool,
    in_flight: usize,
}
pub(super) struct OsDeployAdmissionGuard<'a> {
    admission: &'a OsDeploySendAdmission,
}

impl OsDeploySendAdmission {
    pub(super) fn new() -> Self {
        Self {
            lifecycle: tokio::sync::Mutex::new(()),
            state: Mutex::new(AdmissionState {
                open: false,
                in_flight: 0,
            }),
            drained: Notify::new(),
            closed: watch::channel(true).0,
        }
    }
    pub(super) fn try_enter(&self) -> Result<OsDeployAdmissionGuard<'_>, Error> {
        let mut state = self.state.lock().unwrap();
        if !state.open {
            return Err(Error::AdmissionClosed);
        }
        state.in_flight = state.in_flight.checked_add(1).ok_or(Error::Validation)?;
        Ok(OsDeployAdmissionGuard { admission: self })
    }
    pub(super) fn subscribe(&self) -> watch::Receiver<bool> {
        let _state = self.state.lock().unwrap();
        self.closed.subscribe()
    }
    fn close(&self) {
        let mut state = self.state.lock().unwrap();
        state.open = false;
        // A repeated close must invalidate an open that started while closed.
        self.closed.send_replace(true);
    }
    pub(super) async fn open(&self, scheduler: &Scheduler) -> Result<(), Error> {
        let deadline = Instant::now() + CALL_BOUND;
        let marker = self.subscribe();
        timeout_at(deadline, async {
            let _lifecycle = self.lifecycle.lock().await;
            scheduler.osdeploy_authority_snapshot().await?;
            let mut state = self.state.lock().unwrap();
            if !matches!(marker.has_changed(), Ok(false)) || state.in_flight != 0 {
                return Err(Error::AdmissionClosed);
            }
            if Instant::now() >= deadline {
                return Err(Error::TimedOut);
            }
            state.open = true;
            self.closed.send_replace(false);
            Ok(())
        })
        .await
        .map_err(|_| Error::TimedOut)?
    }
    pub(super) async fn close_and_drain(&self, bound: Duration) -> Result<(), Error> {
        let deadline = Instant::now() + bound.min(CALL_BOUND);
        self.close();
        let result = timeout_at(deadline, async {
            let _lifecycle = self.lifecycle.lock().await;
            self.close();
            loop {
                let notified = self.drained.notified();
                tokio::pin!(notified);
                notified.as_mut().enable();
                if self.state.lock().unwrap().in_flight == 0 {
                    return;
                }
                notified.await;
            }
        })
        .await;
        if result.is_err() {
            self.close();
            return Err(Error::TimedOut);
        }
        Ok(())
    }
}
impl Drop for OsDeployAdmissionGuard<'_> {
    fn drop(&mut self) {
        let mut state = self.admission.state.lock().unwrap();
        state.in_flight -= 1;
        if state.in_flight == 0 {
            self.admission.drained.notify_waiters();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    #[tokio::test]
    async fn close_publishes_before_contended_lifecycle_wait() {
        let admission = Arc::new(OsDeploySendAdmission::new());
        // Unit setup exercises local accounting only; real authority opening
        // is covered by the DB-backed integration races.
        admission.state.lock().unwrap().open = true;
        admission.closed.send_replace(false);
        let activity = admission.try_enter().unwrap();
        let (entered_tx, entered_rx) = tokio::sync::oneshot::channel();
        let (release_tx, release_rx) = tokio::sync::oneshot::channel();
        let holder = tokio::spawn({
            let admission = admission.clone();
            async move {
                let _lock = admission.lifecycle.lock().await;
                entered_tx.send(()).unwrap();
                release_rx.await.unwrap();
            }
        });
        tokio::time::timeout(Duration::from_secs(1), entered_rx)
            .await
            .unwrap()
            .unwrap();
        let mut signal = admission.closed.subscribe();
        let mut close = Box::pin(admission.close_and_drain(Duration::from_millis(100)));
        tokio::select! {
            result = &mut close => panic!("contended close completed early: {result:?}"),
            _ = signal.changed() => {},
        }
        assert!(matches!(admission.try_enter(), Err(Error::AdmissionClosed)));
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(1), close)
                .await
                .unwrap(),
            Err(Error::TimedOut)
        );
        assert_eq!(admission.state.lock().unwrap().in_flight, 1);
        release_tx.send(()).unwrap();
        tokio::time::timeout(Duration::from_secs(1), holder)
            .await
            .unwrap()
            .unwrap();
        drop(activity);
        admission
            .close_and_drain(Duration::from_secs(1))
            .await
            .unwrap();
        assert_eq!(admission.state.lock().unwrap().in_flight, 0);
    }
}

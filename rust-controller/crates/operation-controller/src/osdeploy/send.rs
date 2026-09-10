//! One consuming call, followed only by bounded original-response persistence.
use super::admission::OsDeploySendAdmission;
use super::{OsDeployControllerError as Error, READ_BOUND, before_close, budget_deadline};
use postgres_store::{
    LeaseGrant, OsDeployDispatchPermit, OsDeployExecutionError, OsDeployResponseCapture, Scheduler,
};
use pve_port::ProvisioningFakePort;
use std::time::Duration;
use tokio::time::{Instant, sleep, timeout_at};

pub(super) enum OsDeploySendObservation {
    ReceiptCaptured,
    Uncertain,
}

fn retry_delay(attempt: usize) -> Duration {
    match attempt {
        1 => Duration::from_millis(100),
        2 => Duration::from_millis(250),
        _ => Duration::ZERO,
    }
}

#[allow(
    clippy::too_many_arguments,
    reason = "governing private signature keeps consumed capabilities and original endpoint explicit"
)]
pub(super) async fn submit_and_capture_once<P: ProvisioningFakePort + ?Sized>(
    admission: &OsDeploySendAdmission,
    scheduler: &Scheduler,
    fake: &P,
    grant: &LeaseGrant,
    workflow_sha256: &str,
    permit: OsDeployDispatchPermit,
    capture: OsDeployResponseCapture,
    whole: Instant,
) -> Result<OsDeploySendObservation, Error> {
    if Instant::now() >= whole {
        return Err(Error::TimedOut);
    }
    let _guard = admission.try_enter()?;
    let mut closed = admission.subscribe();
    let check_start = Instant::now();
    let status = timeout_at(
        whole,
        before_close(
            &mut closed,
            scheduler.continuation_osdeploy_bound(grant, workflow_sha256),
        ),
    )
    .await
    .map_err(|_| Error::TimedOut)??;
    let deadline = budget_deadline(check_start, status.remaining(), whole);
    if Instant::now() >= deadline {
        return Err(Error::TimedOut);
    }
    // No await between checking the close marker and starting the owned call.
    if *closed.borrow() || !matches!(closed.has_changed(), Ok(false)) {
        return Err(Error::AdmissionClosed);
    }
    let receipt = match timeout_at(
        deadline.min(Instant::now() + READ_BOUND),
        permit.submit_fake_once(fake),
    )
    .await
    {
        Ok(Ok(receipt)) => receipt,
        Ok(Err(_)) | Err(_) => return Ok(OsDeploySendObservation::Uncertain),
    };
    // A returned original response needs no new authority. The original outer
    // controller deadline still owns this entire future, including each retry.
    for attempt in 0..=2 {
        if attempt > 0
            && timeout_at(whole, sleep(retry_delay(attempt)))
                .await
                .is_err()
        {
            return Ok(OsDeploySendObservation::Uncertain);
        }
        match timeout_at(
            whole,
            scheduler.record_osdeploy_pve_receipt(&capture, &receipt),
        )
        .await
        {
            Ok(Ok(())) => return Ok(OsDeploySendObservation::ReceiptCaptured),
            Ok(Err(OsDeployExecutionError::StorageUnavailable)) => {}
            Ok(Err(error)) => return Err(error.into()),
            Err(_) => return Ok(OsDeploySendObservation::Uncertain),
        }
    }
    Ok(OsDeploySendObservation::Uncertain)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn selected_retry_delays_are_exactly_100_then_250_milliseconds() {
        assert_eq!(
            (0..=2).map(retry_delay).collect::<Vec<_>>(),
            vec![
                Duration::ZERO,
                Duration::from_millis(100),
                Duration::from_millis(250)
            ]
        );
    }
}

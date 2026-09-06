//! Original dispatch response capture.
use super::*;
use controller_domain::RunId;
use pve_port::{
    MutationReceipt, NativeEvidenceSource, NativeFakePve, ProvisioningDispatchV1,
    ProvisioningFakePort, ProvisioningReceiptV1, PveWriteError,
};

/// A committed capability consumed by its single fake submission.
///
/// ```compile_fail,E0382
/// async fn moved(permit: postgres_store::OsDeployDispatchPermit, fake: &pve_port::NativeFakePve) {
///     drop(permit.submit_fake_once(fake));
///     drop(permit.submit_fake_once(fake));
/// }
/// ```
/// ```compile_fail,E0599
/// fn clone_handle(permit: postgres_store::OsDeployDispatchPermit) { let _ = permit.clone(); }
/// ```
/// ```compile_fail,E0277
/// fn copy_handle<T: Copy>() {}
/// copy_handle::<postgres_store::OsDeployDispatchPermit>();
/// ```
/// ```compile_fail,E0277
/// let _: postgres_store::OsDeployDispatchPermit = serde_json::from_str("{}").unwrap();
/// ```
/// ```compile_fail,E0277
/// fn serialize_handle(permit: postgres_store::OsDeployDispatchPermit) { let _ = serde_json::to_value(permit); }
/// ```
/// ```compile_fail,E0599
/// fn request(permit: postgres_store::OsDeployDispatchPermit) { let _ = permit.request(); }
/// ```
/// ```compile_fail,E0277
/// fn restore(snapshot: &postgres_store::OsDeployOperationSnapshot) {
///     let _: postgres_store::OsDeployDispatchPermit = snapshot.dispatch().unwrap().clone().into();
/// }
/// ```
pub struct OsDeployDispatchPermit {
    dispatch: ProvisioningDispatchV1,
}

/// Original-response identity, with no request or execution permission.
/// Borrowing it only permits persistence retries of the same original response.
///
/// ```compile_fail,E0599
/// fn clone_handle(capture: postgres_store::OsDeployResponseCapture) { let _ = capture.clone(); }
/// ```
/// ```compile_fail,E0277
/// fn copy_handle<T: Copy>() {}
/// copy_handle::<postgres_store::OsDeployResponseCapture>();
/// ```
/// ```compile_fail,E0277
/// let _: postgres_store::OsDeployResponseCapture = serde_json::from_str("{}").unwrap();
/// ```
/// ```compile_fail,E0277
/// fn serialize_handle(capture: postgres_store::OsDeployResponseCapture) { let _ = serde_json::to_value(capture); }
/// ```
/// ```compile_fail,E0599
/// fn request(capture: postgres_store::OsDeployResponseCapture) { let _ = capture.request(); }
/// ```
/// ```compile_fail,E0277
/// fn restore(snapshot: &postgres_store::OsDeployOperationSnapshot) {
///     let _: postgres_store::OsDeployResponseCapture = snapshot.dispatch().unwrap().clone().into();
/// }
/// ```
pub struct OsDeployResponseCapture {
    identity: OriginalOsDeployDispatchIdentity,
}

struct OriginalOsDeployDispatchIdentity {
    run_id: RunId,
    operation_id: OperationId,
    attempt_id: AttemptId,
    source: NativeEvidenceSource,
    workflow_sha256: String,
    pve_plan_sha256: String,
    request_sha256: String,
    dispatch_event_id: EventId,
    dispatch_revision: i64,
    original_generation: i64,
    preflight_event_id: EventId,
    dispatched_at: DateTime<Utc>,
}

// Only the successful dispatch transaction calls this, after commit.
pub(super) fn committed(
    dispatch: ProvisioningDispatchV1,
    dispatch_event_id: EventId,
) -> (OsDeployDispatchPermit, OsDeployResponseCapture) {
    let binding = dispatch.request().binding();
    let identity = OriginalOsDeployDispatchIdentity {
        run_id: binding.run_id(),
        operation_id: binding.operation_id(),
        attempt_id: binding.attempt_id(),
        source: dispatch.source(),
        workflow_sha256: binding.workflow_sha256().to_owned(),
        pve_plan_sha256: binding.operation_plan_sha256().to_owned(),
        request_sha256: dispatch.request_sha256().to_owned(),
        dispatch_event_id,
        dispatch_revision: dispatch.dispatch_revision() as i64,
        original_generation: dispatch.original_generation(),
        preflight_event_id: dispatch.preflight_event_id(),
        dispatched_at: dispatch.dispatched_at(),
    };
    (
        OsDeployDispatchPermit { dispatch },
        OsDeployResponseCapture { identity },
    )
}

impl OsDeployDispatchPermit {
    pub async fn submit_fake_once(
        self,
        fake: &NativeFakePve,
    ) -> Result<MutationReceipt, PveWriteError> {
        fake.submit_provisioning(self.dispatch.request()).await
    }
}

impl Scheduler {
    pub async fn record_osdeploy_pve_receipt(
        &self,
        capture: &OsDeployResponseCapture,
        receipt: &MutationReceipt,
    ) -> Result<(), Error> {
        Box::pin(async move {
            let original = &capture.identity;
            let mut tx = self.store.pool().begin().await?;
            // Original response capture grants no current execution authority.
            sqlx::query("SELECT singleton_key FROM rust_controller.orchestration_authority WHERE singleton_key=1 FOR SHARE")
                .fetch_one(&mut *tx).await?;
            let snapshot = locked_execution(&mut tx, original.operation_id).await?;
            let dispatch = snapshot.dispatch().ok_or(Error::Validation)?;
            let binding = dispatch.request().binding();
            let event: Uuid = sqlx::query_scalar("SELECT dispatch_event_id FROM rust_controller.osdeploy_pve_dispatches WHERE operation_id=$1")
                .bind(original.operation_id.as_uuid()).fetch_one(&mut *tx).await?;
            wire::require(snapshot.run_id() == original.run_id
                && snapshot.operation_id() == original.operation_id
                && snapshot.attempt_id() == Some(original.attempt_id)
                && binding.run_id() == original.run_id
                && binding.operation_id() == original.operation_id
                && binding.attempt_id() == original.attempt_id
                && dispatch.source() == original.source
                && binding.workflow_sha256() == original.workflow_sha256
                && binding.operation_plan_sha256() == original.pve_plan_sha256
                && dispatch.request_sha256() == original.request_sha256
                && event == original.dispatch_event_id.as_uuid()
                && dispatch.dispatch_revision() == original.dispatch_revision as u64
                && dispatch.original_generation() == original.original_generation
                && dispatch.preflight_event_id() == original.preflight_event_id
                && dispatch.dispatched_at() == original.dispatched_at)?;
            if let Some(first) = snapshot.receipt() {
                // Validate before classifying conflicting admitted responses; do
                // not replace the first server time on equivalent storage retry.
                ProvisioningReceiptV1::new(dispatch.clone(), first.accepted_at(), receipt.clone()).map_err(|_| Error::Validation)?;
                if first.receipt() != receipt { return Err(Error::Conflict); }
                tx.commit().await?;
                return Ok(());
            }
            let occupied: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.journal_events WHERE operation_id=$1 AND semantic_key='osdeploy:receipt')")
                .bind(original.operation_id.as_uuid()).fetch_one(&mut *tx).await?;
            if occupied { return Err(Error::Conflict); }
            let at = now(&mut tx).await?;
            ProvisioningReceiptV1::new(dispatch.clone(), at, receipt.clone()).map_err(|_| Error::Validation)?;
            let (kind, upid) = match receipt {
                MutationReceipt::Task(upid) => ("task", Some(upid.to_string())),
                MutationReceipt::SynchronousAccepted => ("synchronous", None),
            };
            let payload = wire::Receipt {
                contract_version: 1, action: "pve_receipt_captured".to_owned(),
                dispatch_event_id: original.dispatch_event_id, request_sha256: original.request_sha256.clone(),
                receipt_kind: kind.to_owned(), upid: upid.clone(), accepted_at: at,
            };
            wire::decode_exact::<wire::Receipt>(&wire::canonical(&payload)?, wire::DECISION_LIMIT)?;
            let event = EventId::new();
            let revision = snapshot.revision().checked_add(1).ok_or(Error::Validation)?;
            let journal = JournalEvent::new(event, original.operation_id, Some(original.attempt_id), revision,
                "osdeploy:receipt".to_owned(), wire::digest(&payload)?, EventKind::EvidenceRecorded,
                serde_json::to_value(&payload)?, at).map_err(|_| Error::Validation)?;
            sqlx::query("INSERT INTO rust_controller.journal_events(event_id,operation_id,attempt_id,aggregate_revision,semantic_key,payload_digest,event_kind,execution_state,payload,observed_at) VALUES($1,$2,$3,$4,'osdeploy:receipt',$5,'evidence_recorded',NULL,$6,$7)")
                .bind(event.as_uuid()).bind(original.operation_id.as_uuid()).bind(original.attempt_id.as_uuid()).bind(revision)
                .bind(journal.payload_digest()).bind(journal.payload()).bind(at).execute(&mut *tx).await?;
            sqlx::query("INSERT INTO rust_controller.outbox(event_id,operation_id,topic,payload) VALUES($1,$2,'journal_event',$3)")
                .bind(event.as_uuid()).bind(original.operation_id.as_uuid()).bind(serde_json::to_value(&journal)?).execute(&mut *tx).await?;
            sqlx::query("INSERT INTO rust_controller.osdeploy_pve_receipts(operation_id,receipt_event_id,receipt_kind,upid,accepted_at,recorded_at) VALUES($1,$2,$3,$4,$5,$5)")
                .bind(original.operation_id.as_uuid()).bind(event.as_uuid()).bind(kind).bind(upid).bind(at).execute(&mut *tx).await?;
            let changed = sqlx::query("UPDATE rust_controller.operations SET revision=$2,updated_at=clock_timestamp() WHERE operation_id=$1 AND revision=$3")
                .bind(original.operation_id.as_uuid()).bind(revision).bind(snapshot.revision()).execute(&mut *tx).await?.rows_affected();
            if changed != 1 { return Err(Error::FenceLost); }
            sqlx::query("INSERT INTO rust_controller.operation_projection(operation_id,state,revision,last_event_id,rebuilt_at) SELECT operation_id,state,revision,$2,clock_timestamp() FROM rust_controller.operations WHERE operation_id=$1 ON CONFLICT(operation_id) DO UPDATE SET state=EXCLUDED.state,revision=EXCLUDED.revision,last_event_id=EXCLUDED.last_event_id,rebuilt_at=EXCLUDED.rebuilt_at")
                .bind(original.operation_id.as_uuid()).bind(event.as_uuid()).execute(&mut *tx).await?;
            load::load_execution(&mut tx, original.operation_id).await?;
            tx.commit().await?;
            Ok(())
        }).await
    }
}

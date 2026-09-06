//! Typed physical observations and request advice; no dispatch authority.
use super::*;
use crate::osdeploy::execution::history;
use pve_port::{
    NativeEvidenceSource, ProvisioningEvaluationContextV1, ProvisioningEvaluationModeV1,
    ProvisioningEvidenceV1, ProvisioningMutationRequestV1,
};

impl PgStore {
    pub async fn load_osdeploy_pve_context(
        &self,
        operation: OperationId,
        expected_revision: i64,
        mode: ProvisioningEvaluationModeV1,
    ) -> Result<ProvisioningEvaluationContextV1, Error> {
        Box::pin(async move {
            let mut tx = self.pool().begin().await?;
            sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                .execute(&mut *tx)
                .await?;
            let context =
                history::load_context(&mut tx, operation, expected_revision, mode).await?;
            tx.commit().await?;
            Ok(context)
        })
        .await
    }

    pub async fn record_osdeploy_pve_evidence(
        &self,
        operation: OperationId,
        attempt: AttemptId,
        expected_revision: i64,
        evidence: &ProvisioningEvidenceV1,
    ) -> Result<EventId, Error> {
        Box::pin(async move {
        let mut tx = self.pool().begin().await?;
        // Observation has no generation ownership requirement and creates no
        // continuation permission; retain the common authority/run lock order.
        sqlx::query("SELECT singleton_key FROM rust_controller.orchestration_authority WHERE singleton_key=1 FOR SHARE")
            .fetch_one(&mut *tx).await?;
        let snapshot = locked_execution(&mut tx, operation).await?;
        history::enabled(snapshot.plan().stage())?;
        let f = evidence.facts();
        let b = &f.binding;
        wire::require(expected_revision >= 0 && b.evidence_fence() == expected_revision as u64
            && b.operation_id() == operation && b.attempt_id() == attempt && snapshot.attempt_id() == Some(attempt)
            && b.run_id() == snapshot.run_id() && b.workflow_sha256() == snapshot.plan().workflow_sha256()
            && Some(&f.plan) == snapshot.plan().pve() && f.source == NativeEvidenceSource::FakePve
            && b.operation_plan_sha256() == f.plan.fingerprint().map_err(|_| Error::Validation)?)?;
        let text = wire::canonical(evidence)?;
        let restored: ProvisioningEvidenceV1 = wire::decode_exact(&text, wire::EVIDENCE_LIMIT)?;
        wire::require(&restored == evidence)?;
        let hash = wire::digest(evidence)?;
        let key = format!("osdeploy:evidence:{}:{expected_revision}", attempt.as_uuid());
        if let Some(row) = sqlx::query("SELECT j.event_id,j.payload_digest,j.payload,e.event_id AS indexed_event FROM rust_controller.journal_events j LEFT JOIN rust_controller.osdeploy_pve_evidence e ON e.event_id=j.event_id AND e.operation_id=j.operation_id WHERE j.operation_id=$1 AND j.semantic_key=$2")
            .bind(operation.as_uuid()).bind(&key).fetch_optional(&mut *tx).await? {
            wire::require(row.try_get::<Option<Uuid>, _>("indexed_event")?.is_some())?;
            if row.try_get::<String, _>("payload_digest")? != hash
                || row.try_get::<serde_json::Value, _>("payload")? != serde_json::to_value(evidence)? {
                return Err(Error::Conflict);
            }
            let event = load::id(row.try_get("event_id")?)?;
            tx.commit().await?;
            return Ok(event);
        }
        if snapshot.revision() != expected_revision { return Err(Error::FenceLost); }
        wire::require(f.receipt.as_ref() == snapshot.receipt())?;
        let at = now(&mut tx).await?;
        wire::require(f.collected_at <= at && snapshot.activated_at().is_some_and(|t| f.collected_at >= t))?;
        let event = EventId::new();
        let revision = expected_revision.checked_add(1).ok_or(Error::Validation)?;
        let journal = JournalEvent::new(event, operation, Some(attempt), revision, key.clone(), hash.clone(),
            EventKind::EvidenceRecorded, serde_json::to_value(evidence)?, f.collected_at).map_err(|_| Error::Validation)?;
        sqlx::query("INSERT INTO rust_controller.journal_events(event_id,operation_id,attempt_id,aggregate_revision,semantic_key,payload_digest,event_kind,execution_state,payload,observed_at) VALUES($1,$2,$3,$4,$5,$6,'evidence_recorded',NULL,$7,$8)")
            .bind(event.as_uuid()).bind(operation.as_uuid()).bind(attempt.as_uuid()).bind(revision).bind(key)
            .bind(&hash).bind(journal.payload()).bind(f.collected_at).execute(&mut *tx).await?;
        sqlx::query("INSERT INTO rust_controller.outbox(event_id,operation_id,topic,payload) VALUES($1,$2,'journal_event',$3)")
            .bind(event.as_uuid()).bind(operation.as_uuid()).bind(serde_json::to_value(&journal)?).execute(&mut *tx).await?;
        sqlx::query("INSERT INTO rust_controller.osdeploy_pve_evidence(event_id,operation_id,run_id,attempt_id,evidence_revision,evidence_sha256,source,evidence_canonical_json) VALUES($1,$2,$3,$4,$5,$6,'fake_pve',$7)")
            .bind(event.as_uuid()).bind(operation.as_uuid()).bind(snapshot.run_id().as_uuid()).bind(attempt.as_uuid())
            .bind(revision).bind(hash).bind(text).execute(&mut *tx).await?;
        let changed = sqlx::query("UPDATE rust_controller.operations SET revision=$2,updated_at=clock_timestamp() WHERE operation_id=$1 AND revision=$3")
            .bind(operation.as_uuid()).bind(revision).bind(expected_revision).execute(&mut *tx).await?.rows_affected();
        if changed != 1 { return Err(Error::FenceLost); }
        sqlx::query("INSERT INTO rust_controller.operation_projection(operation_id,state,revision,last_event_id,rebuilt_at) SELECT operation_id,state,revision,$2,clock_timestamp() FROM rust_controller.operations WHERE operation_id=$1 ON CONFLICT(operation_id) DO UPDATE SET state=EXCLUDED.state,revision=EXCLUDED.revision,last_event_id=EXCLUDED.last_event_id,rebuilt_at=EXCLUDED.rebuilt_at")
            .bind(operation.as_uuid()).bind(event.as_uuid()).execute(&mut *tx).await?;
        load::load_execution(&mut tx, operation).await?;
        tx.commit().await?;
        Ok(event)
        }).await
    }

    pub async fn prepare_osdeploy_pve_request(
        &self,
        operation: OperationId,
        expected_revision: i64,
        preflight_event: EventId,
    ) -> Result<ProvisioningMutationRequestV1, Error> {
        Box::pin(async move {
            let mut tx = self.pool().begin().await?;
            sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                .execute(&mut *tx)
                .await?;
            let (context, evidence) =
                history::preflight(&mut tx, operation, expected_revision, preflight_event).await?;
            let request = history::request(&context, &evidence, now(&mut tx).await?)?;
            tx.commit().await?;
            Ok(request)
        })
        .await
    }
}

//! One lock order and atomic journal/index/projection persistence.
use super::*;

pub(super) fn scheduler_error(error: SchedulerError) -> Error {
    match error {
        SchedulerError::Database(e) => e.into(),
        _ => Error::FenceLost,
    }
}

pub(super) async fn authority(
    s: &Scheduler,
    tx: &mut Transaction<'_, Postgres>,
) -> Result<AuthoritySnapshot, Error> {
    if s.executor_kind != ExecutorKind::Rust {
        return Err(Error::FenceLost);
    }
    s.lock_authority(tx).await.map_err(scheduler_error)
}

pub(super) async fn locked_execution(
    tx: &mut Transaction<'_, Postgres>,
    operation: OperationId,
) -> Result<OsDeployOperationSnapshot, Error> {
    locked_execution_with_cap(tx, operation, false).await
}

pub(super) async fn locked_execution_with_cap(
    tx: &mut Transaction<'_, Postgres>,
    operation: OperationId,
    cap: bool,
) -> Result<OsDeployOperationSnapshot, Error> {
    let run: Uuid =
        sqlx::query_scalar("SELECT run_id FROM rust_controller.operations WHERE operation_id=$1")
            .bind(operation.as_uuid())
            .fetch_optional(&mut **tx)
            .await?
            .ok_or(Error::Validation)?;
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1,0))")
        .bind(format!("native:run:{run}"))
        .execute(&mut **tx)
        .await?;
    if cap {
        sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended('rust-controller:scheduler:os_deploy',0))").execute(&mut **tx).await?;
    }
    let operations: Vec<Uuid> = sqlx::query_scalar("SELECT operation_id FROM rust_controller.operations WHERE run_id=$1 ORDER BY operation_id FOR UPDATE")
        .bind(run).fetch_all(&mut **tx).await?;
    // Attempts and leases follow the same sorted operation order. This also
    // serializes future run cancellation without reversing a row lock edge.
    for op in operations {
        sqlx::query("SELECT attempt_id FROM rust_controller.attempts WHERE operation_id=$1 ORDER BY attempt_id FOR UPDATE")
            .bind(op).fetch_all(&mut **tx).await?;
        sqlx::query("SELECT operation_id FROM rust_controller.worker_leases WHERE operation_id=$1 FOR UPDATE")
            .bind(op).fetch_all(&mut **tx).await?;
    }
    let snapshot = load::load_execution(tx, operation).await?;
    Ok(snapshot)
}

#[cfg(feature = "fixture-ipc")]
pub(super) async fn requires_delivery(
    tx: &mut Transaction<'_, Postgres>,
    snapshot: &OsDeployOperationSnapshot,
) -> Result<bool, Error> {
    if snapshot.plan().stage() != OsDeployStage::StartPe {
        return Ok(false);
    }
    Ok(sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.fixture_osdeploy_origins WHERE run_id=$1 AND credential_sink_id IS NOT NULL)")
        .bind(snapshot.run_id().as_uuid()).fetch_one(&mut **tx).await?)
}

pub(super) async fn admit_delivery_policy(
    tx: &mut Transaction<'_, Postgres>,
    scheduler: &Scheduler,
    snapshot: &OsDeployOperationSnapshot,
) -> Result<(), Error> {
    #[cfg(feature = "fixture-ipc")]
    if requires_delivery(tx, snapshot).await? && !scheduler.fixture_credential_delivery {
        return Err(Error::CapabilityUnavailable);
    }
    #[cfg(not(feature = "fixture-ipc"))]
    let _ = (tx, scheduler, snapshot);
    Ok(())
}

/// A prepared credential dispatch is not evidence that VM send authority escaped.
pub(super) async fn require_delivery_exposed(
    tx: &mut Transaction<'_, Postgres>,
    snapshot: &OsDeployOperationSnapshot,
) -> Result<(), Error> {
    #[cfg(feature = "fixture-ipc")]
    if snapshot.dispatch().is_some() && requires_delivery(tx, snapshot).await? {
        let exposed: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.fixture_pe_delivery_exposures WHERE operation_id=$1)")
            .bind(snapshot.operation_id().as_uuid()).fetch_one(&mut **tx).await?;
        if !exposed {
            return Err(Error::CapabilityUnavailable);
        }
    }
    #[cfg(not(feature = "fixture-ipc"))]
    let _ = (tx, snapshot);
    Ok(())
}

pub(super) fn admit(
    scheduler: &Scheduler,
    snapshot: &OsDeployOperationSnapshot,
    hash: &str,
) -> Result<(), Error> {
    #[cfg(feature = "fixture-ipc")]
    let fixture_registration = scheduler.fixture_credential_delivery;
    #[cfg(not(feature = "fixture-ipc"))]
    let fixture_registration = false;
    if !(matches!(
        snapshot.plan().stage(),
        OsDeployStage::Clone | OsDeployStage::DiskCapacity | OsDeployStage::ConfigurePe
    ) || (scheduler.fixture_start_pe && snapshot.plan().stage() == OsDeployStage::StartPe)
        || (fixture_registration
            && matches!(
                snapshot.plan().stage(),
                OsDeployStage::PeRegister | OsDeployStage::PeComplete
            )))
    {
        return Err(Error::CapabilityUnavailable);
    }
    if !wire::hash_valid(hash) {
        return Err(Error::Validation);
    }
    if snapshot.cancelled() || hash != snapshot.plan().workflow_sha256() {
        return Err(Error::FenceLost);
    }
    Ok(())
}

pub(super) async fn now(tx: &mut Transaction<'_, Postgres>) -> Result<DateTime<Utc>, Error> {
    Ok(sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(&mut **tx)
        .await?)
}

pub(super) fn active_at(grant: &LeaseGrant, at: DateTime<Utc>) -> Result<(), Error> {
    if at < grant.acquired_at
        || at < grant.heartbeat_at
        || at >= grant.lease_expires_at
        || at >= grant.deadline_at
    {
        return Err(Error::FenceLost);
    }
    Ok(())
}

pub(super) async fn current_grant(
    tx: &mut Transaction<'_, Postgres>,
    scheduler: &Scheduler,
    original: &LeaseGrant,
    snapshot: &OsDeployOperationSnapshot,
) -> Result<(LeaseGrant, EventId), Error> {
    admit_delivery_policy(tx, scheduler, snapshot).await?;
    scheduler
        .validate_grant_owner(original)
        .map_err(scheduler_error)?;
    if snapshot.attempt_id() != Some(original.attempt_id())
        || !matches!(
            snapshot.state(),
            ExecutionState::Leased | ExecutionState::Running | ExecutionState::Waiting
        )
    {
        return Err(Error::FenceLost);
    }
    let row = sqlx::query("SELECT l.*,e.acquisition_event_id FROM rust_controller.worker_leases l JOIN rust_controller.osdeploy_lease_epochs e ON e.operation_id=l.operation_id AND e.lease_token_sha256=encode(sha256(convert_to(l.lease_token,'UTF8')),'hex') WHERE l.operation_id=$1")
        .bind(original.operation_id().as_uuid()).fetch_optional(&mut **tx).await?.ok_or(Error::FenceLost)?;
    if row.try_get::<Uuid, _>("attempt_id")? != original.attempt_id().as_uuid()
        || row.try_get::<String, _>("lease_token")? != original.lease_token().to_string()
        || row.try_get::<String, _>("worker_id")? != scheduler.worker_id
        || row.try_get::<String, _>("executor_kind")? != "rust"
        || row.try_get::<i64, _>("generation")? != scheduler.generation
        || row.try_get::<DateTime<Utc>, _>("acquired_at")? != *original.acquired_at()
        || snapshot.deadline_at() != Some(*original.deadline_at())
        || original.attempt_number() != 1
    {
        return Err(Error::FenceLost);
    }
    let mut grant = original.clone();
    grant.heartbeat_at = row.try_get("heartbeat_at")?;
    grant.lease_expires_at = row.try_get("lease_expires_at")?;
    Ok((grant, load::id(row.try_get("acquisition_event_id")?)?))
}

pub(super) fn envelope(
    snapshot: &OsDeployOperationSnapshot,
    attempt: AttemptId,
    generation: i64,
    revision: i64,
    at: DateTime<Utc>,
    detail: wire::Detail,
) -> Result<wire::DecisionEnvelope, Error> {
    Ok(wire::DecisionEnvelope {
        contract_version: 1,
        run_id: snapshot.run_id(),
        operation_id: snapshot.operation_id(),
        workflow_sha256: snapshot.plan().workflow_sha256().to_owned(),
        stage_sha256: snapshot.plan().fingerprint()?,
        attempt_id: Some(attempt),
        generation,
        before_revision: revision,
        evaluated_at: at,
        resolution: match &detail {
            wire::Detail::FixturePeRegistered(_) => Some(pve_port::NativeDecision::Satisfied),
            wire::Detail::FixturePeCompleted(d) => Some(if d.succeeded {
                pve_port::NativeDecision::Satisfied
            } else {
                pve_port::NativeDecision::Failed
            }),
            wire::Detail::FixtureGraceWaiting(_) => Some(pve_port::NativeDecision::Waiting),
            _ => None,
        },
        detail,
    })
}

pub(super) async fn append_osdeploy_decision(
    tx: &mut Transaction<'_, Postgres>,
    event: EventId,
    semantic_key: &str,
    decision: &wire::DecisionEnvelope,
) -> Result<i64, Error> {
    decision.validate()?;
    let text = wire::canonical(decision)?;
    // The same decoder enforces the closed persisted wire size and all fields.
    wire::DecisionEnvelope::decode(&text)?;
    let revision = decision
        .before_revision
        .checked_add(1)
        .ok_or(Error::Validation)?;
    let current: i64 =
        sqlx::query_scalar("SELECT revision FROM rust_controller.operations WHERE operation_id=$1")
            .bind(decision.operation_id.as_uuid())
            .fetch_one(&mut **tx)
            .await?;
    if current != decision.before_revision {
        return Err(Error::FenceLost);
    }
    let payload = serde_json::to_value(decision)?;
    let journal = JournalEvent::new(
        event,
        decision.operation_id,
        decision.attempt_id,
        revision,
        semantic_key.to_owned(),
        wire::digest(decision)?,
        EventKind::DecisionRecorded,
        payload,
        decision.evaluated_at,
    )
    .map_err(|_| Error::Validation)?;
    sqlx::query("INSERT INTO rust_controller.journal_events(event_id,operation_id,attempt_id,aggregate_revision,semantic_key,payload_digest,event_kind,execution_state,payload,observed_at) VALUES($1,$2,$3,$4,$5,$6,'decision_recorded',NULL,$7,$8)")
        .bind(event.as_uuid()).bind(decision.operation_id.as_uuid()).bind(decision.attempt_id.map(|a|a.as_uuid())).bind(revision).bind(semantic_key).bind(journal.payload_digest()).bind(journal.payload()).bind(decision.evaluated_at).execute(&mut **tx).await?;
    sqlx::query("INSERT INTO rust_controller.outbox(event_id,operation_id,topic,payload) VALUES($1,$2,'journal_event',$3)")
        .bind(event.as_uuid()).bind(decision.operation_id.as_uuid()).bind(serde_json::to_value(&journal)?).execute(&mut **tx).await?;
    sqlx::query("INSERT INTO rust_controller.osdeploy_decisions(operation_id,decision_revision,event_id,run_id,attempt_id,action,resolution,workflow_sha256,stage_sha256,generation,evaluated_at,payload_canonical_json) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)")
        .bind(decision.operation_id.as_uuid()).bind(revision).bind(event.as_uuid()).bind(decision.run_id.as_uuid()).bind(decision.attempt_id.map(|a|a.as_uuid())).bind(decision.action())
        .bind(decision.resolution.map(|v| serde_json::to_value(v).map(|v| v.as_str().map(str::to_owned))).transpose()?.flatten())
        .bind(&decision.workflow_sha256).bind(&decision.stage_sha256).bind(decision.generation).bind(decision.evaluated_at).bind(text).execute(&mut **tx).await?;
    let changed = sqlx::query("UPDATE rust_controller.operations SET revision=$2,updated_at=clock_timestamp() WHERE operation_id=$1 AND revision=$3")
        .bind(decision.operation_id.as_uuid()).bind(revision).bind(decision.before_revision).execute(&mut **tx).await?.rows_affected();
    if changed != 1 {
        return Err(Error::FenceLost);
    }
    sqlx::query("INSERT INTO rust_controller.operation_projection(operation_id,state,revision,last_event_id,rebuilt_at) SELECT operation_id,state,revision,$2,clock_timestamp() FROM rust_controller.operations WHERE operation_id=$1 ON CONFLICT(operation_id) DO UPDATE SET state=EXCLUDED.state,revision=EXCLUDED.revision,last_event_id=EXCLUDED.last_event_id,rebuilt_at=EXCLUDED.rebuilt_at")
        .bind(decision.operation_id.as_uuid()).bind(event.as_uuid()).execute(&mut **tx).await?;
    Ok(revision)
}

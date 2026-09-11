//! Restore immutable execution indexes against their exact journal facts.
use super::{OsDeployExecutionError as Error, OsDeployOperationSnapshot, wire::*};
use crate::osdeploy::{OsDeployRegistrationV1, records};
use chrono::{DateTime, Utc};
use controller_domain::{AttemptId, EventId, ExecutionState, OperationId};
use osdeploy_adapter::OsDeployStage;
use pve_port::{
    MutationReceipt, NativeDecision, NativeEvidenceSource, ProvisioningDispatchInputV1,
    ProvisioningDispatchV1, ProvisioningEvidenceV1, ProvisioningMutationRequestV1,
    ProvisioningReceiptV1, Upid,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sqlx::{Postgres, Row, Transaction, postgres::PgRow};
use std::collections::BTreeMap;
use uuid::Uuid;

/// Resolve package bytes from the immutable origin selector. Absent origins
/// are historical physical fixtures and retain the original materialization.
#[cfg(feature = "fixture-ipc")]
pub(crate) async fn fixture_package(
    tx: &mut Transaction<'_, Postgres>,
    registration: &OsDeployRegistrationV1,
) -> Result<crate::MaterializedPePackageSemanticsV1, Error> {
    let completion: Option<bool> = sqlx::query_scalar(
        "SELECT completion_package FROM rust_controller.fixture_osdeploy_origins WHERE run_id=$1",
    )
    .bind(registration.ids().run_id().as_uuid())
    .fetch_optional(&mut **tx)
    .await?;
    if completion == Some(true) {
        registration.materialize_fixture_completion_package()
    } else {
        registration.materialize_pe_package_semantics()
    }
    .map_err(|_| Error::Validation)
}

/// Reuse declaration validation under the scheduler's existing run lock.
pub(crate) async fn load_registration(
    tx: &mut Transaction<'_, Postgres>,
    run: controller_domain::RunId,
) -> Result<OsDeployRegistrationV1, Error> {
    Ok(records::load(tx, run).await?)
}

pub(crate) async fn load_execution(
    tx: &mut Transaction<'_, Postgres>,
    operation: OperationId,
) -> Result<OsDeployOperationSnapshot, Error> {
    // Large typed proof values must not multiply every caller's future frame.
    let records = Box::pin(load_records(tx, operation)).await?;
    Box::pin(super::history::validate(tx, &records)).await?;
    Ok(records.snapshot)
}

pub(super) struct Records {
    pub(super) snapshot: OsDeployOperationSnapshot,
    pub(super) registration: OsDeployRegistrationV1,
    pub(super) journal: BTreeMap<Uuid, Event>,
    pub(super) decisions: Vec<Decision>,
    pub(super) evidence: BTreeMap<Uuid, Evidence>,
    pub(super) receipt_revision: Option<i64>,
    pub(super) cancelled_at: Option<DateTime<Utc>>,
}

pub(super) async fn load_records(
    tx: &mut Transaction<'_, Postgres>,
    operation: OperationId,
) -> Result<Records, Error> {
    let run: Uuid =
        sqlx::query_scalar("SELECT run_id FROM rust_controller.operations WHERE operation_id=$1")
            .bind(operation.as_uuid())
            .fetch_one(&mut **tx)
            .await?;
    let registration = records::load(tx, id(run)?).await?;
    let stage = OsDeployStage::ALL
        .into_iter()
        .find(|s| registration.ids().operation(*s) == operation)
        .ok_or(Error::Validation)?;
    let plan = registration.stage(stage).clone();
    let row =
        sqlx::query("SELECT state,revision FROM rust_controller.operations WHERE operation_id=$1")
            .bind(operation.as_uuid())
            .fetch_one(&mut **tx)
            .await?;
    let current: ExecutionState = decode_name(row.try_get("state")?)?;
    let revision: i64 = row.try_get("revision")?;
    let journal = load_journal(tx, operation, revision).await?;
    let decisions = load_decisions(tx, &registration).await?;
    let own: Vec<_> = decisions
        .values()
        .filter(|d| d.value.operation_id == operation)
        .collect();
    for event in journal.values().filter(|e| e.kind == "decision_recorded") {
        require(
            decisions
                .get(&event.id.as_uuid())
                .is_some_and(|d| d.revision == event.revision),
        )?;
    }
    let bindings = rows(tx, "osdeploy_attempt_bindings", operation).await?;
    let attempts = rows(tx, "attempts", operation).await?;
    require(bindings.len() <= 1 && attempts.len() == bindings.len())?;
    let binding = bindings.first();
    let (attempt, activated, deadline) = if let Some(b) = binding {
        let a = &attempts[0];
        let attempt: AttemptId = id(b.try_get("attempt_id")?)?;
        let activated: DateTime<Utc> = b.try_get("activated_at")?;
        let deadline: DateTime<Utc> = b.try_get("deadline_at")?;
        require(
            b.try_get::<Uuid, _>("run_id")? == run
                && a.try_get::<Uuid, _>("attempt_id")? == attempt.as_uuid()
                && a.try_get::<i32, _>("attempt_number")? == 1
                && a.try_get::<DateTime<Utc>, _>("started_at")? == activated
                && a.try_get::<DateTime<Utc>, _>("deadline_at")? == deadline
                && decode_name::<ExecutionState>(a.try_get("state")?)? == current
                && current.is_terminal()
                    == a.try_get::<Option<DateTime<Utc>>, _>("completed_at")?
                        .is_some()
                && activated < deadline
                && b.try_get::<String, _>("activation_mode")? == "leased",
        )?;
        let activation = decision(&decisions, b.try_get("activation_event_id")?, operation)?;
        let Detail::StageActivated(d) = &activation.value.detail else {
            return Err(Error::Validation);
        };
        require(
            activation.value.attempt_id == Some(attempt)
                && activation.value.evaluated_at == activated
                && d.deadline_at == deadline
                && name(d.scope_key)? == b.try_get::<String, _>("scope_key")?,
        )?;
        require(
            own.iter()
                .filter(|d| matches!(d.value.detail, Detail::StageActivated(_)))
                .count()
                == 1,
        )?;
        (Some(attempt), Some(activated), Some(deadline))
    } else {
        require(
            !own.iter()
                .any(|d| matches!(d.value.detail, Detail::StageActivated(_))),
        )?;
        (None, None, None)
    };
    for d in &own {
        require(d.value.attempt_id == attempt)?;
    }
    for event in journal.values() {
        require(
            event.attempt == attempt
                || (event.kind == "evidence_recorded" && event.attempt.is_none()),
        )?;
    }

    let scopes = load_scopes(tx, &registration, &decisions).await?;
    if let Some(b) = binding {
        let scope: Scope = decode_name(b.try_get("scope_key")?)?;
        require(scope == stage_scope(stage))?;
        let s = scopes.get(&name(scope)?).ok_or(Error::Validation)?;
        require(Some(s.deadline) == deadline && activated.is_some_and(|t| t >= s.opened))?;
    }
    validate_activations(&registration, &own, &decisions, &scopes, stage, operation)?;
    let epochs = load_epochs(tx, operation, run, attempt, deadline, &decisions).await?;
    let evidence = load_evidence(tx, operation, run, attempt, &plan, &journal).await?;
    let dispatch = load_dispatch(
        tx, operation, run, attempt, &plan, &journal, &decisions, &epochs, &evidence,
    )
    .await?;
    let receipt = load_receipt(tx, operation, dispatch.as_ref(), &journal).await?;
    for e in evidence.values() {
        if let Some(captured) = &e.value.facts().receipt {
            require(
                receipt.as_ref().map(|r| &r.value) == Some(captured)
                    && dispatch
                        .as_ref()
                        .is_some_and(|d| captured.dispatch() == &d.value),
            )?;
        }
    }
    let cancelled = load_cancellation(tx, &registration, &decisions).await?;
    validate_decision_links(
        &own,
        &decisions,
        &scopes,
        &epochs,
        &evidence,
        dispatch.as_ref(),
        receipt.as_ref(),
        operation,
        attempt,
        deadline,
        cancelled,
        stage_scope(stage),
    )?;
    let next_check = validate_state_history(
        &journal, &own, &decisions, current, attempt, activated, deadline,
    )?;
    if let Some(a) = attempts.first() {
        let latest_state = journal
            .values()
            .filter(|e| e.kind == "execution_state_changed")
            .max_by_key(|e| e.revision);
        require(
            a.try_get::<Option<DateTime<Utc>>, _>("completed_at")?
                == latest_state
                    .filter(|e| e.state.is_some_and(|s| s.is_terminal()))
                    .map(|e| e.at),
        )?;
    }
    validate_live_lease(tx, operation, attempt, deadline, &epochs, &own, current).await?;
    // Scheduling rows are disposable hints. Their absence/staleness is valid;
    // the snapshot's due time is always replayed from immutable decisions.
    let cancelled_at = decisions
        .values()
        .find(|d| matches!(d.value.detail, Detail::RunCancelled(_)))
        .map(|d| d.value.evaluated_at);
    let receipt_revision = receipt.as_ref().map(|r| r.revision);
    let credential_history = own.iter().any(|d| {
        matches!(
            d.value.detail,
            Detail::CredentialDeliveryReclaimed(_)
                | Detail::LeaseAcquired(Acquisition {
                    purpose: Purpose::ReclaimedCredentialDelivery,
                    ..
                })
        )
    });
    require(!credential_history || plan.stage() == OsDeployStage::StartPe)?;
    #[cfg(not(feature = "fixture-ipc"))]
    require(!credential_history)?;
    #[cfg(feature = "fixture-ipc")]
    if plan.stage() == OsDeployStage::StartPe {
        let session = sqlx::query("SELECT s.*,e.payload_canonical_json AS epoch_json,EXISTS(SELECT 1 FROM rust_controller.osdeploy_pve_dispatches d WHERE d.operation_id=s.operation_id AND d.dispatch_event_id=s.dispatch_event_id AND d.lease_acquisition_event_id=s.lease_acquisition_event_id AND d.original_generation=s.original_generation) AS dispatch_matches FROM rust_controller.fixture_pe_boot_sessions s JOIN rust_controller.osdeploy_decisions e ON e.event_id=s.lease_acquisition_event_id AND e.operation_id=s.operation_id WHERE s.operation_id=$1")
            .bind(operation.as_uuid()).fetch_optional(&mut **tx).await?;
        require(session.is_some() == dispatch.is_some())?;
        let sink: Option<Uuid> = sqlx::query_scalar("SELECT credential_sink_id FROM rust_controller.fixture_osdeploy_origins WHERE run_id=$1")
            .bind(run).fetch_optional(&mut **tx).await?.flatten();
        let delivery = sqlx::query("SELECT d.*,a.acknowledged_at,x.exposed_at,x.generation AS exposure_generation,x.worker_id AS exposure_worker,e.operation_id AS exposure_operation,e.generation AS epoch_generation,e.worker_id AS epoch_worker,e.acquired_at,e.initial_expires_at FROM rust_controller.fixture_pe_deliveries d LEFT JOIN rust_controller.fixture_pe_delivery_acks a USING(operation_id) LEFT JOIN rust_controller.fixture_pe_delivery_exposures x USING(operation_id) LEFT JOIN rust_controller.osdeploy_lease_epochs e ON e.acquisition_event_id=x.lease_acquisition_event_id WHERE d.operation_id=$1")
            .bind(operation.as_uuid()).fetch_optional(&mut **tx).await?;
        require(delivery.is_some() == (sink.is_some() && dispatch.is_some()))?;
        require(!credential_history || delivery.is_some())?;
        if let Some(delivery) = delivery {
            let d = dispatch.as_ref().ok_or(Error::Validation)?;
            let package = fixture_package(tx, &registration).await?;
            let deadline = scopes
                .get("pe_registration")
                .ok_or(Error::Validation)?
                .deadline;
            require(
                delivery.try_get::<Uuid, _>("run_id")? == run
                    && Some(delivery.try_get::<Uuid, _>("attempt_id")?)
                        == attempt.map(|a| a.as_uuid())
                    && delivery.try_get::<Uuid, _>("dispatch_event_id")? == d.event.as_uuid()
                    && delivery.try_get::<String, _>("package_sha256")?
                        == package.envelope_sha256()
                    && Some(delivery.try_get::<Uuid, _>("sink_id")?) == sink
                    && delivery.try_get::<i64, _>("expires_at")? == deadline.timestamp()
                    && delivery.try_get::<Vec<u8>, _>("alias_sha256")?.len() == 32,
            )?;
            let ack: Option<DateTime<Utc>> = delivery.try_get("acknowledged_at")?;
            // Reclaims preserve the original dispatch/session. Later delivery
            // is legal, but acceptance must not predate any reclaim decision.
            for reclaim in own
                .iter()
                .filter(|d| matches!(d.value.detail, Detail::CredentialDeliveryReclaimed(_)))
            {
                require(ack.is_none_or(|at| at >= reclaim.value.evaluated_at))?;
            }
            if let Some(at) = ack {
                require(at >= d.value.dispatched_at() && at < deadline)?;
            }
            if let Some(exposed) = delivery.try_get::<Option<DateTime<Utc>>, _>("exposed_at")? {
                require(
                    ack.is_some_and(|at| exposed >= at)
                        && exposed < deadline
                        && delivery.try_get::<Uuid, _>("exposure_operation")?
                            == operation.as_uuid()
                        && delivery.try_get::<i64, _>("exposure_generation")?
                            == delivery.try_get::<i64, _>("epoch_generation")?
                        && delivery.try_get::<String, _>("exposure_worker")?
                            == delivery.try_get::<String, _>("epoch_worker")?
                        && exposed >= delivery.try_get::<DateTime<Utc>, _>("acquired_at")?,
                )?;
            }
        }
        if let (Some(s), Some(d)) = (session, dispatch.as_ref()) {
            let package = fixture_package(tx, &registration).await?;
            let epoch = DecisionEnvelope::decode(&s.try_get::<String, _>("epoch_json")?)?;
            let Detail::LeaseAcquired(lease) = epoch.detail else {
                return Err(Error::Validation);
            };
            let scope = scopes.get("pe_registration").ok_or(Error::Validation)?;
            require(
                s.try_get::<bool, _>("dispatch_matches")?
                    && s.try_get::<Uuid, _>("run_id")? == run
                    && Some(id::<AttemptId>(s.try_get("attempt_id")?)?) == attempt
                    && s.try_get::<Uuid, _>("dispatch_event_id")? == d.event.as_uuid()
                    && s.try_get::<String, _>("package_sha256")? == package.envelope_sha256()
                    && s.try_get::<Vec<u8>, _>("package_canonical_bytes")?
                        == package.canonical_bytes()
                    && s.try_get::<i64, _>("original_generation")? == d.value.original_generation()
                    && s.try_get::<String, _>("worker_id")? == lease.worker_id
                    && s.try_get::<DateTime<Utc>, _>("opened_at")? == d.value.dispatched_at()
                    && scope.anchor == d.event
                    && scope.anchor_op == operation
                    && s.try_get::<DateTime<Utc>, _>("registration_deadline")? == scope.deadline,
            )?;
        }
    }
    #[cfg(not(feature = "fixture-ipc"))]
    require(
        !own.iter()
            .any(|d| matches!(d.value.detail, Detail::FixturePeRegistered(_))),
    )?;
    #[cfg(feature = "fixture-ipc")]
    {
        let selected: Vec<_> = own
            .iter()
            .filter(|d| matches!(d.value.detail, Detail::FixturePeRegistered(_)))
            .collect();
        let rows = sqlx::query("SELECT r.*,d.alias_sha256 AS delivered_alias,d.dispatch_event_id,d.package_sha256 FROM rust_controller.fixture_pe_registrations r JOIN rust_controller.fixture_pe_deliveries d ON d.operation_id=r.start_operation_id WHERE r.operation_id=$1")
            .bind(operation.as_uuid()).fetch_all(&mut **tx).await?;
        require(selected.len() <= 1 && rows.len() == selected.len())?;
        if let (Some(d), Some(row)) = (selected.first(), rows.first()) {
            let Detail::FixturePeRegistered(value) = &d.value.detail else {
                return Err(Error::Validation);
            };
            let identity = crate::FixturePeRegistrationIdentity::expected(&registration);
            let completion = scopes.get("pe_completion").ok_or(Error::Validation)?;
            require(
                plan.stage() == OsDeployStage::PeRegister
                    && current == ExecutionState::Satisfied
                    && row.try_get::<Uuid, _>("run_id")? == run
                    && Some(id::<AttemptId>(row.try_get("attempt_id")?)?) == attempt
                    && row.try_get::<Uuid, _>("selected_event_id")? == d.event.as_uuid()
                    && row.try_get::<DateTime<Utc>, _>("selected_at")? == d.value.evaluated_at
                    && row.try_get::<String, _>("identity_canonical_json")?
                        == canonical(&identity)?
                    && row.try_get::<String, _>("identity_sha256")? == digest(&identity)?
                    && value.identity_sha256 == digest(&identity)?
                    && row.try_get::<Vec<u8>, _>("alias_sha256")?
                        == row.try_get::<Vec<u8>, _>("delivered_alias")?
                    && row.try_get::<Uuid, _>("start_operation_id")?
                        == value.start_operation_id.as_uuid()
                    && value.start_operation_id
                        == registration.ids().operation(OsDeployStage::StartPe)
                    && row.try_get::<Uuid, _>("dispatch_event_id")?
                        == value.dispatch_event_id.as_uuid()
                    && row.try_get::<String, _>("package_sha256")? == value.package_sha256
                    && completion.anchor == d.event
                    && completion.anchor_op == operation
                    && completion.opened == d.value.evaluated_at,
            )?;
            require_fixture_registration_origin(tx, &registration, d.value.evaluated_at).await?;
        }
    }
    let own = own.into_iter().cloned().collect();
    let snapshot = OsDeployOperationSnapshot {
        operation_id: operation,
        run_id: id(run)?,
        revision,
        state: current,
        cancelled,
        plan,
        attempt_id: attempt,
        activated_at: activated,
        deadline_at: deadline,
        // A run fence cancels future checks even when an earlier terminal
        // Unknown decision and its original scheduling history are preserved.
        next_check_at: if cancelled { None } else { next_check },
        dispatch: dispatch.map(|d| d.value),
        receipt: receipt.map(|r| r.value),
    };
    Ok(Records {
        snapshot,
        registration,
        journal,
        decisions: own,
        evidence,
        receipt_revision,
        cancelled_at,
    })
}

pub(crate) fn id<T: serde::de::DeserializeOwned>(v: Uuid) -> Result<T, Error> {
    Ok(serde_json::from_value(serde_json::to_value(v)?)?)
}
fn decode_name<T: serde::de::DeserializeOwned>(s: String) -> Result<T, Error> {
    Ok(serde_json::from_value(s.into())?)
}
fn name<T: Serialize>(v: T) -> Result<String, Error> {
    serde_json::to_value(v)?
        .as_str()
        .map(str::to_owned)
        .ok_or(Error::Validation)
}
async fn rows(
    tx: &mut Transaction<'_, Postgres>,
    table: &str,
    operation: OperationId,
) -> Result<Vec<PgRow>, Error> {
    // Table names are internal literals, never caller data.
    Ok(sqlx::query(&format!(
        "SELECT * FROM rust_controller.{table} WHERE operation_id=$1"
    ))
    .bind(operation.as_uuid())
    .fetch_all(&mut **tx)
    .await?)
}
pub(super) struct Event {
    pub(super) id: EventId,
    pub(super) attempt: Option<AttemptId>,
    pub(super) revision: i64,
    pub(super) kind: String,
    pub(super) state: Option<ExecutionState>,
    pub(super) payload: Value,
    pub(super) at: DateTime<Utc>,
}
async fn load_journal(
    tx: &mut Transaction<'_, Postgres>,
    op: OperationId,
    revision: i64,
) -> Result<BTreeMap<Uuid, Event>, Error> {
    let rows=sqlx::query("SELECT * FROM rust_controller.journal_events WHERE operation_id=$1 ORDER BY aggregate_revision")
        .bind(op.as_uuid()).fetch_all(&mut **tx).await?;
    require(i64::try_from(rows.len()).ok() == Some(revision))?;
    let mut out = BTreeMap::new();
    for (index, r) in rows.iter().enumerate() {
        let payload: Value = r.try_get("payload")?;
        require(
            r.try_get::<i64, _>("aggregate_revision")? == index as i64 + 1
                && digest(&payload)? == r.try_get::<String, _>("payload_digest")?,
        )?;
        let e = Event {
            id: id(r.try_get("event_id")?)?,
            attempt: r
                .try_get::<Option<Uuid>, _>("attempt_id")?
                .map(id)
                .transpose()?,
            revision: r.try_get("aggregate_revision")?,
            kind: r.try_get("event_kind")?,
            state: r
                .try_get::<Option<String>, _>("execution_state")?
                .map(decode_name)
                .transpose()?,
            payload,
            at: r.try_get("observed_at")?,
        };
        require(
            (e.kind == "execution_state_changed") == e.state.is_some()
                && e.kind != "command_accepted",
        )?;
        out.insert(e.id.as_uuid(), e);
    }
    Ok(out)
}
#[derive(Clone)]
pub(super) struct Decision {
    pub(super) event: EventId,
    pub(super) revision: i64,
    pub(super) value: DecisionEnvelope,
}
fn decision(
    ds: &BTreeMap<Uuid, Decision>,
    event: Uuid,
    op: OperationId,
) -> Result<&Decision, Error> {
    ds.get(&event)
        .filter(|d| d.value.operation_id == op)
        .ok_or(Error::Validation)
}
async fn load_decisions(
    tx: &mut Transaction<'_, Postgres>,
    reg: &OsDeployRegistrationV1,
) -> Result<BTreeMap<Uuid, Decision>, Error> {
    let rows=sqlx::query("SELECT d.*,(SELECT attempt_id FROM rust_controller.osdeploy_attempt_bindings b WHERE b.operation_id=d.operation_id) AS bound_attempt,e.event_kind,e.aggregate_revision,e.attempt_id AS journal_attempt,e.payload,e.payload_digest,e.observed_at,e.execution_state FROM rust_controller.osdeploy_decisions d LEFT JOIN rust_controller.journal_events e ON e.event_id=d.event_id AND e.operation_id=d.operation_id WHERE d.run_id=$1 ORDER BY d.decision_revision")
        .bind(reg.ids().run_id().as_uuid()).fetch_all(&mut **tx).await?;
    let mut out = BTreeMap::new();
    for r in rows {
        let text: String = r.try_get("payload_canonical_json")?;
        let v = DecisionEnvelope::decode(&text)?;
        let stage = OsDeployStage::ALL
            .into_iter()
            .find(|s| reg.ids().operation(*s) == v.operation_id)
            .ok_or(Error::Validation)?;
        let revision: i64 = r.try_get("decision_revision")?;
        require(
            v.attempt_id.map(|a| a.as_uuid()) == r.try_get::<Option<Uuid>, _>("bound_attempt")?
                && (!matches!(v.detail, Detail::StageActivated(_))
                    || super::history::enabled(stage).is_ok()
                    || (cfg!(feature = "fixture-ipc") && stage == OsDeployStage::PeRegister)),
        )?;
        require(
            canonical(&v)? == text
                && serde_json::to_value(&v)? == r.try_get::<Value, _>("payload")?
                && digest(&v)? == r.try_get::<String, _>("payload_digest")?
                && r.try_get::<String, _>("event_kind")? == "decision_recorded"
                && r.try_get::<Option<String>, _>("execution_state")?.is_none()
                && revision == r.try_get::<i64, _>("aggregate_revision")?
                && revision == v.before_revision + 1
                && v.run_id == reg.ids().run_id()
                && v.operation_id.as_uuid() == r.try_get::<Uuid, _>("operation_id")?
                && v.attempt_id.map(|a| a.as_uuid())
                    == r.try_get::<Option<Uuid>, _>("attempt_id")?
                && v.attempt_id.map(|a| a.as_uuid())
                    == r.try_get::<Option<Uuid>, _>("journal_attempt")?
                && v.workflow_sha256 == reg.ids().workflow_sha256()
                && v.workflow_sha256 == r.try_get::<String, _>("workflow_sha256")?
                && v.stage_sha256 == reg.stage(stage).fingerprint()?
                && v.stage_sha256 == r.try_get::<String, _>("stage_sha256")?
                && v.generation == r.try_get::<i64, _>("generation")?
                && v.evaluated_at == r.try_get::<DateTime<Utc>, _>("evaluated_at")?
                && v.evaluated_at == r.try_get::<DateTime<Utc>, _>("observed_at")?
                && v.action() == r.try_get::<String, _>("action")?
                && v.resolution.map(name).transpose()?
                    == r.try_get::<Option<String>, _>("resolution")?,
        )?;
        let event: EventId = id(r.try_get("event_id")?)?;
        out.insert(
            event.as_uuid(),
            Decision {
                event,
                revision,
                value: v,
            },
        );
    }
    for op in reg.ids().operations() {
        let mut ordered: Vec<_> = out
            .values()
            .filter(|d| d.value.operation_id == *op)
            .collect();
        ordered.sort_by_key(|d| d.revision);
        require(
            ordered
                .windows(2)
                .all(|pair| pair[0].value.evaluated_at <= pair[1].value.evaluated_at),
        )?;
    }
    Ok(out)
}

struct Deadline {
    anchor_op: OperationId,
    anchor: EventId,
    opened: DateTime<Utc>,
    budget: u32,
    deadline: DateTime<Utc>,
}
pub(crate) fn stage_scope(stage: OsDeployStage) -> Scope {
    use OsDeployStage::*;
    match stage {
        Clone => Scope::MutationClone,
        DiskCapacity => Scope::MutationDiskCapacity,
        ConfigurePe => Scope::MutationConfigurePe,
        StartPe => Scope::MutationStartPe,
        PeEnsureStopped => Scope::MutationPeEnsureStopped,
        ConfigureDisk => Scope::MutationConfigureDisk,
        StartDisk => Scope::MutationStartDisk,
        PeRegister => Scope::PeRegistration,
        PeComplete => Scope::PeCompletion,
        PeShutdownGrace => Scope::ShutdownGrace,
        InstallQga | VerifyQga | InstallQgaWatchdog | InstallAgent | AgentHeartbeat
        | VerifyOperational => Scope::FullOs,
    }
}
async fn load_scopes(
    tx: &mut Transaction<'_, Postgres>,
    reg: &OsDeployRegistrationV1,
    ds: &BTreeMap<Uuid, Decision>,
) -> Result<BTreeMap<String, Deadline>, Error> {
    let rows = sqlx::query("SELECT * FROM rust_controller.osdeploy_deadlines WHERE run_id=$1")
        .bind(reg.ids().run_id().as_uuid())
        .fetch_all(&mut **tx)
        .await?;
    let mut out = BTreeMap::new();
    for r in rows {
        let key: String = r.try_get("scope_key")?;
        let scope: Scope = decode_name(key.clone())?;
        let s = Deadline {
            anchor_op: id(r.try_get("anchor_operation_id")?)?,
            anchor: id(r.try_get("anchor_event_id")?)?,
            opened: r.try_get("opened_at")?,
            budget: r
                .try_get::<i32, _>("budget_seconds")?
                .try_into()
                .map_err(|_| Error::Validation)?,
            deadline: r.try_get("deadline_at")?,
        };
        let d = decision(ds, s.anchor.as_uuid(), s.anchor_op)?;
        let policy = reg.plan().policy();
        let (anchor_stage, budget) = match scope {
            Scope::PeRegistration => (OsDeployStage::StartPe, policy.registration_seconds()),
            Scope::PeCompletion => (OsDeployStage::PeRegister, policy.pe_seconds()),
            Scope::ShutdownGrace => (OsDeployStage::PeComplete, policy.shutdown_grace_seconds()),
            Scope::FullOs => (OsDeployStage::StartDisk, policy.full_os_seconds()),
            _ => (
                OsDeployStage::ALL
                    .into_iter()
                    .find(|s| stage_scope(*s) == scope)
                    .ok_or(Error::Validation)?,
                policy.mutation_seconds(),
            ),
        };
        require(
            s.anchor_op == reg.ids().operation(anchor_stage)
                && s.budget == budget
                && s.opened == d.value.evaluated_at
                && s.opened
                    .checked_add_signed(chrono::Duration::seconds(i64::from(budget)))
                    == Some(s.deadline),
        )?;
        match scope {
            Scope::PeRegistration | Scope::FullOs => {
                require(matches!(d.value.detail, Detail::PveDispatchCommitted(_)))?
            }
            Scope::PeCompletion | Scope::ShutdownGrace => {
                require(d.value.resolution == Some(NativeDecision::Satisfied))?
            }
            _ => {
                let Detail::StageActivated(a) = &d.value.detail else {
                    return Err(Error::Validation);
                };
                require(
                    a.scope_key == scope
                        && a.anchor_event_id == s.anchor
                        && a.anchor_operation_id == s.anchor_op
                        && a.opened_at == s.opened
                        && a.budget_seconds == s.budget
                        && a.deadline_at == s.deadline,
                )?;
            }
        }
        out.insert(key, s);
    }
    Ok(out)
}
fn validate_activations(
    reg: &OsDeployRegistrationV1,
    own: &[&Decision],
    ds: &BTreeMap<Uuid, Decision>,
    scopes: &BTreeMap<String, Deadline>,
    stage: OsDeployStage,
    op: OperationId,
) -> Result<(), Error> {
    for d in own {
        if let Detail::StageActivated(a) = &d.value.detail {
            let inherited = cfg!(feature = "fixture-ipc") && stage == OsDeployStage::PeRegister;
            require(inherited || super::history::enabled(stage).is_ok())?;
            let scope = scopes.get(&name(a.scope_key)?).ok_or(Error::Validation)?;
            require(
                scope.anchor == a.anchor_event_id
                    && scope.anchor_op == a.anchor_operation_id
                    && a.opened_at == scope.opened
                    && a.budget_seconds == scope.budget
                    && a.deadline_at == scope.deadline
                    && if inherited {
                        a.anchor_operation_id == reg.ids().operation(OsDeployStage::StartPe)
                            && a.opened_at <= d.value.evaluated_at
                            && d.value.evaluated_at < a.deadline_at
                    } else {
                        a.anchor_operation_id == op
                            && d.event == a.anchor_event_id
                            && a.opened_at == d.value.evaluated_at
                    }
                    && a.scope_key == stage_scope(stage),
            )?;
            if stage == OsDeployStage::Clone {
                require(
                    a.predecessor_operation_id.is_none()
                        && a.predecessor_decision_event_id.is_none(),
                )?;
            } else {
                let prior = if stage == OsDeployStage::DiskCapacity {
                    OsDeployStage::Clone
                } else if stage == OsDeployStage::PeRegister {
                    OsDeployStage::StartPe
                } else if stage == OsDeployStage::StartPe {
                    OsDeployStage::ConfigurePe
                } else {
                    OsDeployStage::DiskCapacity
                };
                let prior_op = reg.ids().operation(prior);
                require(a.predecessor_operation_id == Some(prior_op))?;
                let predecessor = decision(
                    ds,
                    a.predecessor_decision_event_id
                        .ok_or(Error::Validation)?
                        .as_uuid(),
                    prior_op,
                )?;
                require(
                    predecessor.value.resolution == Some(NativeDecision::Satisfied)
                        && predecessor.value.evaluated_at <= d.value.evaluated_at
                        && !ds.values().any(|other| {
                            other.value.operation_id == prior_op
                                && selected(other)
                                && other.revision > predecessor.revision
                        }),
                )?;
            }
        }
    }
    Ok(())
}
/// Restore the immutable credential edges before admitting a callback attempt.
/// This only establishes provenance; it does not authenticate a callback.
#[cfg(feature = "fixture-ipc")]
pub(crate) async fn require_fixture_registration_origin(
    tx: &mut Transaction<'_, Postgres>,
    reg: &OsDeployRegistrationV1,
    activated_at: DateTime<Utc>,
) -> Result<(), Error> {
    let valid: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.fixture_pe_deliveries d JOIN rust_controller.fixture_pe_delivery_acks a USING(operation_id) JOIN rust_controller.fixture_pe_delivery_exposures x USING(operation_id) JOIN rust_controller.fixture_pe_boot_sessions s USING(operation_id) JOIN rust_controller.fixture_osdeploy_origins o ON o.run_id=d.run_id AND o.credential_sink_id=d.sink_id JOIN rust_controller.fixture_pe_credential_aliases c ON c.alias_sha256=d.alias_sha256 AND c.operation_id=d.operation_id AND c.run_id=d.run_id AND c.attempt_id=d.attempt_id AND c.package_sha256=d.package_sha256 AND c.expires_at=d.expires_at WHERE d.operation_id=$1 AND d.run_id=$2 AND s.dispatch_event_id=d.dispatch_event_id AND s.attempt_id=d.attempt_id AND s.package_sha256=d.package_sha256 AND a.acknowledged_at<=x.exposed_at AND x.exposed_at<=$3 AND $3<s.registration_deadline)")
        .bind(reg.ids().operation(OsDeployStage::StartPe).as_uuid())
        .bind(reg.ids().run_id().as_uuid()).bind(activated_at)
        .fetch_one(&mut **tx).await?;
    require(valid)
}

fn selected(d: &Decision) -> bool {
    d.value
        .resolution
        .is_some_and(|r| r != NativeDecision::Ready)
}

struct Epoch {
    event: EventId,
    attempt: AttemptId,
    generation: i64,
    value: Acquisition,
}
async fn load_epochs(
    tx: &mut Transaction<'_, Postgres>,
    op: OperationId,
    run: Uuid,
    attempt: Option<AttemptId>,
    deadline: Option<DateTime<Utc>>,
    ds: &BTreeMap<Uuid, Decision>,
) -> Result<BTreeMap<Uuid, Epoch>, Error> {
    let mut out = BTreeMap::new();
    for r in rows(tx, "osdeploy_lease_epochs", op).await? {
        let event: EventId = id(r.try_get("acquisition_event_id")?)?;
        let d = decision(ds, event.as_uuid(), op)?;
        let Detail::LeaseAcquired(a) = &d.value.detail else {
            return Err(Error::Validation);
        };
        let at: AttemptId = id(r.try_get("attempt_id")?)?;
        require(
            r.try_get::<Uuid, _>("run_id")? == run
                && Some(at) == attempt
                && d.value.attempt_id == Some(at)
                && r.try_get::<String, _>("executor_kind")? == "rust"
                && d.value.generation == r.try_get::<i64, _>("generation")?
                && a.acquisition_event_id == event
                && a.worker_id == r.try_get::<String, _>("worker_id")?
                && a.token_sha256 == r.try_get::<String, _>("lease_token_sha256")?
                && a.acquired_at == r.try_get::<DateTime<Utc>, _>("acquired_at")?
                && a.acquired_at == d.value.evaluated_at
                && a.expires_at == r.try_get::<DateTime<Utc>, _>("initial_expires_at")?
                && a.deadline_at == r.try_get::<DateTime<Utc>, _>("deadline_at")?
                && Some(a.deadline_at) == deadline
                && name(a.purpose)? == r.try_get::<String, _>("purpose")?,
        )?;
        out.insert(
            event.as_uuid(),
            Epoch {
                event,
                attempt: at,
                generation: d.value.generation,
                value: a.clone(),
            },
        );
    }
    let count = ds
        .values()
        .filter(|d| {
            d.value.operation_id == op && matches!(d.value.detail, Detail::LeaseAcquired(_))
        })
        .count();
    require(count == out.len() && (attempt.is_none() || !out.is_empty()))?;
    let mut ordered: Vec<_> = out.values().collect();
    ordered.sort_by_key(|e| ds[&e.event.as_uuid()].revision);
    if let Some(first) = ordered.first() {
        let activation = ds
            .values()
            .find(|d| {
                d.value.operation_id == op && matches!(d.value.detail, Detail::StageActivated(_))
            })
            .ok_or(Error::Validation)?;
        require(
            first.value.purpose == Purpose::InitialEvaluation
                && first.value.acquired_at == activation.value.evaluated_at
                && first.generation == activation.value.generation
                && ds[&first.event.as_uuid()].revision == activation.revision + 1
                && ordered
                    .iter()
                    .skip(1)
                    .all(|e| e.value.purpose != Purpose::InitialEvaluation),
        )?;
    }
    Ok(out)
}

pub(super) struct Evidence {
    pub(super) revision: i64,
    pub(super) value: ProvisioningEvidenceV1,
    pub(super) hash: String,
}
async fn load_evidence(
    tx: &mut Transaction<'_, Postgres>,
    op: OperationId,
    run: Uuid,
    attempt: Option<AttemptId>,
    plan: &crate::OsDeployOperationPlanV1,
    journal: &BTreeMap<Uuid, Event>,
) -> Result<BTreeMap<Uuid, Evidence>, Error> {
    let mut out = BTreeMap::new();
    for r in rows(tx, "osdeploy_pve_evidence", op).await? {
        let event: EventId = id(r.try_get("event_id")?)?;
        let e = journal.get(&event.as_uuid()).ok_or(Error::Validation)?;
        let text: String = r.try_get("evidence_canonical_json")?;
        let value: ProvisioningEvidenceV1 = decode_exact(&text, EVIDENCE_LIMIT)?;
        let f = value.facts();
        let b = &f.binding;
        let hash = digest(&value)?;
        require(
            canonical(&value)? == text
                && serde_json::to_value(&value)? == e.payload
                && e.kind == "evidence_recorded"
                && e.revision == r.try_get::<i64, _>("evidence_revision")?
                && e.attempt == attempt
                && hash == r.try_get::<String, _>("evidence_sha256")?
                && r.try_get::<String, _>("source")? == "fake_pve"
                && f.source == NativeEvidenceSource::FakePve
                && b.run_id().as_uuid() == run
                && r.try_get::<Uuid, _>("run_id")? == run
                && b.operation_id() == op
                && Some(b.attempt_id()) == attempt
                && b.attempt_id().as_uuid() == r.try_get::<Uuid, _>("attempt_id")?
                && b.workflow_sha256() == plan.workflow_sha256()
                && Some(&f.plan) == plan.pve()
                && b.operation_plan_sha256()
                    == f.plan.fingerprint().map_err(|_| Error::Validation)?
                && b.evidence_fence()
                    == u64::try_from(e.revision - 1).map_err(|_| Error::Validation)?
                // PostgreSQL timestamptz persists microseconds; the typed
                // evidence JSON may retain additional Rust nanoseconds.
                // Preserve the database-precision instant without rejecting
                // otherwise identical evidence on reload.
                && e.at.timestamp_micros() == f.collected_at.timestamp_micros(),
        )?;
        out.insert(
            event.as_uuid(),
            Evidence {
                revision: e.revision,
                value,
                hash,
            },
        );
    }
    Ok(out)
}

struct DispatchRecord {
    event: EventId,
    value: ProvisioningDispatchV1,
}
#[allow(clippy::too_many_arguments)]
async fn load_dispatch(
    tx: &mut Transaction<'_, Postgres>,
    op: OperationId,
    run: Uuid,
    attempt: Option<AttemptId>,
    plan: &crate::OsDeployOperationPlanV1,
    journal: &BTreeMap<Uuid, Event>,
    ds: &BTreeMap<Uuid, Decision>,
    epochs: &BTreeMap<Uuid, Epoch>,
    evidence: &BTreeMap<Uuid, Evidence>,
) -> Result<Option<DispatchRecord>, Error> {
    let rows = rows(tx, "osdeploy_pve_dispatches", op).await?;
    require(
        rows.len() <= 1
            && ds
                .values()
                .filter(|d| {
                    d.value.operation_id == op
                        && matches!(d.value.detail, Detail::PveDispatchCommitted(_))
                })
                .count()
                == rows.len(),
    )?;
    let Some(r) = rows.first() else {
        return Ok(None);
    };
    let event: EventId = id(r.try_get("dispatch_event_id")?)?;
    let d = decision(ds, event.as_uuid(), op)?;
    let Detail::PveDispatchCommitted(detail) = &d.value.detail else {
        return Err(Error::Validation);
    };
    let text: String = r.try_get("request_canonical_json")?;
    let request: ProvisioningMutationRequestV1 = decode_exact(&text, EVIDENCE_LIMIT)?;
    let preflight: EventId = id(r.try_get("preflight_event_id")?)?;
    let evidence = evidence
        .get(&preflight.as_uuid())
        .ok_or(Error::Validation)?;
    let epoch_id: EventId = id(r.try_get("lease_acquisition_event_id")?)?;
    let epoch = epochs.get(&epoch_id.as_uuid()).ok_or(Error::Validation)?;
    let generation: i64 = r.try_get("original_generation")?;
    let revision: i64 = r.try_get("dispatch_revision")?;
    let at: DateTime<Utc> = r.try_get("dispatched_at")?;
    let historical_expiry = ds
        .values()
        .filter_map(|prior| {
            if prior.value.operation_id == op
                && prior.revision < revision
                && let Detail::LeaseRenewed(renewed) = &prior.value.detail
                && renewed.lease_acquisition_event_id == epoch_id
            {
                return Some((prior.revision, renewed.expires_at));
            }
            None
        })
        .max_by_key(|(revision, _)| *revision)
        .map(|(_, expires)| expires)
        .unwrap_or(epoch.value.expires_at);
    require(
        at < historical_expiry
            && ds
                .values()
                .filter(|d| {
                    d.value.operation_id == op
                        && d.revision < revision
                        && matches!(d.value.detail, Detail::LeaseAcquired(_))
                })
                .max_by_key(|d| d.revision)
                .is_some_and(|d| d.event == epoch_id),
    )?;
    let facts = evidence.value.facts();
    let (config, power) = if plan.stage() == OsDeployStage::Clone {
        (&facts.source_config, &facts.source_power)
    } else {
        (&facts.target_config, &facts.target_power)
    };
    require(
        config.as_ref().and_then(|r| r.result.as_ref().ok())
            == Some(request.expected_before().config())
            && power.as_ref().and_then(|r| r.result.as_ref().ok())
                == Some(request.expected_before().power()),
    )?;
    require(
        canonical(&request)? == text
            && Some(request.plan()) == plan.pve()
            && request.binding() == &evidence.value.facts().binding
            && request.binding().run_id().as_uuid() == run
            && r.try_get::<Uuid, _>("run_id")? == run
            && request.binding().operation_id() == op
            && Some(request.binding().attempt_id()) == attempt
            && Some(id::<AttemptId>(r.try_get("attempt_id")?)?) == attempt
            && request.binding().workflow_sha256() == plan.workflow_sha256()
            && r.try_get::<String, _>("workflow_sha256")? == plan.workflow_sha256()
            && request
                .plan()
                .fingerprint()
                .map_err(|_| Error::Validation)?
                == r.try_get::<String, _>("pve_plan_sha256")?
            && detail.pve_plan_sha256 == r.try_get::<String, _>("pve_plan_sha256")?
            && request.request_digest().map_err(|_| Error::Validation)?
                == r.try_get::<String, _>("request_sha256")?
            && detail.request_sha256 == r.try_get::<String, _>("request_sha256")?
            && r.try_get::<String, _>("source")? == "fake_pve"
            && detail.preflight_event_id == preflight
            && detail.lease_acquisition_event_id == epoch_id
            && detail.dispatched_at == at
            && d.value.evaluated_at == at
            && d.revision == revision
            && d.value.generation == generation
            && epoch.generation == generation
            && at >= epoch.value.acquired_at
            && at < epoch.value.deadline_at
            && evidence.revision < revision
            && journal.contains_key(&event.as_uuid()),
    )?;
    let value = ProvisioningDispatchV1::new(ProvisioningDispatchInputV1 {
        request,
        source: NativeEvidenceSource::FakePve,
        preflight_event_id: preflight,
        original_generation: generation,
        dispatch_revision: revision.try_into().map_err(|_| Error::Validation)?,
        dispatched_at: at,
    })
    .map_err(|_| Error::Validation)?;
    Ok(Some(DispatchRecord { event, value }))
}
struct ReceiptRecord {
    revision: i64,
    value: ProvisioningReceiptV1,
}
async fn load_receipt(
    tx: &mut Transaction<'_, Postgres>,
    op: OperationId,
    dispatch: Option<&DispatchRecord>,
    journal: &BTreeMap<Uuid, Event>,
) -> Result<Option<ReceiptRecord>, Error> {
    let rows = rows(tx, "osdeploy_pve_receipts", op).await?;
    require(rows.len() <= 1)?;
    let Some(r) = rows.first() else {
        return Ok(None);
    };
    let dispatch = dispatch.ok_or(Error::Validation)?;
    let e = journal
        .get(&r.try_get::<Uuid, _>("receipt_event_id")?)
        .ok_or(Error::Validation)?;
    let payload: Receipt = decode_exact(&canonical(&e.payload)?, DECISION_LIMIT)?;
    let at: DateTime<Utc> = r.try_get("accepted_at")?;
    require(
        e.kind == "evidence_recorded"
            && e.attempt == Some(dispatch.value.request().binding().attempt_id())
            && e.revision
                > i64::try_from(dispatch.value.dispatch_revision())
                    .map_err(|_| Error::Validation)?
            && e.at == at
            && at == r.try_get::<DateTime<Utc>, _>("recorded_at")?
            && payload.accepted_at == at
            && payload.contract_version == 1
            && payload.action == "pve_receipt_captured"
            && payload.dispatch_event_id == dispatch.event
            && payload.request_sha256 == dispatch.value.request_sha256()
            && payload.receipt_kind == r.try_get::<String, _>("receipt_kind")?
            && payload.upid == r.try_get::<Option<String>, _>("upid")?,
    )?;
    let receipt = match (payload.receipt_kind.as_str(), payload.upid) {
        ("task", Some(upid)) => {
            MutationReceipt::Task(Upid::parse(upid).map_err(|_| Error::Validation)?)
        }
        ("synchronous", None) => MutationReceipt::SynchronousAccepted,
        _ => return Err(Error::Validation),
    };
    Ok(Some(ReceiptRecord {
        revision: e.revision,
        value: ProvisioningReceiptV1::new(dispatch.value.clone(), at, receipt)
            .map_err(|_| Error::Validation)?,
    }))
}
async fn load_cancellation(
    tx: &mut Transaction<'_, Postgres>,
    reg: &OsDeployRegistrationV1,
    ds: &BTreeMap<Uuid, Decision>,
) -> Result<bool, Error> {
    let rows =
        sqlx::query("SELECT * FROM rust_controller.osdeploy_run_cancellations WHERE run_id=$1")
            .bind(reg.ids().run_id().as_uuid())
            .fetch_all(&mut **tx)
            .await?;
    require(
        rows.len() <= 1
            && ds
                .values()
                .filter(|d| matches!(d.value.detail, Detail::RunCancelled(_)))
                .count()
                == rows.len(),
    )?;
    let Some(r) = rows.first() else {
        return Ok(false);
    };
    let op = reg.ids().operation(OsDeployStage::Clone);
    let d = decision(ds, r.try_get("decision_event_id")?, op)?;
    require(
        r.try_get::<Uuid, _>("anchor_operation_id")? == op.as_uuid()
            && matches!(d.value.detail, Detail::RunCancelled(_))
            && d.value.generation == r.try_get::<i64, _>("generation")?
            && d.value.evaluated_at == r.try_get::<DateTime<Utc>, _>("requested_at")?,
    )?;
    Ok(true)
}

#[allow(clippy::too_many_arguments)]
fn validate_decision_links(
    own: &[&Decision],
    ds: &BTreeMap<Uuid, Decision>,
    scopes: &BTreeMap<String, Deadline>,
    epochs: &BTreeMap<Uuid, Epoch>,
    evidence: &BTreeMap<Uuid, Evidence>,
    dispatch: Option<&DispatchRecord>,
    receipt: Option<&ReceiptRecord>,
    op: OperationId,
    attempt: Option<AttemptId>,
    deadline: Option<DateTime<Utc>>,
    cancelled: bool,
    expected_scope: Scope,
) -> Result<(), Error> {
    let epoch = |event: EventId, d: &Decision| -> Result<&Epoch, Error> {
        let e = epochs.get(&event.as_uuid()).ok_or(Error::Validation)?;
        require(
            e.attempt == attempt.ok_or(Error::Validation)?
                && e.value.acquired_at <= d.value.evaluated_at
                && own
                    .iter()
                    .filter(|prior| {
                        prior.revision < d.revision
                            && matches!(prior.value.detail, Detail::LeaseAcquired(_))
                    })
                    .max_by_key(|prior| prior.revision)
                    .is_some_and(|prior| prior.event == event),
        )?;
        Ok(e)
    };
    let scope = |key: Scope, time: DateTime<Utc>| -> Result<&Deadline, Error> {
        let s = scopes.get(&name(key)?).ok_or(Error::Validation)?;
        require(key == expected_scope && s.deadline == time && deadline.is_none_or(|d| d == time))?;
        Ok(s)
    };
    let terminal = |event: EventId, d: &Decision| -> Result<(), Error> {
        let t = decision(ds, event.as_uuid(), op)?;
        require(
            t.revision < d.revision
                && t.value
                    .resolution
                    .is_some_and(|r| !matches!(r, NativeDecision::Ready | NativeDecision::Waiting))
                && !own.iter().any(|other| {
                    selected(other) && other.revision > t.revision && other.revision < d.revision
                }),
        )
    };
    let dispatched = |event: EventId, d: &Decision| -> Result<(), Error> {
        let x = dispatch.ok_or(Error::Validation)?;
        require(x.event == event && x.value.dispatch_revision() < (d.revision as u64))
    };
    for d in own {
        match &d.value.detail {
            Detail::StageActivated(_) | Detail::RunCancelled(_) => {}
            Detail::FixturePeRegistered(a) => {
                let e = epoch(a.lease_acquisition_event_id, d)?;
                require(
                    e.generation == d.value.generation
                        && Some(a.registration_deadline) == deadline
                        && d.value.evaluated_at < a.registration_deadline,
                )?;
            }
            Detail::LeaseAcquired(a) => {
                if a.purpose == Purpose::ReclaimedCredentialDelivery {
                    let prior = own
                        .iter()
                        .filter(|p| p.revision < d.revision)
                        .max_by_key(|p| p.revision)
                        .ok_or(Error::Validation)?;
                    require(
                        matches!(prior.value.detail, Detail::CredentialDeliveryReclaimed(_))
                            && prior.value.evaluated_at <= d.value.evaluated_at,
                    )?;
                }
                if let Some(prior) = a.prior_schedule_event_id {
                    let p = decision(ds, prior.as_uuid(), op)?;
                    require(
                        p.revision < d.revision
                            && p.value.resolution == Some(NativeDecision::Waiting),
                    )?;
                }
            }
            Detail::EvaluationStarted(a) => {
                epoch(a.lease_acquisition_event_id, d)?;
                require(epoch(a.lease_acquisition_event_id, d)?.generation == d.value.generation)?;
                let has_dispatch =
                    dispatch.is_some_and(|x| x.value.dispatch_revision() < (d.revision as u64));
                require(
                    a.activity
                        == if has_dispatch {
                            Activity::OutcomeRead
                        } else {
                            Activity::PreflightRead
                        },
                )?;
            }
            Detail::LeaseRenewed(a) => {
                let e = epoch(a.lease_acquisition_event_id, d)?;
                let previous = own
                    .iter()
                    .filter_map(|prior| {
                        if prior.revision < d.revision {
                            if let Detail::LeaseRenewed(r) = &prior.value.detail {
                                if r.lease_acquisition_event_id == e.event {
                                    Some((r.heartbeat_at, r.expires_at))
                                } else {
                                    None
                                }
                            } else {
                                None
                            }
                        } else {
                            None
                        }
                    })
                    .max()
                    .unwrap_or((e.value.acquired_at, e.value.expires_at));
                require(
                    e.generation == d.value.generation
                        && a.heartbeat_at == d.value.evaluated_at
                        && Some(a.deadline_at) == deadline
                        && a.heartbeat_at < previous.1
                        && previous
                            .0
                            .checked_add_signed(chrono::Duration::seconds(10))
                            .is_some_and(|t| a.heartbeat_at >= t),
                )?;
            }
            Detail::PveDispatchCommitted(a) => {
                epoch(a.lease_acquisition_event_id, d)?;
            }
            Detail::PveEvaluated(a) => {
                scope(a.scope_key, a.deadline_at)?;
                let e = evidence
                    .get(&a.evidence_event_id.as_uuid())
                    .ok_or(Error::Validation)?;
                require(
                    e.revision < d.revision
                        && e.hash == a.evidence_sha256
                        && e.value.facts().collected_at <= d.value.evaluated_at,
                )?;
                if let Some(event) = a.lease_acquisition_event_id {
                    require(epoch(event, d)?.generation == d.value.generation)?;
                }
                if a.mode != Mode::Preflight {
                    require(
                        dispatch.is_some_and(|x| x.value.dispatch_revision() < (d.revision as u64)),
                    )?;
                }
                require(
                    d.value.evaluated_at < a.deadline_at
                        || d.value.resolution == Some(NativeDecision::Unknown),
                )?;
            }
            Detail::LeaseReclaimedSameAttempt(a) => {
                epoch(a.lease_acquisition_event_id, d)?;
                scope(a.scope_key, a.deadline_at)?;
            }
            Detail::CredentialDeliveryReclaimed(a) => {
                let e = epoch(a.lease_acquisition_event_id, d)?;
                scope(a.scope_key, a.deadline_at)?;
                let expiry = own
                    .iter()
                    .filter_map(|prior| {
                        if prior.revision < d.revision
                            && let Detail::LeaseRenewed(r) = &prior.value.detail
                            && r.lease_acquisition_event_id == e.event
                        {
                            return Some(r.expires_at);
                        }
                        None
                    })
                    .max()
                    .unwrap_or(e.value.expires_at);
                require(
                    dispatch.is_some_and(|x| x.value.dispatch_revision() < d.revision as u64)
                        && d.value.evaluated_at >= expiry
                        && d.value.evaluated_at < a.deadline_at,
                )?;
            }
            Detail::EvaluationReparked(a) => {
                epoch(a.lease_acquisition_event_id, d)?;
                scope(a.scope_key, a.deadline_at)?;
                let activity = decision(ds, a.activity_event_id.as_uuid(), op)?;
                require(
                    activity.revision < d.revision
                        && (matches!(&activity.value.detail,
                Detail::EvaluationStarted(s) if s.lease_acquisition_event_id==a.lease_acquisition_event_id)
                            || activity.event == a.lease_acquisition_event_id),
                )?;
            }
            Detail::ScopeExpiredBeforeActivation(a) => {
                let s = scope(a.scope_key, a.deadline_at)?;
                require(s.anchor == a.anchor_event_id && s.anchor_op == a.anchor_operation_id)?;
            }
            Detail::ActivatedScopeExpired(a) => {
                let s = scope(a.scope_key, a.deadline_at)?;
                require(s.anchor == a.anchor_event_id && s.anchor_op == a.anchor_operation_id)?;
                if let (Some(p), Some(e)) =
                    (a.pe_complete_operation_id, a.pe_complete_decision_event_id)
                {
                    require(p == s.anchor_op && e == s.anchor)?;
                }
            }
            Detail::StageCancelledUnexposed(a) => {
                require(cancelled)?;
                let c = ds
                    .get(&a.cancellation_event_id.as_uuid())
                    .ok_or(Error::Validation)?;
                require(
                    matches!(c.value.detail, Detail::RunCancelled(_))
                        && c.value.evaluated_at <= d.value.evaluated_at
                        && dispatch.is_none(),
                )?;
                if let (Some(k), Some(t)) = (a.scope_key, a.deadline_at) {
                    scope(k, t)?;
                }
            }
            Detail::StageCancelledExposed(a) => {
                require(cancelled)?;
                scope(a.scope_key, a.deadline_at)?;
                dispatched(a.dispatch_event_id, d)?;
                let c = ds
                    .get(&a.cancellation_event_id.as_uuid())
                    .ok_or(Error::Validation)?;
                require(matches!(c.value.detail, Detail::RunCancelled(_)))?;
            }
            Detail::ResidualLeaseRevoked(a) => {
                epoch(a.lease_acquisition_event_id, d)?;
                terminal(a.terminal_decision_event_id, d)?;
            }
            Detail::ReconciliationScheduled(a) => {
                scope(a.scope_key, a.deadline_at)?;
                terminal(a.terminal_decision_event_id, d)?;
                dispatched(a.dispatch_event_id, d)?;
                require(
                    decision(ds, a.terminal_decision_event_id.as_uuid(), op)?
                        .value
                        .resolution
                        == Some(NativeDecision::Unknown),
                )?;
                let x = dispatch.ok_or(Error::Validation)?;
                let task_required = !matches!(
                    x.value.request().plan().action(),
                    pve_port::ProvisioningActionV1::ConfigurePe
                        | pve_port::ProvisioningActionV1::ConfigureDisk
                );
                let missing_task_receipt =
                    task_required && receipt.is_none_or(|r| r.revision > d.revision);
                require(a.schedule.next_check_at.is_none() == missing_task_receipt)?;
            }
            Detail::LeaseExpiredUncertain(a) => {
                epoch(a.lease_acquisition_event_id, d)?;
                scope(a.scope_key, a.deadline_at)?;
                dispatched(a.dispatch_event_id, d)?;
            }
        }
    }
    Ok(())
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct StateChange {
    state: ExecutionState,
    decision_event_id: EventId,
}
fn state_for(d: &Decision) -> Result<ExecutionState, Error> {
    if let Some(resolution) = d.value.resolution.filter(|r| *r != NativeDecision::Ready) {
        return decode_name(name(resolution)?);
    }
    match d.value.detail {
        Detail::LeaseAcquired(ref a) if a.purpose == Purpose::ReclaimedCredentialDelivery => {
            Ok(ExecutionState::Running)
        }
        Detail::LeaseAcquired(ref a) if a.purpose != Purpose::ResumeEvaluation => {
            Ok(ExecutionState::Leased)
        }
        Detail::EvaluationStarted(_) => Ok(ExecutionState::Running),
        Detail::LeaseReclaimedSameAttempt(_) | Detail::CredentialDeliveryReclaimed(_) => {
            Ok(ExecutionState::Pending)
        }
        _ => Err(Error::Validation),
    }
}
fn legal_state_edge(from: ExecutionState, to: ExecutionState, d: &Decision) -> bool {
    use ExecutionState::*;
    match &d.value.detail {
        Detail::FixturePeRegistered(_) => from == Running && to == Satisfied,
        Detail::LeaseAcquired(a) => {
            from == Pending
                && if a.purpose == Purpose::ReclaimedCredentialDelivery {
                    to == Running
                } else {
                    to == Leased && a.purpose != Purpose::ResumeEvaluation
                }
        }
        Detail::EvaluationStarted(_) => matches!(from, Leased | Waiting) && to == Running,
        Detail::LeaseReclaimedSameAttempt(_) => from == Leased && to == Pending,
        Detail::CredentialDeliveryReclaimed(_) => from == Running && to == Pending,
        Detail::EvaluationReparked(_) => from == Running && to == Waiting,
        Detail::ScopeExpiredBeforeActivation(_) => from == Pending && to == Unknown,
        Detail::ActivatedScopeExpired(_) => !from.is_terminal() && to == Unknown,
        Detail::StageCancelledUnexposed(_) => !from.is_terminal() && to == Blocked,
        Detail::StageCancelledExposed(_) => {
            matches!(from, Pending | Leased | Running | Waiting | Cancelling) && to == Unknown
        }
        Detail::LeaseExpiredUncertain(_) => {
            matches!(from, Leased | Running | Waiting | Cancelling) && to == Unknown
        }
        Detail::PveEvaluated(a) => {
            if a.mode == Mode::Reconciliation {
                from == Unknown && matches!(to, Satisfied | Failed | Conflicted)
            } else {
                from == Running
                    && matches!(
                        to,
                        Waiting | Satisfied | Failed | Blocked | Unknown | Conflicted
                    )
            }
        }
        _ => false,
    }
}
fn admits_decision_in(state: ExecutionState, detail: &Detail) -> bool {
    use ExecutionState::*;
    match detail {
        Detail::FixturePeRegistered(_) => state == Running,
        Detail::StageActivated(_) => state == Pending,
        Detail::LeaseAcquired(a) => {
            if a.purpose == Purpose::ResumeEvaluation {
                state == Waiting
            } else {
                state == Pending
            }
        }
        Detail::EvaluationStarted(_) => matches!(state, Leased | Waiting),
        Detail::LeaseRenewed(_) => matches!(state, Leased | Running | Waiting),
        Detail::PveDispatchCommitted(_) => state == Running,
        Detail::PveEvaluated(a) => {
            if a.mode == Mode::Reconciliation {
                state == Unknown
            } else {
                state == Running
            }
        }
        Detail::LeaseReclaimedSameAttempt(_) => state == Leased,
        Detail::CredentialDeliveryReclaimed(_) => state == Running,
        Detail::EvaluationReparked(_) => matches!(state, Running | Waiting),
        Detail::ScopeExpiredBeforeActivation(_) => state == Pending,
        Detail::ActivatedScopeExpired(_) => !state.is_terminal() || state == Unknown,
        Detail::RunCancelled(_) => true,
        Detail::StageCancelledUnexposed(_) => !state.is_terminal(),
        Detail::StageCancelledExposed(_) => {
            matches!(state, Pending | Leased | Running | Waiting | Cancelling)
        }
        Detail::LeaseExpiredUncertain(_) => {
            matches!(state, Leased | Running | Waiting | Cancelling)
        }
        Detail::ResidualLeaseRevoked(_) => state.is_terminal(),
        Detail::ReconciliationScheduled(_) => state == Unknown,
    }
}
fn validate_state_history(
    journal: &BTreeMap<Uuid, Event>,
    own: &[&Decision],
    ds: &BTreeMap<Uuid, Decision>,
    current: ExecutionState,
    attempt: Option<AttemptId>,
    activated: Option<DateTime<Utc>>,
    deadline: Option<DateTime<Utc>>,
) -> Result<Option<DateTime<Utc>>, Error> {
    let mut ordered: Vec<_> = journal.values().collect();
    ordered.sort_by_key(|e| e.revision);
    let mut state = ExecutionState::Pending;
    let mut state_basis = None;
    let mut started = 0;
    let mut ran = false;
    let mut pending_transition = None;
    let mut pending_first_start: Option<&Event> = None;
    for e in ordered {
        require(pending_transition.is_none() || e.kind == "execution_state_changed")?;
        if let Some(start) = pending_first_start {
            // The first start is one atomic three-event transaction. Keep its
            // journal identity until the immediately adjacent evaluation consumes it.
            require(
                e.kind == "decision_recorded"
                    && e.revision == start.revision + 1
                    && e.at == start.at
                    && e.attempt == start.attempt,
            )?;
        }
        match e.kind.as_str() {
            "attempt_started" => {
                started += 1;
                require(
                    started == 1
                        && attempt.is_some()
                        && activated.is_some_and(|a| a <= e.at)
                        && deadline.is_some_and(|d| e.at < d)
                        && e.payload == serde_json::json!({"phase":"mutation_started"}),
                )?;
                pending_first_start = Some(e);
            }
            "execution_state_changed" => {
                let p: StateChange = decode_exact(&canonical(&e.payload)?, DECISION_LIMIT)?;
                let d = ds
                    .get(&p.decision_event_id.as_uuid())
                    .ok_or(Error::Validation)?;
                require(
                    pending_transition == Some(d.event)
                        && own.iter().any(|own| own.event == d.event)
                        && d.revision + 1 == e.revision
                        && d.value.attempt_id == e.attempt
                        && d.value.evaluated_at == e.at
                        && Some(p.state) == e.state
                        && state_for(d)? == p.state
                        && state != p.state
                        && legal_state_edge(state, p.state, d)
                        && !own.iter().any(|other| {
                            other.revision > d.revision
                                && other.revision < e.revision
                                && state_for(other).is_ok()
                        }),
                )?;
                if p.state == ExecutionState::Running {
                    require(started == 1)?;
                    ran = true;
                }
                state = p.state;
                state_basis = Some(d.event);
                pending_transition = None;
            }
            "decision_recorded" => {
                let d = ds.get(&e.id.as_uuid()).ok_or(Error::Validation)?;
                require(admits_decision_in(state, &d.value.detail))?;
                if !ran && matches!(d.value.detail, Detail::EvaluationStarted(_)) {
                    require(pending_first_start.take().is_some())?;
                }
                require(pending_first_start.is_none())?;
                if let Ok(target) = state_for(d)
                    && target != state
                {
                    pending_transition = Some(d.event);
                }
            }
            "evidence_recorded" => {}
            _ => return Err(Error::Validation),
        }
    }
    require(
        state == current
            && (started == 1) == ran
            && pending_transition.is_none()
            && pending_first_start.is_none(),
    )?;
    if let Some(selected) = own
        .iter()
        .filter(|d| selected(d))
        .max_by_key(|d| d.revision)
    {
        let target = state_for(selected)?;
        // A resumed Waiting decision can legitimately have subsequent
        // lease/start state transitions; control-only events cannot replace it.
        if current.is_terminal() || target != ExecutionState::Waiting {
            require(
                current == target
                    && state_basis.is_some_and(|event| {
                        ds.get(&event.as_uuid()).is_some_and(|d| {
                            d.revision <= selected.revision && state_for(d).ok() == Some(target)
                        })
                    }),
            )?;
        }
    } else {
        require(!current.is_terminal() && current != ExecutionState::Waiting)?;
    }
    if attempt.is_none() {
        require(matches!(
            current,
            ExecutionState::Pending | ExecutionState::Blocked | ExecutionState::Unknown
        ))?;
    }
    let mut next = None;
    let mut sorted = own.to_vec();
    sorted.sort_by_key(|d| d.revision);
    for d in sorted {
        match &d.value.detail {
            Detail::PveEvaluated(v) => next = v.schedule.as_ref().and_then(|s| s.next_check_at),
            Detail::EvaluationReparked(v) => next = v.schedule.next_check_at,
            Detail::ReconciliationScheduled(v) => next = v.schedule.next_check_at,
            Detail::LeaseAcquired(_)
            | Detail::ActivatedScopeExpired(_)
            | Detail::ScopeExpiredBeforeActivation(_)
            | Detail::StageCancelledExposed(_)
            | Detail::StageCancelledUnexposed(_) => next = None,
            _ => {}
        }
    }
    Ok(next)
}
#[allow(clippy::too_many_arguments)]
async fn validate_live_lease(
    tx: &mut Transaction<'_, Postgres>,
    op: OperationId,
    attempt: Option<AttemptId>,
    deadline: Option<DateTime<Utc>>,
    epochs: &BTreeMap<Uuid, Epoch>,
    own: &[&Decision],
    state: ExecutionState,
) -> Result<(), Error> {
    let rows=sqlx::query("SELECT *,encode(sha256(convert_to(lease_token,'UTF8')),'hex') AS token_sha256 FROM rust_controller.worker_leases WHERE operation_id=$1")
        .bind(op.as_uuid()).fetch_all(&mut **tx).await?;
    require(
        rows.len() <= 1
            && (!matches!(state, ExecutionState::Leased | ExecutionState::Running)
                || rows.len() == 1),
    )?;
    if let Some(r) = rows.first() {
        let hash: String = r.try_get("token_sha256")?;
        let epoch = epochs
            .values()
            .find(|e| e.value.token_sha256 == hash)
            .ok_or(Error::Validation)?;
        let acquisition = own
            .iter()
            .find(|d| d.event == epoch.event)
            .ok_or(Error::Validation)?;
        require(!own.iter().any(|d| {
            d.revision > acquisition.revision
                && (matches!(
                    d.value.detail,
                    Detail::LeaseAcquired(_)
                        | Detail::ResidualLeaseRevoked(_)
                        | Detail::LeaseReclaimedSameAttempt(_)
                        | Detail::CredentialDeliveryReclaimed(_)
                        | Detail::EvaluationReparked(_)
                ) || matches!(&d.value.detail, Detail::PveEvaluated(v)
                    if d.value.resolution == Some(NativeDecision::Waiting)
                        && v.lease_acquisition_event_id == Some(epoch.event)))
        }))?;
        require(
            Some(id::<AttemptId>(r.try_get("attempt_id")?)?) == attempt
                && r.try_get::<String, _>("executor_kind")? == "rust"
                && r.try_get::<i64, _>("generation")? == epoch.generation
                && r.try_get::<String, _>("worker_id")? == epoch.value.worker_id
                && r.try_get::<DateTime<Utc>, _>("acquired_at")? == epoch.value.acquired_at
                && Some(r.try_get::<DateTime<Utc>, _>("deadline_at")?) == deadline,
        )?;
        let renewal = own
            .iter()
            .filter_map(|d| {
                if let Detail::LeaseRenewed(v) = &d.value.detail {
                    if v.lease_acquisition_event_id == epoch.event {
                        Some((d.revision, v))
                    } else {
                        None
                    }
                } else {
                    None
                }
            })
            .max_by_key(|(r, _)| *r);
        let (heartbeat, expires) = renewal
            .map(|(_, v)| (v.heartbeat_at, v.expires_at))
            .unwrap_or((epoch.value.acquired_at, epoch.value.expires_at));
        require(
            r.try_get::<DateTime<Utc>, _>("heartbeat_at")? == heartbeat
                && r.try_get::<DateTime<Utc>, _>("lease_expires_at")? == expires,
        )?;
    }
    Ok(())
}

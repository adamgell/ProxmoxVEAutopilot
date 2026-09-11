use super::*;
use crate::{OsDeployDue, OsDeployDueKind, OsDeployExpiryCursor, OsDeployRepairCursor};
use controller_domain::RunId;
impl Scheduler {
    /// Discovery never acquires an operation lock or grants execution authority.
    pub async fn discover_osdeploy_due(&self) -> Result<Vec<OsDeployDue>, Error> {
        #[cfg(feature = "fixture-ipc")]
        let fixture_stop = self.fixture_credential_delivery;
        #[cfg(not(feature = "fixture-ipc"))]
        let fixture_stop = false;
        let rows=sqlx::query("SELECT s.operation_id,s.attempt_id,o.revision,w.workflow_sha256,s.basis_event_id,s.mode FROM rust_controller.osdeploy_schedule_projection s JOIN rust_controller.operations o USING(operation_id) JOIN rust_controller.osdeploy_operation_plans p USING(operation_id) JOIN rust_controller.osdeploy_runs w ON w.run_id=p.run_id JOIN rust_controller.osdeploy_attempt_bindings b ON b.operation_id=s.operation_id AND b.attempt_id=s.attempt_id AND b.scope_key=s.scope_key AND b.run_id=s.run_id JOIN rust_controller.osdeploy_deadlines d ON d.run_id=s.run_id AND d.scope_key=s.scope_key WHERE o.workflow_kind='os_deploy' AND (p.stage IN ('clone','disk_capacity','configure_pe') OR ($1 AND p.stage='pe_ensure_stopped')) AND s.next_check_at<=clock_timestamp() AND NOT EXISTS(SELECT 1 FROM rust_controller.worker_leases l WHERE l.operation_id=s.operation_id) AND NOT EXISTS(SELECT 1 FROM rust_controller.osdeploy_run_cancellations c WHERE c.run_id=s.run_id) AND ((s.mode='waiting' AND o.state='waiting') OR (s.mode='unknown_reconciliation' AND o.state='unknown' AND clock_timestamp()<d.deadline_at AND EXISTS(SELECT 1 FROM rust_controller.osdeploy_pve_dispatches x WHERE x.operation_id=s.operation_id AND x.attempt_id=s.attempt_id) AND (p.stage='configure_pe' OR EXISTS(SELECT 1 FROM rust_controller.osdeploy_pve_receipts r WHERE r.operation_id=s.operation_id)))) ORDER BY s.next_check_at,s.operation_id LIMIT 32")
            .bind(fixture_stop)
            .fetch_all(self.store.pool()).await?;
        rows.into_iter()
            .map(|row| {
                Ok(OsDeployDue {
                    operation: load::id(row.try_get("operation_id")?)?,
                    attempt: load::id(row.try_get("attempt_id")?)?,
                    revision: row.try_get("revision")?,
                    workflow_sha256: row.try_get("workflow_sha256")?,
                    basis_event: load::id(row.try_get("basis_event_id")?)?,
                    kind: if row.try_get::<String, _>("mode")? == "waiting" {
                        OsDeployDueKind::Waiting
                    } else {
                        OsDeployDueKind::UnknownReconciliation
                    },
                })
            })
            .collect()
    }

    pub async fn repair_osdeploy_schedules(
        &self,
        cursor: &mut OsDeployRepairCursor,
    ) -> Result<OsDeployMaintenanceSummary, Error> {
        let rows:Vec<Uuid>=sqlx::query_scalar("SELECT o.operation_id FROM rust_controller.operations o JOIN rust_controller.osdeploy_operation_plans p USING(operation_id) WHERE o.workflow_kind='os_deploy' AND o.state IN ('waiting','unknown') AND ($1::uuid IS NULL OR o.operation_id>$1) ORDER BY o.operation_id LIMIT 32")
            .bind(cursor.after.map(|op|op.as_uuid())).fetch_all(self.store.pool()).await?;
        let at_end = rows.len() < 32;
        let mut result = OsDeployMaintenanceSummary::default();
        for id in rows {
            let op = load::id(id)?;
            result.examined += 1;
            let outcome = Box::pin(repair_one(self, op)).await;
            cursor.after = Some(op);
            count_result(&mut result, outcome)?;
        }
        if at_end {
            cursor.after = None;
        }
        Ok(result)
    }

    pub async fn expire_osdeploy_scopes(
        &self,
        cursor: &mut OsDeployExpiryCursor,
    ) -> Result<OsDeployMaintenanceSummary, Error> {
        // Derive membership from the closed existing stage mapping. No mutable
        // projection, caller string, or cursor can open an inherited anchor.
        let mut stages = Vec::new();
        let mut scopes = Vec::new();
        for stage in OsDeployStage::ALL {
            stages.push(
                serde_json::to_value(stage)?
                    .as_str()
                    .ok_or(Error::Validation)?
                    .to_owned(),
            );
            scopes.push(recovery::scope_name(load::stage_scope(stage))?);
        }
        let after = cursor.after.as_ref();
        let rows=sqlx::query("SELECT d.deadline_at,d.run_id,d.scope_key,o.operation_id FROM rust_controller.osdeploy_deadlines d JOIN unnest($1::text[],$2::text[]) AS membership(stage,scope_key) ON membership.scope_key=d.scope_key JOIN rust_controller.osdeploy_operation_plans p ON p.run_id=d.run_id AND p.stage=membership.stage JOIN rust_controller.operations o USING(operation_id) LEFT JOIN rust_controller.osdeploy_attempt_bindings b ON b.operation_id=o.operation_id WHERE d.deadline_at<=clock_timestamp() AND o.state IN ('pending','leased','running','waiting','cancelling','unknown') AND (b.operation_id IS NOT NULL OR d.scope_key IN ('pe_registration','pe_completion','shutdown_grace','full_os')) AND NOT EXISTS(SELECT 1 FROM rust_controller.osdeploy_run_cancellations c WHERE c.run_id=d.run_id) AND NOT EXISTS(SELECT 1 FROM rust_controller.osdeploy_decisions x WHERE x.operation_id=o.operation_id AND x.action IN ('activated_scope_expired','scope_expired_before_activation') AND x.attempt_id IS NOT DISTINCT FROM b.attempt_id AND x.payload_canonical_json::jsonb->'detail'->>'scope_key'=d.scope_key) AND ($3::timestamptz IS NULL OR (d.deadline_at,d.run_id,d.scope_key,o.operation_id)>($3,$4::uuid,$5::text,$6::uuid)) ORDER BY d.deadline_at,d.run_id,d.scope_key,o.operation_id LIMIT 32")
            .bind(stages).bind(scopes).bind(after.map(|v|v.0)).bind(after.map(|v|v.1.as_uuid())).bind(after.map(|v|v.2.as_str())).bind(after.map(|v|v.3.as_uuid())).fetch_all(self.store.pool()).await?;
        let at_end = rows.len() < 32;
        let mut result = OsDeployMaintenanceSummary::default();
        for row in rows {
            let deadline = row.try_get("deadline_at")?;
            let run = load::id(row.try_get("run_id")?)?;
            let scope: String = row.try_get("scope_key")?;
            let op = load::id(row.try_get("operation_id")?)?;
            result.examined += 1;
            let outcome = Box::pin(expire_one(self, op, run, &scope, deadline)).await;
            cursor.after = Some((deadline, run, scope, op));
            count_result(&mut result, outcome)?;
        }
        if at_end {
            cursor.after = None;
        }
        Ok(result)
    }
}

fn count_result(
    summary: &mut OsDeployMaintenanceSummary,
    result: Result<bool, Error>,
) -> Result<(), Error> {
    match result {
        Ok(true) => summary.changed += 1,
        Ok(false) => {}
        Err(
            Error::Validation | Error::Conflict | Error::FenceLost | Error::CapabilityUnavailable,
        ) => summary.rejected += 1,
        Err(error) => return Err(error),
    }
    Ok(())
}

async fn expire_one(
    s: &Scheduler,
    op: OperationId,
    run: RunId,
    scope: &str,
    deadline: DateTime<Utc>,
) -> Result<bool, Error> {
    let mut tx = s.store.pool().begin().await?;
    authority(s, &mut tx).await?;
    let snapshot = Box::pin(locked_execution(&mut tx, op)).await?;
    if snapshot.cancelled()
        || (snapshot.state().is_terminal() && snapshot.state() != ExecutionState::Unknown)
    {
        tx.commit().await?;
        return Ok(false);
    }
    wire::require(
        snapshot.run_id() == run
            && recovery::scope_name(load::stage_scope(snapshot.plan().stage()))? == scope,
    )?;
    let original:Option<DateTime<Utc>>=sqlx::query_scalar("SELECT deadline_at FROM rust_controller.osdeploy_deadlines WHERE run_id=$1 AND scope_key=$2").bind(run.as_uuid()).bind(scope).fetch_optional(&mut *tx).await?;
    wire::require(original == Some(deadline))?;
    if now(&mut tx).await? < deadline {
        tx.commit().await?;
        return Ok(false);
    }
    let already:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.osdeploy_decisions WHERE operation_id=$1 AND action IN ('activated_scope_expired','scope_expired_before_activation') AND attempt_id IS NOT DISTINCT FROM $2 AND payload_canonical_json::jsonb->'detail'->>'scope_key'=$3)")
        .bind(op.as_uuid()).bind(snapshot.attempt_id().map(|a|a.as_uuid())).bind(scope).fetch_one(&mut *tx).await?;
    if already {
        tx.commit().await?;
        return Ok(false);
    }
    let proof = if snapshot.attempt_id().is_some() {
        OsDeployTransitionProof::activated_scope_expired(s, &mut tx, op, snapshot.revision())
            .await?
    } else {
        OsDeployTransitionProof::unactivated_scope_expired(s, &mut tx, op, snapshot.revision())
            .await?
    };
    append_osdeploy_transition(&mut tx, proof).await?;
    recovery::settle_selected(&mut tx, &snapshot).await?;
    Box::pin(load::load_execution(&mut tx, op)).await?;
    tx.commit().await?;
    Ok(true)
}

async fn repair_one(s: &Scheduler, op: OperationId) -> Result<bool, Error> {
    let mut tx = s.store.pool().begin().await?;
    authority(s, &mut tx).await?;
    let snapshot = Box::pin(locked_execution(&mut tx, op)).await?;
    let before:Option<serde_json::Value>=sqlx::query_scalar("SELECT to_jsonb(p) FROM rust_controller.osdeploy_schedule_projection p WHERE operation_id=$1 FOR UPDATE").bind(op.as_uuid()).fetch_optional(&mut *tx).await?;
    let lease: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM rust_controller.worker_leases WHERE operation_id=$1)",
    )
    .bind(op.as_uuid())
    .fetch_one(&mut *tx)
    .await?;
    if !lease && !snapshot.cancelled() && snapshot.deadline_at().is_some() {
        let at = now(&mut tx).await?;
        if snapshot.deadline_at().is_some_and(|deadline| at < deadline) {
            if snapshot.state() == ExecutionState::Waiting {
                recovery::restore_waiting_projection(&mut tx, &snapshot).await?;
            } else if snapshot.state() == ExecutionState::Unknown {
                recovery::restore_unknown_schedule(s, &mut tx, &snapshot, None).await?;
            }
        }
    }
    if lease
        || snapshot.cancelled()
        || !matches!(
            snapshot.state(),
            ExecutionState::Waiting | ExecutionState::Unknown
        )
        || (snapshot.state() == ExecutionState::Unknown && snapshot.dispatch().is_none())
    {
        sqlx::query(
            "DELETE FROM rust_controller.osdeploy_schedule_projection WHERE operation_id=$1",
        )
        .bind(op.as_uuid())
        .execute(&mut *tx)
        .await?;
    }
    let after:Option<serde_json::Value>=sqlx::query_scalar("SELECT to_jsonb(p) FROM rust_controller.osdeploy_schedule_projection p WHERE operation_id=$1").bind(op.as_uuid()).fetch_optional(&mut *tx).await?;
    Box::pin(load::load_execution(&mut tx, op)).await?;
    tx.commit().await?;
    Ok(before != after)
}

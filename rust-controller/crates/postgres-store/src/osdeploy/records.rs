use super::*;
use controller_domain::ExecutionState;
use osdeploy_adapter::restore_osdeploy_plan_v1;
use sqlx::{Postgres, Row, Transaction};
use uuid::Uuid;

pub(super) async fn load(
    tx: &mut Transaction<'_, Postgres>,
    run: RunId,
) -> Result<OsDeployRegistrationV1, OsDeployStoreError> {
    let row = sqlx::query("SELECT contract_version,workflow_sha256,plan_canonical_json FROM rust_controller.osdeploy_runs WHERE run_id=$1").bind(run.as_uuid()).fetch_optional(&mut **tx).await?.ok_or(OsDeployStoreError::Validation)?;
    if row.try_get::<i16, _>("contract_version")? != 1 {
        return Err(OsDeployStoreError::Validation);
    }
    let hash: String = row.try_get("workflow_sha256")?;
    let plan = restore_osdeploy_plan_v1(&row.try_get::<String, _>("plan_canonical_json")?, &hash)
        .map_err(|_| OsDeployStoreError::Validation)?;
    let stages = stage::plans(&plan)?;
    let reservation = sqlx::query("SELECT cluster_key,vmid,vm_uuid,mac,plan_digest FROM rust_controller.native_vm_reservations WHERE run_id=$1").bind(run.as_uuid()).fetch_optional(&mut **tx).await?.ok_or(OsDeployStoreError::Validation)?;
    let vm = plan.vm();
    if reservation.try_get::<String, _>("cluster_key")? != vm.cluster_key().to_string()
        || reservation.try_get::<i32, _>("vmid")?.to_string() != vm.target_vmid().to_string()
        || reservation.try_get::<Uuid, _>("vm_uuid")?.to_string() != vm.uuid().to_string()
        || reservation.try_get::<String, _>("mac")? != vm.mac().to_string()
        || reservation.try_get::<String, _>("plan_digest")? != crate::native::digest(vm)?
    {
        return Err(OsDeployStoreError::Validation);
    }
    let agents: Vec<String> = sqlx::query_scalar(
        "SELECT expected_agent_id FROM rust_controller.osdeploy_agent_reservations WHERE run_id=$1",
    )
    .bind(run.as_uuid())
    .fetch_all(&mut **tx)
    .await?;
    if agents != [plan.names().expected_agent_id()] {
        return Err(OsDeployStoreError::Validation);
    }
    let count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM rust_controller.operations WHERE run_id=$1")
            .bind(run.as_uuid())
            .fetch_one(&mut **tx)
            .await?;
    let rows = sqlx::query("SELECT p.operation_id,p.ordinal,p.stage,p.predecessor_id,p.dependency_kind,p.command_sha256,p.pve_plan_sha256,o.run_id,o.workflow_kind,o.operation_key,o.contract_version,o.state,o.revision FROM rust_controller.osdeploy_operation_plans p JOIN rust_controller.operations o USING(operation_id) WHERE p.run_id=$1 ORDER BY p.ordinal").bind(run.as_uuid()).fetch_all(&mut **tx).await?;
    if count != 16 || rows.len() != 16 {
        return Err(OsDeployStoreError::Validation);
    }
    let mut operations = Vec::with_capacity(16);
    for (index, (row, derived)) in rows.iter().zip(&stages).enumerate() {
        let operation: OperationId = crate::native::id(row.try_get("operation_id")?)?;
        let _: ExecutionState =
            serde_json::from_value(serde_json::Value::String(row.try_get("state")?))?;
        let command_hash = derived.fingerprint()?;
        let pve_hash = derived
            .pve()
            .map(|p| p.fingerprint().map_err(|_| OsDeployStoreError::Validation))
            .transpose()?;
        let expected_predecessor = operations.last().map(|p: &OperationId| p.as_uuid());
        if row.try_get::<i16, _>("ordinal")? != index as i16
            || row.try_get::<String, _>("stage")? != stage::name(derived.stage())?
            || row.try_get::<Option<Uuid>, _>("predecessor_id")? != expected_predecessor
            || row.try_get::<String, _>("dependency_kind")? != stage::dependency(index)
            || row.try_get::<String, _>("command_sha256")? != command_hash
            || row.try_get::<Option<String>, _>("pve_plan_sha256")? != pve_hash
            || row.try_get::<Uuid, _>("run_id")? != run.as_uuid()
            || row.try_get::<String, _>("workflow_kind")? != "os_deploy"
            || row.try_get::<String, _>("operation_key")? != derived.stage().operation_key()
            || row.try_get::<i16, _>("contract_version")? != 1
            || row.try_get::<i64, _>("revision")? < 0
        {
            return Err(OsDeployStoreError::Validation);
        }
        let commands: Vec<(String, String)> = sqlx::query_as("SELECT idempotency_key,payload_digest FROM rust_controller.commands WHERE operation_id=$1").bind(operation.as_uuid()).fetch_all(&mut **tx).await?;
        if commands != [(stage::command_key(run, derived.stage()), command_hash)] {
            return Err(OsDeployStoreError::Validation);
        }
        operations.push(operation);
    }
    Ok(OsDeployRegistrationV1 {
        ids: OsDeployWorkflowIds {
            run_id: run,
            operations: operations
                .try_into()
                .map_err(|_| OsDeployStoreError::Validation)?,
            workflow_sha256: hash,
        },
        plan,
        stages,
    })
}

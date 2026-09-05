use super::*;
use crate::{CommandAppend, PgStore};
use controller_domain::{CommandEnvelope, SemanticOperationKey, WorkflowKind};
use osdeploy_adapter::restore_osdeploy_plan_v1;
use uuid::Uuid;

impl PgStore {
    pub async fn enqueue_osdeploy(
        &self,
        run: RunId,
        plan: &OsDeployPlanV1,
    ) -> Result<OsDeployWorkflowIds, OsDeployStoreError> {
        let canonical = serde_json::to_string(plan)?;
        let hash = plan
            .fingerprint()
            .map_err(|_| OsDeployStoreError::Validation)?;
        let plan = restore_osdeploy_plan_v1(&canonical, &hash)
            .map_err(|_| OsDeployStoreError::Validation)?;
        let stages = stage::plans(&plan)?;
        let vm = plan.vm();
        let vmid: i32 = vm
            .target_vmid()
            .to_string()
            .parse()
            .map_err(|_| OsDeployStoreError::Validation)?;
        let uuid: Uuid = vm
            .uuid()
            .to_string()
            .parse()
            .map_err(|_| OsDeployStoreError::Validation)?;
        let vm_digest = crate::native::digest(vm)?;
        // All fallible plan conversion, fingerprints and database integer casts precede writes.
        let commands = stages
            .iter()
            .enumerate()
            .map(|(index, stage)| {
                let ordinal = i16::try_from(index).map_err(|_| OsDeployStoreError::Validation)?;
                let semantic = SemanticOperationKey::new(
                    WorkflowKind::OsDeploy,
                    run,
                    stage.stage().operation_key(),
                    1,
                )
                .map_err(|_| OsDeployStoreError::Validation)?;
                let command = CommandEnvelope::new(
                    stage::command_key(run, stage.stage()),
                    semantic,
                    stage.fingerprint()?,
                )
                .map_err(|_| OsDeployStoreError::Validation)?;
                let pve_hash = stage
                    .pve()
                    .map(|p| p.fingerprint().map_err(|_| OsDeployStoreError::Validation))
                    .transpose()?;
                Ok((ordinal, command, pve_hash, stage::name(stage.stage())?))
            })
            .collect::<Result<Vec<_>, OsDeployStoreError>>()?;
        let mut tx = self.pool().begin().await?;
        crate::native::lock_run(&mut tx, run).await?;
        let existing: Option<String> = sqlx::query_scalar(
            "SELECT workflow_sha256 FROM rust_controller.osdeploy_runs WHERE run_id=$1",
        )
        .bind(run.as_uuid())
        .fetch_optional(&mut *tx)
        .await?;
        if let Some(existing) = existing {
            if existing != hash {
                return Err(OsDeployStoreError::Conflict);
            }
            let loaded = records::load(&mut tx, run).await?;
            if loaded.plan() != &plan {
                return Err(OsDeployStoreError::Conflict);
            }
            tx.commit().await?;
            return Ok(loaded.ids);
        }
        let occupied: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM rust_controller.operations WHERE run_id=$1) OR EXISTS(SELECT 1 FROM rust_controller.native_vm_reservations WHERE run_id=$1)").bind(run.as_uuid()).fetch_one(&mut *tx).await?;
        if occupied {
            return Err(OsDeployStoreError::Conflict);
        }
        let mut identities = [
            format!(
                "native:identity:{}:vmid:{}",
                vm.cluster_key(),
                vm.target_vmid()
            ),
            format!("native:identity:{}:uuid:{}", vm.cluster_key(), vm.uuid()),
            format!("native:identity:{}:mac:{}", vm.cluster_key(), vm.mac()),
        ];
        identities.sort();
        for key in identities.into_iter().chain(std::iter::once(format!(
            "osdeploy:agent:{}",
            plan.names().expected_agent_id()
        ))) {
            sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1,0))")
                .bind(key)
                .execute(&mut *tx)
                .await?;
        }
        sqlx::query("INSERT INTO rust_controller.native_vm_reservations(cluster_key,vmid,vm_uuid,mac,run_id,plan_digest) VALUES($1,$2,$3,$4,$5,$6)").bind(vm.cluster_key().to_string()).bind(vmid).bind(uuid).bind(vm.mac().to_string()).bind(run.as_uuid()).bind(vm_digest).execute(&mut *tx).await?;
        sqlx::query("INSERT INTO rust_controller.osdeploy_runs(run_id,contract_version,workflow_sha256,plan_canonical_json) VALUES($1,1,$2,$3)").bind(run.as_uuid()).bind(&hash).bind(canonical).execute(&mut *tx).await?;
        sqlx::query("INSERT INTO rust_controller.osdeploy_agent_reservations(expected_agent_id,run_id) VALUES($1,$2)").bind(plan.names().expected_agent_id()).bind(run.as_uuid()).execute(&mut *tx).await?;
        let ids = OsDeployWorkflowIds {
            run_id: run,
            operations: std::array::from_fn(|_| OperationId::new()),
            workflow_sha256: hash,
        };
        for (index, (ordinal, command, pve_hash, name)) in commands.into_iter().enumerate() {
            let operation = ids.operations[index];
            if Self::append_command_tx(&mut tx, operation, &command).await?
                != CommandAppend::Appended(operation)
            {
                return Err(OsDeployStoreError::Conflict);
            }
            let predecessor = index
                .checked_sub(1)
                .map(|previous| ids.operations[previous].as_uuid());
            sqlx::query("INSERT INTO rust_controller.osdeploy_operation_plans(operation_id,run_id,ordinal,stage,predecessor_id,dependency_kind,command_sha256,pve_plan_sha256) VALUES($1,$2,$3,$4,$5,$6,$7,$8)").bind(operation.as_uuid()).bind(run.as_uuid()).bind(ordinal).bind(name).bind(predecessor).bind(stage::dependency(index)).bind(command.payload_digest()).bind(pve_hash).execute(&mut *tx).await?;
        }
        tx.commit().await?;
        Ok(ids)
    }
    pub async fn load_osdeploy_registration(
        &self,
        run: RunId,
    ) -> Result<OsDeployRegistrationV1, OsDeployStoreError> {
        let mut tx = self.pool().begin().await?;
        sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            .execute(&mut *tx)
            .await?;
        let loaded = records::load(&mut tx, run).await?;
        tx.commit().await?;
        Ok(loaded)
    }
}

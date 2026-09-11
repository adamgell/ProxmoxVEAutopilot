//! Fixture credential delivery after committed boot arming. No callback authority.
use super::*;
use api_compat::run_bearer::{IssuedRunBearer, RunBearerIdentity, issue_run_bearer};

impl Scheduler {
    /// Issue only for the server-created fixture origin and current StartPe
    /// attempt. The credential becomes observable only after its permanent alias
    /// commits. Reissue and renewal preserve the original package and deadline.
    /// This does not make initial dispatch and credential delivery atomic.
    pub async fn issue_fixture_pe_credential(
        &self,
        grant: &LeaseGrant,
        expires_at: i64,
        signing_secret: &[u8],
    ) -> Result<IssuedRunBearer, Error> {
        if !self.fixture_start_pe {
            return Err(Error::CapabilityUnavailable);
        }
        let mut tx = self.store.pool().begin().await?;
        authority(self, &mut tx).await?;
        let snapshot = locked_execution(&mut tx, grant.operation_id()).await?;
        wire::require(snapshot.plan().stage() == OsDeployStage::StartPe)?;
        admit(self, &snapshot, snapshot.plan().workflow_sha256())?;
        let (current, _) = current_grant(&mut tx, self, grant, &snapshot).await?;
        let at = now(&mut tx).await?;
        active_at(&current, at)?;
        let origin: Option<(String, String, String, String)> = sqlx::query_as("SELECT source_namespace,claim_kind,claim_value,workflow_sha256 FROM rust_controller.fixture_osdeploy_origins WHERE run_id=$1")
            .bind(snapshot.run_id().as_uuid()).fetch_optional(&mut *tx).await?;
        let (namespace, kind, identity, workflow) = origin.ok_or(Error::Validation)?;
        wire::require(
            namespace == "rust-owned-fixture-v1"
                && kind == "text"
                && identity == snapshot.run_id().as_uuid().to_string()
                && workflow == snapshot.plan().workflow_sha256(),
        )?;
        let registration = load::load_registration(&mut tx, snapshot.run_id()).await?;
        let package = registration
            .materialize_pe_package_semantics()
            .map_err(|_| Error::Validation)?;
        let session: Option<(Uuid, Uuid, String, DateTime<Utc>)> = sqlx::query_as("SELECT run_id,attempt_id,package_sha256,registration_deadline FROM rust_controller.fixture_pe_boot_sessions WHERE operation_id=$1 FOR UPDATE")
            .bind(grant.operation_id().as_uuid()).fetch_optional(&mut *tx).await?;
        let (run, attempt, digest, deadline) = session.ok_or(Error::Validation)?;
        wire::require(
            run == snapshot.run_id().as_uuid()
                && attempt == current.attempt_id().as_uuid()
                && digest == package.envelope_sha256(),
        )?;
        if at >= deadline || expires_at < at.timestamp() {
            return Err(Error::FenceLost);
        }
        let issued = issue_run_bearer(
            RunBearerIdentity::Text(&identity),
            expires_at,
            signing_secret,
        )
        .map_err(|_| Error::Validation)?;
        let alias = issued.metadata().alias_sha256().as_slice();
        // ON CONFLICT waits for a concurrent owner; the following read checks
        // its entire immutable association rather than treating conflict as success.
        sqlx::query("INSERT INTO rust_controller.fixture_pe_credential_aliases(alias_sha256,operation_id,run_id,attempt_id,package_sha256,expires_at,created_at) VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(alias_sha256) DO NOTHING")
            .bind(alias).bind(grant.operation_id().as_uuid()).bind(run).bind(attempt).bind(&digest).bind(expires_at).bind(at).execute(&mut *tx).await?;
        let owner: (Uuid, Uuid, Uuid, String, i64) = sqlx::query_as("SELECT operation_id,run_id,attempt_id,package_sha256,expires_at FROM rust_controller.fixture_pe_credential_aliases WHERE alias_sha256=$1")
            .bind(alias).fetch_one(&mut *tx).await?;
        if owner
            != (
                grant.operation_id().as_uuid(),
                run,
                attempt,
                digest,
                expires_at,
            )
        {
            return Err(Error::Conflict);
        }
        let final_at = now(&mut tx).await?;
        active_at(&current, final_at)?;
        if final_at >= deadline || expires_at < final_at.timestamp() {
            return Err(Error::FenceLost);
        }
        tx.commit().await?;
        Ok(issued)
    }
}

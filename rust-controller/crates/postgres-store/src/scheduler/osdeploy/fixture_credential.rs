//! Fixture credential delivery after committed boot arming. No callback authority.
use super::*;
use api_compat::run_bearer::{IssuedRunBearer, RunBearerIdentity, issue_run_bearer};

/// Transaction-local ownership data, not a credential or dispatch capability.
/// The caller must derive every field from its locked origin/session and issuer.
pub(super) struct FixtureAliasOwner<'a> {
    pub(super) operation: Uuid,
    pub(super) run: Uuid,
    pub(super) attempt: Uuid,
    pub(super) package_sha256: &'a str,
    pub(super) expires_at: i64,
}

/// Insert or verify the complete immutable owner in the caller's transaction.
/// This never begins/commits a transaction, issues a token or grants authority.
/// Callers retain responsibility for admission and final fence/deadline checks.
pub(super) async fn retain_alias_owner(
    tx: &mut Transaction<'_, Postgres>,
    alias_sha256: &[u8; 32],
    owner: FixtureAliasOwner<'_>,
    at: DateTime<Utc>,
) -> Result<(), Error> {
    // Conflict waits for a concurrent owner; compare all fields afterwards.
    sqlx::query("INSERT INTO rust_controller.fixture_pe_credential_aliases(alias_sha256,operation_id,run_id,attempt_id,package_sha256,expires_at,created_at) VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(alias_sha256) DO NOTHING")
        .bind(alias_sha256.as_slice()).bind(owner.operation).bind(owner.run).bind(owner.attempt).bind(owner.package_sha256).bind(owner.expires_at).bind(at).execute(&mut **tx).await?;
    let persisted: (Uuid, Uuid, Uuid, String, i64) = sqlx::query_as("SELECT operation_id,run_id,attempt_id,package_sha256,expires_at FROM rust_controller.fixture_pe_credential_aliases WHERE alias_sha256=$1")
        .bind(alias_sha256.as_slice()).fetch_one(&mut **tx).await?;
    if persisted
        != (
            owner.operation,
            owner.run,
            owner.attempt,
            owner.package_sha256.to_owned(),
            owner.expires_at,
        )
    {
        return Err(Error::Conflict);
    }
    Ok(())
}

impl Scheduler {
    /// Issue only for the server-created fixture origin and current StartPe
    /// attempt. The credential becomes observable only after its permanent alias
    /// commits. Reissue and renewal preserve the original package and deadline.
    /// This does not make initial dispatch and credential delivery atomic.
    /// Transaction-local alias retention is private to the scheduler; callers
    /// cannot bypass the origin, session, lease and deadline admission here.
    ///
    /// ```compile_fail
    /// use postgres_store::scheduler::osdeploy::fixture_credential::retain_alias_owner;
    /// ```
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
        if requires_delivery(&mut tx, &snapshot).await? {
            return Err(Error::CapabilityUnavailable);
        }
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
        retain_alias_owner(
            &mut tx,
            issued.metadata().alias_sha256(),
            FixtureAliasOwner {
                operation: grant.operation_id().as_uuid(),
                run,
                attempt,
                package_sha256: &digest,
                expires_at,
            },
            at,
        )
        .await?;
        let final_at = now(&mut tx).await?;
        active_at(&current, final_at)?;
        if final_at >= deadline || expires_at < final_at.timestamp() {
            return Err(Error::FenceLost);
        }
        tx.commit().await?;
        Ok(issued)
    }
}

//! Stored fixture observation data, never original-IPC or execution authority.
use super::*;
use pve_port::{
    fixture_ipc::FixtureStageRequest,
    fixture_support::{FixtureLedgerStage, FixtureStageIdentity},
};

/// Validated persisted observation data, not an original IPC capture.
/// ```compile_fail
/// let _: postgres_store::FixtureStoredStartPeResponseV1 = serde_json::from_str("{}").unwrap();
/// ```
/// ```compile_fail
/// fn recapture(v: postgres_store::FixtureStoredStartPeResponseV1) {
///     let _: pve_port::fixture_support::FixtureStartPeResponseV1 = v.into();
/// }
/// ```
pub struct FixtureStoredStartPeResponseV1 {
    provenance_sha256: Option<String>,
    identity: FixtureStageIdentity,
    request: FixtureStageRequest,
    predecessor_identity: FixtureStageIdentity,
    predecessor: FixtureStageRequest,
    response: Vec<u8>,
}
impl FixtureStoredStartPeResponseV1 {
    pub fn identity(&self) -> &FixtureStageIdentity {
        &self.identity
    }
    pub fn request(&self) -> &FixtureStageRequest {
        &self.request
    }
    pub fn predecessor_identity(&self) -> &FixtureStageIdentity {
        &self.predecessor_identity
    }
    pub fn predecessor(&self) -> &FixtureStageRequest {
        &self.predecessor
    }
    pub fn original_receipt(&self) -> &[u8] {
        &self.response
    }
}
impl PgStore {
    /// Validate the original configured route, without granting execution authority.
    /// Historical rows without captured provenance are rejected, never backfilled.
    pub async fn load_fixture_start_pe_response_for_route(
        &self,
        operation: OperationId,
        route: &pve_port::fixture_ipc::FixtureSharedHistoryProvenanceV1,
    ) -> Result<Option<FixtureStoredStartPeResponseV1>, Error> {
        wire::require(route.operation() == operation.as_uuid())?;
        let stored = self.load_fixture_start_pe_response(operation).await?;
        if let Some(value) = &stored {
            wire::require(
                value.provenance_sha256.as_deref() == Some(route.sha256().as_str())
                    && value.identity.generation == route.generation(),
            )?;
        }
        Ok(stored)
    }
    /// Read-only repeatable snapshot validation. This does not authenticate a
    /// configured checkpoint channel or grant current continuation authority.
    pub async fn load_fixture_start_pe_response(
        &self,
        operation: OperationId,
    ) -> Result<Option<FixtureStoredStartPeResponseV1>, Error> {
        Box::pin(async move {
            let mut tx = self.pool().begin().await?;
            sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY").execute(&mut *tx).await?;
            let snapshot = load::load_execution(&mut tx, operation).await?;
            wire::require(snapshot.plan().stage() == OsDeployStage::StartPe)?;
            let row = sqlx::query("SELECT provenance_sha256,identity_canonical_json,request_envelope,predecessor_identity_canonical_json,predecessor_request_envelope,response_envelope FROM rust_controller.fixture_start_pe_responses WHERE operation_id=$1").bind(operation.as_uuid()).fetch_optional(&mut *tx).await?;
            let Some(row) = row else { tx.commit().await?; return Ok(None); };
            let identity: FixtureStageIdentity = wire::decode_exact(&row.try_get::<String,_>("identity_canonical_json")?, 8192)?;
            let predecessor_identity: FixtureStageIdentity = wire::decode_exact(&row.try_get::<String,_>("predecessor_identity_canonical_json")?, 8192)?;
            let request_bytes: Vec<u8> = row.try_get("request_envelope")?;
            let predecessor_bytes: Vec<u8> = row.try_get("predecessor_request_envelope")?;
            let response: Vec<u8> = row.try_get("response_envelope")?;
            let provenance_sha256: Option<String> = row.try_get("provenance_sha256")?;
            let request = FixtureStageRequest::decode(&request_bytes).map_err(|_| Error::Validation)?;
            let predecessor = FixtureStageRequest::decode(&predecessor_bytes).map_err(|_| Error::Validation)?;
            identity.validate_request(&request).map_err(|_| Error::Validation)?;
            predecessor_identity.validate_request(&predecessor).map_err(|_| Error::Validation)?;
            let dispatch = snapshot.dispatch().ok_or(Error::Validation)?;
            let receipt = snapshot.receipt().ok_or(Error::Validation)?;
            let pve_port::ProvisioningMutationRequestV1::Start(start) = request.request() else { return Err(Error::Validation); };
            wire::require(request.request() == dispatch.request()
                && request.encode().map_err(|_| Error::Validation)? == request_bytes
                && predecessor.encode().map_err(|_| Error::Validation)? == predecessor_bytes
                && request.fixture_id() == predecessor.fixture_id()
                && predecessor_identity.stage == FixtureLedgerStage::ConfigurePe
                && predecessor.request().binding().same_operation_attempt(start.predecessor_binding())
                && predecessor.request().plan() == start.predecessor_plan())?;
            let prior = load::load_execution(&mut tx, predecessor.request().binding().operation_id()).await?;
            wire::require(prior.run_id() == snapshot.run_id()
                && prior.plan().stage() == OsDeployStage::ConfigurePe
                && prior.state() == controller_domain::ExecutionState::Satisfied
                && prior.dispatch().is_some_and(|d| d.request() == predecessor.request())
                && prior.attempt_id() == Some(predecessor.request().binding().attempt_id()))?;
            let decoded = request.decode_receipt(&response).map_err(|_| Error::Validation)?;
            wire::require(decoded.receipt() == receipt.receipt()
                && request.encode_receipt(decoded.submission_sequence(), decoded.receipt().clone()).map_err(|_| Error::Validation)? == response)?;
            tx.commit().await?;
            Ok(Some(FixtureStoredStartPeResponseV1 { provenance_sha256, identity, request, predecessor_identity, predecessor, response }))
        }).await
    }
}

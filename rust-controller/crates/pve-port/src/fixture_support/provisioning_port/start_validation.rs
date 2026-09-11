//! General-adapter read-only validation; never installs facts or a decision.
use super::*;
use crate::fixture_ipc::FixtureStageRequest;

/// Complete fixture postconditions validated against the caller's exact durable
/// stage binding. This is not a controller decision or a submission capability.
/// Private construction prevents raw deserialization from claiming validation.
#[derive(Debug)]
pub struct FixtureStartPeValidationOutcome {
    publication: FixtureStartPeFullPublicationV1,
}
impl FixtureStartPeValidationOutcome {
    pub fn publication(&self) -> &FixtureStartPeFullPublicationV1 {
        &self.publication
    }
}
impl FixtureProvisioningPort {
    /// Read/validate only. `None` means evidence is missing, never satisfied.
    /// A PostgreSQL caller must independently retain its lease/journal and
    /// progression rules. No general read cache or dispatch context is changed.
    pub async fn validate_start_pe(
        &self,
        identity: &FixtureStageIdentity,
        request: &FixtureStageRequest,
        original_receipt: &[u8],
    ) -> Result<Option<FixtureStartPeValidationOutcome>, PveReadError> {
        identity.validate_request(request).map_err(transport)?;
        let vm = request.request().plan().expected().vm();
        if identity.stage != FixtureLedgerStage::StartPe
            || identity.operation != self.identity.operation
            || request.fixture_id() != self.identity.fixture_id
            || vm.node().as_str() != self.identity.node
            || vm.source_vmid().get() != self.identity.source_vmid
            || vm.target_vmid().get() != self.identity.target_vmid
            || self
                .exact_identity
                .as_ref()
                .is_some_and(|exact| exact.request_sha256 != identity.request_sha256)
            || self.checkpoint.is_some()
            || self.resize.is_some()
            || self.legacy_resize.is_some()
            || self.late_configure.is_some()
            || self.late_start.is_some()
            || self
                .dispatched
                .lock()
                .map_err(|_| PveReadError::InvalidResponse)?
                .is_some()
        {
            return Err(PveReadError::InvalidResponse);
        }
        self.reads
            .start_pe_full(identity, request, original_receipt)
            .await
            .map_err(transport)
            .map(|publication| {
                publication.map(|publication| FixtureStartPeValidationOutcome { publication })
            })
    }
}

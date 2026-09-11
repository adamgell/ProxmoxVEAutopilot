//! Source-only atomic-result contract. No authenticated or committed path.
use crate::{OsDeployExecutionError as Error, PgStore};
use osdeploy_adapter::{
    AuthenticatedPeWitnessV1, PeRegisterAuthorityFenceV1, PeRegisterCallbackCandidateV1,
    PeRegisterVerifierContractV1, PeRegisterVerifierRefusal,
};

/// Diagnostic inputs only. A future transaction must load the contract, current
/// fence, original result and database clock under authority/run/operation locks.
/// Caller-supplied equality does not authenticate these values.
pub struct PeRegisterResultTransactionInputV1<'a> {
    pub contract: &'a PeRegisterVerifierContractV1,
    pub candidate: &'a PeRegisterCallbackCandidateV1,
    pub current_fence: &'a PeRegisterAuthorityFenceV1,
    pub result_revision: u64,
    pub original: Option<(&'a PeRegisterCallbackCandidateV1, u64)>,
    pub checked_unix_micros: u64,
}

/// Required disposition, not evidence that a database transaction was opened or
/// rolled back. Even Duplicate is a refusal, not an authenticated HTTP success.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PeRegisterResultRefusalV1 {
    RollbackRequired(PeRegisterVerifierRefusal),
}

impl PgStore {
    /// Never connects or writes. The only witness is Unavailable. Future result,
    /// journal, revision CAS, and registration/grace transitions must share one
    /// fenced transaction; no partial insert or separately committed grace is
    /// authorized by this diagnostic. Any refusal requires abandoning all writes.
    pub async fn assess_pe_register_result_transaction(
        &self,
        input: PeRegisterResultTransactionInputV1<'_>,
    ) -> Result<PeRegisterResultRefusalV1, Error> {
        input
            .contract
            .assess(
                input.candidate,
                input.current_fence,
                input.result_revision,
                AuthenticatedPeWitnessV1::Unavailable,
                input.original,
                input.checked_unix_micros,
            )
            .map(PeRegisterResultRefusalV1::RollbackRequired)
            .map_err(|_| Error::Validation)
    }
}

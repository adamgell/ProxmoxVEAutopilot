//! Pure verifier boundary. Descriptors are not authenticated authority or permits.
use crate::*;
use uuid::Uuid;

/// PostgreSQL-style fence, explicitly separate from UUID session context fields.
/// Its values must be reconstructed under the store's real authority locks.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PeRegisterAuthorityFenceV1 {
    pub authority_generation: u64,
    pub worker_id: String,
    pub lease_epoch: Uuid,
    pub operation_revision: u64,
}
impl PeRegisterAuthorityFenceV1 {
    fn validate(&self) -> Result<(), StartPeArmingError> {
        if self.authority_generation == 0
            || self.authority_generation > i64::MAX as u64
            || self.operation_revision == 0
            || self.operation_revision > i64::MAX as u64
            || self.lease_epoch.is_nil()
            || self.worker_id.is_empty()
            || self.worker_id.len() > 128
            || self.worker_id.trim() != self.worker_id
            || self.worker_id.chars().any(char::is_control)
        {
            return Err(StartPeArmingError);
        }
        Ok(())
    }
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PeRegisterVerifierRefusal {
    FenceMismatch,
    SessionBindingMismatch,
    ResultRevisionMismatch,
    AuthenticatedWitnessUnavailable,
    OriginalDeadlineExpired,
    Duplicate,
    Conflict,
}
/// Immutable expected session/request/revision descriptor. No Deserialize,
/// credentials, verification-key handling, result commit or success outcome.
#[derive(Clone, Debug)]
pub struct PeRegisterVerifierContractV1 {
    context: StartPeArmingContextV1,
    anchor: PeRegistrationAnchorV2,
    request: PeRegisterRequestIdentityV1,
    fence: PeRegisterAuthorityFenceV1,
    result_revision: u64,
}
impl PeRegisterVerifierContractV1 {
    pub fn new(
        context: StartPeArmingContextV1,
        anchor: PeRegistrationAnchorV2,
        request: PeRegisterRequestIdentityV1,
        fence: PeRegisterAuthorityFenceV1,
        result_revision: u64,
    ) -> Result<Self, StartPeArmingError> {
        context.validate()?;
        request.validate()?;
        fence.validate()?;
        if context.start_operation != anchor.start_operation()
            || result_revision == 0
            || result_revision > i64::MAX as u64
        {
            return Err(StartPeArmingError);
        }
        Ok(Self {
            context,
            anchor,
            request,
            fence,
            result_revision,
        })
    }
    /// Both expected/current fence and original result must come from trusted
    /// durable state. Equality here is only a necessary condition; without the
    /// actual authenticated witness and atomic store CAS it never grants access.
    pub fn assess(
        &self,
        candidate: &PeRegisterCallbackCandidateV1,
        current_fence: &PeRegisterAuthorityFenceV1,
        result_revision: u64,
        witness: AuthenticatedPeWitnessV1,
        original: Option<(&PeRegisterCallbackCandidateV1, u64)>,
        checked_unix_micros: u64,
    ) -> Result<PeRegisterVerifierRefusal, StartPeArmingError> {
        current_fence.validate()?;
        if current_fence != &self.fence {
            return Ok(PeRegisterVerifierRefusal::FenceMismatch);
        }
        if result_revision != self.result_revision {
            return Ok(PeRegisterVerifierRefusal::ResultRevisionMismatch);
        }
        let bytes = serde_json::to_vec(candidate).map_err(|_| StartPeArmingError)?;
        if PeRegisterCallbackCandidateV1::decode(&bytes, &self.context, &self.anchor, &self.request)
            .is_err()
        {
            return Ok(PeRegisterVerifierRefusal::SessionBindingMismatch);
        }
        if original.is_some_and(|(_, revision)| revision != self.result_revision) {
            return Ok(PeRegisterVerifierRefusal::Conflict);
        }
        Ok(
            match candidate.assess(
                checked_unix_micros,
                witness,
                original.map(|(candidate, _)| candidate),
            )? {
                PeRegisterCallbackRefusal::AuthenticatedWitnessUnavailable => {
                    PeRegisterVerifierRefusal::AuthenticatedWitnessUnavailable
                }
                PeRegisterCallbackRefusal::OriginalDeadlineExpired => {
                    PeRegisterVerifierRefusal::OriginalDeadlineExpired
                }
                PeRegisterCallbackRefusal::Duplicate => PeRegisterVerifierRefusal::Duplicate,
                PeRegisterCallbackRefusal::Conflict => PeRegisterVerifierRefusal::Conflict,
            },
        )
    }
}

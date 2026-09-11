//! Descriptive PeRegister callback binding, not authentication or acceptance.
use crate::{
    AuthenticatedPeWitnessV1, PeRegistrationAnchorV2, StartPeArmingContextV1, StartPeArmingError,
};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PeRegisterRequestIdentityV1 {
    pub request_id: Uuid,
    pub request_sha256: String,
    pub session_revision: u64,
}
impl PeRegisterRequestIdentityV1 {
    pub(crate) fn validate(&self) -> Result<(), StartPeArmingError> {
        if self.request_id.is_nil()
            || self.session_revision == 0
            || self.session_revision > i64::MAX as u64
            || !hash(&self.request_sha256)
        {
            return Err(StartPeArmingError);
        }
        Ok(())
    }
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PeRegisterResultIdentityV1 {
    pub result_id: Uuid,
    /// Claimed semantic payload identity, not evidence of authentication.
    pub payload_sha256: String,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PeRegisterCallbackRefusal {
    AuthenticatedWitnessUnavailable,
    OriginalDeadlineExpired,
    Duplicate,
    Conflict,
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct PeRegisterCallbackCandidateV1 {
    version: u8,
    context: StartPeArmingContextV1,
    request: PeRegisterRequestIdentityV1,
    result: PeRegisterResultIdentityV1,
    dispatch_event: Uuid,
    opened_unix_micros: u64,
    deadline_unix_micros: u64,
}
fn hash(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
impl PeRegisterCallbackCandidateV1 {
    pub fn new(
        context: StartPeArmingContextV1,
        anchor: &PeRegistrationAnchorV2,
        request: PeRegisterRequestIdentityV1,
        result: PeRegisterResultIdentityV1,
    ) -> Result<Self, StartPeArmingError> {
        context.validate()?;
        request.validate()?;
        if context.start_operation != anchor.start_operation()
            || request.request_id.is_nil()
            || result.result_id.is_nil()
            || request.session_revision == 0
            || request.session_revision > i64::MAX as u64
            || !hash(&request.request_sha256)
            || !hash(&result.payload_sha256)
        {
            return Err(StartPeArmingError);
        }
        Ok(Self {
            version: 1,
            context,
            request,
            result,
            dispatch_event: anchor.dispatch_event(),
            opened_unix_micros: anchor.opened_unix_micros(),
            deadline_unix_micros: anchor.deadline_unix_micros(),
        })
    }
    /// Expected session, request/revision and original anchor must come from
    /// durable server-side state. Incoming fields cannot establish that state.
    pub fn decode(
        bytes: &[u8],
        expected: &StartPeArmingContextV1,
        anchor: &PeRegistrationAnchorV2,
        request: &PeRegisterRequestIdentityV1,
    ) -> Result<Self, StartPeArmingError> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Wire {
            version: u8,
            context: StartPeArmingContextV1,
            request: PeRegisterRequestIdentityV1,
            result: PeRegisterResultIdentityV1,
            dispatch_event: Uuid,
            opened_unix_micros: u64,
            deadline_unix_micros: u64,
        }
        if bytes.is_empty() || bytes.len() > 4096 {
            return Err(StartPeArmingError);
        }
        let wire: Wire = serde_json::from_slice(bytes).map_err(|_| StartPeArmingError)?;
        if wire.version != 1
            || &wire.context != expected
            || &wire.request != request
            || wire.dispatch_event != anchor.dispatch_event()
            || wire.opened_unix_micros != anchor.opened_unix_micros()
            || wire.deadline_unix_micros != anchor.deadline_unix_micros()
        {
            return Err(StartPeArmingError);
        }
        Self::new(wire.context, anchor, wire.request, wire.result)
    }
    /// Pure refusal classification. Original means an independently restored
    /// original candidate, not a client-provided replacement. No result is
    /// accepted, persisted, acknowledged or converted to stage satisfaction.
    pub fn assess(
        &self,
        checked_unix_micros: u64,
        witness: AuthenticatedPeWitnessV1,
        original: Option<&Self>,
    ) -> Result<PeRegisterCallbackRefusal, StartPeArmingError> {
        if checked_unix_micros < self.opened_unix_micros {
            return Err(StartPeArmingError);
        }
        if checked_unix_micros >= self.deadline_unix_micros {
            return Ok(PeRegisterCallbackRefusal::OriginalDeadlineExpired);
        }
        if let Some(original) = original {
            return Ok(if original == self {
                PeRegisterCallbackRefusal::Duplicate
            } else {
                PeRegisterCallbackRefusal::Conflict
            });
        }
        match witness {
            AuthenticatedPeWitnessV1::Unavailable => {
                Ok(PeRegisterCallbackRefusal::AuthenticatedWitnessUnavailable)
            }
        }
    }
}

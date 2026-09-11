//! Descriptive atomic-arming proposal. No sessions, secrets or effects are created.
use serde::{Deserialize, Serialize};
use uuid::Uuid;

#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
#[error("StartPe arming contract rejected")]
pub struct StartPeArmingError;

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StartPeArmingContextV1 {
    pub run_id: Uuid,
    pub start_operation: Uuid,
    pub registration_operation: Uuid,
    pub attempt: Uuid,
    pub generation: Uuid,
    pub owner: Uuid,
    /// Public correlation identity only, never a session credential.
    pub session_id: Uuid,
    pub request_sha256: String,
}
impl StartPeArmingContextV1 {
    fn validate(&self) -> Result<(), StartPeArmingError> {
        if [
            self.run_id,
            self.start_operation,
            self.registration_operation,
            self.attempt,
            self.generation,
            self.owner,
            self.session_id,
        ]
        .iter()
        .any(Uuid::is_nil)
            || self.start_operation == self.registration_operation
            || self.request_sha256.len() != 64
            || !self
                .request_sha256
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(StartPeArmingError);
        }
        Ok(())
    }
}
/// Original StartPe PveDispatchCommitted event and original registration policy.
/// Callers must load these from durable history; a replay clock is not an anchor.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PeRegistrationAnchorV1 {
    start_operation: Uuid,
    dispatch_event: Uuid,
    opened_unix_ms: u64,
    budget_seconds: u32,
    deadline_unix_ms: u64,
}
impl PeRegistrationAnchorV1 {
    pub fn new(
        start_operation: Uuid,
        dispatch_event: Uuid,
        opened_unix_ms: u64,
        budget_seconds: u32,
    ) -> Result<Self, StartPeArmingError> {
        if start_operation.is_nil()
            || dispatch_event.is_nil()
            || opened_unix_ms == 0
            || !(1..=86400).contains(&budget_seconds)
        {
            return Err(StartPeArmingError);
        }
        let deadline_unix_ms = opened_unix_ms
            .checked_add(u64::from(budget_seconds) * 1000)
            .ok_or(StartPeArmingError)?;
        Ok(Self {
            start_operation,
            dispatch_event,
            opened_unix_ms,
            budget_seconds,
            deadline_unix_ms,
        })
    }
    pub fn deadline_unix_ms(&self) -> u64 {
        self.deadline_unix_ms
    }
    fn validate(&self) -> Result<(), StartPeArmingError> {
        if Self::new(
            self.start_operation,
            self.dispatch_event,
            self.opened_unix_ms,
            self.budget_seconds,
        )? != *self
        {
            return Err(StartPeArmingError);
        }
        Ok(())
    }
}
/// The authenticated callback verifier is not implemented. Deliberately no
/// authenticated/success variant exists; wire data cannot manufacture a witness.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AuthenticatedPeWitnessV1 {
    Unavailable,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum StartPeArmingRefusal {
    AuthenticatedWitnessUnavailable,
    OriginalRegistrationDeadlineExpired,
}
/// One immutable proposed unit for a future fenced session/dispatch transaction.
/// This type offers no commit/arm success. Serializing it is not persistence.
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct StartPeAtomicArmingProposalV1 {
    version: u8,
    context: StartPeArmingContextV1,
    anchor: PeRegistrationAnchorV1,
}
impl StartPeAtomicArmingProposalV1 {
    pub fn new(
        context: StartPeArmingContextV1,
        anchor: PeRegistrationAnchorV1,
    ) -> Result<Self, StartPeArmingError> {
        context.validate()?;
        anchor.validate()?;
        if context.start_operation != anchor.start_operation {
            return Err(StartPeArmingError);
        }
        Ok(Self {
            version: 1,
            context,
            anchor,
        })
    }
    /// Expected context and anchor must come from original durable authority,
    /// never from fields in the untrusted proposal being decoded.
    pub fn decode(
        bytes: &[u8],
        expected: &StartPeArmingContextV1,
        original: &PeRegistrationAnchorV1,
    ) -> Result<Self, StartPeArmingError> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Wire {
            version: u8,
            context: StartPeArmingContextV1,
            anchor: PeRegistrationAnchorV1,
        }
        if bytes.is_empty() || bytes.len() > 4096 {
            return Err(StartPeArmingError);
        }
        let wire: Wire = serde_json::from_slice(bytes).map_err(|_| StartPeArmingError)?;
        if wire.version != 1 || &wire.context != expected || &wire.anchor != original {
            return Err(StartPeArmingError);
        }
        Self::new(wire.context, wire.anchor)
    }
    pub fn anchor(&self) -> &PeRegistrationAnchorV1 {
        &self.anchor
    }
    /// Assessment only. All paths refuse arming and perform no I/O. Repeated
    /// assessment never extends the original registration deadline.
    pub fn assess(
        &self,
        now_unix_ms: u64,
        witness: AuthenticatedPeWitnessV1,
    ) -> Result<StartPeArmingRefusal, StartPeArmingError> {
        if now_unix_ms < self.anchor.opened_unix_ms {
            return Err(StartPeArmingError);
        }
        if now_unix_ms >= self.anchor.deadline_unix_ms {
            return Ok(StartPeArmingRefusal::OriginalRegistrationDeadlineExpired);
        }
        match witness {
            AuthenticatedPeWitnessV1::Unavailable => {
                Ok(StartPeArmingRefusal::AuthenticatedWitnessUnavailable)
            }
        }
    }
}

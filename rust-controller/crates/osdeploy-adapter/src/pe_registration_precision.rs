//! Exact microsecond anchor; no implicit rounding to the v1 millisecond contract.
use crate::{PeRegistrationAnchorV1, StartPeArmingError};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct PeRegistrationAnchorV2 {
    version: u8,
    start_operation: Uuid,
    dispatch_event: Uuid,
    opened_unix_micros: u64,
    budget_seconds: u32,
    deadline_unix_micros: u64,
}
impl PeRegistrationAnchorV2 {
    pub fn start_operation(&self) -> Uuid {
        self.start_operation
    }
    pub fn dispatch_event(&self) -> Uuid {
        self.dispatch_event
    }
    pub fn budget_seconds(&self) -> u32 {
        self.budget_seconds
    }

    /// Pure diagnostic assessment, not session or dispatch authority.
    pub fn assess(
        &self,
        context: &crate::StartPeArmingContextV1,
        checked_unix_micros: u64,
    ) -> Result<crate::StartPeArmingRefusal, StartPeArmingError> {
        context.validate()?;
        if context.start_operation != self.start_operation
            || checked_unix_micros < self.opened_unix_micros
        {
            return Err(StartPeArmingError);
        }
        if checked_unix_micros >= self.deadline_unix_micros {
            return Ok(crate::StartPeArmingRefusal::OriginalRegistrationDeadlineExpired);
        }
        Ok(crate::StartPeArmingRefusal::AuthenticatedWitnessUnavailable)
    }
    pub fn new(
        start_operation: Uuid,
        dispatch_event: Uuid,
        opened_unix_micros: u64,
        budget_seconds: u32,
    ) -> Result<Self, StartPeArmingError> {
        if start_operation.is_nil()
            || dispatch_event.is_nil()
            || opened_unix_micros == 0
            || !(1..=86400).contains(&budget_seconds)
        {
            return Err(StartPeArmingError);
        }
        let deadline_unix_micros = opened_unix_micros
            .checked_add(u64::from(budget_seconds) * 1_000_000)
            .ok_or(StartPeArmingError)?;
        Ok(Self {
            version: 2,
            start_operation,
            dispatch_event,
            opened_unix_micros,
            budget_seconds,
            deadline_unix_micros,
        })
    }
    /// The expected anchor must be reconstructed from the original durable
    /// dispatch event and policy, not from this untrusted document or retry time.
    pub fn decode(bytes: &[u8], original: &Self) -> Result<Self, StartPeArmingError> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Wire {
            version: u8,
            start_operation: Uuid,
            dispatch_event: Uuid,
            opened_unix_micros: u64,
            budget_seconds: u32,
            deadline_unix_micros: u64,
        }
        if bytes.is_empty() || bytes.len() > 4096 {
            return Err(StartPeArmingError);
        }
        let wire: Wire = serde_json::from_slice(bytes).map_err(|_| StartPeArmingError)?;
        let value = Self::new(
            wire.start_operation,
            wire.dispatch_event,
            wire.opened_unix_micros,
            wire.budget_seconds,
        )?;
        if wire.version != 2
            || wire.deadline_unix_micros != value.deadline_unix_micros
            || &value != original
        {
            return Err(StartPeArmingError);
        }
        Ok(value)
    }
    pub fn from_v1(original: &PeRegistrationAnchorV1) -> Result<Self, StartPeArmingError> {
        // Revalidate because the historical v1 anchor has a serde decoder.
        let validated = PeRegistrationAnchorV1::new(
            original.start_operation(),
            original.dispatch_event(),
            original.opened_unix_ms(),
            original.budget_seconds(),
        )?;
        if validated != *original {
            return Err(StartPeArmingError);
        }
        let opened = original
            .opened_unix_ms()
            .checked_mul(1000)
            .ok_or(StartPeArmingError)?;
        let value = Self::new(
            original.start_operation(),
            original.dispatch_event(),
            opened,
            original.budget_seconds(),
        )?;
        if original.deadline_unix_ms().checked_mul(1000) != Some(value.deadline_unix_micros) {
            return Err(StartPeArmingError);
        }
        Ok(value)
    }
    /// Lossless-only downgrade. No rounding/truncation or deadline extension.
    pub fn try_into_v1(&self) -> Result<PeRegistrationAnchorV1, StartPeArmingError> {
        if !self.opened_unix_micros.is_multiple_of(1000)
            || !self.deadline_unix_micros.is_multiple_of(1000)
        {
            return Err(StartPeArmingError);
        }
        PeRegistrationAnchorV1::new(
            self.start_operation,
            self.dispatch_event,
            self.opened_unix_micros / 1000,
            self.budget_seconds,
        )
    }
    pub fn opened_unix_micros(&self) -> u64 {
        self.opened_unix_micros
    }
    pub fn deadline_unix_micros(&self) -> u64 {
        self.deadline_unix_micros
    }
}

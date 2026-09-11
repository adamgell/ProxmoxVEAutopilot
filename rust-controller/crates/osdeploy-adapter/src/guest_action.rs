use serde::Serialize;
use uuid::Uuid;

use crate::{ContractError, OsDeployStage, StageKind};

/// Stable identity for a guest action exposed by a later callback service.
///
/// This value is descriptive only: it contains no callback session, secret,
/// result, or dispatch capability. `step_id` intentionally equals the durable
/// operation identity so reconstruction cannot manufacture a retry identity.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub struct GuestActionIdentity {
    pub step_id: Uuid,
    pub operation_id: Uuid,
    pub attempt_id: Uuid,
    pub retry_count: u8,
}

impl GuestActionIdentity {
    pub fn new(
        stage: OsDeployStage,
        operation_id: Uuid,
        attempt_id: Uuid,
    ) -> Result<Self, ContractError> {
        if stage.kind() != StageKind::GuestAction {
            return Err(ContractError::InvalidGuestActionStage);
        }
        if operation_id.is_nil() || attempt_id.is_nil() {
            return Err(ContractError::InvalidGuestActionIdentity);
        }
        Ok(Self {
            step_id: operation_id,
            operation_id,
            attempt_id,
            retry_count: 0,
        })
    }
}

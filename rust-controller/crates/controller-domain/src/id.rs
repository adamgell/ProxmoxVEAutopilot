use serde::{Deserialize, Serialize};
use thiserror::Error;
use uuid::Uuid;

macro_rules! opaque_id {
    ($name:ident) => {
        #[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Serialize, Deserialize)]
        #[serde(transparent)]
        pub struct $name(Uuid);

        impl $name {
            #[must_use]
            pub fn new() -> Self {
                Self(Uuid::now_v7())
            }

            #[must_use]
            pub const fn as_uuid(self) -> Uuid {
                self.0
            }
        }

        impl Default for $name {
            fn default() -> Self {
                Self::new()
            }
        }
    };
}

opaque_id!(RunId);
opaque_id!(OperationId);
opaque_id!(AttemptId);
opaque_id!(EventId);

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WorkflowKind {
    CloudOsd,
    OsDeploy,
    TaskSequence,
    SyntheticLongSleep,
}

#[derive(Clone, Debug, Eq, PartialEq, Hash, Serialize, Deserialize)]
pub struct SemanticOperationKey {
    pub workflow_kind: WorkflowKind,
    pub run_id: RunId,
    pub operation_key: String,
    pub contract_version: u16,
}

impl SemanticOperationKey {
    pub fn new(
        workflow_kind: WorkflowKind,
        run_id: RunId,
        operation_key: impl Into<String>,
        contract_version: u16,
    ) -> Result<Self, ValidationError> {
        let operation_key = operation_key.into();
        if operation_key.trim().is_empty() {
            return Err(ValidationError::EmptyOperationKey);
        }
        if contract_version == 0 {
            return Err(ValidationError::ZeroContractVersion);
        }

        Ok(Self {
            workflow_kind,
            run_id,
            operation_key,
            contract_version,
        })
    }

    pub fn validate(&self) -> Result<(), ValidationError> {
        if self.operation_key.trim().is_empty() {
            return Err(ValidationError::EmptyOperationKey);
        }
        if self.contract_version == 0 {
            return Err(ValidationError::ZeroContractVersion);
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Error, Eq, PartialEq)]
pub enum ValidationError {
    #[error("operation key must not be empty")]
    EmptyOperationKey,
    #[error("contract version must be greater than zero")]
    ZeroContractVersion,
}

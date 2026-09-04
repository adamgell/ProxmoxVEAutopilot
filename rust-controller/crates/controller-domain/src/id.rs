use serde::{Deserialize, Serialize, de};
use thiserror::Error;
use uuid::Uuid;

macro_rules! opaque_id {
    ($name:ident, $validate:expr, $error:literal) => {
        #[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Serialize)]
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

        impl<'de> Deserialize<'de> for $name {
            fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
            where
                D: de::Deserializer<'de>,
            {
                let uuid = Uuid::deserialize(deserializer)?;
                if !($validate)(uuid) {
                    return Err(de::Error::custom($error));
                }
                Ok(Self(uuid))
            }
        }
    };
}

fn is_existing_workflow_uuid(uuid: Uuid) -> bool {
    !uuid.is_nil()
}

fn is_uuid_v7(uuid: Uuid) -> bool {
    uuid.get_version_num() == 7
}

// A run can refer to a pre-Rust workflow UUID, while newly-created runs use UUIDv7.
opaque_id!(
    RunId,
    is_existing_workflow_uuid,
    "run id must be a non-nil workflow UUID"
);
opaque_id!(OperationId, is_uuid_v7, "operation id must be a UUIDv7");
opaque_id!(AttemptId, is_uuid_v7, "attempt id must be a UUIDv7");
opaque_id!(EventId, is_uuid_v7, "event id must be a UUIDv7");

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WorkflowKind {
    CloudOsd,
    OsDeploy,
    TaskSequence,
    SyntheticLongSleep,
}

/// ```compile_fail
/// use controller_domain::{RunId, SemanticOperationKey, WorkflowKind};
///
/// let _ = SemanticOperationKey {
///     workflow_kind: WorkflowKind::SyntheticLongSleep,
///     run_id: RunId::new(),
///     operation_key: String::new(),
///     contract_version: 0,
/// };
/// ```
#[derive(Clone, Debug, Eq, PartialEq, Hash, Serialize)]
pub struct SemanticOperationKey {
    workflow_kind: WorkflowKind,
    run_id: RunId,
    operation_key: String,
    contract_version: u16,
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

    #[must_use]
    pub const fn workflow_kind(&self) -> WorkflowKind {
        self.workflow_kind
    }

    #[must_use]
    pub const fn run_id(&self) -> RunId {
        self.run_id
    }

    #[must_use]
    pub fn operation_key(&self) -> &str {
        &self.operation_key
    }

    #[must_use]
    pub const fn contract_version(&self) -> u16 {
        self.contract_version
    }
}

#[derive(Deserialize)]
struct SemanticOperationKeyWire {
    workflow_kind: WorkflowKind,
    run_id: RunId,
    operation_key: String,
    contract_version: u16,
}

impl TryFrom<SemanticOperationKeyWire> for SemanticOperationKey {
    type Error = ValidationError;

    fn try_from(value: SemanticOperationKeyWire) -> Result<Self, Self::Error> {
        Self::new(
            value.workflow_kind,
            value.run_id,
            value.operation_key,
            value.contract_version,
        )
    }
}

impl<'de> Deserialize<'de> for SemanticOperationKey {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: de::Deserializer<'de>,
    {
        SemanticOperationKeyWire::deserialize(deserializer)?
            .try_into()
            .map_err(de::Error::custom)
    }
}

#[derive(Clone, Debug, Error, Eq, PartialEq)]
pub enum ValidationError {
    #[error("operation key must not be empty")]
    EmptyOperationKey,
    #[error("contract version must be greater than zero")]
    ZeroContractVersion,
}

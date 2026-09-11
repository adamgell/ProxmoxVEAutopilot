//! Complete identity shared by stage checkpoint, mutation and recovery consumers.
use super::durable_fixture_log::{FixtureLedgerStage, StageBinding};
use crate::{ProvisioningActionV1, fixture_ipc::FixtureStageRequest};
use serde::{Deserialize, Serialize};
use std::io;
use uuid::Uuid;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureStageIdentity {
    pub operation: Uuid,
    pub stage: FixtureLedgerStage,
    pub attempt: Uuid,
    pub generation: Uuid,
    pub owner: Uuid,
    pub request_sha256: String,
}
impl FixtureStageIdentity {
    pub fn validate(&self) -> io::Result<()> {
        if self.operation.is_nil()
            || self.attempt.is_nil()
            || self.generation.is_nil()
            || self.owner.is_nil()
            || self.request_sha256.len() != 64
            || !self
                .request_sha256
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(invalid());
        }
        Ok(())
    }
    pub fn validate_request(&self, request: &FixtureStageRequest) -> io::Result<()> {
        self.validate()?;
        let stage = match request.request().plan().action() {
            ProvisioningActionV1::Clone => FixtureLedgerStage::Clone,
            ProvisioningActionV1::EnsureCapacity => FixtureLedgerStage::DiskCapacity,
            ProvisioningActionV1::ConfigurePe => FixtureLedgerStage::ConfigurePe,
            ProvisioningActionV1::StartPe => FixtureLedgerStage::StartPe,
            ProvisioningActionV1::EnsureStopped => FixtureLedgerStage::PeEnsureStopped,
            _ => return Err(invalid()),
        };
        if self.operation != request.request().binding().operation_id().as_uuid()
            || self.attempt != request.request().binding().attempt_id().as_uuid()
            || self.stage != stage
            || self.request_sha256 != request.request_sha256()
        {
            return Err(invalid());
        }
        Ok(())
    }
    pub(crate) fn ledger_binding(&self) -> StageBinding {
        StageBinding {
            stage: self.stage,
            attempt: self.attempt,
            generation: self.generation,
            owner: self.owner,
        }
    }
}
pub(crate) fn invalid() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "stage identity rejected")
}

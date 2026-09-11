//! Independent, compact StartPe observations, supplied only by the supervisor.
use super::{
    FixtureStageIdentity, FixtureTaskObservation, FixtureTaskState, SeedPower, SeedRead,
    durable_fixture_log::{FixtureLog, StartObservationV1},
    stage_identity::invalid,
};
use crate::{MutationReceipt, PowerState, fixture_ipc::FixtureStageRequest};
use serde::{Deserialize, Serialize};
use std::io;
use uuid::Uuid;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FixtureStartPeObservationV1 {
    pub version: u8,
    pub task: FixtureTaskObservation,
    pub power: SeedRead<SeedPower>,
}

impl FixtureStartPeObservationV1 {
    pub(crate) fn publish(
        &self,
        identity: &FixtureStageIdentity,
        request: &FixtureStageRequest,
        log: &mut FixtureLog,
        generation: Uuid,
        accepted: u64,
    ) -> io::Result<StartObservationV1> {
        identity.validate_request(request)?;
        if identity.stage != super::FixtureLedgerStage::StartPe || self.version != 1 {
            return Err(invalid());
        }
        let effect = log
            .accepted_stage_effect(
                identity.operation,
                &identity.request_sha256,
                identity.ledger_binding(),
            )?
            .ok_or_else(invalid)?;
        let receipt = request
            .decode_receipt(effect.receipt().ok_or_else(invalid)?)
            .map_err(|_| invalid())?;
        let MutationReceipt::Task(upid) = receipt.receipt() else {
            return Err(invalid());
        };
        let task = FixtureTaskObservation::decode(&serde_json::to_vec(&self.task)?)?;
        if task.identity.fixture_id != request.fixture_id()
            || task.identity.node != request.request().plan().expected().vm().node().as_str()
            || task.identity.operation != identity.operation
            || task.identity.request_sha256 != identity.request_sha256
            || task.identity.upid != upid.as_str()
            || !matches!(task.result, FixtureTaskState::Succeeded {})
        {
            return Err(invalid());
        }
        let SeedRead::Observed {
            observed_unix_ms,
            value,
        } = &self.power
        else {
            return Err(invalid());
        };
        if value.power != PowerState::Running || value.locked {
            return Err(invalid());
        }
        log.record_start_observation(
            identity.operation,
            &identity.request_sha256,
            identity.ledger_binding(),
            generation,
            accepted,
            *observed_unix_ms,
            task.identity.upid,
            task.observed_unix_ms,
            super::post_dispatch_publication::now()?,
        )
    }
}

//! Power-aware release evidence; this is not permission to report task success.
use super::{
    FixtureStageIdentity, SeedPower, VmState,
    durable_fixture_log::{FixtureLog, PowerObservationV1},
    stage_identity::invalid,
};
use crate::{PowerState, fixture_ipc::FixtureStageRequest};
use serde::{Deserialize, Serialize};
use std::io;
use uuid::Uuid;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StartPePowerAuthorizationV1 {
    pub version: u8,
    pub predecessor: FixtureStageIdentity,
    pub predecessor_request: Vec<u8>,
    pub predecessor_receipt: Vec<u8>,
    pub power: SeedPower,
}

impl StartPePowerAuthorizationV1 {
    pub(crate) fn validate(
        &self,
        request: &FixtureStageRequest,
        log: &FixtureLog,
        daemon_generation: Uuid,
    ) -> io::Result<(VmState, PowerObservationV1)> {
        if self.version != 1 || self.power.power != PowerState::Stopped || self.power.locked {
            return Err(invalid());
        }
        let predecessor =
            FixtureStageRequest::decode(&self.predecessor_request).map_err(|_| invalid())?;
        self.predecessor.validate_request(&predecessor)?;
        request
            .validate_start_pe_predecessor(&predecessor, &self.predecessor_receipt)
            .map_err(|_| invalid())?;
        let observed = log.current_stopped_power(
            self.predecessor.operation,
            &self.predecessor.request_sha256,
            self.predecessor.ledger_binding(),
            &self.predecessor_receipt,
            daemon_generation,
            super::post_dispatch_publication::now()?,
        )?;
        let vm = request.request().plan().expected().vm();
        let state = VmState {
            disk_bytes: request
                .request()
                .plan()
                .expected()
                .effective_capacity_bytes(),
            pe_configured: true,
        };
        if log.world().get(&vm.target_vmid().get()) != Some(&state) {
            return Err(invalid());
        }
        Ok((state, observed))
    }
}

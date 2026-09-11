//! Read-only restoration. No dispatch/checkpoint or controller satisfaction trait.
use super::*;
use crate::{MutationReceipt, PowerState, fixture_ipc::FixtureStageRequest};
use serde::{Deserialize, Serialize};
use std::{io, path::PathBuf, time::Duration};
use uuid::Uuid;

pub struct FixtureStartPeRestoration {
    reads: FixtureReadClient,
    identity: FixtureStageIdentity,
    request: FixtureStageRequest,
    receipt: Vec<u8>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RestoredStartPeObservationV1 {
    pub version: u8,
    pub identity: FixtureStageIdentity,
    pub daemon_generation: Uuid,
    pub accepted_unix_ms: u64,
    pub published_unix_ms: u64,
    pub task: FixtureTaskObservation,
    pub power: SeedRead<SeedPower>,
}

impl FixtureStartPeRestoration {
    /// Read the independently collected full bundle without installing adapter
    /// state or returning a controller decision. Missing evidence stays missing.
    pub async fn observe_full(&self) -> io::Result<Option<FixtureStartPeFullPublicationV1>> {
        self.reads
            .start_pe_full(&self.identity, &self.request, &self.receipt)
            .await
    }
    pub fn restore(
        socket: PathBuf,
        timeout: Duration,
        identity: FixtureStageIdentity,
        request: FixtureStageRequest,
        receipt: Vec<u8>,
    ) -> io::Result<Self> {
        identity.validate_request(&request)?;
        let decoded = request
            .decode_receipt(&receipt)
            .map_err(|_| stage_identity::invalid())?;
        if identity.stage != FixtureLedgerStage::StartPe
            || !matches!(decoded.receipt(), MutationReceipt::Task(_))
        {
            return Err(stage_identity::invalid());
        }
        Ok(Self {
            reads: FixtureReadClient::new(socket, timeout)?,
            identity,
            request,
            receipt,
        })
    }

    /// Missing evidence stays missing. Original times are never refreshed and no
    /// unobserved configuration/inventory facts are projected from this record.
    pub async fn observe(&self) -> io::Result<Option<RestoredStartPeObservationV1>> {
        let Some(observation) = self
            .reads
            .start_pe_observation(&self.identity, &self.request, &self.receipt)
            .await?
        else {
            return Ok(None);
        };
        let (daemon_generation, accepted_unix_ms, power_observed) =
            observation.observation_clocks();
        Ok(Some(RestoredStartPeObservationV1 {
            version: 1,
            identity: self.identity.clone(),
            daemon_generation,
            accepted_unix_ms,
            published_unix_ms: observation.published_unix_ms,
            task: FixtureTaskObservation {
                version: 1,
                identity: FixtureTaskIdentity {
                    fixture_id: self.request.fixture_id(),
                    node: self
                        .request
                        .request()
                        .plan()
                        .expected()
                        .vm()
                        .node()
                        .as_str()
                        .to_owned(),
                    operation: self.identity.operation,
                    request_sha256: self.identity.request_sha256.clone(),
                    upid: observation.task_upid,
                },
                observed_unix_ms: observation.task_observed_unix_ms,
                result: FixtureTaskState::Succeeded {},
            },
            power: SeedRead::Observed {
                observed_unix_ms: power_observed,
                value: SeedPower {
                    power: PowerState::Running,
                    locked: false,
                },
            },
        }))
    }
}

use osdeploy_adapter::{ContractError, GuestActionIdentity, OsDeployStage, StageKind};
use uuid::Uuid;

const OP: Uuid = Uuid::from_u128(1);
const ATTEMPT: Uuid = Uuid::from_u128(2);

#[test]
fn only_the_five_guest_stages_admit_identity() {
    let admitted = OsDeployStage::ALL
        .into_iter()
        .filter(|stage| GuestActionIdentity::new(*stage, OP, ATTEMPT).is_ok())
        .collect::<Vec<_>>();
    assert_eq!(admitted.len(), 5);
    assert!(
        admitted
            .into_iter()
            .all(|stage| stage.kind() == StageKind::GuestAction)
    );
}

#[test]
fn identity_uses_operation_as_stable_step_and_zero_retry() {
    let identity = GuestActionIdentity::new(OsDeployStage::InstallAgent, OP, ATTEMPT).unwrap();
    assert_eq!(identity.step_id, OP);
    assert_eq!(identity.operation_id, OP);
    assert_eq!(identity.attempt_id, ATTEMPT);
    assert_eq!(identity.retry_count, 0);
    let reconstructed = GuestActionIdentity::new(OsDeployStage::InstallAgent, OP, ATTEMPT).unwrap();
    assert_eq!(identity, reconstructed);
}

#[test]
fn non_guest_stage_and_nil_identities_fail_closed() {
    assert_eq!(
        GuestActionIdentity::new(OsDeployStage::StartDisk, OP, ATTEMPT),
        Err(ContractError::InvalidGuestActionStage)
    );
    assert_eq!(
        GuestActionIdentity::new(OsDeployStage::InstallQga, Uuid::nil(), ATTEMPT),
        Err(ContractError::InvalidGuestActionIdentity)
    );
    assert_eq!(
        GuestActionIdentity::new(OsDeployStage::InstallQga, OP, Uuid::nil()),
        Err(ContractError::InvalidGuestActionIdentity)
    );
}

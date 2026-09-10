#![cfg(feature = "fixture-ipc")]
mod provisioning_support;
use provisioning_support::chain;
use pve_port::{fixture_ipc::*, *};

#[test]
fn first_three_stages_bind_exact_request_and_receipt_kind() {
    let episodes = chain();
    let fixture = controller_domain::RunId::new().as_uuid();
    let mut receipts: Vec<Vec<u8>> = Vec::new();
    for (i, episode) in episodes.iter().enumerate() {
        let envelope = FixtureStageRequest::new(fixture, episode.request.clone());
        if i > 2 {
            assert!(envelope.is_err());
            continue;
        }
        let envelope = envelope.unwrap();
        assert_eq!(
            FixtureStageRequest::decode(&envelope.encode().unwrap()).unwrap(),
            envelope
        );
        let receipt = match i {
            0 => MutationReceipt::Task(
                Upid::parse("UPID:pve-test:00000001:00000001:00000001:qmclone:900:fake@pve:")
                    .unwrap(),
            ),
            1 => MutationReceipt::Task(
                Upid::parse("UPID:pve-test:00000001:00000001:00000001:resize:101:fake@pve:")
                    .unwrap(),
            ),
            _ => MutationReceipt::SynchronousAccepted,
        };
        assert!(envelope.encode_receipt(0, receipt.clone()).is_err());
        let bytes = envelope.encode_receipt(1, receipt.clone()).unwrap();
        let decoded = envelope.decode_receipt(&bytes).unwrap();
        let mut altered: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        altered["request_sha256"] = serde_json::json!("0".repeat(64));
        assert!(
            envelope
                .decode_receipt(&serde_json::to_vec(&altered).unwrap())
                .is_err()
        );
        let mut wire: serde_json::Value =
            serde_json::from_slice(&envelope.encode().unwrap()).unwrap();
        wire["version"] = serde_json::json!(1);
        assert!(FixtureStageRequest::decode(&serde_json::to_vec(&wire).unwrap()).is_err());
        assert_eq!(decoded.receipt(), &receipt);
        assert_eq!(decoded.submission_sequence(), 1);
        for prior in &receipts {
            assert!(envelope.decode_receipt(prior).is_err());
        }
        let foreign = FixtureStageRequest::new(
            controller_domain::RunId::new().as_uuid(),
            episode.request.clone(),
        )
        .unwrap();
        assert!(foreign.decode_receipt(&bytes).is_err());
        receipts.push(bytes);
        for wrong in [
            "UPID:pve-test:00000001:00000001:00000001:resize:900:fake@pve:",
            "UPID:other:00000001:00000001:00000001:resize:101:fake@pve:",
            "UPID:pve-test:00000001:00000001:00000001:resize:101:other@pve:",
            "UPID:pve-test:00000001:00000001:00000001:qmstart:101:fake@pve:",
        ] {
            assert!(
                envelope
                    .encode_receipt(1, MutationReceipt::Task(Upid::parse(wrong).unwrap()))
                    .is_err()
            );
        }
        if i < 2 {
            assert!(
                envelope
                    .encode_receipt(1, MutationReceipt::SynchronousAccepted)
                    .is_err()
            );
        }
        assert!(FixtureStageRequest::new(uuid::Uuid::nil(), episode.request.clone()).is_err());
    }
}

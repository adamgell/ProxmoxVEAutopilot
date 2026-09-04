mod fake;
mod model;
mod observer;

use async_trait::async_trait;
use chrono::Duration as ChronoDuration;
use controller_domain::ObservationHealth;

pub use fake::{FakePve, PveRequest};
pub use model::{
    CloneIntent, EvidenceSource, MacAddress, NodeName, PveBaseUrl, PveEvidence, PveFact,
    PveFactKind, PveReadError, PveValidationError, QgaStatus, StorageName, TaskState, TaskStatus,
    Upid, VmConfig, VmUuid, Vmid, Volume,
};
pub use observer::{
    PveAccessMode, PveObserverBuildError, PveObserverConfig, PveRequestAudit, ReqwestPveObserver,
};

#[async_trait]
pub trait PveReadPort: Send + Sync {
    async fn vm_config(&self, node: &NodeName, vmid: Vmid) -> Result<VmConfig, PveReadError>;
    async fn task_status(&self, node: &NodeName, upid: &Upid) -> Result<TaskStatus, PveReadError>;
    async fn storage_content(
        &self,
        node: &NodeName,
        storage: &StorageName,
    ) -> Result<Vec<Volume>, PveReadError>;
    async fn qga_ping(&self, node: &NodeName, vmid: Vmid) -> Result<QgaStatus, PveReadError>;
}

pub async fn observe_clone_outcome<P>(port: &P, intent: CloneIntent) -> PveEvidence
where
    P: PveReadPort + ?Sized,
{
    let task = port.task_status(&intent.node, &intent.upid).await;
    let vm = port.vm_config(&intent.node, intent.vmid).await;
    evaluate_clone_evidence(task, vm, &intent)
}

fn evaluate_clone_evidence(
    task: Result<TaskStatus, PveReadError>,
    vm: Result<VmConfig, PveReadError>,
    intent: &CloneIntent,
) -> PveEvidence {
    let mut facts = Vec::new();
    let mut errors = Vec::new();
    let mut stale = false;
    let mut contradicted = false;

    let task_complete = match task {
        Ok(status) if status.upid() == &intent.upid => {
            stale |= is_stale(status.observed_at(), intent);
            let complete = status.succeeded();
            facts.push(PveFact {
                kind: PveFactKind::TaskCompletion { complete },
                observed_at: status.observed_at(),
            });
            Some(complete)
        }
        Ok(_) => {
            contradicted = true;
            None
        }
        Err(error) => {
            errors.push(error);
            None
        }
    };

    let vm_identity_satisfied = match vm {
        Ok(config) => {
            stale |= is_stale(config.observed_at(), intent);
            let satisfied = config.vmid() == intent.vmid
                && config.smbios_uuid() == Some(intent.expected_uuid)
                && config.mac_addresses() == &intent.expected_macs;
            contradicted |= !satisfied;
            facts.push(PveFact {
                kind: PveFactKind::VmIdentity { satisfied },
                observed_at: config.observed_at(),
            });
            Some(satisfied)
        }
        Err(error) => {
            errors.push(error);
            None
        }
    };

    let health = if contradicted {
        ObservationHealth::Contradicted
    } else if errors.contains(&PveReadError::Unauthorized) {
        ObservationHealth::Unauthorized
    } else if errors.contains(&PveReadError::TimedOut) {
        ObservationHealth::TimedOut
    } else if !errors.is_empty() {
        ObservationHealth::Unavailable
    } else if stale {
        ObservationHealth::Stale
    } else {
        ObservationHealth::Fresh
    };

    PveEvidence {
        task_complete,
        vm_identity_satisfied,
        health,
        observed_at: intent.as_of,
        source: EvidenceSource::PveApi,
        facts,
    }
}

fn is_stale(observed_at: chrono::DateTime<chrono::Utc>, intent: &CloneIntent) -> bool {
    let Ok(maximum_age) = ChronoDuration::from_std(intent.maximum_age) else {
        return true;
    };
    observed_at > intent.as_of || intent.as_of - observed_at > maximum_age
}

#[cfg(test)]
mod tests {
    use std::{
        collections::BTreeSet,
        sync::{
            Arc,
            atomic::{AtomicUsize, Ordering},
        },
        time::Duration,
    };

    use chrono::{DateTime, Utc};
    use controller_domain::ObservationHealth;
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::TcpListener,
    };

    use super::{
        CloneIntent, FakePve, MacAddress, NodeName, PveAccessMode, PveBaseUrl, PveObserverConfig,
        PveReadError, PveReadPort, ReqwestPveObserver, TaskStatus, Upid, VmConfig, VmUuid, Vmid,
        observe_clone_outcome,
    };

    fn time(value: &str) -> DateTime<Utc> {
        value.parse().unwrap()
    }

    fn node() -> NodeName {
        NodeName::parse("pve-test").unwrap()
    }

    fn upid() -> Upid {
        Upid::parse("UPID:pve-test:00000001:00000002:00000003:clone:101:test:").unwrap()
    }

    fn expected_uuid() -> VmUuid {
        VmUuid::parse("3f2504e0-4f89-41d3-9a0c-0305e82c3301").unwrap()
    }

    fn expected_mac() -> MacAddress {
        MacAddress::parse("02:00:00:00:01:01").unwrap()
    }

    fn intent(as_of: DateTime<Utc>) -> CloneIntent {
        CloneIntent::new(
            node(),
            Vmid::new(101).unwrap(),
            upid(),
            expected_uuid(),
            BTreeSet::from([expected_mac()]),
            as_of,
            Duration::from_secs(30),
        )
        .unwrap()
    }

    fn matching_vm(observed_at: DateTime<Utc>) -> VmConfig {
        VmConfig::new(
            Vmid::new(101).unwrap(),
            Some(expected_uuid()),
            BTreeSet::from([expected_mac()]),
            observed_at,
        )
    }

    #[test]
    fn identifiers_and_base_urls_reject_unsafe_or_ambiguous_values() {
        assert!(NodeName::parse("../pve").is_err());
        assert!(Upid::parse("task-not-a-upid").is_err());
        assert!(Vmid::new(0).is_err());
        assert!(MacAddress::parse("02:00:00:00:01").is_err());
        assert!(PveBaseUrl::parse("http://pve.example.invalid:8006").is_err());
        assert!(PveBaseUrl::parse("https://user@pve.example.invalid:8006").is_err());
        assert!(PveBaseUrl::parse("https://pve.example.invalid:8006/api2/json?raw=1").is_err());
        assert!(PveBaseUrl::parse("http://127.0.0.1:8006").is_ok());
        assert!(PveBaseUrl::parse("https://pve.example.invalid:8006").is_ok());
    }

    #[tokio::test]
    async fn upid_success_is_not_vm_postcondition() {
        let observed_at = time("2026-09-04T12:00:00Z");
        let fake = FakePve::new();
        fake.enqueue_task_status(
            node(),
            upid(),
            Ok(TaskStatus::complete(upid(), observed_at)),
        );

        let evidence = observe_clone_outcome(&fake, intent(time("2026-09-04T12:00:10Z"))).await;

        assert_eq!(evidence.task_complete, Some(true));
        assert_eq!(evidence.vm_identity_satisfied, None);
        assert_eq!(evidence.health, ObservationHealth::Unavailable);
        assert_eq!(evidence.facts.len(), 1);
    }

    #[tokio::test]
    async fn not_found_and_conflict_are_unavailable_observations() {
        for error in [PveReadError::NotFound, PveReadError::Conflict] {
            let fake = FakePve::new();
            fake.enqueue_task_status(node(), upid(), Err(error));
            fake.enqueue_vm_config(node(), Vmid::new(101).unwrap(), Err(PveReadError::NotFound));

            let evidence = observe_clone_outcome(&fake, intent(time("2026-09-04T12:00:10Z"))).await;

            assert_eq!(evidence.health, ObservationHealth::Unavailable);
            assert_eq!(evidence.task_complete, None);
        }
    }

    #[tokio::test]
    async fn timeout_is_timed_out_not_failed() {
        let fake = FakePve::new();
        fake.enqueue_task_status(node(), upid(), Err(PveReadError::TimedOut));
        fake.enqueue_vm_config(
            node(),
            Vmid::new(101).unwrap(),
            Ok(matching_vm(time("2026-09-04T12:00:00Z"))),
        );

        let evidence = observe_clone_outcome(&fake, intent(time("2026-09-04T12:00:10Z"))).await;

        assert_eq!(evidence.health, ObservationHealth::TimedOut);
        assert_eq!(evidence.task_complete, None);
        assert_eq!(evidence.vm_identity_satisfied, Some(true));
    }

    #[tokio::test]
    async fn old_observations_are_stale() {
        let old = time("2026-09-04T11:58:00Z");
        let fake = FakePve::new();
        fake.enqueue_task_status(node(), upid(), Ok(TaskStatus::complete(upid(), old)));
        fake.enqueue_vm_config(node(), Vmid::new(101).unwrap(), Ok(matching_vm(old)));

        let evidence = observe_clone_outcome(&fake, intent(time("2026-09-04T12:00:00Z"))).await;

        assert_eq!(evidence.task_complete, Some(true));
        assert_eq!(evidence.vm_identity_satisfied, Some(true));
        assert_eq!(evidence.health, ObservationHealth::Stale);
    }

    #[tokio::test]
    async fn contradictory_uuid_or_mac_is_never_identity_satisfied() {
        let observed_at = time("2026-09-04T12:00:00Z");
        let cases = [
            VmConfig::new(
                Vmid::new(101).unwrap(),
                Some(VmUuid::parse("c56a4180-65aa-42ec-a945-5fd21dec0538").unwrap()),
                BTreeSet::from([expected_mac()]),
                observed_at,
            ),
            VmConfig::new(
                Vmid::new(101).unwrap(),
                Some(expected_uuid()),
                BTreeSet::from([MacAddress::parse("02:00:00:00:01:02").unwrap()]),
                observed_at,
            ),
        ];

        for vm in cases {
            let fake = FakePve::new();
            fake.enqueue_task_status(
                node(),
                upid(),
                Ok(TaskStatus::complete(upid(), observed_at)),
            );
            fake.enqueue_vm_config(node(), Vmid::new(101).unwrap(), Ok(vm));

            let evidence = observe_clone_outcome(&fake, intent(time("2026-09-04T12:00:10Z"))).await;

            assert_eq!(evidence.vm_identity_satisfied, Some(false));
            assert_eq!(evidence.health, ObservationHealth::Contradicted);
        }
    }

    #[tokio::test]
    async fn late_upid_completion_is_a_new_fact_without_replaying_requests() {
        let observed_at = time("2026-09-04T12:00:00Z");
        let fake = FakePve::new();
        fake.enqueue_task_status(node(), upid(), Ok(TaskStatus::running(upid(), observed_at)));
        fake.enqueue_task_status(
            node(),
            upid(),
            Ok(TaskStatus::complete(upid(), observed_at)),
        );
        fake.enqueue_vm_config(
            node(),
            Vmid::new(101).unwrap(),
            Ok(matching_vm(observed_at)),
        );
        fake.enqueue_vm_config(
            node(),
            Vmid::new(101).unwrap(),
            Ok(matching_vm(observed_at)),
        );

        let first = observe_clone_outcome(&fake, intent(time("2026-09-04T12:00:10Z"))).await;
        let late = observe_clone_outcome(&fake, intent(time("2026-09-04T12:00:20Z"))).await;

        assert_eq!(first.task_complete, Some(false));
        assert_eq!(late.task_complete, Some(true));
        assert_eq!(late.vm_identity_satisfied, Some(true));
        assert_eq!(fake.recorded_requests().len(), 4);
    }

    async fn serve_once(
        status: u16,
        body: &'static str,
        delay: Duration,
    ) -> (String, Arc<AtomicUsize>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let requests = Arc::new(AtomicUsize::new(0));
        let recorded = Arc::clone(&requests);
        tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut request = [0_u8; 4096];
            let _ = stream.read(&mut request).await.unwrap();
            recorded.fetch_add(1, Ordering::SeqCst);
            tokio::time::sleep(delay).await;
            let reason = match status {
                200 => "OK",
                401 => "Unauthorized",
                403 => "Forbidden",
                404 => "Not Found",
                409 => "Conflict",
                _ => "Test",
            };
            let response = format!(
                "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            let _ = stream.write_all(response.as_bytes()).await;
        });
        (format!("http://{address}"), requests)
    }

    fn observer_for(base_url: &str, timeout: Duration) -> ReqwestPveObserver {
        ReqwestPveObserver::new(
            PveObserverConfig::new(
                PveBaseUrl::parse(base_url).unwrap(),
                PveAccessMode::Observe,
                false,
            )
            .with_timeout(timeout),
        )
        .unwrap()
    }

    #[tokio::test]
    async fn http_401_and_403_are_unauthorized_without_response_payloads() {
        for status in [401, 403] {
            let (base_url, requests) = serve_once(status, "sensitive body", Duration::ZERO).await;
            let observer = observer_for(&base_url, Duration::from_secs(1));

            let error = observer.task_status(&node(), &upid()).await.unwrap_err();

            assert_eq!(error, PveReadError::Unauthorized);
            assert!(!error.to_string().contains("sensitive body"));
            assert_eq!(requests.load(Ordering::SeqCst), 1);
        }
    }

    #[tokio::test]
    async fn http_404_and_409_remain_distinct_read_errors() {
        for (status, expected) in [(404, PveReadError::NotFound), (409, PveReadError::Conflict)] {
            let (base_url, _) = serve_once(status, "", Duration::ZERO).await;
            let observer = observer_for(&base_url, Duration::from_secs(1));

            assert_eq!(
                observer.task_status(&node(), &upid()).await.unwrap_err(),
                expected
            );
        }
    }

    #[tokio::test]
    async fn delayed_loopback_response_maps_to_timeout() {
        let (base_url, requests) = serve_once(
            200,
            r#"{"data":{"status":"running"}}"#,
            Duration::from_millis(100),
        )
        .await;
        let observer = observer_for(&base_url, Duration::from_millis(10));

        assert_eq!(
            observer.task_status(&node(), &upid()).await.unwrap_err(),
            PveReadError::TimedOut
        );
        assert_eq!(requests.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn sanitized_upid_fixture_decodes_as_completed_task() {
        let (base_url, _) = serve_once(
            200,
            include_str!("../../../fixtures/pve/upid-complete.json"),
            Duration::ZERO,
        )
        .await;
        let observer = observer_for(&base_url, Duration::from_secs(1));

        let status = observer.task_status(&node(), &upid()).await.unwrap();

        assert!(status.succeeded());
        assert_eq!(status.upid(), &upid());
    }

    #[tokio::test]
    async fn qga_ping_uses_the_typed_post_observation_endpoint() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let (request_sender, request_receiver) = tokio::sync::oneshot::channel();
        tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut request = [0_u8; 4096];
            let length = stream.read(&mut request).await.unwrap();
            request_sender
                .send(String::from_utf8_lossy(&request[..length]).into_owned())
                .unwrap();
            let body = r#"{"data":{}}"#;
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            stream.write_all(response.as_bytes()).await.unwrap();
        });
        let observer = observer_for(&format!("http://{address}"), Duration::from_secs(1));

        let status = observer
            .qga_ping(&node(), Vmid::new(101).unwrap())
            .await
            .unwrap();
        let request = request_receiver.await.unwrap();

        assert!(status.reachable());
        assert!(request.starts_with("POST /api2/json/nodes/pve-test/qemu/101/agent/ping HTTP/1.1"));
    }
}

mod credentials;
mod fake;
mod model;
mod native;
mod native_fake;
mod observer;
mod preflight;
mod visibility;

use async_trait::async_trait;
use chrono::Duration as ChronoDuration;
use controller_domain::ObservationHealth;

pub use credentials::{InvalidPveApiToken, PveApiToken};
pub use fake::{FakePve, PveRequest};
pub use model::{
    BridgeName, CloneIntent, EvidenceSource, MacAddress, NativeVmName, NodeName, PveBaseUrl,
    PveEvidence, PveFact, PveFactKind, PveReadError, PveValidationError, QgaStatus, StorageName,
    TaskState, TaskStatus, Upid, VmConfig, VmUuid, Vmid, Volume,
};
pub use native::{
    CloneRequest, ConfigureRequest, FakeCloneProvenance, InvalidNativeEvidence, InvalidNativePlan,
    MutationReceipt, NativeBinding, NativeCloneOwnership, NativeDecision, NativeEvaluation,
    NativeEvaluationContext, NativeEvaluationMode, NativeEvidence, NativeEvidenceInput,
    NativeEvidenceSource, NativeIdentityRead, NativeOperationPlan, NativeRead, NativeReason,
    NativeReceipt, NativeStep, NativeVmPlan, PveMutationPort, PveWriteError, StartRequest,
    evaluate_native_outcome, evaluate_native_preflight, is_fresh,
};
pub use native_fake::{
    FakeConfigRead, FakeControllerCheckpoint, FakeMutationOutcome, FakePause, NativeFakePve,
    NativeMutationRequest, UnsupportedFakeOutcome,
};
pub use observer::{
    PveAccessMode, PveObserverBuildError, PveObserverConfig, PveRequestAudit, ReqwestPveObserver,
};
pub use preflight::{
    BootDisk, BridgeInventory, ClusterVm, ClusterVmInventory, NativeVmConfig, NodeStatus,
    PowerState, PvePreflightReadPort, StorageStatus, UnsupportedConfig, VmPowerStatus,
    observe_target_absence, observe_target_absence_with_clock,
};
pub use visibility::{
    ClusterVisibility, GuestKind, PveVisibilityReadPort, VisibleGuest, VisiblePower,
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
    observe_clone_outcome_with_clock(port, intent, chrono::Utc::now).await
}

/// Sample the evaluation clock only after both reads finish. Inject a fixed
/// clock explicitly for historical replay; intent construction never dates facts.
pub async fn observe_clone_outcome_with_clock<P, C>(
    port: &P,
    intent: CloneIntent,
    clock: C,
) -> PveEvidence
where
    P: PveReadPort + ?Sized,
    C: FnOnce() -> chrono::DateTime<chrono::Utc>,
{
    let task = port.task_status(&intent.node, &intent.upid).await;
    let vm = port.vm_config(&intent.node, intent.vmid).await;
    evaluate_clone_evidence(task, vm, &intent, clock())
}

fn evaluate_clone_evidence(
    task: Result<TaskStatus, PveReadError>,
    vm: Result<VmConfig, PveReadError>,
    intent: &CloneIntent,
    as_of: chrono::DateTime<chrono::Utc>,
) -> PveEvidence {
    let mut facts = Vec::new();
    let mut errors = Vec::new();
    let mut stale = false;
    let mut contradicted = false;
    let mut task_state = None;

    let task_complete = match task {
        Ok(status) if status.upid() == &intent.upid => {
            stale |= is_stale(status.observed_at(), intent, as_of);
            let state = status.state();
            task_state = Some(state);
            let complete = status.succeeded();
            facts.push(PveFact {
                kind: PveFactKind::TaskState { state },
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
            stale |= is_stale(config.observed_at(), intent, as_of);
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
        task_state,
        vm_identity_satisfied,
        health,
        observed_at: as_of,
        source: EvidenceSource::PveApi,
        facts,
    }
}

fn is_stale(
    observed_at: chrono::DateTime<chrono::Utc>,
    intent: &CloneIntent,
    as_of: chrono::DateTime<chrono::Utc>,
) -> bool {
    let Ok(maximum_age) = ChronoDuration::from_std(intent.maximum_age) else {
        return true;
    };
    observed_at > as_of || as_of - observed_at > maximum_age
}

#[cfg(test)]
mod tests {
    use std::{
        collections::BTreeSet,
        ffi::OsString,
        io,
        sync::{Arc, Mutex},
        time::Duration,
    };

    use chrono::{DateTime, Utc};
    use controller_domain::ObservationHealth;
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::{TcpListener, TcpStream},
        task::JoinHandle,
    };

    use super::{
        CloneIntent, FakePve, MacAddress, NodeName, PveAccessMode, PveBaseUrl, PveObserverConfig,
        PveReadError, PveReadPort, ReqwestPveObserver, TaskStatus, Upid, VmConfig, VmUuid, Vmid,
        observe_clone_outcome, observe_clone_outcome_with_clock,
    };

    fn time(value: &str) -> DateTime<Utc> {
        value.parse().unwrap()
    }

    fn node() -> NodeName {
        NodeName::parse("pve-test").unwrap()
    }

    fn upid() -> Upid {
        Upid::parse("UPID:pve-test:00000001:00000002:00000003:clone:101:tester@pve:").unwrap()
    }

    fn expected_uuid() -> VmUuid {
        VmUuid::parse("3f2504e0-4f89-41d3-9a0c-0305e82c3301").unwrap()
    }

    fn expected_mac() -> MacAddress {
        MacAddress::parse("02:00:00:00:01:01").unwrap()
    }

    fn intent() -> CloneIntent {
        CloneIntent::new(
            node(),
            Vmid::new(101).unwrap(),
            upid(),
            expected_uuid(),
            BTreeSet::from([expected_mac()]),
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

    #[test]
    fn upid_requires_the_documented_proxmox_field_shape() {
        for malformed in [
            "UPID:pve-test",
            "UPID:pve-test:1:00000002:00000003:clone:101:tester@pve:",
            "UPID:pve-test:0000000g:00000002:00000003:clone:101:tester@pve:",
            "UPID:pve-test:00000001:00000002:00000003::101:tester@pve:",
            "UPID:pve-test:00000001:00000002:00000003:clone:101:tester:",
            "UPID:pve-test:00000001:00000002:00000003:clone:101:tester@pve@other:",
            "UPID:pve-test:00000001:00000002:00000003:clone:101:tester@pve",
        ] {
            assert!(Upid::parse(malformed).is_err(), "accepted {malformed}");
        }

        let parsed = upid();
        assert_eq!(parsed.node(), &node());
        assert_eq!(parsed.process_id(), 1);
        assert_eq!(parsed.process_start(), 2);
        assert_eq!(parsed.task_start(), 3);
        assert_eq!(parsed.worker_type(), "clone");
        assert_eq!(parsed.worker_id(), Some("101"));
        assert_eq!(parsed.authenticated_user(), "tester@pve");
    }

    #[test]
    fn clone_intent_rejects_a_upid_for_another_node() {
        let other_node_upid =
            Upid::parse("UPID:pve-other:00000001:00000002:00000003:clone:101:tester@pve:").unwrap();

        assert!(
            CloneIntent::new(
                node(),
                Vmid::new(101).unwrap(),
                other_node_upid,
                expected_uuid(),
                BTreeSet::from([expected_mac()]),
                Duration::from_secs(30),
            )
            .is_err()
        );
    }

    #[tokio::test]
    async fn task_status_rejects_route_node_mismatch_before_a_request() {
        let audit = super::PveRequestAudit::new();
        let observer = ReqwestPveObserver::new(
            PveObserverConfig::new(
                PveBaseUrl::parse("http://127.0.0.1:9").unwrap(),
                PveAccessMode::Observe,
                false,
            )
            .with_request_audit(audit.clone()),
        )
        .unwrap();
        let other_node_upid =
            Upid::parse("UPID:pve-other:00000001:00000002:00000003:clone:101:tester@pve:").unwrap();

        assert_eq!(
            observer
                .task_status(&node(), &other_node_upid)
                .await
                .unwrap_err(),
            PveReadError::UpidNodeMismatch
        );
        assert_eq!(audit.request_count(), 0);
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

        let evidence =
            observe_clone_outcome_with_clock(&fake, intent(), || time("2026-09-04T12:00:10Z"))
                .await;

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

            let evidence =
                observe_clone_outcome_with_clock(&fake, intent(), || time("2026-09-04T12:00:10Z"))
                    .await;

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

        let evidence =
            observe_clone_outcome_with_clock(&fake, intent(), || time("2026-09-04T12:00:10Z"))
                .await;

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

        let evidence =
            observe_clone_outcome_with_clock(&fake, intent(), || time("2026-09-04T12:00:00Z"))
                .await;

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

            let evidence =
                observe_clone_outcome_with_clock(&fake, intent(), || time("2026-09-04T12:00:10Z"))
                    .await;

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

        let first =
            observe_clone_outcome_with_clock(&fake, intent(), || time("2026-09-04T12:00:10Z"))
                .await;
        let late =
            observe_clone_outcome_with_clock(&fake, intent(), || time("2026-09-04T12:00:20Z"))
                .await;

        assert_eq!(first.task_complete, Some(false));
        assert_eq!(first.task_state, Some(super::TaskState::Running));
        assert_eq!(late.task_complete, Some(true));
        assert_eq!(late.task_state, Some(super::TaskState::CompleteSuccess));
        assert_eq!(late.vm_identity_satisfied, Some(true));
        assert_eq!(fake.recorded_requests().len(), 4);
    }

    #[tokio::test]
    async fn stopped_failure_is_distinct_from_a_running_task() {
        let observed_at = time("2026-09-04T12:00:00Z");
        let fake = FakePve::new();
        fake.enqueue_task_status(node(), upid(), Ok(TaskStatus::failed(upid(), observed_at)));
        fake.enqueue_vm_config(
            node(),
            Vmid::new(101).unwrap(),
            Ok(matching_vm(observed_at)),
        );

        let evidence =
            observe_clone_outcome_with_clock(&fake, intent(), || time("2026-09-04T12:00:10Z"))
                .await;

        assert_eq!(evidence.task_complete, Some(false));
        assert_eq!(evidence.task_state, Some(super::TaskState::CompleteFailure));
        assert_eq!(evidence.health, ObservationHealth::Fresh);
    }

    const MAX_TEST_HEADER_BYTES: usize = 16 * 1024;

    struct TestResponse {
        status: u16,
        body: String,
        delay: Duration,
        headers: Vec<(String, String)>,
    }

    impl TestResponse {
        fn json(status: u16, body: impl Into<String>) -> Self {
            Self {
                status,
                body: body.into(),
                delay: Duration::ZERO,
                headers: Vec::new(),
            }
        }

        const fn delayed(mut self, delay: Duration) -> Self {
            self.delay = delay;
            self
        }

        fn with_header(mut self, name: &str, value: impl Into<String>) -> Self {
            self.headers.push((name.to_owned(), value.into()));
            self
        }
    }

    struct TestServer {
        base_url: String,
        requests: Arc<Mutex<Vec<String>>>,
        handle: Option<JoinHandle<()>>,
    }

    impl TestServer {
        async fn start(response: TestResponse) -> Self {
            Self::start_with_accept_timeout(response, Duration::from_secs(1)).await
        }

        async fn start_with_accept_timeout(
            response: TestResponse,
            accept_timeout: Duration,
        ) -> Self {
            Self::start_with_limit(response, 1, accept_timeout).await
        }

        async fn start_with_limit(
            response: TestResponse,
            request_limit: usize,
            accept_timeout: Duration,
        ) -> Self {
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let address = listener.local_addr().unwrap();
            let requests = Arc::new(Mutex::new(Vec::new()));
            let recorded = Arc::clone(&requests);
            let handle = tokio::spawn(async move {
                let reason = match response.status {
                    200 => "OK",
                    302 => "Found",
                    401 => "Unauthorized",
                    403 => "Forbidden",
                    404 => "Not Found",
                    409 => "Conflict",
                    _ => "Test",
                };
                let extra_headers = response
                    .headers
                    .iter()
                    .map(|(name, value)| format!("{name}: {value}\r\n"))
                    .collect::<String>();
                for _ in 0..request_limit {
                    let Ok(Ok((mut stream, _))) =
                        tokio::time::timeout(accept_timeout, listener.accept()).await
                    else {
                        return;
                    };
                    let request = read_test_headers(&mut stream).await.unwrap();
                    recorded.lock().unwrap().push(request);
                    tokio::time::sleep(response.delay).await;
                    let wire = format!(
                        "HTTP/1.1 {} {reason}\r\nContent-Type: application/json\r\n{extra_headers}Content-Length: {}\r\nConnection: close\r\n\r\n{}",
                        response.status,
                        response.body.len(),
                        response.body
                    );
                    let _ = stream.write_all(wire.as_bytes()).await;
                }
            });
            Self {
                base_url: format!("http://{address}"),
                requests,
                handle: Some(handle),
            }
        }

        fn base_url(&self) -> &str {
            &self.base_url
        }

        async fn finish(mut self) -> Vec<String> {
            let mut handle = self.handle.take().unwrap();
            match tokio::time::timeout(Duration::from_secs(2), &mut handle).await {
                Ok(Ok(())) => {}
                Ok(Err(error)) => panic!("loopback test server failed: {error}"),
                Err(_) => {
                    handle.abort();
                    let _ = handle.await;
                    panic!("loopback test server did not finish");
                }
            }
            self.requests.lock().unwrap().clone()
        }
    }

    impl Drop for TestServer {
        fn drop(&mut self) {
            if let Some(handle) = &self.handle {
                handle.abort();
            }
        }
    }

    async fn read_test_headers(stream: &mut TcpStream) -> io::Result<String> {
        let mut request = Vec::new();
        let mut chunk = [0_u8; 1024];
        loop {
            let length = stream.read(&mut chunk).await?;
            if length == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "request ended before HTTP headers",
                ));
            }
            request.extend_from_slice(&chunk[..length]);
            if request.len() > MAX_TEST_HEADER_BYTES {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "test request headers exceeded bound",
                ));
            }
            if request.windows(4).any(|window| window == b"\r\n\r\n") {
                return String::from_utf8(request)
                    .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error));
            }
        }
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
    async fn combined_http_observation_evaluates_fresh_facts_after_collection() {
        let server = TestServer::start_with_limit(
            TestResponse::json(200, r#"{"data":{"status":"stopped","exitstatus":"OK","smbios1":"uuid=3f2504e0-4f89-41d3-9a0c-0305e82c3301","net0":"virtio=02:00:00:00:01:01"}}"#)
                .delayed(Duration::from_millis(10)),
            2, Duration::from_secs(1),
        ).await;
        let observer = observer_for(server.base_url(), Duration::from_secs(1));
        let evidence = observe_clone_outcome(&observer, intent()).await;
        assert_eq!(evidence.health, ObservationHealth::Fresh);
        assert_eq!(evidence.task_complete, Some(true));
        assert_eq!(evidence.vm_identity_satisfied, Some(true));
        assert_eq!(server.finish().await.len(), 2);
    }

    #[tokio::test]
    async fn injected_evaluation_clock_runs_after_http_collection_and_rejects_old_or_future_facts()
    {
        for (offset_seconds, expected) in [
            (0, ObservationHealth::Fresh),
            (60, ObservationHealth::Stale),
            (-60, ObservationHealth::Stale),
        ] {
            let server = TestServer::start_with_limit(
                TestResponse::json(200, r#"{"data":{"status":"stopped","exitstatus":"OK","smbios1":"uuid=3f2504e0-4f89-41d3-9a0c-0305e82c3301","net0":"virtio=02:00:00:00:01:01"}}"#),
                2, Duration::from_secs(1),
            ).await;
            let observer = observer_for(server.base_url(), Duration::from_secs(1));
            let mut evaluated_at = None;
            let evidence = observe_clone_outcome_with_clock(&observer, intent(), || {
                assert_eq!(
                    server.requests.lock().unwrap().len(),
                    2,
                    "clock sampled before collection"
                );
                let now = Utc::now() + chrono::Duration::seconds(offset_seconds);
                evaluated_at = Some(now);
                now
            })
            .await;
            assert_eq!(evidence.health, expected);
            assert_eq!(Some(evidence.observed_at), evaluated_at);
            assert_eq!(evidence.task_complete, Some(true));
            assert_eq!(evidence.vm_identity_satisfied, Some(true));
            assert_eq!(server.finish().await.len(), 2);
        }
    }

    #[tokio::test]
    async fn http_401_and_403_are_unauthorized_without_response_payloads() {
        for status in [401, 403] {
            let server = TestServer::start(TestResponse::json(status, "sensitive body")).await;
            let observer = observer_for(server.base_url(), Duration::from_secs(1));

            let error = observer.task_status(&node(), &upid()).await.unwrap_err();
            let requests = server.finish().await;

            assert_eq!(error, PveReadError::Unauthorized);
            assert!(!error.to_string().contains("sensitive body"));
            assert_eq!(requests.len(), 1);
        }
    }

    #[tokio::test]
    async fn http_404_and_409_remain_distinct_read_errors() {
        for (status, expected) in [(404, PveReadError::NotFound), (409, PveReadError::Conflict)] {
            let server = TestServer::start(TestResponse::json(status, "")).await;
            let observer = observer_for(server.base_url(), Duration::from_secs(1));

            let error = observer.task_status(&node(), &upid()).await.unwrap_err();
            let requests = server.finish().await;

            assert_eq!(error, expected);
            assert_eq!(requests.len(), 1);
        }
    }

    #[tokio::test]
    async fn delayed_loopback_response_maps_to_timeout() {
        let server = TestServer::start(
            TestResponse::json(200, r#"{"data":{"status":"running"}}"#)
                .delayed(Duration::from_millis(500)),
        )
        .await;
        // Allow real HTTP headers to arrive under AMD64 emulation while keeping
        // a fivefold gap between the client deadline and the delayed response.
        let observer = observer_for(server.base_url(), Duration::from_millis(100));

        let error = observer.task_status(&node(), &upid()).await.unwrap_err();
        let requests = server.finish().await;

        assert_eq!(error, PveReadError::TimedOut);
        assert_eq!(requests.len(), 1);
    }

    #[tokio::test]
    async fn observer_never_follows_a_redirect_hop() {
        let destination = TestServer::start_with_accept_timeout(
            TestResponse::json(200, r#"{"data":{"status":"running"}}"#),
            Duration::from_millis(250),
        )
        .await;
        let destination_url = format!(
            "{}/api2/json/nodes/pve-test/tasks/{}/status",
            destination.base_url(),
            upid()
        );
        let redirect =
            TestServer::start(TestResponse::json(302, "").with_header("Location", destination_url))
                .await;
        let observer = observer_for(redirect.base_url(), Duration::from_secs(1));

        let result = observer.task_status(&node(), &upid()).await;
        let redirect_requests = redirect.finish().await;
        let destination_requests = destination.finish().await;

        assert_eq!(result.unwrap_err(), PveReadError::TransportUnavailable);
        assert_eq!(redirect_requests.len(), 1);
        assert!(destination_requests.is_empty());
    }

    #[test]
    fn loopback_target_never_uses_proxy_environment() {
        if std::env::var("PVE_PROXY_ISOLATED_CHILD").as_deref() == Ok("1") {
            tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .unwrap()
                .block_on(proxy_environment_child());
            return;
        }

        let status = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "tests::loopback_target_never_uses_proxy_environment",
                "--nocapture",
            ])
            .env("PVE_PROXY_ISOLATED_CHILD", "1")
            .status()
            .unwrap();

        assert!(status.success(), "isolated proxy regression failed");
    }

    async fn proxy_environment_child() {
        let proxy = TestServer::start_with_accept_timeout(
            TestResponse::json(502, ""),
            Duration::from_millis(250),
        )
        .await;
        let target = TestServer::start_with_accept_timeout(
            TestResponse::json(200, r#"{"data":{"status":"running"}}"#),
            Duration::from_millis(250),
        )
        .await;
        let proxy_url = proxy.base_url().to_owned();
        let environment = ScopedEnvironment::set(&[
            ("HTTP_PROXY", Some(proxy_url.as_str())),
            ("http_proxy", Some(proxy_url.as_str())),
            ("HTTPS_PROXY", Some(proxy_url.as_str())),
            ("https_proxy", Some(proxy_url.as_str())),
            ("ALL_PROXY", Some(proxy_url.as_str())),
            ("all_proxy", Some(proxy_url.as_str())),
            ("NO_PROXY", None),
            ("no_proxy", None),
        ]);
        let observer = observer_for(target.base_url(), Duration::from_secs(1));

        let result = observer.task_status(&node(), &upid()).await;
        let proxy_requests = proxy.finish().await;
        let target_requests = target.finish().await;
        drop(environment);

        assert_eq!(result.unwrap().state(), super::TaskState::Running);
        assert!(proxy_requests.is_empty());
        assert_eq!(target_requests.len(), 1);
    }

    #[tokio::test]
    async fn redirect_to_production_is_rejected_without_a_production_attempt() {
        let server = TestServer::start(TestResponse::json(302, "").with_header(
            "Location",
            format!(
                "https://192.168.2.4:8006/api2/json/nodes/pve-test/tasks/{}/status",
                upid()
            ),
        ))
        .await;
        let audit = super::PveRequestAudit::new();
        let observer = ReqwestPveObserver::new(
            PveObserverConfig::new(
                PveBaseUrl::parse(server.base_url()).unwrap(),
                PveAccessMode::Observe,
                false,
            )
            .with_timeout(Duration::from_secs(1))
            .with_request_audit(audit.clone()),
        )
        .unwrap();

        let result = observer.task_status(&node(), &upid()).await;
        let requests = server.finish().await;

        assert_eq!(result.unwrap_err(), PveReadError::TransportUnavailable);
        assert_eq!(
            audit.request_count(),
            1,
            "only the loopback request is built"
        );
        assert_eq!(requests.len(), 1);
        assert!(
            requests[0]
                .to_ascii_lowercase()
                .contains("host: 127.0.0.1:")
        );
        assert!(!requests[0].contains("192.168.2.4"));
    }

    #[tokio::test]
    async fn retryable_protocol_response_has_at_most_one_wire_attempt() {
        let server = TestServer::start_with_limit(
            TestResponse::json(503, ""),
            3,
            Duration::from_millis(100),
        )
        .await;
        let retrying_builder = reqwest::Client::builder().retry(
            reqwest::retry::for_host("127.0.0.1")
                .classify_fn(|request_response| {
                    if request_response.status() == Some(reqwest::StatusCode::SERVICE_UNAVAILABLE) {
                        request_response.retryable()
                    } else {
                        request_response.success()
                    }
                })
                .no_budget()
                .max_retries_per_request(2),
        );
        let client = crate::observer::build_verified_test_client_from(
            retrying_builder,
            Duration::from_secs(1),
        )
        .unwrap();

        let response = client.get(server.base_url()).send().await.unwrap();
        let requests = server.finish().await;

        assert_eq!(response.status(), reqwest::StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(requests.len(), 1);
    }

    #[tokio::test]
    async fn sanitized_upid_fixture_decodes_as_completed_task() {
        let server = TestServer::start(TestResponse::json(
            200,
            include_str!("../../../fixtures/pve/upid-complete.json"),
        ))
        .await;
        let observer = observer_for(server.base_url(), Duration::from_secs(1));

        let status = observer.task_status(&node(), &upid()).await.unwrap();
        let requests = server.finish().await;

        assert!(status.succeeded());
        assert_eq!(status.upid(), &upid());
        assert_eq!(requests.len(), 1);
    }

    #[tokio::test]
    async fn sanitized_upid_fixture_decodes_as_failed_task() {
        let server = TestServer::start(TestResponse::json(
            200,
            include_str!("../../../fixtures/pve/upid-failed.json"),
        ))
        .await;
        let observer = observer_for(server.base_url(), Duration::from_secs(1));

        let status = observer.task_status(&node(), &upid()).await.unwrap();
        let requests = server.finish().await;

        assert_eq!(status.state(), super::TaskState::CompleteFailure);
        assert_eq!(requests.len(), 1);
    }

    #[tokio::test]
    async fn qga_ping_uses_the_typed_post_observation_endpoint() {
        let server = TestServer::start(TestResponse::json(200, r#"{"data":{}}"#)).await;
        let observer = observer_for(server.base_url(), Duration::from_secs(1));

        let status = observer
            .qga_ping(&node(), Vmid::new(101).unwrap())
            .await
            .unwrap();
        let requests = server.finish().await;

        assert!(status.reachable());
        assert!(
            requests[0].starts_with("POST /api2/json/nodes/pve-test/qemu/101/agent/ping HTTP/1.1")
        );
    }

    struct ScopedEnvironment(Vec<(String, Option<OsString>)>);

    impl ScopedEnvironment {
        fn set(values: &[(&str, Option<&str>)]) -> Self {
            let previous = values
                .iter()
                .map(|(name, _)| ((*name).to_owned(), std::env::var_os(name)))
                .collect();
            for (name, value) in values {
                match value {
                    Some(value) => {
                        // SAFETY: This helper is used only by an exact, current-thread test in a
                        // dedicated child process, so no other thread can read this environment.
                        unsafe { std::env::set_var(name, value) };
                    }
                    None => {
                        // SAFETY: See the set_var safety argument above.
                        unsafe { std::env::remove_var(name) };
                    }
                }
            }
            Self(previous)
        }
    }

    impl Drop for ScopedEnvironment {
        fn drop(&mut self) {
            for (name, value) in &self.0 {
                match value {
                    Some(value) => {
                        // SAFETY: Drop runs in the same isolated current-thread test process.
                        unsafe { std::env::set_var(name, value) };
                    }
                    None => {
                        // SAFETY: Drop runs in the same isolated current-thread test process.
                        unsafe { std::env::remove_var(name) };
                    }
                }
            }
        }
    }
}

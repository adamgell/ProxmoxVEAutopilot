mod provisioning_support;
use controller_domain::{AttemptId, OperationId, RunId};
use provisioning_support::*;
use pve_port::*;
use serde_json::json;

#[test]
fn binding_rejects_invalid_full_workflow_hash() {
    for bad in ["", "abc", &"g".repeat(64), &"é".repeat(32)] {
        assert!(
            ProvisioningBindingV1::new(
                RunId::new(),
                OperationId::new(),
                AttemptId::new(),
                bad,
                &plan(ProvisioningActionV1::Clone),
                0
            )
            .is_err()
        );
    }
}

#[test]
fn requests_pin_exact_forms_routes_and_distinct_start_digests() {
    let episodes = chain();
    let forms: Vec<Vec<(&str, String)>> = vec![
        vec![
            ("newid", "101".into()),
            ("name", "deploy-101".into()),
            ("full", "1".into()),
            ("storage", "local-lvm".into()),
        ],
        vec![
            ("disk", "scsi0".into()),
            ("size", "120G".into()),
            ("digest", "clone-digest".into()),
        ],
        vec![
            ("digest", "after-EnsureCapacity".into()),
            ("cores", "4".into()),
            ("memory", "4096".into()),
            ("cpu", "host".into()),
            ("balloon", "0".into()),
            ("bios", "seabios".into()),
            ("agent", "enabled=1,type=virtio".into()),
            (
                "smbios1",
                "uuid=3f2504e0-4f89-41d3-9a0c-0305e82c3301,serial=SYS-101".into(),
            ),
            (
                "net0",
                "virtio=02:00:00:00:01:01,bridge=vmbr0,firewall=0".into(),
            ),
            ("scsi0", "local-lvm:vm-101-disk-0,serial=DISK-101".into()),
            ("ide2", "local:iso/deployment.iso,media=cdrom".into()),
            ("ide3", "drivers:iso/virtio.iso,media=cdrom".into()),
            ("boot", "order=ide2;scsi0".into()),
        ],
        vec![],
        vec![],
        vec![
            ("digest", "after-EnsureStopped".into()),
            ("delete", "ide2,ide3".into()),
            ("boot", "order=scsi0".into()),
        ],
        vec![],
    ];
    let suffixes = [
        vec!["900", "clone"],
        vec!["101", "resize"],
        vec!["101", "config"],
        vec!["101", "status", "start"],
        vec!["101", "status", "stop"],
        vec!["101", "config"],
        vec!["101", "status", "start"],
    ];
    for (n, e) in episodes.iter().enumerate() {
        assert_eq!(e.request.form(), forms[n]);
        let mut route = vec!["nodes", "pve-test", "qemu"];
        route.extend(&suffixes[n]);
        assert_eq!(e.request.path_segments(), route);
        assert_eq!(
            e.request.method(),
            if [1, 2, 5].contains(&n) {
                "PUT"
            } else {
                "POST"
            }
        );
        assert_eq!(
            serde_json::from_value::<ProvisioningMutationRequestV1>(
                serde_json::to_value(&e.request).unwrap()
            )
            .unwrap(),
            e.request
        );
        assert_ne!(
            e.request.request_digest().unwrap(),
            clone_request().request_digest()
        );
    }
    assert_eq!(episodes[3].request.form(), episodes[6].request.form());
    assert_ne!(
        episodes[3].request.request_digest().unwrap(),
        episodes[6].request.request_digest().unwrap()
    );
}

#[test]
fn complete_request_json_and_digest_golden() {
    let r = &chain()[0].request;
    let v = serde_json::to_value(r).unwrap();
    assert_eq!(
        v,
        json!({"kind":"clone","request":{"contract_version":1,"binding":binding(ProvisioningActionV1::Clone,10,0),"plan":plan(ProvisioningActionV1::Clone),"clone":clone_request(),"expected_before":{"config":source(),"power":power(900,false)}}})
    );
    assert_eq!(
        r.request_digest().unwrap(),
        "168b99497d1525af7198cfff73cfe6f6060c0caf0ae5bf366111d44aa947329d"
    );
}

#[test]
fn every_closed_variant_has_a_pinned_complete_digest() {
    let hashes: Vec<_> = chain()
        .iter()
        .map(|e| e.request.request_digest().unwrap())
        .collect();
    assert_eq!(
        hashes,
        vec![
            "168b99497d1525af7198cfff73cfe6f6060c0caf0ae5bf366111d44aa947329d",
            "e581cbd9386ab4fdd45c52c96cd4d210011533bb1a73af369d04b056e3170e30",
            "08d7c733e120027b4075b310ff024427e5edebbdf7b7ae881ce32563a3a0e6b3",
            "93a30d6718ae84d1a9cbc55e0e25aa3b371a522ad26ad068d22e77f03921dad6",
            "c3ff7858c875923f4a46742839d81751355ef9671a83e20148eb442600765361",
            "9590dec25cfbf2ec0b1727bb7eca7d5f90b7f9b128574bf78ac6217d87daa297",
            "47a21cc43bde7949ed4c4c2336df1bcce0bb9f9a58755314d42396d03ba6878b",
        ]
    );
    let episodes = chain();
    for (n, e) in episodes.iter().enumerate().skip(1) {
        let c = e.pre.facts();
        let owner = c.clone_ownership.as_ref().unwrap();
        let prior = c.predecessor.as_ref().unwrap();
        let value = serde_json::to_value(&e.request).unwrap();
        assert_eq!(
            value,
            json!({"kind":(["clone","grow_disk","configure","start","stop","configure","start"][n]),"request":{
                "contract_version":1,"binding":c.binding,"plan":c.plan,"clone_request":clone_request(),"clone_binding":owner.request().binding(),"clone_evidence_sha256":event_journal::payload_digest(&serde_json::to_value(owner.proof()).unwrap()).unwrap(),"clone_config":intermediate(),"predecessor_binding":prior.binding(),"predecessor_plan":prior.plan(),"predecessor_evidence_sha256":prior.evidence_sha256(),"predecessor_config":prior.config(),"predecessor_power":prior.power(),"expected_before":e.request.expected_before()
            }})
        );
    }
}

#[test]
fn every_request_hash_binds_clock_digest_operation_attempt_workflow_and_fence() {
    for episode in chain() {
        let request = episode.request;
        let original = request.request_digest().unwrap();
        for pointer in [
            "/request/expected_before/config/digest",
            "/request/expected_before/config/observed_at",
            "/request/expected_before/power/observed_at",
            "/request/binding/attempt_id",
            "/request/binding/evidence_fence",
        ] {
            let mut value = serde_json::to_value(&request).unwrap();
            let change = match pointer {
                p if p.ends_with("digest") => json!("changed-digest"),
                p if p.ends_with("observed_at") => json!("2026-09-05T11:59:59Z"),
                p if p.ends_with("attempt_id") => json!(id::<AttemptId>(777)),
                _ => json!(123),
            };
            *value.pointer_mut(pointer).unwrap() = change;
            let changed: ProvisioningMutationRequestV1 = serde_json::from_value(value).unwrap();
            assert_ne!(changed.request_digest().unwrap(), original, "{pointer}");
        }
    }
}

#[test]
fn bindings_compare_attempt_identity_but_not_collection_fence() {
    let p = plan(ProvisioningActionV1::Clone);
    let a = binding(ProvisioningActionV1::Clone, 10, 0);
    let b = binding(ProvisioningActionV1::Clone, 10, 99);
    assert!(a.same_operation_attempt(&b));
    assert_ne!(a, b);
    let upper = ProvisioningBindingV1::new(
        a.run_id(),
        a.operation_id(),
        a.attempt_id(),
        &SHA.to_uppercase(),
        &p,
        0,
    )
    .unwrap();
    assert_eq!(a, upper);
    for field in [
        "run_id",
        "operation_id",
        "attempt_id",
        "workflow_sha256",
        "operation_plan_sha256",
    ] {
        let mut v = serde_json::to_value(&a).unwrap();
        v[field] = if field.ends_with("sha256") {
            json!("a".repeat(64))
        } else {
            json!(id::<RunId>(777))
        };
        let b: ProvisioningBindingV1 = serde_json::from_value(v).unwrap();
        assert!(!a.same_operation_attempt(&b), "{field}");
    }
}

#[test]
fn persisted_requests_reject_missing_unknown_duplicate_and_object_discriminators() {
    for e in chain() {
        let v = serde_json::to_value(&e.request).unwrap();
        for field in v["request"].as_object().unwrap().keys() {
            let mut bad = v.clone();
            bad["request"].as_object_mut().unwrap().remove(field);
            assert!(
                serde_json::from_value::<ProvisioningMutationRequestV1>(bad).is_err(),
                "missing {field}"
            );
        }
        for pointer in [
            "",
            "/request",
            "/request/expected_before",
            "/request/expected_before/config",
            "/request/expected_before/power",
            "/request/binding",
            "/request/plan",
        ] {
            let mut bad = v.clone();
            bad.pointer_mut(pointer)
                .unwrap()
                .as_object_mut()
                .unwrap()
                .insert("unknown".into(), json!(true));
            assert!(
                serde_json::from_value::<ProvisioningMutationRequestV1>(bad).is_err(),
                "unknown {pointer}"
            );
        }
        for pointer in [
            "/kind",
            "/request/plan/action",
            "/request/expected_before/config/source",
            "/request/expected_before/config/firmware",
            "/request/expected_before/power/power",
        ] {
            let mut bad = v.clone();
            let s = bad.pointer(pointer).unwrap().as_str().unwrap().to_owned();
            *bad.pointer_mut(pointer).unwrap() = json!({s:null});
            assert!(
                serde_json::from_value::<ProvisioningMutationRequestV1>(bad).is_err(),
                "object {pointer}"
            );
        }
        let text = serde_json::to_string(&v).unwrap();
        let duplicate = text.replacen(
            "\"contract_version\":1",
            "\"contract_version\":1,\"contract_version\":1",
            1,
        );
        assert!(serde_json::from_str::<ProvisioningMutationRequestV1>(&duplicate).is_err());
        let text = text.replacen("\"locked\":false", "\"locked\":false,\"locked\":false", 1);
        assert!(serde_json::from_str::<ProvisioningMutationRequestV1>(&text).is_err());
    }
}

#[test]
fn dispatch_and_receipt_reconstruction_enforce_full_request_and_provenance() {
    for e in chain() {
        let ProvisioningDispatchStateV1::Recorded {
            dispatch,
            receipt: Some(receipt),
        } = &e.outcome.facts().dispatch
        else {
            unreachable!()
        };
        let v = serde_json::to_value(dispatch).unwrap();
        assert_eq!(
            serde_json::from_value::<ProvisioningDispatchV1>(v.clone()).unwrap(),
            *dispatch
        );
        for (field, values) in [
            ("original_generation", vec![json!(0), json!(-1)]),
            ("dispatch_revision", vec![json!(0), json!(1)]),
            ("request_sha256", vec![json!("a".repeat(64))]),
            ("dispatched_at", vec![json!("2026-09-05T11:59:59Z")]),
            ("source", vec![json!("pve_api"), json!({"fake_pve":null})]),
        ] {
            for value in values {
                let mut bad = v.clone();
                bad[field] = value;
                assert!(
                    serde_json::from_value::<ProvisioningDispatchV1>(bad).is_err(),
                    "{field}"
                );
            }
        }
        let mut bad = v.clone();
        bad["request"]["request"]["binding"]["evidence_fence"] = json!(u64::MAX);
        assert!(serde_json::from_value::<ProvisioningDispatchV1>(bad).is_err());
        let r = serde_json::to_value(receipt).unwrap();
        assert_eq!(
            serde_json::from_value::<ProvisioningReceiptV1>(r.clone()).unwrap(),
            *receipt
        );
        let mut bad = r.clone();
        bad["accepted_at"] = json!("2026-09-05T11:59:59Z");
        assert!(serde_json::from_value::<ProvisioningReceiptV1>(bad).is_err());
        let mut bad = r;
        bad["receipt"] = if matches!(receipt.receipt(), MutationReceipt::Task(_)) {
            json!("synchronous_accepted")
        } else {
            json!({"task":"UPID:pve-test:00000001:00000002:00000003:qmstart:101:root@pam:"})
        };
        assert!(serde_json::from_value::<ProvisioningReceiptV1>(bad).is_err());
    }
}

#[test]
fn receipt_workers_nodes_and_vmids_are_closed() {
    for e in chain() {
        let d = dispatch(e.request);
        for worker in [
            "qmclone", "resize", "qmresize", "qmstart", "qmstop", "other",
        ] {
            for node in ["pve-test", "other"] {
                for vmid in [900, 101, 102] {
                    let action = d.request().plan().action();
                    let expected_worker = match action {
                        ProvisioningActionV1::Clone => "qmclone",
                        ProvisioningActionV1::EnsureCapacity => "resize",
                        ProvisioningActionV1::StartPe | ProvisioningActionV1::StartDisk => {
                            "qmstart"
                        }
                        ProvisioningActionV1::EnsureStopped => "qmstop",
                        _ => "none",
                    };
                    let valid = worker == expected_worker
                        && node == "pve-test"
                        && vmid
                            == if action == ProvisioningActionV1::Clone {
                                900
                            } else {
                                101
                            };
                    let receipt = MutationReceipt::Task(
                        Upid::parse(format!(
                            "UPID:{node}:00000001:00000002:00000003:{worker}:{vmid}:root@pam:"
                        ))
                        .unwrap(),
                    );
                    assert_eq!(
                        ProvisioningReceiptV1::new(d.clone(), time(), receipt).is_ok(),
                        valid,
                        "{action:?}/{worker}/{node}/{vmid}"
                    );
                }
            }
        }
    }
}

#[test]
fn every_action_admits_twenty_and_rejects_twenty_one_byte_desired_serials() {
    for episode in chain() {
        for length in [20, 21] {
            let mut v = serde_json::to_value(&episode.request).unwrap();
            let serial = "D".repeat(length);
            v["request"]["plan"]["expected"]["disk_serial"] = json!(serial);
            let p: ProvisioningOperationPlanV1 =
                serde_json::from_value(v["request"]["plan"].clone()).unwrap();
            v["request"]["binding"]["operation_plan_sha256"] = json!(p.fingerprint().unwrap());
            if v["kind"] != "clone" {
                v["request"]["clone_binding"]["operation_plan_sha256"] = json!(
                    ProvisioningOperationPlanV1::new(
                        ProvisioningActionV1::Clone,
                        p.expected().clone()
                    )
                    .fingerprint()
                    .unwrap()
                );
                v["request"]["predecessor_plan"]["expected"]["disk_serial"] = json!(serial);
                let prior: ProvisioningOperationPlanV1 =
                    serde_json::from_value(v["request"]["predecessor_plan"].clone()).unwrap();
                v["request"]["predecessor_binding"]["operation_plan_sha256"] =
                    json!(prior.fingerprint().unwrap());
                if matches!(
                    p.action(),
                    ProvisioningActionV1::StartPe
                        | ProvisioningActionV1::EnsureStopped
                        | ProvisioningActionV1::ConfigureDisk
                        | ProvisioningActionV1::StartDisk
                ) {
                    v["request"]["predecessor_config"]["primary_disk"]["serial"] = json!(serial);
                    v["request"]["expected_before"]["config"]["primary_disk"]["serial"] =
                        json!(serial);
                }
            }
            assert_eq!(
                serde_json::from_value::<ProvisioningMutationRequestV1>(v).is_ok(),
                length == 20,
                "{:?}/{length}",
                p.action()
            );
            if p.action() == ProvisioningActionV1::Clone {
                let b = ProvisioningBindingV1::new(id(1), id(10), id(1010), SHA, &p, 0).unwrap();
                assert_eq!(
                    CloneProvisioningRequestV1::new(
                        b,
                        p,
                        clone_request(),
                        before(source(), false),
                        time(),
                        30
                    )
                    .is_ok(),
                    length == 20
                );
            }
        }
    }
}

#[test]
fn persisted_owned_reference_tampering_never_recreates_historical_authority() {
    for episode in chain().into_iter().skip(1) {
        for field in ["clone_evidence_sha256", "predecessor_evidence_sha256"] {
            let mut v = serde_json::to_value(&episode.request).unwrap();
            v["request"][field] = json!("b".repeat(64));
            let request: ProvisioningMutationRequestV1 = serde_json::from_value(v).unwrap();
            let mut c = episode.outcome.facts().clone();
            c.dispatch = ProvisioningDispatchStateV1::Recorded {
                dispatch: dispatch(request),
                receipt: None,
            };
            assert!(ProvisioningEvaluationContextV1::new(c).is_err(), "{field}");
        }
        for pointer in [
            "/request/clone_binding/run_id",
            "/request/predecessor_binding/run_id",
            "/request/clone_binding/operation_id",
            "/request/clone_config/fake_clone_provenance/request_marker",
            "/request/expected_before/config/fake_clone_provenance/request_marker",
        ] {
            let mut v = serde_json::to_value(&episode.request).unwrap();
            *v.pointer_mut(pointer).unwrap() = json!(id::<RunId>(777));
            assert!(
                serde_json::from_value::<ProvisioningMutationRequestV1>(v).is_err(),
                "{pointer}"
            );
        }
    }
}

#[test]
fn constructor_serial_compatibility_is_checked_for_every_family() {
    let episodes = chain_variant(80 * GIB, 120 * GIB, "DDDDDDDDDDDDDDDDDDDD");
    assert_eq!(episodes.len(), 7);
    for episode in episodes.into_iter().skip(1) {
        let c = episode.pre.facts();
        let mut v = serde_json::to_value(&c.plan).unwrap();
        v["expected"]["disk_serial"] = json!("DDDDDDDDDDDDDDDDDDDDD");
        let plan: ProvisioningOperationPlanV1 = serde_json::from_value(v).unwrap();
        let binding = ProvisioningBindingV1::new(
            c.binding.run_id(),
            c.binding.operation_id(),
            c.binding.attempt_id(),
            SHA,
            &plan,
            0,
        )
        .unwrap();
        let i = ProvisioningOwnedRequestInputV1 {
            binding,
            plan,
            ownership: c.clone_ownership.as_ref().unwrap(),
            predecessor: c.predecessor.as_ref().unwrap(),
            expected_before: episode.request.expected_before().clone(),
            as_of: time(),
            freshness_seconds: 30,
        };
        let rejected = match episode.request {
            ProvisioningMutationRequestV1::GrowDisk(_) => GrowDiskRequestV1::new(i).is_err(),
            ProvisioningMutationRequestV1::Configure(_) => {
                ConfigureProvisioningRequestV1::new(i).is_err()
            }
            ProvisioningMutationRequestV1::Start(_) => StartProvisioningRequestV1::new(i).is_err(),
            ProvisioningMutationRequestV1::Stop(_) => StopProvisioningRequestV1::new(i).is_err(),
            _ => unreachable!(),
        };
        assert!(rejected);
    }
}

# Rust Selected-Node Observation Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Observe one explicitly selected node and its network through the local service, without changing cluster observation or execution authority.

**Architecture:** A validated optional node target creates a separate narrow infrastructure collector. Its independent10second loop reports per-component monotonic freshness and sanitized summaries. Existing cluster cadence/readiness, protected single credential load, database isolation and all mutation denials remain intact.

**Tech Stack:** Existing Rust1.92/edition2024, Tokio, Reqwest/rustls, Axum, Serde, owned loopback/PostgreSQL fixtures. No new dependency or version change.

**Spec:** docs/superpowers/specs/2026-09-04-rust-controller-replacement-design.md sections8,11,16–21; docs/superpowers/plans/2026-09-05-rust-node-network-visibility.md explicit remaining service integration.

## Global Constraints

- "The current production controller at `192.168.2.4` remains unchanged until a separately approved cutover."
- "During local development the Rust controller uses a separate database and namespace. It must never claim production `jobs` rows."
- "Secrets never appear in argv, logs, journal payloads, health details, or fixtures."
- "RustedOutClient is out of scope. It is neither a dependency nor a delivery target for this program."
- All tests use owned local loopback and owned local PostgreSQL only. No real PVE/controller/tenant calls, production credentials, SSH, deployment, publication, schema migration outside owned fixtures, or mutation activation.
- Storage content/status GETs excluded because pinned official sources activate storage, including potential additional enabled storages. No storage calls, QGA, POST, discovery or inventory-derived fan-out.
- No strict/native parser, PVE transport, scheduler, store, evidence or existing cluster semantics changes. Existing cluster collection3seconds/interval5seconds/freshness15seconds remains unchanged. HTTP execution ready staysfalse/HTTP503.
- One Astra implementer per task, independent task review, one consolidated phase review, coordinated repairs if needed, scoped re-review, exact-source artifact gate. Start only after predecessor acceptance. User already selected this execution model.

## Scope and file map

One explicit node is the v1 bound. No multi-node list, per-VM fan-out, automatic selection, runtime reload or credential reload. Proposed exact optional setting `RUST_CONTROLLER_PVE_OBSERVE_NODE`. Absence retains cluster-only behavior. Presence is accepted only for observe/http-observe with all existing target/HTTPS/read-opt-in gates. Invalid, empty and non-Unicode node values fail before file/client/network I/O. Presence under fake is a config error rather than ignored input.

Create service `src/infrastructure_observation.rs` for collector/sanitized DTOs and `src/infrastructure_health.rs` for per-component freshness. Modify config/main module declarations, runtime/health wiring, runtime tests and service startup tests. Add `tests/support/infrastructure_service.rs` as a child module of existing observation_service fixture so it can reuse private Database, SQL audit and sampled-child helpers without widening production or fixture APIs. Existing shared logs cap256KiB/3seconds and ownership policy remain unchanged.

## Task 1: Validated singleton target and bounded collector

**Files:** Modify service src/config.rs and module declarations in main.rs; create src/infrastructure_observation.rs. No runtime activation yet.

**Interfaces:** Add private `node_target: Option<NodeName>` to ValidatedObservationConfig and pub(crate) getter `node_target()->Option<&NodeName>`. Existing observation_config remains sole factory. Add cfg(test) `observation_test_config_with_node(path:&Path,base_url:&str,node:&str)->ValidatedObservationConfig` through the same validated factory, not direct field fabrication.

New `InfrastructureObservation::new(config:&ValidatedObservationConfig, token:PveApiToken)->Result<Self,ObservationSetupFailure>` requires Some(node_target), owns only Box<dyn PveInfrastructureVisibilityReadPort> and NodeName, uses existing validated Observe client/auth policy/request2seconds. Reuse existing fieldless ObservationSetupFailure. A cfg(test) `from_port(node:NodeName,port:Box<dyn PveInfrastructureVisibilityReadPort>)->Self` permits deterministic inert mocks only. No broad port/store/runner field.

`async fn collect_once(&self)->InfrastructureResult` calls node then network at most once each. Wrap each port call in2seconds and whole pair in5seconds; normal first-call failure still permits second call. Whole deadline exhaustion conservatively returns both components TimedOut with no current counts/timestamps; cancellation drops active read and prevents an unstarted second read. No spawned collection tasks.

DTO contract (all fields private, Clone+Serialize only, no Debug/Deserialize):

- `NodeObservation`: status:ObservationStatus, observed_at:Option<DateTime<Utc>>, uptime_known:Option<bool>, completed_at:Option<std::time::Instant> with serde(skip). Success sets Fresh and Some(uptime.is_some()), including Some(false) for missing uptime; failure sets no timestamps/knowledge. No raw uptime or node identifier in health.
- `NetworkObservation`: status, observed_at, counts:Option<NetworkObservationCounts>, completed_at skipped. Counts fields: linux_bridges, ovs_bridges, other_interfaces, active, inactive, activity_unknown, rejected_rows (usize). Rejected_rows>0=>Degraded; unknown metadata alone is not malformed. Do not retain interface names/rawtypes/addresses.
- `InfrastructureResult`: node:NodeObservation, network:NetworkObservation; manual Serialize adds coverage fixedunverified. Getters node(),network(),coverage(). Component getters status(),observed_at(),completed_at(), node uptime_known(), network counts(). Counts need no public getters beyond sanitized serialization unless tests require them.

Capture each component completion Instant immediately after its own response, not after the whole pair. Map existing PveReadError variants to the same fixed ObservationStatus categories as cluster collection; no raw errors.

- [ ] **Step1: Config RED with exact factory inputs.** Extend config tests using pure lookup maps. Pin absence unchanged; valid node accepted only observe/http-observe; invalid/empty/nonUnicode rejected; any presence with fake denied. Existing real/native denials and remoteHTTPS/opt-in matrix retained. Use nonexistent synthetic credential paths and no client/loader invocation in config tests.

```rust
let selected = observation_test_config_with_node(
    std::path::Path::new("synthetic-unopened-token"),
    "http://127.0.0.1:5000", "pve-test",
);
assert_eq!(selected.node_target().unwrap().as_str(), "pve-test");
```

- [ ] **Step2: Collector behavioral RED.** Tests implement only the two-method narrow trait, record exactnode/order/callcount and return NodeVisibility/NetworkVisibility fixtures. Pin nodeuptime0/missing, Linux/OVS/other and allactivitystates, rejectedrows, each errorcategory, normal nodefailure/networksuccess and inverse, noidentifier/canaryserialization, per-component completion times,2second timeout despite inertportstall, whole cancellation and no thirdcall. Use pausedTokio tests for clock bounds and cancellation; stdInstant completion ordering can use injected fixture timestamps via cfg(test) helpers with no production setters.

```rust
let value = serde_json::to_value(result).unwrap();
assert_eq!(value["coverage"], "unverified");
assert_eq!(value["node"]["uptime_known"], true);
assert_eq!(value["network"]["counts"]["ovs_bridges"], 1);
assert!(value["node"].get("completed_at").is_none());
```

- [ ] **Step3: Implement validated target and collector.** Parse present target through NodeName::parse before creating ValidatedObservationConfig. Before fake earlyreturn reject present setting. Preserve actual allow_production_reads. Constructor rejects missing selection, builds only validated HTTP observer. Sequence collection without background spawn:

```rust
let node = tokio::time::timeout(Duration::from_secs(2), self.port.node_visibility(&self.node))
    .await.unwrap_or(Err(PveReadError::TimedOut));
let node = NodeObservation::from_read(node);
let network = tokio::time::timeout(Duration::from_secs(2), self.port.network_visibility(&self.node))
    .await.unwrap_or(Err(PveReadError::TimedOut));
let network = NetworkObservation::from_read(network);
InfrastructureResult { node, network }
```

Define private `from_read(Result<NodeVisibility,PveReadError>)->Self` and network equivalent to classify/droprawdata and recordmonotonic time. Wrap the quoted sequence in timeout5seconds; private `InfrastructureResult::timed_out()->Self` creates fixed empty failure components. Add targeted staged deadcode allowances only if needed untilTask2 consumes the new private types; remove them inTask2.
- [ ] **Step4: Verify.** Focused config/infrastructure collector tests, allPVEtests/doctests, service startup tests, fmt and strictworkspaceClippy. Avoid unfiltered service unit target until ownedPG destinations inspected and overrides cleared; if used, verifylocalUnixDocker and cleanup. No Linuxbuildyet.
- [ ] **Step5: Commit scoped files** as `feat(rust): add selected-node observation collector`; report runtime stillcluster-only and exact newguard/timeout evidence. Independent review precedesTask2.

## Task 2: Independent service loop, health and no-write proof

**Files:** Create src/infrastructure_health.rs and tests/support/infrastructure_service.rs. Modify main.rs module declaration, runtime.rs/runtime_tests.rs, health.rs, service tests and one child module declaration in tests/support/observation_service.rs; README and Dockerfile.test selectedtestfilters only. Minor cfg(test) DTO fabrication helpers in collector permitted for deterministic freshness tests, with no production construction bypass. No new sharedfixture API/destination/logcap change.

**Interfaces:** `InfrastructureProgress::record(&mut self,result:InfrastructureResult)` stores currentresult and last successful UTC separately per component; private lastsuccess updates only on Fresh with complete requiredfields and completionInstant. `snapshot(&self,now:Instant)->Option<InfrastructureHealth>` returns sanitized nested node/network health with fresh and last_success fields plus fixedcoverage. Nodefresh requires Fresh/observedAt/uptimeKnown/completion and age<=30seconds; networkfresh requires Fresh/observedAt/counts/completion and age<=30seconds. Use saturating_duration_since as existinghealthpolicy. Error/degraded currentresult cannot reuseoldsuccess tobecomefresh. Snapshotmustnotrefreshstoredtime.

HealthState gains `infrastructure:Arc<RwLock<InfrastructureProgress>>`. ReadyResponse adds `infrastructure_observation:Option<InfrastructureHealth>` and `infrastructure_observation_ready:bool` (bothcomponentsfresh). These are additive; existing observation_ready meansclusteronly, existing ready remains unchanged. Fake/unselected showsnull/false. HTTP readiness response remains503.

Runtime loads token once inside existing validated observation branch, clones the already-Clone PveApiToken only when targetSome, constructs InfrastructureObservation and PveObservation before anyruntimeworker; no tokenfilereopen/environmentreparse. Add one optional joinedshutdown-aware task `infrastructure_sweeps(observation:InfrastructureObservation, progress:Arc<RwLock<InfrastructureProgress>>, stop:watch::Receiver<bool>)`. Immediatefirsttick,10secondinterval Skip, nooverlap,biasedshutdown, dropinflightcollection, joinonserviceexit. Existing cluster/DB loops unchanged.

- [ ] **Step1: Health and runtime RED.** Unit tests direct freshness at30seconds vs30001ms, nodeolderthannetwork, nodefailure/networkfresh andinverse, retainedlastsuccesswithoutready, degradedrows, repeatedsnapshotnorefresh, noidentifier/secret/Instantserialization, DB/executionfault notcleared. Pausedclock loop verifiesimmediatecall/noearlysecond/SkipnotBurst/singleactivepair/cancelnoadditionalrequest/shutdownjoin. Preserveexisting cluster cadence tests. Example boundary:

```rust
let fresh = progress.snapshot(completed + Duration::from_secs(30)).unwrap();
assert!(fresh.ready());
let stale = progress.snapshot(completed + Duration::from_millis(30_001)).unwrap();
assert!(!stale.ready());
```

Define `InfrastructureHealth::ready()->bool` as bothcomponentfresh; health.rsusesit. cfg(test) fixedresulthelper supplies explicit completionInstantandUTC separately, withsanitized fields only.
- [ ] **Step2: Actual service RED and isolation proof.** New childtestmodule reusesparent Database::new/snapshot/cleanup, audit_read_only_trace and assert_no_service_children; Rustchildprivacy allowsreusewithoutpublicAPI. Its own bounded HTTPfixture admits only clusterGET and selectedpve-test status/networkGETs. Use env-cleared actualservice, synthetic0600credential, optionalnodeconfig. Routesauthenticateandrejectunexpectedmethod/query/node/path; no arbitrary fixture destination. Runtimeprovebeforewiringfails toproduceinfrastructurehealth.

OwnedPGproof total85seconds includingexisting30secondsetup; node/network each3collections fresh->network401->fresh, withnodefreshthroughout. Shutdownafterthirdnetworkresult. Eachselectedroute exactly3requests; clustercounts bounded4..6 dueindependent5secondcadence/scheduling, not assumedcross-looporder. Waithealthpoll at mostonce/second tokeepcompleteSQLtracebelowexisting256KiBcap; pertransition12seconddeadline. Require observedmixedfailurewhilecluster observation_ready remains true, infrastructure_readyfalse andretainednetworklastsuccess, thenrecovery; executionreadyfalse/503always. Assertnode/networkids/rawtype/address/token/pathabsentfromhealth. Preservefull15tables populatedsnapshotbefore/after3cycles/exit, completeallowlistedSQLtrace, sampledchildlessprocesses andexactcleanup. Logoverflowfails ratherthanraisingcap/tailing.

Second actualservice testwithunavailableloopbackDB (noDocker) provesbothclusterandselectedinfrastructure fresh whileDB/outbox/readyfalse andzerosuccessfulDBsweeps. Bounded12secondtest/ownedchildcleanup; executeonLinuxafterinspectingnoDatabase::new path. Startupnegative selectednode+fake/invalidnode tests provezeroownedPVE/DBcanaryrequests andnolisten.

```rust
assert_eq!(body["observation_ready"], true);
assert_eq!(body["infrastructure_observation_ready"], true);
assert_eq!(body["database"], false);
assert_eq!(body["ready"], false);
assert_eq!(body["infrastructure_observation"]["coverage"], "unverified");
```

- [ ] **Step3: Wire minimal runtime/health.** Use the existing observationloop pattern with10secondSkip andseparateprogress, no genericloopframework. Destructure optionalcollector beforetaskspawn, preserve actualtransportlabels. CreateprivateInfrastructureHealthDTOs withflattenedsanitizedcomponents andfresh/lastsuccess; no publicconfiguration/reportendpoint. Extend readiness lock/evaluate arguments without holding locks during networkrequests. Removeallconsumedstageddeadcodeallows.
- [ ] **Step4: Full local gates.** Workspace/allfeatures with externalDSN/Dockercontextoverridescleared, inspectedlocalUnixendpoint andexactcleanup; allPVE/service/unit/compilefail, Python8, fmt, strictClippy, cached/offlinecargo-deny withdate/warnings. VerifyexactcommittedSHA inactualmacOSserviceaftercommit. Recordtop-levelexecutions andshared/nestedrepeats, notunique totals.
- [ ] **Step5: Commit scoped source** as `feat(rust): wire isolated selected-node observation service`. Independenttask/finalreview thenonecoordinatedrepairwaveifneeded/scopedreview. Mainartifactworker refreshesexactLinuxsourceafterreviews: verifiedlocalparent/exacttrackedcontext/offlineCargo/nohostsocket, nonzeropure/loopbackfiltersandactualsocketfreeHTTPservice,releasehealth+unmodifiedfakeCompose/adapterproof, allhashes/platform/sourceSHA/cleanup. HTTP+PGno-write remainsmacOS-onlyunlessactuallyprovenotherwise. Trackedacceptancefollowsexactevidenceverification, notmerelybuildsuccess.

## Self-review and remaining work

This phaseactivatesonlyexplicitnode/network visibility, neverauthoritativeabsence/ownership/ready-deviceproof. Singletonchoice tradesbreadth forfixed2requestceiling; multi-nodeoperationsrequireanotherboundedpolicy. Partialfailuresandcompletiontimesindependent; clusterreadinessunchanged. Storage/artifactlocalmodelingandactivationpolicy,realwriteprovenance/retries/reservations,media/firmware/TPM/QGA,OSDeploy/CloudOSD/agentintegration,sharedPythonfencing,restore/nonproductionproof andseparatelyapprovedcutover remainrequired. RustedOutClientexcluded; producttracksstayafterstablecontrollercontracts.

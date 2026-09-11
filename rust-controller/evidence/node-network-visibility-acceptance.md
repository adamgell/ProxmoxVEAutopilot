# Node/network visibility contract acceptance

Accepted locally on2026-09-05 for executable source **41877270834c2aa998fd2a44996a73a551faf291**. This later documentation commit is distinct from embedded artifact source.

Separate non-authoritative node/network DTOs and a two-method authenticated GET capability are implemented. Missing/zero uptime and unknown interface activity remain observations, not execution preconditions. Row/identity/duplicate/binding policies are explicit. Each GET has a two-second request/body/envelope deadline and one-MiB response cap. No native conversion, storage/QGA call, arbitrary route, service activation or runtime fan-out was added.

Both fresh independent Astra task reviews and the final consolidated1395-line review were clean. No repair wave was required. Main independently verified all22 final evidence checksums and read the proof report.

## Verification

- macOS:436 top-level workspace executions (417runtime+19compile-fail),154 PVE executions separately, Python8, fmt/strictworkspaceClippy passed. Ten shared lifecycle definitions run in three consumers produce20 repetitions; four nested proxy reruns excluded. No unique-definition total claimed.
- TDD:26 parser tests exercised against rejecting/permissive skeletons;22 HTTP tests behavioral RED thenGREEN. Three oversize tests failed when the cap was deliberately bypassed and passed after restoration. Temporary mutations not retained.
- Linux AMD64:126 tracked source hashes and127 exact fresh COPY files verified without symlinks/extra hostcache. Release examples/service/verifier built, all test targets compiled. Selected243runtime+12doc=255 executions and Python8 passed; shared/nested repetition limits retained. Includesnew26parser/22HTTP suites.
- Existing actual Linux debug authenticated cluster-service/unavailable-DB test passed; it is not new node/network service scheduling proof. Full HTTP-service/PostgreSQL no-write and native-controller PostgreSQL E2E remain macOS-only. Native_fake is Linux compiled-only.
- Actual release fake health: exactSHA/image, emptyobservermounts, ownednamespace, readytrue/observation_readyfalse/synthetic evidence. Unmodified multiworker proof:3ready/3claimers,cap2,max1attempt,4satisfied/2unknown/1pending,real30secondrecovery; adapter12+4 passed.
- Both exactowned projects fully cleaned, nativeownershiplabel empty, all four old stopped containers unchanged. No pruning/broad cleanup.

## Artifact identity

| Item | SHA256 |
| --- | --- |
| Image/index (129layers) | 820eee713691c278f3e485d837822463a6558bd42a5deb1b6895d2afa23a2a98 |
| Linux AMD64 manifest | 7fa9a532dbf209942db5de495bf2907f84d559eb04e8ef2f86e5481b8683a4bb |
| Image config | e985a8bb1baaf023f211ee1e07a45a4a4404af6eaa80ecd4204cc67d75083350 |
| Release service | 775b2ea7522aeb9f369a15b05258ed9522d30c0efc84950a801b33bfb3a2153a |
| Source manifest | 20e2ff2e6df3fcf0218ae5d43762c1f3e948890f53533b419c0aafd0852bb414 |
|22-entry evidence manifest | 3da6052cb3d14deb5578aac4931062fde07dfa5477287a53458960224b757a7b |

Verified cached parent57a6, content-named local tag, offline locked Cargo and network-none RUN, no dependency pull. No independent daemonmetadata isolation claim. Exact new staging path avoids inherited snapshot overlap; Python isolatedcacheprefix/no-bytecode policy verified. Old obsolete helper remains absent. Historical evidence preserved.

Detailed local evidence: `.superpowers/sdd/2026-09-05-rust-node-network-visibility/`, including reviews/reports/source/evidence manifests and execution logs. These ignored files are not published artifacts. Cached advisory assessment used commit5a0ebedfe8bdd2e295b171f4162f8c977bcad9a5 dated2026-09-02; baseline duplicate-family/unused-license warnings remain, no new advisory refresh claimed.

## Remaining boundary

No real PVE/controller/tenant calls, production credential access, SSH, deployment, push/publication/merge or device readiness claim. Production192.168.2.4 stays unchanged. RustedOutClient excluded; downstream product tracks deferred.

Next: explicit singleton target selection and independently bounded service collection/freshness/no-write proof. Storage GETs are not automatically read-only host operations because official source invokes activation; retain separate local modeling and activation policy. Real write authority/provenance/retries, media/firmware/TPM/QGA, OSDeploy/CloudOSD/agents, shared fencing, restore/nonproduction proof and separately approved cutover remain programme work. This closes the read-contract phase only.

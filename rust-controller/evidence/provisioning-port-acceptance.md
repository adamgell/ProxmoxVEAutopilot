# Provisioning library acceptance

Accepted locally and on Linux at source `849766df50cc13fb993925d5d11a1ee912ff837c`. This covers rich PVE facts, requests/evaluation, one shared synthetic provisioning world and downward OSDeploy conversion. It is not a rebuilt controller service or a production-candidate PoC.

Independent task reviews and complete phase review finished with one standalone source-discriminator defect. Focused repair and scoped re-review closed it; final review reports one addressed, zero open, zero new findings. Legacy source enum behavior remains unchanged.

Linux execution passed297 runtime/support tests and24 doctests: PVE229+18docs, OSDeploy34+6docs, controller decision24, native support10. The requested filter excluded25 database-dependent tests; four nested subprocess canary successes are not double-counted. No database, service or real PVE operation ran. macOS repair verification passed57 affected tests plus formatting and strict workspace/all-target/all-feature Clippy. Prior complete Mac suites and unchanged cached dependency-policy evidence remain separately qualified in their reports.

| Identity | SHA-256 |
| --- | --- |
|175-input source manifest|aa185e846ecebb78eb37829792a0aca5ef421ce86be3f6ffc8940b815d0283a1|
|Linux image/index|83ea9e9863ae75fefabbb1d15faa2c003948b672a4ee714de19eb72f011e19de|
|AMD64 manifest|27773d54c3cca9e6f33dcc5644cd6801e108d9e98cd36b8551fb4dffc9f5f7ce|
|Config|f476bfa9665fbf66b8cd2d2e899a17e59871df03eb6f78afa3405351f6f8e61c|
|26-file evidence manifest|f640ccb37d3c9fde1b745ad6b05e14931be0edb610bb469bd0d1b3edaf1bfd6c|

Tag `rust-controller-provisioning-port-proof:849766d`; cumulative7,888,962,097bytes/193layers is not unique disk use. Fresh staging contained176regularfiles includingmanifest, noextras/symlinks. All21executed ELF paths and hashes matched independently inspected AMD64 image artifacts. Parent comparison had28source additions,zero removals.

Main independently read the complete final report, result/metadata/quality records and verifier, checked actual build result lines, verified all26sealed proof files and all175source checksums at the clean frozenHEAD before recording this acceptance. Evidence lives under `.superpowers/sdd/2026-09-05-rust-provisioning-port/`; historical source checks apply to the frozen commit, not later documentation changes.

Inherited service remains `c429807443f80b74530cbd10af334e50e97b695b`, executableSHA256 `cb8414af170df9ec6276476f19632424d574e52c463cfa41a9c3a23403cb8fd8`; neither rebuilt nor run. Existing Compose alias unchanged. Both owned inspection containers gone; all prior4containers/23image mappings/3networks/3819volumes preserved. Final87,920,452KiBfree exceeded18GiBguard. Nocleanup/pulls/livecredentials/publication/deployment.

Source freeze ends after this accepted gate. Next: validated full-plan restoration and atomic guarded sixteen-stage registration in the existing store, then durable execution/callback/service/recovery integration. Installed PVE compatibility, Windows/agent/build-host/CloudOSD proofs, Python/Rust single-writer transition and full candidate assurance remain required under the active goal. No production mutation or cutover is authorized.

# Request-aware controller checkpoint

The fixture-enabled `OsDeployController` now passes the exact prepared request to
`ControllerFixturePort::provisioning_checkpoint` after the scheduler durably begins
dispatch and before the consuming send path. This gives a stage-aware adapter the
operation, attempt, stage, predecessor plan, and request data required to bind its
barrier. The existing cancellation wrapper still surrounds this await.

The new capability defaults to `Rejected`. Implementing the legacy point-only
checkpoint does not silently grant request-aware authorization. NativeFakePve
explicitly retains its existing dispatch barrier behavior. FixtureProvisioningPort
explicitly supports only Clone, validates its operation and source/target identity
and any configured exact digest, then invokes its existing fallible barrier.
Unsupported stages fail before any checkpoint transport or consuming send.

The dynamic capability regression submits the existing provisioning request chain
to an unbound adapter and requires rejection for every request. The fixture-stage
target and controller feature unit suite pass. This change supplies request context;
it does not implement stage progression or claim successful DiskCapacity/ConfigurePe
controller dispatch. Those still require a stage-aware adapter, stage barrier client,
and publication/readback of complete current target and driver observations.
The daemon stage-effect chain remains independent evidence.

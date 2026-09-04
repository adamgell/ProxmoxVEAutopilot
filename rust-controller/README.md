# Rust Controller

`controller-service` is the local Rust controller foundation for
ProxmoxVEAutopilot. It currently validates configuration and does not bind an
HTTP listener, contact Proxmox, access PostgreSQL, or perform any mutation.

## Local configuration

The service requires these environment variables before it starts:

- `RUST_CONTROLLER_MODE`: exactly `observe`, `adapter`, or `native`
- `RUST_CONTROLLER_DATABASE_URL`: local PostgreSQL URL, for example
  `postgresql://localhost/rust_controller`
- `RUST_CONTROLLER_PVE_BASE_URL`: local controller URL, for example
  `http://127.0.0.1:5000`

`RUST_CONTROLLER_ALLOW_PRODUCTION_READS` defaults to `false`. It accepts only
`true` or `false`.

The test-only `ControllerConfig::local_observe()` helper uses `observe`,
`postgresql://localhost/rust_controller`, and `http://127.0.0.1:5000` as local
defaults. Environment configuration never supplies defaults for mode, database
URL, or PVE URL; missing values terminate before the service can bind.

## Production boundary

The production address `192.168.2.4` is fail-closed:

- `adapter` and `native` modes are always rejected for that address.
- `observe` is accepted only with
  `RUST_CONTROLLER_ALLOW_PRODUCTION_READS=true`.

Native Proxmox mutation is unavailable in this foundation. Passing `native`
only permits configuration parsing; it does not enable an operation.

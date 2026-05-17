from __future__ import annotations

from typing import Any

from starlette.requests import Request

from .registry import ToolRegistry, object_schema


READ = {"readOnlyHint": True, "idempotentHint": True}
MUTATE = {"readOnlyHint": False, "idempotentHint": False}


def _body(cls: Any, args: dict[str, Any]) -> Any:
    return cls(**dict(args))


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def register(registry: ToolRegistry) -> None:
    @registry.register("cloudosd.get_catalog", "Get CloudOSD catalog.", annotations=READ)
    def get_catalog(args: dict[str, Any]) -> dict[str, Any]:
        from web import cloudosd_endpoints

        return cloudosd_endpoints.catalog_payload()

    @registry.register(
        "cloudosd.list_artifacts",
        "List CloudOSD artifacts.",
        object_schema({"architecture": {"type": "string"}}),
        annotations=READ,
    )
    def list_artifacts(args: dict[str, Any]) -> dict[str, Any]:
        from web import cloudosd_endpoints

        return cloudosd_endpoints.list_artifacts(architecture=args.get("architecture"))

    @registry.register("cloudosd.get_assets_status", "Get CloudOSD asset status.", annotations=READ)
    def get_assets_status(args: dict[str, Any]) -> dict[str, Any]:
        from web import cloudosd_endpoints

        return cloudosd_endpoints.assets_status_payload()

    @registry.register("cloudosd.get_proxmox_options", "Get CloudOSD Proxmox options.", annotations=READ)
    def get_proxmox_options(args: dict[str, Any]) -> dict[str, Any]:
        from web import cloudosd_endpoints

        return cloudosd_endpoints.proxmox_options_payload()

    @registry.register("cloudosd.preflight_run", "Run CloudOSD preflight.", object_schema(), annotations=READ)
    def preflight_run(args: dict[str, Any]) -> dict[str, Any]:
        from web import cloudosd_endpoints

        return cloudosd_endpoints.preflight_payload(_body(cloudosd_endpoints.RunCreateBody, args))

    @registry.register(
        "cloudosd.list_runs",
        "List CloudOSD runs.",
        object_schema({"limit": {"type": "integer"}, "include_archived": {"type": "boolean"}}),
        annotations=READ,
    )
    def list_runs(args: dict[str, Any]) -> dict[str, Any]:
        from web import cloudosd_endpoints

        return cloudosd_endpoints.list_runs(
            limit=int(args.get("limit") or 100),
            include_archived=bool(args.get("include_archived") or False),
        )

    @registry.register(
        "cloudosd.get_run",
        "Get CloudOSD run detail.",
        object_schema({"run_id": {"type": "string"}}, required=["run_id"]),
        annotations=READ,
    )
    def get_run(args: dict[str, Any]) -> dict[str, Any]:
        from web import cloudosd_endpoints

        return cloudosd_endpoints.get_run(str(args["run_id"]))

    @registry.register(
        "cloudosd.get_run_events",
        "Get CloudOSD run events.",
        object_schema({"run_id": {"type": "string"}}, required=["run_id"]),
        annotations=READ,
    )
    def get_run_events(args: dict[str, Any]) -> dict[str, Any]:
        from web import cloudosd_endpoints

        return cloudosd_endpoints.list_run_events(str(args["run_id"]))

    @registry.register("cloudosd.get_provision_progress", "Get CloudOSD progress.", annotations=READ)
    def get_provision_progress(args: dict[str, Any]) -> dict[str, Any]:
        from web import cloudosd_endpoints

        return cloudosd_endpoints.provision_progress_payload(limit=int(args.get("limit") or 50))

    @registry.register(
        "cloudosd.get_autopilot_readiness",
        "Get CloudOSD Autopilot readiness.",
        object_schema({"run_id": {"type": "string"}}, required=["run_id"]),
        annotations=READ,
    )
    def get_autopilot_readiness(args: dict[str, Any]) -> dict[str, Any]:
        from web import cloudosd_endpoints

        return cloudosd_endpoints.get_autopilot_readiness(str(args["run_id"]))

    @registry.register("cloudosd.create_run", "Create CloudOSD run.", object_schema(), annotations=MUTATE)
    def create_run(args: dict[str, Any]) -> dict[str, Any]:
        from web import cloudosd_endpoints

        return cloudosd_endpoints.create_run(_body(cloudosd_endpoints.RunCreateBody, args))

    @registry.register(
        "cloudosd.provision_run",
        "Queue CloudOSD Proxmox provisioning.",
        object_schema({"run_id": {"type": "string"}}, required=["run_id"]),
        annotations=MUTATE,
    )
    def provision_run(args: dict[str, Any]) -> dict[str, Any]:
        from web import cloudosd_endpoints

        return cloudosd_endpoints.provision_run(str(args["run_id"]), _request())

    @registry.register("cloudosd.build_artifact", "Queue CloudOSD artifact build.", object_schema(), annotations=MUTATE)
    def build_artifact(args: dict[str, Any]) -> dict[str, Any]:
        from web import cloudosd_endpoints

        return cloudosd_endpoints.build_artifact(_body(cloudosd_endpoints.ArtifactBuildBody, args))

    @registry.register(
        "cloudosd.publish_artifact",
        "Publish CloudOSD artifact to Proxmox.",
        object_schema({"artifact_id": {"type": "string"}}, required=["artifact_id"]),
        annotations=MUTATE,
    )
    def publish_artifact(args: dict[str, Any]) -> dict[str, Any]:
        from web import cloudosd_endpoints

        body_args = {k: v for k, v in args.items() if k != "artifact_id"}
        return cloudosd_endpoints.publish_artifact(
            str(args["artifact_id"]),
            _body(cloudosd_endpoints.ArtifactPublishBody, body_args),
        )

    @registry.register(
        "cloudosd.reconcile_autopilot_readiness",
        "Reconcile CloudOSD Autopilot readiness.",
        object_schema({"run_id": {"type": "string"}}, required=["run_id"]),
        annotations=MUTATE,
    )
    def reconcile_autopilot_readiness(args: dict[str, Any]) -> dict[str, Any]:
        from web import cloudosd_endpoints

        return cloudosd_endpoints.reconcile_autopilot_readiness(str(args["run_id"]))

    @registry.register(
        "cloudosd.sync_autopilot_readiness",
        "Sync Graph-backed CloudOSD Autopilot readiness.",
        object_schema({"run_id": {"type": "string"}}, required=["run_id"]),
        annotations=MUTATE,
    )
    def sync_autopilot_readiness(args: dict[str, Any]) -> dict[str, Any]:
        from web import cloudosd_endpoints

        return cloudosd_endpoints.sync_autopilot_readiness(str(args["run_id"]))

    @registry.register(
        "cloudosd.upload_autopilot_hash",
        "Queue CloudOSD Autopilot hash upload.",
        object_schema({"run_id": {"type": "string"}}, required=["run_id"]),
        annotations=MUTATE,
    )
    def upload_autopilot_hash(args: dict[str, Any]) -> dict[str, Any]:
        from web import cloudosd_endpoints

        return cloudosd_endpoints.upload_autopilot_hash(str(args["run_id"]))

    @registry.register(
        "cloudosd.retry_autopilot_hash_upload",
        "Retry CloudOSD Autopilot hash upload.",
        object_schema({"run_id": {"type": "string"}}, required=["run_id"]),
        annotations=MUTATE,
    )
    def retry_autopilot_hash_upload(args: dict[str, Any]) -> dict[str, Any]:
        from web import cloudosd_endpoints

        return cloudosd_endpoints.retry_autopilot_hash_upload(str(args["run_id"]))

    @registry.register(
        "cloudosd.retry_v2_step",
        "Retry a CloudOSD v2 task sequence step.",
        object_schema({"run_id": {"type": "string"}, "step_id": {"type": "string"}}, required=["run_id", "step_id"]),
        annotations=MUTATE,
    )
    def retry_v2_step(args: dict[str, Any]) -> dict[str, Any]:
        from web import cloudosd_endpoints

        return cloudosd_endpoints.retry_v2_step(str(args["run_id"]), str(args["step_id"]))

    @registry.register(
        "cloudosd.archive_run",
        "Archive a CloudOSD run.",
        object_schema({"run_id": {"type": "string"}, "reason": {"type": "string"}}, required=["run_id"]),
        annotations=MUTATE,
    )
    def archive_run(args: dict[str, Any]) -> dict[str, Any]:
        from web import cloudosd_endpoints

        return cloudosd_endpoints.archive_run(str(args["run_id"]), reason=str(args.get("reason") or ""))

    @registry.register(
        "cloudosd.unarchive_run",
        "Unarchive a CloudOSD run.",
        object_schema({"run_id": {"type": "string"}}, required=["run_id"]),
        annotations=MUTATE,
    )
    def unarchive_run(args: dict[str, Any]) -> dict[str, Any]:
        from web import cloudosd_endpoints

        return cloudosd_endpoints.unarchive_run(str(args["run_id"]))

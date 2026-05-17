from __future__ import annotations

from typing import Any

from .registry import ToolRegistry, object_schema


READ = {"readOnlyHint": True, "idempotentHint": True}
MUTATE = {"readOnlyHint": False, "idempotentHint": False}


def _body(cls: Any, args: dict[str, Any]) -> Any:
    return cls(**dict(args))


def register(registry: ToolRegistry) -> None:
    @registry.register("osdeploy.get_catalog", "Get the OSDeploy Server catalog.", annotations=READ)
    def get_catalog(args: dict[str, Any]) -> dict[str, Any]:
        from web import osdeploy_endpoints

        return osdeploy_endpoints.catalog_payload()

    @registry.register("osdeploy.get_proxmox_options", "Get OSDeploy Proxmox placement options.", annotations=READ)
    def get_proxmox_options(args: dict[str, Any]) -> dict[str, Any]:
        from web import osdeploy_endpoints

        return osdeploy_endpoints.proxmox_options_payload()

    @registry.register(
        "osdeploy.list_artifacts",
        "List OSDeploy build artifacts.",
        object_schema({"architecture": {"type": "string"}}),
        annotations=READ,
    )
    def list_artifacts(args: dict[str, Any]) -> dict[str, Any]:
        from web import osdeploy_endpoints

        return osdeploy_endpoints.list_artifacts(architecture=args.get("architecture"))

    @registry.register(
        "osdeploy.get_artifact_status",
        "Get OSDeploy artifact status and job evidence.",
        object_schema({"artifact_id": {"type": "string"}}, required=["artifact_id"]),
        annotations=READ,
    )
    def get_artifact_status(args: dict[str, Any]) -> dict[str, Any]:
        from web import osdeploy_endpoints

        return osdeploy_endpoints.artifact_status(str(args["artifact_id"]))

    @registry.register("osdeploy.get_build_defaults", "Get OSDeploy build defaults.", annotations=READ)
    def get_build_defaults(args: dict[str, Any]) -> dict[str, Any]:
        from web import osdeploy_endpoints

        return osdeploy_endpoints.build_defaults_payload()

    @registry.register(
        "osdeploy.build_preflight",
        "Run OSDeploy artifact build preflight.",
        object_schema(),
        annotations=READ,
    )
    def build_preflight(args: dict[str, Any]) -> dict[str, Any]:
        from web import osdeploy_endpoints

        return osdeploy_endpoints.build_preflight_payload(
            _body(osdeploy_endpoints.ArtifactBuildBody, args)
        )

    @registry.register("osdeploy.get_cache", "Get OSDeploy cache status.", annotations=READ)
    def get_cache(args: dict[str, Any]) -> dict[str, Any]:
        from web import osdeploy_endpoints

        return osdeploy_endpoints.cache_status()

    @registry.register(
        "osdeploy.preflight_run",
        "Run OSDeploy VM provisioning preflight.",
        object_schema(),
        annotations=READ,
    )
    def preflight_run(args: dict[str, Any]) -> dict[str, Any]:
        from web import osdeploy_endpoints

        return osdeploy_endpoints.preflight_payload(_body(osdeploy_endpoints.RunCreateBody, args))

    @registry.register(
        "osdeploy.list_runs",
        "List OSDeploy runs.",
        object_schema(
            {
                "limit": {"type": "integer", "default": 100},
                "include_archived": {"type": "boolean", "default": False},
            }
        ),
        annotations=READ,
    )
    def list_runs(args: dict[str, Any]) -> dict[str, Any]:
        from web import osdeploy_endpoints

        return osdeploy_endpoints.list_runs(
            limit=int(args.get("limit") or 100),
            include_archived=bool(args.get("include_archived") or False),
        )

    @registry.register(
        "osdeploy.get_progress",
        "Get OSDeploy run progress summary.",
        object_schema(
            {
                "limit": {"type": "integer", "default": 50},
                "include_archived": {"type": "boolean", "default": False},
            }
        ),
        annotations=READ,
    )
    def get_progress(args: dict[str, Any]) -> dict[str, Any]:
        from web import osdeploy_endpoints

        return osdeploy_endpoints.provision_progress_payload(
            limit=int(args.get("limit") or 50),
            include_archived=bool(args.get("include_archived") or False),
        )

    @registry.register(
        "osdeploy.get_run",
        "Get OSDeploy run detail.",
        object_schema({"run_id": {"type": "string"}}, required=["run_id"]),
        annotations=READ,
    )
    def get_run(args: dict[str, Any]) -> dict[str, Any]:
        from web import osdeploy_endpoints

        return osdeploy_endpoints.run_detail(str(args["run_id"]))

    @registry.register("osdeploy.create_run", "Create an OSDeploy run.", object_schema(), annotations=MUTATE)
    def create_run(args: dict[str, Any]) -> dict[str, Any]:
        from web import osdeploy_endpoints

        return osdeploy_endpoints.create_run(_body(osdeploy_endpoints.RunCreateBody, args))

    @registry.register("osdeploy.build_artifact", "Queue an OSDeploy artifact build.", object_schema(), annotations=MUTATE)
    def build_artifact(args: dict[str, Any]) -> dict[str, Any]:
        from web import osdeploy_endpoints

        return osdeploy_endpoints.build_artifact(_body(osdeploy_endpoints.ArtifactBuildBody, args))

    @registry.register(
        "osdeploy.publish_artifact",
        "Publish an OSDeploy artifact to Proxmox ISO storage.",
        object_schema({"artifact_id": {"type": "string"}}, required=["artifact_id"]),
        annotations=MUTATE,
    )
    def publish_artifact(args: dict[str, Any]) -> dict[str, Any]:
        from web import osdeploy_endpoints

        body_args = {k: v for k, v in args.items() if k != "artifact_id"}
        return osdeploy_endpoints.publish_artifact(
            str(args["artifact_id"]),
            _body(osdeploy_endpoints.ArtifactPublishBody, body_args),
        )

    @registry.register(
        "osdeploy.provision_run",
        "Queue Proxmox provisioning for an OSDeploy run.",
        object_schema({"run_id": {"type": "string"}}, required=["run_id"]),
        annotations=MUTATE,
    )
    def provision_run(args: dict[str, Any]) -> dict[str, Any]:
        from starlette.requests import Request
        from web import osdeploy_endpoints

        request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
        return osdeploy_endpoints.provision_run(str(args["run_id"]), request)

    @registry.register(
        "osdeploy.archive_run",
        "Archive an OSDeploy run.",
        object_schema({"run_id": {"type": "string"}, "reason": {"type": "string"}}, required=["run_id"]),
        annotations=MUTATE,
    )
    def archive_run(args: dict[str, Any]) -> dict[str, Any]:
        from web import osdeploy_endpoints

        return osdeploy_endpoints.archive_run(str(args["run_id"]), reason=str(args.get("reason") or ""))

    @registry.register(
        "osdeploy.unarchive_run",
        "Unarchive an OSDeploy run.",
        object_schema({"run_id": {"type": "string"}}, required=["run_id"]),
        annotations=MUTATE,
    )
    def unarchive_run(args: dict[str, Any]) -> dict[str, Any]:
        from web import osdeploy_endpoints

        return osdeploy_endpoints.unarchive_run(str(args["run_id"]))

    @registry.register(
        "osdeploy.activate_build_host_agent",
        "Queue OSDeploy build-host activation for an agent.",
        object_schema({"agent_id": {"type": "string"}}, required=["agent_id"]),
        annotations=MUTATE,
    )
    def activate_build_host_agent(args: dict[str, Any]) -> dict[str, Any]:
        from web import osdeploy_endpoints

        body_args = {k: v for k, v in args.items() if k != "agent_id"}
        return osdeploy_endpoints.activate_build_host_agent(
            str(args["agent_id"]),
            _body(osdeploy_endpoints.BuildHostActivateBody, body_args),
        )

    @registry.register(
        "osdeploy.repair_build_host_agent",
        "Repair an OSDeploy build-host agent through QGA.",
        object_schema({"agent_id": {"type": "string"}}, required=["agent_id"]),
        annotations=MUTATE,
    )
    def repair_build_host_agent(args: dict[str, Any]) -> dict[str, Any]:
        from web import osdeploy_endpoints

        body_args = {k: v for k, v in args.items() if k != "agent_id"}
        return osdeploy_endpoints.repair_build_host_agent(
            str(args["agent_id"]),
            _body(osdeploy_endpoints.BuildHostRepairBody, body_args),
        )

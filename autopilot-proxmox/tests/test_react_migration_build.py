from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPO_ROOT / "autopilot-proxmox"


def test_dockerfile_builds_react_assets_in_node_stage():
    dockerfile = (APP_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM node:25-bookworm-slim AS frontend-build" in dockerfile
    assert "WORKDIR /frontend" in dockerfile
    assert "COPY frontend/package.json frontend/package-lock.json ./" in dockerfile
    assert "RUN npm ci" in dockerfile
    assert "RUN npm run build" in dockerfile
    assert "COPY --from=frontend-build /frontend/dist /app/web/static/react" in dockerfile


def test_docker_publish_builds_on_main_and_release_tags():
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/docker-publish.yml").read_text())
    on_config = workflow.get("on") or workflow.get(True)
    push = on_config["push"]

    # No paths filter (removed intentionally so a release-tag build is never
    # skipped when the tagged commit only bumps VERSION). Every push to main
    # rebuilds the image, so frontend and any other changes are always covered.
    assert "main" in push["branches"]
    assert "paths" not in push
    # Release tags (v*) trigger the immutable :v<CalVer> image build.
    assert any(str(tag).startswith("v") for tag in push["tags"])


def test_pr_validation_checks_frontend_and_docker_smoke():
    workflow_path = REPO_ROOT / ".github/workflows/react-migration-validation.yml"
    workflow = yaml.safe_load(workflow_path.read_text())
    steps = "\n".join(
        step.get("run", "") for step in workflow["jobs"]["validate"]["steps"] if isinstance(step, dict)
    )

    assert "python -m pytest tests/test_react_shell.py tests/test_react_migration_build.py -v" in steps
    assert "npm ci" in steps
    assert "npm run typecheck" in steps
    assert "npm run typecheck:ts7" in steps
    assert "npm run test" in steps
    assert "npx playwright install chromium" in steps
    assert "npm run test:e2e" in steps
    assert "npm run build" in steps
    assert "docker build" in steps
    assert "--platform linux/amd64" in steps


def test_pr_validation_installs_system_build_dependencies_for_python_wheels():
    workflow_path = REPO_ROOT / ".github/workflows/react-migration-validation.yml"
    workflow = yaml.safe_load(workflow_path.read_text())
    steps = workflow["jobs"]["validate"]["steps"]
    install_index = next(index for index, step in enumerate(steps) if step.get("name") == "Install Python dependencies")
    system_step = steps[install_index - 1]

    assert system_step["name"] == "Install Python system build dependencies"
    assert "apt-get update" in system_step["run"]
    assert "krb5-user" in system_step["run"]
    assert "libkrb5-dev" in system_step["run"]
    assert "libldap2-dev" in system_step["run"]
    assert "libsasl2-dev" in system_step["run"]

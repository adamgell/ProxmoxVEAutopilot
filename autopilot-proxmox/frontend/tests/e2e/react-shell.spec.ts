import { expect, test, type Page } from "@playwright/test";

async function mockReadApis(page: Page) {
  await page.route("**/api/services", async (route) => {
    await route.fulfill({
      json: {
        available: true,
        services: [{ service: "autopilot", status: "healthy", age_seconds: 5, detail: "running" }]
      }
    });
  });
  await page.route("**/api/jobs/running", async (route) => {
    await route.fulfill({
      json: {
        running: [{ id: "job-running", playbook: "provision.yml", target: "PC-001", progress_pct: 25 }],
        running_count: 1,
        queued_count: 1
      }
    });
  });
  await page.route("**/api/jobs/recent?limit=5", async (route) => {
    await route.fulfill({
      json: {
        jobs: [
          {
            id: "job-complete",
            playbook: "template.yml",
            status: "complete",
            duration: "3m 0s",
            target: "Win11-Template"
          }
        ]
      }
    });
  });
  await page.route("**/api/fleet/summary", async (route) => {
    await route.fulfill({ json: { total: 4, ad_joined_pct: 75, autopilot_pct: 50, intune_pct: 25 } });
  });
  await page.route("**/api/jobs", async (route) => {
    await route.fulfill({
      json: [
        {
          id: "job-running",
          playbook: "provision.yml",
          status: "running",
          duration: "2m 0s",
          args: { vm_name: "PC-001" },
          paused: false
        },
        {
          id: "job-failed",
          playbook: "capture.yml",
          status: "failed",
          duration: "1m 0s",
          args: { serial: "SN-001" },
          paused: false
        }
      ]
    });
  });
  await page.route("**/api/monitoring/runtime-services", async (route) => {
    await route.fulfill({
      json: {
        available: true,
        error: "",
        containers: [
          {
            id: "abc123",
            name: "autopilot",
            service: "autopilot",
            image: "proxmox-autopilot:latest",
            status: "running",
            health: "healthy",
            restart_count: 0,
            log_url: "/api/monitoring/service-logs?container=autopilot"
          }
        ]
      }
    });
  });
  await page.route("**/api/monitoring/deployments/summary", async (route) => {
    await route.fulfill({ json: { total: 2, running: 1, succeeded: 1, failed: 0 } });
  });
  await page.route("**/api/monitoring/keytab/health", async (route) => {
    await route.fulfill({ json: { status: "ok", detail: "keytab valid" } });
  });
  await page.route("**/api/monitoring/signals", async (route) => {
    await route.fulfill({
      json: {
        generated_at: "2026-05-19T00:00:00Z",
        build: { sha_short: "abc1234", build_time: "2026-05-18T12:00:00Z" },
        source_health: { runtime_available: true, setup_health: "ready" },
        metrics: [],
        signals: [
          {
            id: "runtime",
            family: "runtime",
            label: "Runtime containers",
            status: "healthy",
            tone: "good",
            summary: "autopilot is healthy"
          },
          {
            id: "build-host",
            family: "build_host",
            label: "Build host agent",
            status: "ready",
            tone: "good",
            summary: "buildhost-100 heartbeat fresh"
          }
        ],
        operator_paths: [
          {
            id: "server-deploy",
            priority: 20,
            label: "Windows Server OSDeploy artifact is available",
            status: "ready",
            tone: "good",
            summary: "Open the existing OSDeploy execution flow.",
            action_label: "Open server deploy",
            href: "/osdeploy"
          }
        ],
        lifecycle_lanes: [
          {
            id: "provisioned",
            label: "Provisioned",
            value: "2/3",
            detail: "Running in Proxmox and visible to the monitor.",
            status: "attention",
            tone: "active"
          }
        ],
        deployment_health: {
          summary: {
            total: 4,
            active: 1,
            running: 1,
            completed: 2,
            succeeded: 2,
            failed: 1,
            stuck: 0,
            regressed: 1,
            slow: 0,
            median_completion_seconds: 300,
            p95_completion_seconds: 900,
            recent_failure_rate: 0.25
          },
          active: [
            {
              deployment_key: "osdeploy/run-1",
              deployment_type: "osdeploy",
              current_phase: "windows_setup",
              elapsed_seconds: 120,
              health: "running",
              state: "running",
              next_expected_evidence: "agent heartbeat"
            }
          ],
          recent_completions: [],
          bottlenecks: [
            {
              deployment_type: "osdeploy",
              phase_key: "windows_setup",
              phase_label: "Windows setup",
              count: 1,
              health: "regressed",
              p95_seconds: 900
            }
          ]
        },
        services: [
          {
            service_id: "autopilot-monitor",
            status: "ok",
            age_seconds: 12,
            detail: "sweep idle"
          }
        ],
        runtime: {
          available: true,
          error: "",
          containers: [
            {
              name: "autopilot",
              service: "autopilot",
              image: "proxmox-autopilot:latest",
              status: "running",
              health: "healthy"
            }
          ]
        },
        fleet_attention: [
          {
            vmid: 101,
            vm_name: "WIN-SRV-01",
            node: "pve1",
            lifecycle: "Needs check",
            tone: "bad",
            pve_status: "running",
            windows: "WIN-SRV-01",
            serial: "SER-101",
            ad: "ok",
            entra: "missing",
            intune: "missing",
            last_checked: "2026-05-19T00:00:00Z",
            href: "/devices/101"
          }
        ]
      }
    });
  });
  await page.route("**/api/monitoring/service-logs?tail=180&container=autopilot", async (route) => {
    await route.fulfill({
      json: {
        container: "autopilot",
        service: "autopilot",
        tail: 180,
        lines: ["2026-05-19T00:00:00Z autopilot ready"]
      }
    });
  });
}

test("renders the React shell without layout overlap", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Proxmox VE Autopilot" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Operator workspace" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Signals Hub", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "OSDCloud Desktop Jinja", exact: true })).toBeVisible();

  const hero = await page.locator(".workspace__topbar").boundingBox();
  const panel = await page.locator(".workspace__content").boundingBox();

  expect(hero).not.toBeNull();
  expect(panel).not.toBeNull();
  if (!hero || !panel) {
    throw new Error("React shell layout regions were not measurable.");
  }
  expect(hero.y + hero.height).toBeLessThanOrEqual(panel.y + 1);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 390, height: 844 }
]) {
  test(`renders dashboard read-only route without overlap on ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await mockReadApis(page);
    await page.goto("/react/dashboard");

    await expect(page.getByRole("heading", { name: "Dashboard", exact: true })).toBeVisible();
    await expect(page.getByText("PC-001")).toBeVisible();
    await expect(page.getByRole("link", { name: "Provision", exact: true })).toBeVisible();

    const header = await page.locator(".page-head").boundingBox();
    const metrics = await page.locator(".metric-strip").first().boundingBox();
    expect(header).not.toBeNull();
    expect(metrics).not.toBeNull();
    if (!header || !metrics) {
      throw new Error("Dashboard layout regions were not measurable.");
    }
    expect(header.y + header.height).toBeLessThanOrEqual(metrics.y + 1);
  });

  test(`renders jobs read-only route without overlap on ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await mockReadApis(page);
    await page.goto("/react/jobs");

    await expect(page.getByRole("heading", { name: "Jobs", exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: "job-running" })).toBeVisible();
    await expect(page.getByLabel("Filter jobs")).toBeVisible();

    const metrics = await page.locator(".metric-strip").first().boundingBox();
    const filter = await page.locator(".filter").boundingBox();
    expect(metrics).not.toBeNull();
    expect(filter).not.toBeNull();
    if (!metrics || !filter) {
      throw new Error("Jobs layout regions were not measurable.");
    }
    expect(metrics.y + metrics.height).toBeLessThanOrEqual(filter.y + 1);
  });

  test(`renders Signals Hub read-only route without overlap on ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await mockReadApis(page);
    await page.goto("/react/monitoring");

    await expect(page.getByRole("heading", { name: "Signals Hub", exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Signal families" })).toBeVisible();
    await expect(page.getByText("Build host agent")).toBeVisible();
    await expect(page.getByRole("link", { name: "Open server deploy" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Monitoring settings", exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Deployment speed" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Lifecycle lanes" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Service health" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Runtime containers" })).toBeVisible();
    await page.getByRole("button", { name: "Tail" }).click();
    await expect(page.getByText("2026-05-19T00:00:00Z autopilot ready")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Fleet attention" })).toBeVisible();

    const header = await page.locator(".page-head").boundingBox();
    const metrics = await page.locator(".metric-strip").first().boundingBox();
    expect(header).not.toBeNull();
    expect(metrics).not.toBeNull();
    if (!header || !metrics) {
      throw new Error("Signals Hub layout regions were not measurable.");
    }
    expect(header.y + header.height).toBeLessThanOrEqual(metrics.y + 1);
  });
}

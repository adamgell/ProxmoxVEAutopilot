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
}

test("renders the React shell without layout overlap", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Proxmox VE Autopilot" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Operator workspace" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Monitoring", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "OSDCloud Desktop", exact: true })).toBeVisible();

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

  test(`renders monitoring read-only route without overlap on ${viewport.name}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await mockReadApis(page);
    await page.goto("/react/monitoring");

    await expect(page.getByRole("heading", { name: "Monitoring", exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Runtime services" })).toBeVisible();
    await expect(page.locator(".row-list strong", { hasText: "autopilot" }).first()).toBeVisible();
    await expect(page.getByRole("link", { name: "Monitoring settings", exact: true })).toBeVisible();

    const header = await page.locator(".page-head").boundingBox();
    const metrics = await page.locator(".metric-strip").first().boundingBox();
    expect(header).not.toBeNull();
    expect(metrics).not.toBeNull();
    if (!header || !metrics) {
      throw new Error("Monitoring layout regions were not measurable.");
    }
    expect(header.y + header.height).toBeLessThanOrEqual(metrics.y + 1);
  });
}

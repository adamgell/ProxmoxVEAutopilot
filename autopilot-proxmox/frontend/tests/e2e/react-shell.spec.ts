import { expect, test } from "@playwright/test";

test("renders the React shell without layout overlap", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Proxmox VE Autopilot" })).toBeVisible();
  await expect(page.getByText("React shell foundation")).toBeVisible();
  await expect(page.getByText("Migrated routes")).toBeVisible();

  const hero = await page.locator(".shell__hero").boundingBox();
  const panel = await page.locator(".shell__panel").boundingBox();

  expect(hero).not.toBeNull();
  expect(panel).not.toBeNull();
  if (!hero || !panel) {
    throw new Error("React shell layout regions were not measurable.");
  }
  expect(hero.y + hero.height).toBeLessThanOrEqual(panel.y + 1);
});

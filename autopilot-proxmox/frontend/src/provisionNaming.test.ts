import { describe, expect, test } from "vitest";

import {
  deriveProvisionNaming,
  previewHostnamePattern,
  WINDOWS_COMPUTER_NAME_LIMIT
} from "./provisionNaming";

describe("provision naming", () => {
  test("uses the full run tag as the group tag and a compact tenant hostname", () => {
    const naming = deriveProvisionNaming("NTTENANT01-Desktop");

    expect(naming.groupTag).toBe("NTTENANT01-Desktop");
    expect(naming.hostnamePattern).toBe("ntt01-{index}");
    expect(naming.previewName).toBe("ntt01-01");
    expect(naming.previewLength).toBe(8);
    expect(naming.safe).toBe(true);
  });

  test("keeps descriptive non-numeric tags within the Windows computer-name limit", () => {
    const naming = deriveProvisionNaming("Engineering Desktop Pilot");

    expect(naming.hostnamePattern).toBe("engineering-{index}");
    expect(naming.previewName).toBe("engineering-01");
    expect(naming.previewLength).toBeLessThanOrEqual(WINDOWS_COMPUTER_NAME_LIMIT);
    expect(naming.safe).toBe(true);
  });

  test("truncates long single-token tags before adding the index suffix", () => {
    const naming = deriveProvisionNaming("VeryLongTenantWorkstations");

    expect(naming.hostnamePattern).toBe("verylongtena-{index}");
    expect(naming.previewName).toBe("verylongtena-01");
    expect(naming.previewLength).toBe(WINDOWS_COMPUTER_NAME_LIMIT);
    expect(naming.safe).toBe(true);
  });

  test("prefixes numeric-only tags so Windows does not receive a numeric-only name", () => {
    const naming = deriveProvisionNaming("12345678901234567890");

    expect(naming.hostnamePattern).toBe("pve-12345678-{index}");
    expect(naming.previewName).toBe("pve-12345678-01");
    expect(naming.previewLength).toBe(WINDOWS_COMPUTER_NAME_LIMIT);
    expect(naming.safe).toBe(true);
  });

  test("falls back to a short autopilot prefix for blank tags", () => {
    const naming = deriveProvisionNaming("");

    expect(naming.groupTag).toBe("");
    expect(naming.hostnamePattern).toBe("ap-{index}");
    expect(naming.previewName).toBe("ap-01");
    expect(naming.safe).toBe(true);
  });

  test("previews manual patterns with backend-style two digit indexes", () => {
    const preview = previewHostnamePattern("lab-{index}");

    expect(preview.previewName).toBe("lab-01");
    expect(preview.safe).toBe(true);
  });
});

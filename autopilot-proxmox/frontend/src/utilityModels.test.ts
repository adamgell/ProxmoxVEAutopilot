import { describe, expect, test } from "vitest";

import { bytesLabel, secretState, shortTypeLabel, textValue } from "./utilityModels";

describe("utility React model helpers", () => {
  test("renders absent values as operator-safe dashes", () => {
    expect(textValue(null)).toBe("-");
    expect(textValue(undefined)).toBe("-");
    expect(textValue("")).toBe("-");
    expect(textValue("ready")).toBe("ready");
  });

  test("formats bytes without leaking raw zero values into tables", () => {
    expect(bytesLabel(0)).toBe("-");
    expect(bytesLabel(2048)).toBe("2.0 KB");
    expect(bytesLabel(2 * 1024 * 1024)).toBe("2.0 MB");
  });

  test("keeps secret and credential labels tight", () => {
    expect(secretState(true)).toBe("Set");
    expect(secretState(false)).toBe("Not set");
    expect(shortTypeLabel("domain_join")).toBe("domain join");
  });
});

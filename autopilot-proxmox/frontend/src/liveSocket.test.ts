import { describe, expect, test } from "vitest";

import { liveSocketUrl } from "./liveSocket";

describe("liveSocketUrl", () => {
  test("uses ws for http origins", () => {
    expect(liveSocketUrl("http://controller.local:5000")).toBe(
      "ws://controller.local:5000/api/live/ws"
    );
  });

  test("uses wss for https origins", () => {
    expect(liveSocketUrl("https://autopilot.example")).toBe(
      "wss://autopilot.example/api/live/ws"
    );
  });
});

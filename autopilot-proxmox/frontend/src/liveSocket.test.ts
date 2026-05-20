import { describe, expect, test } from "vitest";

import {
  fleetSubscribeMessage,
  isAgentsLiveMessage,
  isFleetLiveMessage,
  isJobsLiveMessage,
  jobsSubscribeMessage,
  liveSocketUrl,
  parseLiveSocketMessage
} from "./liveSocket";

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

describe("fleet live socket messages", () => {
  test("builds a fleet and agents topic subscription payload", () => {
    expect(fleetSubscribeMessage()).toBe(JSON.stringify({ type: "subscribe", topics: ["fleet", "agents"] }));
  });

  test("detects fleet and agents snapshots and patches", () => {
    expect(isFleetLiveMessage({ topic: "fleet", type: "snapshot", data: { rows: [] } })).toBe(true);
    expect(isFleetLiveMessage({ topic: "fleet", type: "patch", rows: [] })).toBe(true);
    expect(isFleetLiveMessage({ topic: "jobs", type: "snapshot", data: {} })).toBe(false);
    expect(isAgentsLiveMessage({ topic: "agents", type: "snapshot", data: { agents: [] } })).toBe(true);
    expect(isAgentsLiveMessage({ topic: "agents", type: "patch", agents: [] })).toBe(true);
    expect(isAgentsLiveMessage({ topic: "fleet", type: "snapshot", data: {} })).toBe(false);
  });
});

describe("jobs live socket messages", () => {
  test("builds the existing jobs topic subscription payload", () => {
    expect(jobsSubscribeMessage()).toBe(JSON.stringify({ type: "subscribe", topics: ["jobs"] }));
  });

  test("parses valid JSON and ignores invalid JSON", () => {
    expect(parseLiveSocketMessage('{"topic":"jobs","type":"snapshot","data":{"table":{"jobs":[]}}}')).toEqual({
      topic: "jobs",
      type: "snapshot",
      data: { table: { jobs: [] } }
    });
    expect(parseLiveSocketMessage("{broken")).toBeNull();
  });

  test("detects jobs topic snapshots and patches", () => {
    expect(
      isJobsLiveMessage({
        topic: "jobs",
        type: "snapshot",
        data: { running: { running: [], running_count: 0, queued_count: 0 } }
      })
    ).toBe(true);
    expect(isJobsLiveMessage({ topic: "jobs", type: "heartbeat", data: {} })).toBe(false);
    expect(isJobsLiveMessage({ topic: "monitoring", type: "snapshot", data: {} })).toBe(false);
  });
});

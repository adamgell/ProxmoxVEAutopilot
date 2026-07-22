import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fetchState, putState, deleteState, PreconditionFailedError } from "./persistence";

const FETCH = global.fetch;

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  global.fetch = FETCH;
});

describe("persistence layer", () => {
  it("fetchState returns null on 404", async () => {
    global.fetch = vi.fn(async () => new Response("", { status: 404 })) as any;
    expect(await fetchState()).toBeNull();
  });

  it("fetchState returns row and etag from headers on 200", async () => {
    global.fetch = vi.fn(async () => new Response(JSON.stringify({ persona: "lab" }), {
      status: 200,
      headers: { ETag: 'W/"abc"', "Content-Type": "application/json" },
    })) as any;
    const result = await fetchState();
    expect(result?.row.persona).toBe("lab");
    expect(result?.etag).toBe('W/"abc"');
  });

  it("putState surfaces 409 as PreconditionFailedError", async () => {
    global.fetch = vi.fn(async () => new Response("", { status: 409 })) as any;
    await expect(putState({ patch: {} }, "W/\"stale\"")).rejects.toBeInstanceOf(PreconditionFailedError);
  });

  it("putState retries 1s/3s/9s on 5xx then surfaces", async () => {
    const calls: number[] = [];
    global.fetch = vi.fn(async () => {
      calls.push(Date.now());
      return new Response("", { status: 503 });
    }) as any;
    const promise = putState({ patch: { persona: "lab" } }, null).catch((e) => e);
    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(3000);
    await vi.advanceTimersByTimeAsync(9000);
    const err = await promise;
    expect(calls).toHaveLength(4);
    expect(err).toBeInstanceOf(Error);
  });
});

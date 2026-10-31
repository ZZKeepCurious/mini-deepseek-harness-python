import { describe, expect, it, vi } from "vitest";
import { fetchOpenInAppCatalog } from "../src/wire/openInApp";

describe("open-in-app wire", () => {
  it("returns an empty catalog without rendering controls", async () => {
    const fetchImpl = vi.fn(async () =>
      new Response(JSON.stringify({ apps: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const apps = await fetchOpenInAppCatalog(fetchImpl);
    expect(apps).toEqual([]);
    expect(fetchImpl).toHaveBeenCalledWith("/open-in-app/apps", expect.any(Object));
  });
});

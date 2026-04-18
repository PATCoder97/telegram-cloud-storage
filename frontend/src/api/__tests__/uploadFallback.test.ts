import { describe, expect, it } from "vitest";

import { shouldFallbackFromTus } from "../uploadFallback";

describe("shouldFallbackFromTus", () => {
  it("falls back when the tus endpoint is missing", () => {
    expect(
      shouldFallbackFromTus({
        status: 404,
        message: "Not Found",
      })
    ).toBe(true);
  });

  it("falls back when the server explicitly says tus is unsupported", () => {
    expect(shouldFallbackFromTus(new Error("501 Not Implemented"))).toBe(true);
  });

  it("does not mask real upload conflicts", () => {
    expect(
      shouldFallbackFromTus({
        status: 409,
        message: "Conflict",
      })
    ).toBe(false);
  });

  it("does not fall back on generic network failures", () => {
    expect(shouldFallbackFromTus(new Error("000 No connection"))).toBe(false);
  });
});

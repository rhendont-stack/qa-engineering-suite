import { describe, expect, it } from "vitest";

// Counts attempts across retries of the same test within one run.
let flakyAttempts = 0;

describe("mixed outcomes", () => {
  it("passes on addition", () => {
    expect(1 + 1).toBe(2);
  });

  it("passes on string length", () => {
    expect("vitest".length).toBe(6);
  });

  it("fails an assertion", () => {
    expect(2 + 2).toBe(5);
  });

  it.skip("is skipped", () => {
    expect(true).toBe(false);
  });

  it("fails first and passes on retry", { retry: 1 }, () => {
    flakyAttempts += 1;
    expect(flakyAttempts).toBeGreaterThan(1);
  });
});

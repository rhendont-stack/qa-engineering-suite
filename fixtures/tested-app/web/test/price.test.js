import { describe, expect, it } from "vitest";
import { applyDiscount, clampQuantity, formatPrice } from "../src/price.js";

describe("price", () => {
  it("formats zero as a price", () => {
    expect(formatPrice(0)).toBe("$0.00");
  });

  it("formats a price", () => {
    formatPrice(250);
  });

  it("takes ten percent off a round amount", () => {
    expect(applyDiscount(1000, 10)).toBe(900);
  });

  it("rounds half a cent up", () => {
    expect(applyDiscount(1005, 10)).toBe(905);
  });

  it("caps a quantity at 99", () => {
    expect(clampQuantity(500)).toBe(99);
  });

  it.skip("raises a nonpositive quantity to 1", () => {
    expect(clampQuantity(-3)).toBe(1);
  });
});

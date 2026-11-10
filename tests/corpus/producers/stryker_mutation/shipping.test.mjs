import { expect, it } from "vitest";
import { shippingCost } from "./lib/shipping.mjs";

it("a light parcel costs the base rate", () => {
  expect(shippingCost(2)).toBe(5.0);
});

it("a heavy parcel pays per extra kilo", () => {
  expect(shippingCost(14)).toBe(7.0);
});

it("express doubles the cost", () => {
  expect(shippingCost(2, true)).toBe(10.0);
});

it("a weight of zero or less is refused", () => {
  expect(() => shippingCost(0)).toThrow("weight must be positive");
  expect(() => shippingCost(-1)).toThrow("weight must be positive");
});

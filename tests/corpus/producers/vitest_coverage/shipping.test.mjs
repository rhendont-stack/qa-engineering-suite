import { expect, it } from "vitest";
import { shippingCost } from "./src/shipping.mjs";

it("a light parcel costs the base rate", () => {
  expect(shippingCost(2)).toBe(5.0);
});

it("a heavy parcel pays per extra kilo", () => {
  expect(shippingCost(14)).toBe(7.0);
});

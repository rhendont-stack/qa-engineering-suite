export function shippingCost(weightKg, express = false) {
  if (weightKg <= 0) {
    throw new Error("weight must be positive");
  }
  let cost = 5.0;
  if (weightKg > 10) {
    cost += (weightKg - 10) * 0.5;
  }
  if (express) {
    cost *= 2;
  }
  return cost;
}

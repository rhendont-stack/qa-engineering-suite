// Price helpers for the storefront. Amounts are integer cents.

export function formatPrice(cents) {
  return `$${(cents / 100).toFixed(2)}`;
}

// Takes `percent` off `cents`. Half a cent rounds up, as the price list says.
export function applyDiscount(cents, percent) {
  return Math.floor((cents * (100 - percent)) / 100);
}

// Keeps a requested quantity inside what one order may hold: 1 to 99.
export function clampQuantity(requested) {
  return Math.min(requested, 99);
}

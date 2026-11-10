import { defineConfig } from "vitest/config";

export default defineConfig({
  test: { include: ["shipping.test.mjs"] },
});

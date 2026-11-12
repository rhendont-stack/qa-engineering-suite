import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["shipping.test.mjs"],
    coverage: {
      provider: "v8",
      include: ["src/**"],
      reporter: ["text", "lcovonly", "cobertura"],
      reportsDirectory: "coverage",
    },
  },
});

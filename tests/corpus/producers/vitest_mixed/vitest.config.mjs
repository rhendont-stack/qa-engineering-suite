import { defineConfig } from "vitest/config";

// Reporters are chosen on the command line (see generate.py), which overrides any list set here.
export default defineConfig({
  test: {
    include: ["mixed.test.mjs"],
  },
});

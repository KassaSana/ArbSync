import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    coverage: {
      provider: "v8",
      // Count every source file, not only the ones some test happens to import;
      // otherwise an untested component is invisible rather than at 0%.
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/**/*.test.{ts,tsx}", "src/test/**", "src/main.tsx"],
      reporter: ["text-summary", "text"],
      thresholds: {
        statements: 75,
        lines: 75,
      },
    },
  },
});

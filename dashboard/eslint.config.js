import tseslint from "@typescript-eslint/eslint-plugin";
import tsParser from "@typescript-eslint/parser";
import reactHooks from "eslint-plugin-react-hooks";

export default [
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        ecmaVersion: "latest",
        sourceType: "module",
        ecmaFeatures: { jsx: true },
        project: "./tsconfig.json",
        tsconfigRootDir: import.meta.dirname,
      },
      globals: {
        document: "readonly",
        window: "readonly",
        WebSocket: "readonly",
        MessageEvent: "readonly",
        fetch: "readonly",
        setInterval: "readonly",
        clearInterval: "readonly",
      },
    },
    plugins: {
      "@typescript-eslint": tseslint,
      "react-hooks": reactHooks,
    },
    rules: {
      ...tseslint.configs["recommended-type-checked"].rules,
      ...reactHooks.configs.recommended.rules,
    },
  },
  {
    // Test doubles implement async interfaces without awaiting anything.
    files: ["src/**/*.test.{ts,tsx}"],
    rules: {
      "@typescript-eslint/require-await": "off",
    },
  },
];

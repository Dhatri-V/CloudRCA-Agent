import js from "@eslint/js";

export default [
  {
    ignores: ["coverage/**", "dist/**"],
  },
  js.configs.recommended,
  {
    files: ["**/*.js"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
    },
  },
];

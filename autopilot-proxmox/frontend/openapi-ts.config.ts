import { defineConfig } from "@hey-api/openapi-ts";

export default defineConfig({
  input: "src/generated/openapi.json",
  output: "src/generated/client",
  plugins: [
    {
      name: "@hey-api/typescript",
      comments: true,
      enums: "javascript"
    }
  ]
});

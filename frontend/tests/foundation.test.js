import { describe, expect, it } from "vitest";

import { profiles } from "../src/foundation.js";

describe("frontend foundation", () => {
  it("shares the supported application profiles", () => {
    expect(profiles).toEqual(["development", "test", "demo"]);
  });
});

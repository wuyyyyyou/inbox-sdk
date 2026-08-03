import { describe, expect, it } from "vitest";
import { normalizeLocale } from "./locale";

describe("normalizeLocale", () => {
  it("accepts english and simplified chinese tags", () => {
    expect(normalizeLocale("en")).toBe("en-US");
    expect(normalizeLocale("en-US")).toBe("en-US");
    expect(normalizeLocale("zh")).toBe("zh-CN");
    expect(normalizeLocale("zh-CN")).toBe("zh-CN");
    expect(normalizeLocale("zh_Hans")).toBe("zh-CN");
  });

  it("rejects traditional chinese and unknown tags in v1", () => {
    expect(normalizeLocale("zh-TW")).toBeNull();
    expect(normalizeLocale("zh-HK")).toBeNull();
    expect(normalizeLocale("fr")).toBeNull();
    expect(normalizeLocale("")).toBeNull();
  });
});

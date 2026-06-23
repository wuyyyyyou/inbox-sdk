import { describe, expect, it } from "vitest";
import type { FrontendCard } from "../../types/mail";
import { cardCategory, filteredCards, isMainCard, lowerCards, mainCards, resolvedCards, visibleCards } from "./cardHelpers";

const card = (overrides: Partial<FrontendCard>): FrontendCard => ({
  id: overrides.id || "card_1",
  title: "Card",
  status: "pending",
  ...overrides,
});

describe("card helpers", () => {
  it("uses userAction as the primary category signal", () => {
    expect(cardCategory(card({ userAction: "reply", item_type: "account_notice" }))).toBe("reply");
    expect(cardCategory(card({ userAction: "review" }))).toBe("review");
    expect(cardCategory(card({ userAction: "cleanup" }))).toBe("cleanup");
  });

  it("falls back to cleanup bundle and item type mapping", () => {
    expect(cardCategory(card({ cardType: "cleanup_bundle" }))).toBe("cleanup");
    expect(cardCategory(card({ item_type: "reply_required" }))).toBe("reply");
    expect(cardCategory(card({ item_type: "security_risk" }))).toBe("review");
  });

  it("separates visible and resolved cards", () => {
    const cards = [card({ id: "a" }), card({ id: "b", status: "resolved" }), card({ id: "c", status: "snoozed" })];
    expect(visibleCards(cards).map((c) => c.id)).toEqual(["a"]);
    expect(resolvedCards(cards).map((c) => c.id)).toEqual(["b", "c"]);
  });

  it("hides replied cards from active lists", () => {
    const cards = [
      card({ id: "pending", status: "pending" }),
      card({ id: "sent", status: "resolved", resolution: "replied" }),
      card({ id: "gmail", status: "resolved", resolution: "replied_in_gmail" }),
    ];
    expect(visibleCards(cards).map((c) => c.id)).toEqual(["pending"]);
  });

  it("separates main and lower cards", () => {
    const cards = [
      card({ id: "main", displaySection: "main" }),
      card({ id: "lower", displaySection: "lower" }),
      card({ id: "high", priority: "high" }),
    ];
    expect(isMainCard(cards[0])).toBe(true);
    expect(mainCards(cards).map((c) => c.id)).toEqual(["main", "high"]);
    expect(lowerCards(cards).map((c) => c.id)).toEqual(["lower"]);
  });

  it("filters by category", () => {
    const cards = [card({ id: "reply", userAction: "reply" }), card({ id: "review", userAction: "review" })];
    expect(filteredCards(cards, "reply").map((c) => c.id)).toEqual(["reply"]);
    expect(filteredCards(cards, "all")).toHaveLength(2);
  });
});

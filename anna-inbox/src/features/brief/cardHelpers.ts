import type { AppState, FrontendCard, ResultFilter } from "../../types/mail";

const ITEM_TYPE_CATEGORY: Record<string, ResultFilter> = {
  reply_required: "reply",
  confirmation_required: "reply",
  business_or_creator_thread: "reply",
  security_risk: "review",
  billing_or_subscription: "review",
  account_notice: "review",
  low_value_cleanup: "cleanup",
};

export function cardCategory(card: FrontendCard): ResultFilter {
  if (card.userAction === "reply") return "reply";
  if (card.userAction === "review") return "review";
  if (card.userAction === "cleanup") return "cleanup";
  if (card.cardType === "cleanup_bundle") return "cleanup";
  if (!isMainCard(card)) return "cleanup";
  if (card.item_type && ITEM_TYPE_CATEGORY[card.item_type]) return ITEM_TYPE_CATEGORY[card.item_type];
  return "review";
}

export function cardCategoryLabel(card: FrontendCard): string {
  const category = cardCategory(card);
  if (category === "reply") return "Needs reply";
  if (category === "review") return "Needs review";
  if (category === "cleanup") return "Cleanup";
  return "Needs review";
}

export function isMainCard(card: FrontendCard): boolean {
  if (card.displaySection === "lower") return false;
  if (card.displaySection === "main") return true;
  const priority = String(card.priority || "").toLowerCase();
  if (priority === "critical" || priority === "high" || priority === "medium") return true;
  if (priority === "low" || priority === "ignore") return false;
  const label = String(card.label || "").toLowerCase();
  return card.status !== "snoozed" && card.status !== "resolved" && card.status !== "dismissed" &&
    !label.includes("safe") && !label.includes("cleanup") && !label.includes("low");
}

export function visibleCards(cards: FrontendCard[]): FrontendCard[] {
  return cards.filter((card) => {
    if (!card.status || card.status === "pending") return true;
    return false;
  });
}

export function resolvedCards(cards: FrontendCard[]): FrontendCard[] {
  return cards.filter((card) => card.status && card.status !== "pending");
}

export function mainCards(cards: FrontendCard[]): FrontendCard[] {
  return visibleCards(cards).filter(isMainCard);
}

export function lowerCards(cards: FrontendCard[]): FrontendCard[] {
  return visibleCards(cards).filter((card) => !isMainCard(card));
}

export function filteredCards(cards: FrontendCard[], category: ResultFilter): FrontendCard[] {
  const visible = visibleCards(cards);
  if (category === "all") return visible;
  return visible.filter((card) => cardCategory(card) === category);
}

export function currentDisplayList(state: Pick<AppState, "cards" | "resultFilter">): FrontendCard[] {
  if (state.resultFilter === "all") return [...mainCards(state.cards), ...lowerCards(state.cards)];
  return filteredCards(state.cards, state.resultFilter);
}

export function nextCardId(state: Pick<AppState, "selectedCard" | "cards" | "resultFilter">): string | null {
  if (!state.selectedCard) return null;
  const list = currentDisplayList(state);
  const currentKey = state.selectedCard.uiKey || state.selectedCard.id;
  const idx = list.findIndex((card) => (card.uiKey || card.id) === currentKey);
  if (idx < 0 || idx >= list.length - 1) return null;
  return list[idx + 1]?.uiKey || list[idx + 1]?.id || null;
}

export function primaryAction(card: FrontendCard): { id: string; label: string } {
  const actions = Array.isArray(card.actions) ? card.actions : [];
  const primary = actions.find((action) => action.primary)
    || actions.find((action) => action.id !== "view")
    || { id: "handle", label: "Handle", buttonLabel: "" };
  return {
    id: primary.id || "handle",
    label: "Handle",
  };
}

export function normalizeRecommendation(text: unknown): string {
  const value = String(text || "").trim();
  if (!value) return "Suggested: Review this item.";
  return value.toLowerCase().startsWith("suggested:") ? value : `Suggested: ${value}`;
}

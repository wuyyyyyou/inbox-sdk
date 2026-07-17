import { describe, expect, it, vi } from "vitest";
import { PendingSendScheduler } from "./pendingSend";

describe("PendingSendScheduler", () => {
  it("counts down and cancels the send when Undo is selected", () => {
    vi.useFakeTimers();
    const showToast = vi.fn();
    const onSend = vi.fn(async () => undefined);
    const onUndo = vi.fn();
    const scheduler = new PendingSendScheduler({
      showToast,
      setTimeout: globalThis.setTimeout,
      clearTimeout: globalThis.clearTimeout,
      setInterval: globalThis.setInterval,
      clearInterval: globalThis.clearInterval,
    });

    expect(scheduler.schedule({
      countdownMessage: (seconds) => `Will send in ${seconds} seconds.`,
      sendingMessage: "Sending...",
      onUndo,
      onSend,
      onError: vi.fn(),
    })).toBe(true);
    expect(showToast).toHaveBeenLastCalledWith(
      "Will send in 10 seconds.",
      expect.objectContaining({ actionLabel: "Undo" }),
    );

    const undo = showToast.mock.calls.at(-1)?.[1]?.onAction;
    undo?.();
    vi.advanceTimersByTime(10_000);

    expect(onUndo).toHaveBeenCalledOnce();
    expect(onSend).not.toHaveBeenCalled();
    vi.useRealTimers();
  });

  it("blocks a second send group until the first group has finished", async () => {
    vi.useFakeTimers();
    const showToast = vi.fn();
    let finishSend: (() => void) | undefined;
    const scheduler = new PendingSendScheduler({
      showToast,
      setTimeout: globalThis.setTimeout,
      clearTimeout: globalThis.clearTimeout,
      setInterval: globalThis.setInterval,
      clearInterval: globalThis.clearInterval,
    });
    const options = {
      countdownMessage: (seconds: number) => `Will send in ${seconds} seconds.`,
      sendingMessage: "Sending...",
      onUndo: vi.fn(),
      onSend: () => new Promise<void>((resolve) => {
        finishSend = resolve;
      }),
      onError: vi.fn(),
    };

    expect(scheduler.schedule(options)).toBe(true);
    expect(scheduler.schedule(options)).toBe(false);
    expect(showToast).toHaveBeenLastCalledWith(
      "A send is already pending. Undo it or wait for it to finish.",
    );

    vi.advanceTimersByTime(10_000);
    finishSend?.();
    await vi.runAllTimersAsync();
    expect(scheduler.schedule(options)).toBe(true);
    scheduler.dispose();
    vi.useRealTimers();
  });

  it("clears pending state when timer setup fails", () => {
    const showToast = vi.fn();
    const onError = vi.fn();
    const scheduler = new PendingSendScheduler({
      showToast,
      setTimeout: (() => {
        throw new TypeError("Illegal invocation");
      }) as typeof globalThis.setTimeout,
      clearTimeout: globalThis.clearTimeout,
      setInterval: globalThis.setInterval,
      clearInterval: globalThis.clearInterval,
    });
    const options = {
      countdownMessage: () => "Will send soon.",
      sendingMessage: "Sending...",
      onUndo: vi.fn(),
      onSend: vi.fn(async () => undefined),
      onError,
    };

    expect(scheduler.schedule(options)).toBe(false);
    expect(onError).toHaveBeenCalledWith(expect.any(TypeError));
    expect(scheduler.schedule(options)).toBe(false);
    expect(onError).toHaveBeenCalledTimes(2);
  });
});

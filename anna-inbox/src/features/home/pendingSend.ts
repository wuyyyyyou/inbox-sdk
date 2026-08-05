export type PendingSendToastOptions = {
  actionLabel?: string;
  onAction?: () => void;
  durationMs?: number;
};

export type PendingSendOptions = {
  countdownMessage: (seconds: number) => string;
  sendingMessage: string;
  pendingMessage?: string;
  undoLabel?: string;
  onUndo: () => void;
  onSend: () => Promise<void>;
  onError: (reason: unknown) => void;
};

type PendingSendDependencies = {
  showToast: (message: string, options?: PendingSendToastOptions) => void;
  setTimeout: typeof window.setTimeout;
  clearTimeout: typeof window.clearTimeout;
  setInterval: typeof window.setInterval;
  clearInterval: typeof window.clearInterval;
  now?: () => number;
};

const SEND_DELAY_MS = 10_000;

export class PendingSendScheduler {
  private timer: number | null = null;
  private countdown: number | null = null;
  private active = false;

  constructor(private readonly dependencies: PendingSendDependencies) {}

  schedule(options: PendingSendOptions): boolean {
    if (this.active) {
      this.dependencies.showToast(
        options.pendingMessage || "A send is already pending. Undo it or wait for it to finish.",
      );
      return false;
    }

    this.active = true;
    const deadline = (this.dependencies.now?.() ?? Date.now()) + SEND_DELAY_MS;
    const clearCountdown = () => {
      if (this.countdown !== null) {
        this.dependencies.clearInterval(this.countdown);
        this.countdown = null;
      }
    };
    const undo = () => {
      if (!this.active) return;
      if (this.timer !== null) {
        this.dependencies.clearTimeout(this.timer);
        this.timer = null;
      }
      clearCountdown();
      this.active = false;
      options.onUndo();
    };
    const updateCountdown = () => {
      const seconds = Math.max(
        1,
        Math.ceil((deadline - (this.dependencies.now?.() ?? Date.now())) / 1000),
      );
      this.dependencies.showToast(options.countdownMessage(seconds), {
        actionLabel: options.undoLabel || "Undo",
        onAction: undo,
        durationMs: 1_100,
      });
    };

    try {
      updateCountdown();
      this.countdown = this.dependencies.setInterval(updateCountdown, 1_000);
      this.timer = this.dependencies.setTimeout(() => {
        this.timer = null;
        clearCountdown();
        this.dependencies.showToast(options.sendingMessage, { durationMs: 30_000 });
        void options
          .onSend()
          .catch(options.onError)
          .finally(() => {
            this.active = false;
          });
      }, SEND_DELAY_MS);
    } catch (reason) {
      if (this.timer !== null) this.dependencies.clearTimeout(this.timer);
      clearCountdown();
      this.timer = null;
      this.active = false;
      options.onError(reason);
      return false;
    }
    return true;
  }

  dispose() {
    if (this.timer !== null) this.dependencies.clearTimeout(this.timer);
    if (this.countdown !== null) this.dependencies.clearInterval(this.countdown);
    this.timer = null;
    this.countdown = null;
    this.active = false;
  }
}

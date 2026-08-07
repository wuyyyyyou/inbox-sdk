const ATTACHMENT_FETCH_TIMEOUT_MS = 30_000;
const ATTACHMENT_FETCH_MAX_ATTEMPTS = 2;
const ATTACHMENT_RETRY_DELAY_MS = 100;

function isRetryableAttachmentFailure(error: unknown) {
  if (error instanceof TypeError) return true;
  if (error instanceof DOMException && error.name === "TimeoutError") return true;
  // 浏览器网络错误如 net::ERR_CONNECTION_CLOSED / ERR_CONNECTION_REFUSED 使用下划线，
  // 也兼容 "connection closed" 空格变体；仅 5xx 属于可重试的 HTTP 状态。
  return /connection[_\s]*(?:closed|reset|refused)|network|fetch failed with 5\d\d/i.test(
    error instanceof Error ? error.message : String(error),
  );
}

function isCallerAbort(error: unknown, signal?: AbortSignal) {
  return Boolean(signal?.aborted) || (error instanceof DOMException && error.name === "AbortError");
}

function waitForRetry(signal?: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason ?? new DOMException("The operation was aborted.", "AbortError"));
      return;
    }
    let settled = false;
    let timer = 0;
    const onAbort = () => {
      if (settled) return;
      settled = true;
      globalThis.clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      reject(signal?.reason ?? new DOMException("The operation was aborted.", "AbortError"));
    };
    timer = globalThis.setTimeout(() => {
      if (settled) return;
      settled = true;
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ATTACHMENT_RETRY_DELAY_MS);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/** Fetches attachment bytes with timeout, caller cancellation, and one bounded retry. */
export async function fetchAttachmentBlob(url: string, signal?: AbortSignal): Promise<Blob> {
  let lastError: unknown;
  for (let attempt = 0; attempt < ATTACHMENT_FETCH_MAX_ATTEMPTS; attempt += 1) {
    if (signal?.aborted) throw signal.reason ?? new DOMException("The operation was aborted.", "AbortError");
    const controller = new AbortController();
    const timeout = globalThis.setTimeout(() => controller.abort(new DOMException("Attachment fetch timed out.", "TimeoutError")), ATTACHMENT_FETCH_TIMEOUT_MS);
    const onAbort = () => controller.abort(signal?.reason ?? new DOMException("The operation was aborted.", "AbortError"));
    signal?.addEventListener("abort", onAbort, { once: true });
    try {
      const response = await fetch(url, { cache: "no-store", signal: controller.signal });
      if (!response.ok) {
        throw new Error(`Attachment fetch failed with ${response.status}`);
      }
      return await response.blob();
    } catch (error) {
      lastError = error;
      if (isCallerAbort(error, signal) || !isRetryableAttachmentFailure(error) || attempt + 1 >= ATTACHMENT_FETCH_MAX_ATTEMPTS) {
        throw error;
      }
      await waitForRetry(signal);
    } finally {
      globalThis.clearTimeout(timeout);
      signal?.removeEventListener("abort", onAbort);
    }
  }
  throw lastError;
}

export async function triggerBrowserDownload(
  url: string,
  filename: string,
  ownerDocument: Document = document,
  signal?: AbortSignal,
): Promise<void> {
  const isBlobUrl = url.startsWith("blob:");
  let downloadUrl = url;
  let materializedUrl = "";
  if (!isBlobUrl) {
    const blob = await fetchAttachmentBlob(url, signal);
    materializedUrl = URL.createObjectURL(blob);
    downloadUrl = materializedUrl;
  }
  const link = ownerDocument.createElement("a");
  link.href = downloadUrl;
  link.download = filename || "attachment";
  link.rel = "noopener noreferrer";
  link.style.display = "none";
  ownerDocument.body.appendChild(link);
  try {
    link.click();
  } finally {
    link.remove();
    if (materializedUrl) {
      const schedule = ownerDocument.defaultView?.setTimeout || globalThis.setTimeout;
      schedule(() => URL.revokeObjectURL(materializedUrl), 30_000);
    }
  }
}

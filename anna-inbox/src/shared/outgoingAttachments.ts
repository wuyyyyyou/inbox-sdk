import type { OutgoingAttachmentMeta } from "../types/mail";

/** 个人 Gmail：全部附件合计 ≤ 25MB */
export const OUTGOING_ATTACHMENT_TOTAL_MAX_BYTES = 25 * 1024 * 1024;

/** 对齐 Gmail 常见可执行/脚本拦截扩展名 */
const BLOCKED_EXTENSIONS = new Set([
  "ade", "adp", "apk", "appx", "appxbundle", "bat", "cab", "chm", "cmd", "com",
  "cpl", "diagcab", "diagcfg", "diagpack", "dll", "dmg", "ex", "ex_", "exe",
  "hta", "img", "ins", "iso", "isp", "jar", "jnlp", "js", "jse", "lib", "lnk",
  "mde", "mjs", "msc", "msi", "msix", "msixbundle", "msp", "mst", "nsh", "pif",
  "ps1", "scr", "sct", "shb", "sys", "vb", "vbe", "vbs", "vhd", "vxd", "wsc",
  "wsf", "wsh", "xll",
]);

export function attachmentExtension(filename: string) {
  const name = String(filename || "").trim().toLowerCase();
  if (!name.includes(".")) return "";
  return name.split(".").pop() || "";
}

export function isBlockedOutgoingFilename(filename: string) {
  const ext = attachmentExtension(filename);
  return Boolean(ext && BLOCKED_EXTENSIONS.has(ext));
}

export function isImageOutgoingAttachment(item: Pick<OutgoingAttachmentMeta, "filename" | "mime_type">) {
  const mime = String(item.mime_type || "").toLowerCase();
  if (mime.startsWith("image/")) return true;
  const ext = attachmentExtension(item.filename);
  return ["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"].includes(ext);
}

export function totalOutgoingAttachmentBytes(items: OutgoingAttachmentMeta[]) {
  return items.reduce((sum, item) => sum + Math.max(0, Number(item.size) || 0), 0);
}

export function toPersistedOutgoingAttachments(items: OutgoingAttachmentMeta[]) {
  return items
    .filter((item) => item.status === "ready" && item.storage_key)
    .map((item) => ({
      id: item.id,
      filename: item.filename,
      mime_type: item.mime_type,
      size: item.size,
      storage_key: item.storage_key,
    }));
}

export function formatOutgoingAttachmentSize(bytes: number) {
  const value = Math.max(0, Number(bytes) || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export async function putFileToUploadUrl(
  uploadUrl: string,
  file: File,
  onProgress?: (ratio: number) => void,
): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", uploadUrl, true);
    xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) return;
      onProgress?.(Math.min(1, event.loaded / Math.max(1, event.total)));
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(1);
        resolve();
        return;
      }
      reject(new Error(`Upload failed (${xhr.status})`));
    };
    xhr.onerror = () => reject(new Error("Upload failed"));
    xhr.onabort = () => reject(new Error("Upload aborted"));
    xhr.send(file);
  });
}

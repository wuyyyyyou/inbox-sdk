import type {
  InboxMessage,
  InboxThreadPagePayload,
  MailAttachmentMeta,
  MailboxInfo,
} from "../types/mail";

const DEVICE_DB_NAME = "anna-inbox-device";
const DEVICE_DB_VERSION = 1;
const MAILBOX_DB_VERSION = 1;
const MESSAGE_BODY_TTL_MS = 7 * 24 * 60 * 60 * 1000;
const META_SELECTED_MAILBOX = "selected_mailbox";
const META_LAST_USED_MAILBOXES = "last_used_mailboxes";
const MAIL_FLAGS_KEY = "current";
const CONTACT_AVATARS_KEY = "current";

export type BrowserMailUiFlags = {
  todos: string[];
  snoozed: string[];
  snoozedUntil?: Record<string, string>;
  done: string[];
  doneRemoved: string[];
  drafts: string[];
  saved: Record<string, InboxMessage>;
};

export type ContactAvatarCache = {
  avatars: Record<string, string>;
  missing: string[];
  avatarsUpdatedAt: number;
  missingUpdatedAt: number;
};

export type CachedMessageBody = {
  key: string;
  mailbox: string;
  message_id: string;
  thread_id: string;
  internal_date: string;
  subject: string;
  from: string;
  to: string;
  date: string;
  body_text: string;
  body_html?: string;
  body_truncated?: boolean;
  attachments: MailAttachmentMeta[];
  updated_at: number;
  expires_at: number;
};

export type CachedThreadPage = {
  key: string;
  mailbox: string;
  thread_id: string;
  latest_message_id: string;
  updated_at: number;
  expires_at: number;
  page: InboxThreadPagePayload;
};

function hasIndexedDb() {
  return typeof indexedDB !== "undefined";
}

function openDb(
  name: string,
  version: number,
  upgrade: (db: IDBDatabase) => void,
): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    if (!hasIndexedDb()) {
      reject(new Error("IndexedDB is unavailable."));
      return;
    }
    const request = indexedDB.open(name, version);
    request.onupgradeneeded = () => upgrade(request.result);
    request.onerror = () => reject(request.error || new Error(`Failed to open ${name}.`));
    request.onsuccess = () => resolve(request.result);
  });
}

function requestToPromise<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onerror = () => reject(request.error || new Error("IndexedDB request failed."));
    request.onsuccess = () => resolve(request.result);
  });
}

async function withStore<T>(
  db: IDBDatabase,
  storeName: string,
  mode: IDBTransactionMode,
  run: (store: IDBObjectStore) => IDBRequest<T>,
): Promise<T> {
  try {
    const tx = db.transaction(storeName, mode);
    const txDone = new Promise<void>((resolve, reject) => {
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error || new Error("IndexedDB transaction failed."));
      tx.onabort = () => reject(tx.error || new Error("IndexedDB transaction aborted."));
    });
    const result = await requestToPromise(run(tx.objectStore(storeName)));
    await txDone;
    return result;
  } finally {
    db.close();
  }
}

function mailboxHash(mailbox: string): string {
  const value = mailbox.trim().toLowerCase();
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

function mailboxDbName(mailbox: string) {
  return `anna-inbox-mailbox-${mailboxHash(mailbox)}`;
}

function appMetaKey(key: string) {
  return key;
}

function messageBodyKey(messageId: string, internalDate?: string | null) {
  return `${messageId}::${internalDate || ""}`;
}

function threadPageKey(threadId: string, latestMessageId?: string | null) {
  return `${threadId}::${latestMessageId || ""}`;
}

function openDeviceDb() {
  return openDb(DEVICE_DB_NAME, DEVICE_DB_VERSION, (db) => {
    if (!db.objectStoreNames.contains("app_meta")) db.createObjectStore("app_meta");
    if (!db.objectStoreNames.contains("mailboxes")) db.createObjectStore("mailboxes", { keyPath: "email" });
    if (!db.objectStoreNames.contains("permissions")) db.createObjectStore("permissions");
  });
}

function openMailboxDb(mailbox: string) {
  return openDb(mailboxDbName(mailbox), MAILBOX_DB_VERSION, (db) => {
    if (!db.objectStoreNames.contains("message_bodies")) db.createObjectStore("message_bodies", { keyPath: "key" });
    if (!db.objectStoreNames.contains("thread_pages")) db.createObjectStore("thread_pages", { keyPath: "key" });
    if (!db.objectStoreNames.contains("mail_flags")) db.createObjectStore("mail_flags", { keyPath: "key" });
    if (!db.objectStoreNames.contains("contact_avatars")) db.createObjectStore("contact_avatars", { keyPath: "key" });
    if (!db.objectStoreNames.contains("draft_mirror")) db.createObjectStore("draft_mirror", { keyPath: "key" });
  });
}

async function withMailboxStore<T>(
  mailbox: string,
  storeName: string,
  mode: IDBTransactionMode,
  run: (store: IDBObjectStore) => IDBRequest<T>,
): Promise<T | null> {
  const normalized = mailbox.trim().toLowerCase();
  if (!normalized) return null;
  try {
    const db = await openMailboxDb(normalized);
    return await withStore<T>(db, storeName, mode, run);
  } catch {
    return null;
  }
}

async function withDeviceStore<T>(
  storeName: string,
  mode: IDBTransactionMode,
  run: (store: IDBObjectStore) => IDBRequest<T>,
): Promise<T | null> {
  try {
    const db = await openDeviceDb();
    return await withStore<T>(db, storeName, mode, run);
  } catch {
    return null;
  }
}

export async function getSelectedMailbox(): Promise<string> {
  const value = await withDeviceStore<string>("app_meta", "readonly", (store) => store.get(appMetaKey(META_SELECTED_MAILBOX)));
  return typeof value === "string" ? value : "";
}

export async function setSelectedMailbox(mailbox: string): Promise<void> {
  const normalized = mailbox.trim().toLowerCase();
  if (!normalized || normalized === "all") return;
  await withDeviceStore("app_meta", "readwrite", (store) => store.put(normalized, appMetaKey(META_SELECTED_MAILBOX)));
  const lastUsed = await getLastUsedMailboxes();
  await withDeviceStore("app_meta", "readwrite", (store) => store.put(
    [normalized, ...lastUsed.filter((item) => item !== normalized)].slice(0, 8),
    appMetaKey(META_LAST_USED_MAILBOXES),
  ));
}

export async function getLastUsedMailboxes(): Promise<string[]> {
  const value = await withDeviceStore<string[]>("app_meta", "readonly", (store) => store.get(appMetaKey(META_LAST_USED_MAILBOXES)));
  return Array.isArray(value) ? value.filter((item) => typeof item === "string") : [];
}

export async function migrateSelectedMailboxFromLocalStorage(key: string): Promise<string> {
  const stored = await getSelectedMailbox();
  if (stored) return stored;
  if (typeof window === "undefined") return "";
  let legacy = "";
  try {
    legacy = (window.localStorage.getItem(key) || "").trim().toLowerCase();
  } catch {
    legacy = "";
  }
  if (!legacy) return "";
  await setSelectedMailbox(legacy);
  try {
    window.localStorage.removeItem(key);
  } catch {
    // Best-effort migration only.
  }
  return legacy;
}

export async function cacheMailboxes(mailboxes: MailboxInfo[]): Promise<void> {
  if (!mailboxes.length) return;
  try {
    const db = await openDeviceDb();
    const tx = db.transaction("mailboxes", "readwrite");
    const store = tx.objectStore("mailboxes");
    for (const item of mailboxes) {
      const email = String(item.email || "").trim().toLowerCase();
      if (!email) continue;
      store.put({
        email,
        display_name: item.display_name || "",
        avatar_url: item.avatar_url || "",
        provider: item.provider || "gmail",
        selected: item.selected !== false,
        authorized: item.authorized !== false,
        auth_source: item.auth_source || "",
      });
    }
    await new Promise<void>((resolve, reject) => {
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error || new Error("IndexedDB transaction failed."));
      tx.onabort = () => reject(tx.error || new Error("IndexedDB transaction aborted."));
    });
    db.close();
  } catch {
    // Device mailbox cache is optional.
  }
}

export async function getMailFlags(mailbox: string, legacyKey: string): Promise<BrowserMailUiFlags | null> {
  const cached = await withMailboxStore<{ value?: BrowserMailUiFlags }>(mailbox, "mail_flags", "readonly", (store) => store.get(MAIL_FLAGS_KEY));
  if (cached?.value) return normalizeFlags(cached.value);
  const legacy = readLegacyJson<BrowserMailUiFlags>(legacyKey);
  if (legacy) {
    const normalized = normalizeFlags(legacy);
    await setMailFlags(mailbox, normalized);
    removeLegacyKey(legacyKey);
    return normalized;
  }
  return null;
}

export async function setMailFlags(mailbox: string, flags: BrowserMailUiFlags): Promise<void> {
  await withMailboxStore(mailbox, "mail_flags", "readwrite", (store) => store.put({
    key: MAIL_FLAGS_KEY,
    value: normalizeFlags(flags),
    updated_at: Date.now(),
  }));
}

export async function getContactAvatarCache(mailbox: string, legacyKey: string): Promise<ContactAvatarCache | null> {
  const cached = await withMailboxStore<{ value?: ContactAvatarCache }>(mailbox, "contact_avatars", "readonly", (store) => store.get(CONTACT_AVATARS_KEY));
  if (cached?.value) return normalizeAvatarCache(cached.value);
  const legacy = readLegacyJson<ContactAvatarCache>(legacyKey);
  if (legacy) {
    const normalized = normalizeAvatarCache(legacy);
    await setContactAvatarCache(mailbox, normalized);
    removeLegacyKey(legacyKey);
    return normalized;
  }
  return null;
}

export async function setContactAvatarCache(mailbox: string, cache: ContactAvatarCache): Promise<void> {
  await withMailboxStore(mailbox, "contact_avatars", "readwrite", (store) => store.put({
    key: CONTACT_AVATARS_KEY,
    value: normalizeAvatarCache(cache),
    updated_at: Date.now(),
  }));
}

export async function getCachedMessageBody(
  mailbox: string,
  messageId: string,
  internalDate?: string | null,
): Promise<CachedMessageBody | null> {
  const cached = await withMailboxStore<CachedMessageBody>(mailbox, "message_bodies", "readonly", (store) => store.get(messageBodyKey(messageId, internalDate)));
  if (!cached || Number(cached.expires_at || 0) <= Date.now()) return null;
  return cached;
}

export async function setCachedMessageBody(
  mailbox: string,
  message: InboxMessage,
  body: Pick<CachedMessageBody, "body_text"> & Partial<Pick<CachedMessageBody, "body_html" | "body_truncated" | "attachments">>,
): Promise<void> {
  const normalized = mailbox.trim().toLowerCase();
  const messageId = message.id;
  if (!normalized || !messageId) return;
  const now = Date.now();
  await withMailboxStore(normalized, "message_bodies", "readwrite", (store) => store.put({
    key: messageBodyKey(messageId, message.internal_date),
    mailbox: normalized,
    message_id: messageId,
    thread_id: message.thread_id || messageId,
    internal_date: message.internal_date || "",
    subject: message.subject || "",
    from: message.from || "",
    to: message.to || "",
    date: message.date || "",
    body_text: body.body_text || "",
    body_html: body.body_html || "",
    body_truncated: Boolean(body.body_truncated),
    attachments: body.attachments || [],
    updated_at: now,
    expires_at: now + MESSAGE_BODY_TTL_MS,
  } satisfies CachedMessageBody));
}

export async function getCachedThreadPage(
  mailbox: string,
  threadId: string,
  latestMessageId?: string | null,
): Promise<InboxThreadPagePayload | null> {
  const cached = await withMailboxStore<CachedThreadPage>(mailbox, "thread_pages", "readonly", (store) => store.get(threadPageKey(threadId, latestMessageId)));
  if (!cached || Number(cached.expires_at || 0) <= Date.now()) return null;
  return cached.page || null;
}

export async function setCachedThreadPage(mailbox: string, page: InboxThreadPagePayload): Promise<void> {
  const normalized = mailbox.trim().toLowerCase();
  if (!normalized || !page.thread_id || !page.latest_message_id) return;
  const now = Date.now();
  await withMailboxStore(normalized, "thread_pages", "readwrite", (store) => store.put({
    key: threadPageKey(page.thread_id, page.latest_message_id),
    mailbox: normalized,
    thread_id: page.thread_id,
    latest_message_id: page.latest_message_id,
    page,
    updated_at: now,
    expires_at: now + MESSAGE_BODY_TTL_MS,
  } satisfies CachedThreadPage));
}

export async function clearMailboxDatabase(mailbox: string): Promise<void> {
  const normalized = mailbox.trim().toLowerCase();
  if (!normalized || !hasIndexedDb()) return;
  await new Promise<void>((resolve) => {
    const request = indexedDB.deleteDatabase(mailboxDbName(normalized));
    request.onsuccess = () => resolve();
    request.onerror = () => resolve();
    request.onblocked = () => resolve();
  });
}

export async function clearMailboxCacheData(mailbox: string): Promise<void> {
  const normalized = mailbox.trim().toLowerCase();
  if (!normalized) return;
  try {
    const db = await openMailboxDb(normalized);
    const cacheStores = ["message_bodies", "thread_pages", "contact_avatars"]
      .filter((storeName) => db.objectStoreNames.contains(storeName));
    if (!cacheStores.length) {
      db.close();
      return;
    }
    const tx = db.transaction(cacheStores, "readwrite");
    for (const storeName of cacheStores) {
      tx.objectStore(storeName).clear();
    }
    await new Promise<void>((resolve, reject) => {
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error || new Error("IndexedDB transaction failed."));
      tx.onabort = () => reject(tx.error || new Error("IndexedDB transaction aborted."));
    });
    db.close();
  } catch {
    // Browser cache cleanup is best-effort; backend cache reset still proceeds.
  }
}

function readLegacyJson<T>(key: string): T | null {
  if (typeof window === "undefined") return null;
  try {
    const value = window.localStorage.getItem(key);
    return value ? JSON.parse(value) as T : null;
  } catch {
    return null;
  }
}

function removeLegacyKey(key: string) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(key);
  } catch {
    // Best effort only.
  }
}

function normalizeFlags(value: Partial<BrowserMailUiFlags> | null | undefined): BrowserMailUiFlags {
  return {
    todos: Array.isArray(value?.todos) ? value.todos : [],
    snoozed: Array.isArray(value?.snoozed) ? value.snoozed : [],
    snoozedUntil: value?.snoozedUntil && typeof value.snoozedUntil === "object" ? value.snoozedUntil : {},
    done: Array.isArray(value?.done) ? value.done : [],
    doneRemoved: Array.isArray(value?.doneRemoved) ? value.doneRemoved : [],
    drafts: Array.isArray(value?.drafts) ? value.drafts : [],
    saved: value?.saved && typeof value.saved === "object" ? value.saved : {},
  };
}

function normalizeAvatarCache(value: Partial<ContactAvatarCache> | null | undefined): ContactAvatarCache {
  const rawAvatars = value?.avatars && typeof value.avatars === "object" ? value.avatars : {};
  const avatars = Object.fromEntries(
    Object.entries(rawAvatars).filter(([, url]) => !/https?:\/\/(?:www\.)?gravatar\.com\//i.test(String(url || ""))),
  );
  return {
    avatars,
    missing: Array.isArray(value?.missing) ? value.missing : [],
    avatarsUpdatedAt: Number(value?.avatarsUpdatedAt || (value as { updatedAt?: number } | undefined)?.updatedAt || 0),
    missingUpdatedAt: Number(value?.missingUpdatedAt || 0),
  };
}

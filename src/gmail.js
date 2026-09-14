// Gmail via the REST API with OAuth 2.0 (web application client).
//
// The Client ID / Client Secret pasted into Settings identify the app. "Connect
// Gmail" sends the user through Google's consent screen; the token (with a
// refresh token) is stored in the D1 state table and refreshed automatically.

import { getState, setState, deleteState } from "./db.js";
import * as fixtures from "./fixtures.js";

export const SCOPES = "https://www.googleapis.com/auth/gmail.modify";
const AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth";
const TOKEN_URL = "https://oauth2.googleapis.com/token";
const API = "https://gmail.googleapis.com/gmail/v1/users/me";

export const SUPPORTED_MIME = {
  "application/pdf": ".pdf",
  "image/png": ".png",
  "image/jpeg": ".jpg",
  "image/webp": ".webp",
  "image/gif": ".gif",
};
export const EXT_TO_MIME = { ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp" };
export const MIN_IMAGE_BYTES = 20 * 1024; // anything smaller is almost certainly a logo or signature

export class GmailNotConnected extends Error {}

/** Gmail search wants label names with spaces and slashes as hyphens. */
export const searchLabel = (name) => String(name || "").trim().replace(/[\s/]+/g, "-");

// --------------------------------------------------------------------------- OAuth

export function authUrl(settings, redirectUri, state) {
  const params = new URLSearchParams({
    client_id: settings.google_client_id,
    redirect_uri: redirectUri,
    response_type: "code",
    scope: SCOPES,
    access_type: "offline",
    prompt: "consent",
    include_granted_scopes: "true",
    state,
  });
  return `${AUTH_URL}?${params}`;
}

async function tokenRequest(params) {
  const response = await fetch(TOKEN_URL, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(params),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(`${payload.error || "token_error"}: ${payload.error_description || response.status}`);
  return payload;
}

export async function exchangeCode(db, settings, code, redirectUri) {
  const payload = await tokenRequest({
    code,
    client_id: settings.google_client_id,
    client_secret: settings.google_client_secret,
    redirect_uri: redirectUri,
    grant_type: "authorization_code",
  });
  const token = {
    access_token: payload.access_token,
    refresh_token: payload.refresh_token || "",
    scope: payload.scope || "",
    expires_at: Date.now() + (payload.expires_in || 3600) * 1000,
  };
  await setState(db, "google_token", JSON.stringify(token));
  return token;
}

export async function loadToken(db) {
  const raw = await getState(db, "google_token");
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export async function clearToken(db) {
  await deleteState(db, "google_token");
}

export async function hasToken(env) {
  if (fixtures.enabled(env)) return true;
  return Boolean(await getState(env.DB, "google_token"));
}

async function accessToken(db, settings) {
  const token = await loadToken(db);
  if (!token || !token.access_token) throw new GmailNotConnected("Gmail is not connected. Open Settings and click Connect Gmail.");
  if (Date.now() < token.expires_at - 60 * 1000) return token.access_token;
  if (!token.refresh_token) throw new GmailNotConnected("The Gmail token has expired and cannot be refreshed. Connect Gmail again in Settings.");
  const payload = await tokenRequest({
    refresh_token: token.refresh_token,
    client_id: settings.google_client_id,
    client_secret: settings.google_client_secret,
    grant_type: "refresh_token",
  });
  const refreshed = { ...token, access_token: payload.access_token, expires_at: Date.now() + (payload.expires_in || 3600) * 1000 };
  await setState(db, "google_token", JSON.stringify(refreshed));
  return refreshed.access_token;
}

// --------------------------------------------------------------------------- Helpers

export function b64urlDecode(data) {
  const padded = String(data || "").replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(padded + "=".repeat((4 - (padded.length % 4)) % 4));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

const decoder = new TextDecoder("utf-8", { fatal: false });

function header(headers, name) {
  const lower = name.toLowerCase();
  for (const h of headers || []) if ((h.name || "").toLowerCase() === lower) return h.value || "";
  return "";
}

export function stripHtml(html) {
  let text = String(html).replace(/<(script|style)[^>]*>[\s\S]*?<\/\1>/gi, " ");
  text = text.replace(/<br\s*\/?>|<\/p>|<\/div>|<\/tr>/gi, "\n");
  text = text.replace(/<[^>]+>/g, " ");
  text = text
    .replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&#(\d+);/g, (_, n) => String.fromCharCode(Number(n)));
  return text.replace(/[ \t]+/g, " ").trim();
}

function walkParts(part, out) {
  out.push(part);
  for (const child of part.parts || []) walkParts(child, out);
}

export function senderAllowed(fromAddr, allowed) {
  const entries = String(allowed || "").replace(/\n/g, ",").split(",").map((e) => e.trim().toLowerCase()).filter(Boolean);
  if (!entries.length) return true;
  const from = String(fromAddr || "").toLowerCase();
  return entries.some((e) => from.includes(e));
}

// --------------------------------------------------------------------------- Client

/** A Gmail client bound to the stored token. In fixture mode a fake mailbox is returned. */
export function client(env, settings) {
  if (fixtures.enabled(env)) return fixtures.gmailClient(env);
  const db = env.DB;

  async function call(path, { method = "GET", body, query } = {}) {
    const token = await accessToken(db, settings);
    const url = new URL(`${API}/${path}`);
    for (const [k, v] of Object.entries(query || {})) if (v !== undefined && v !== "") url.searchParams.set(k, v);
    const response = await fetch(url, {
      method,
      headers: { authorization: `Bearer ${token}`, ...(body ? { "content-type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (response.status === 401) throw new GmailNotConnected("Google no longer accepts the Gmail token. Connect Gmail again in Settings.");
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(`Gmail API ${response.status}: ${(payload.error && payload.error.message) || "request failed"}`);
    return payload;
  }

  const labelCache = new Map();

  return {
    async profile() {
      return call("profile");
    },

    async listMessages(query, maxMessages = 10) {
      const payload = await call("messages", { query: { q: query, maxResults: String(maxMessages) } });
      return (payload.messages || []).map((m) => m.id);
    },

    async fetchMessage(msgId) {
      const msg = await call(`messages/${msgId}`, { query: { format: "full" } });
      const payload = msg.payload || {};
      const headers = payload.headers || [];
      const parts = [];
      walkParts(payload, parts);

      let bodyText = "";
      let bodyHtml = "";
      const attachments = [];
      for (const part of parts) {
        let mime = String(part.mimeType || "").toLowerCase();
        const filename = part.filename || "";
        const body = part.body || {};
        if (filename) {
          const ext = filename.includes(".") ? filename.slice(filename.lastIndexOf(".")).toLowerCase() : "";
          if (!SUPPORTED_MIME[mime]) mime = EXT_TO_MIME[ext] || mime;
          if (!SUPPORTED_MIME[mime]) continue;
          let data;
          if (body.attachmentId) {
            const att = await call(`messages/${msgId}/attachments/${body.attachmentId}`);
            data = b64urlDecode(att.data || "");
          } else if (body.data) {
            data = b64urlDecode(body.data);
          } else {
            continue;
          }
          if (mime.startsWith("image/") && data.length < MIN_IMAGE_BYTES) continue;
          attachments.push({ filename, mime_type: mime, data, size: data.length });
        } else if (mime === "text/plain" && body.data && !bodyText) {
          bodyText = decoder.decode(b64urlDecode(body.data));
        } else if (mime === "text/html" && body.data && !bodyHtml) {
          bodyHtml = decoder.decode(b64urlDecode(body.data));
        }
      }
      if (!bodyText && bodyHtml) bodyText = stripHtml(bodyHtml);
      const internalMs = Number(msg.internalDate || 0);
      const receivedAt = internalMs ? new Date(internalMs).toISOString().replace(/\.\d{3}Z$/, "+00:00") : "";
      return {
        id: msgId,
        thread_id: msg.threadId,
        from: header(headers, "From"),
        to: header(headers, "To"),
        subject: header(headers, "Subject"),
        date: header(headers, "Date"),
        received_at: receivedAt,
        snippet: msg.snippet || "",
        body_text: bodyText.slice(0, 4000),
        attachments,
        label_ids: msg.labelIds || [],
      };
    },

    /** Find a label by name, creating it (and any missing parents of a nested name) if needed. */
    async labelId(name) {
      const wanted = String(name || "").trim();
      if (!wanted) return "";
      if (labelCache.has(wanted.toLowerCase())) return labelCache.get(wanted.toLowerCase());
      const existing = new Map();
      for (const l of (await call("labels")).labels || []) existing.set(String(l.name || "").toLowerCase(), l.id);
      if (existing.has(wanted.toLowerCase())) {
        labelCache.set(wanted.toLowerCase(), existing.get(wanted.toLowerCase()));
        return existing.get(wanted.toLowerCase());
      }
      const parts = wanted.split("/").map((p) => p.trim()).filter(Boolean);
      let labelId = "";
      for (let depth = 1; depth <= parts.length; depth++) {
        const partial = parts.slice(0, depth).join("/");
        if (existing.has(partial.toLowerCase())) {
          labelId = existing.get(partial.toLowerCase());
          continue;
        }
        const created = await call("labels", {
          method: "POST",
          body: { name: partial, labelListVisibility: "labelShow", messageListVisibility: "show" },
        });
        labelId = created.id;
        existing.set(partial.toLowerCase(), labelId);
      }
      labelCache.set(wanted.toLowerCase(), labelId);
      return labelId;
    },

    /** Add label names to a message; optionally clear the unread flag. */
    async modify(msgId, { addLabels = [], markRead = false } = {}) {
      const body = {};
      const ids = [];
      for (const name of addLabels) {
        const id = await this.labelId(name);
        if (id) ids.push(id);
      }
      if (ids.length) body.addLabelIds = ids;
      if (markRead) body.removeLabelIds = ["UNREAD"];
      if (!Object.keys(body).length) return;
      await call(`messages/${msgId}/modify`, { method: "POST", body });
    },
  };
}

/** Cheap check used by the Settings page. */
export async function connectionStatus(env, settings) {
  if (!(await hasToken(env))) return { connected: false, email: null, error: null };
  try {
    const profile = await client(env, settings).profile();
    return { connected: true, email: profile.emailAddress, error: null };
  } catch (err) {
    return { connected: false, email: null, error: err.message || String(err) };
  }
}

// Claude Messages API calls for the two agents. Plain fetch, no SDK.
// tool_choice stays "auto" with a text-JSON fallback: forcing a tool is rejected
// when a model has thinking on. No thinking parameter is sent.

import { CLASSIFY_TOOL, INVOICE_TOOL, classificationSystemPrompt, extractionSystemPrompt } from "./schema.js";
import { normalise } from "./normalise.js";
import { retrieve } from "./rag.js";
import * as fixtures from "./fixtures.js";

export const API_BASE = "https://api.anthropic.com";
export const MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024;
const CLASSIFY_MAX_ATTACHMENTS = 3;
const CLASSIFY_MAX_TOTAL_BYTES = 12 * 1024 * 1024;

export function toBase64(bytes) {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < view.length; i += chunk) binary += String.fromCharCode.apply(null, view.subarray(i, i + chunk));
  return btoa(binary);
}

export function mediaBlock(bytes, mimeType) {
  const data = toBase64(bytes);
  if (mimeType === "application/pdf") {
    return { type: "document", source: { type: "base64", media_type: "application/pdf", data } };
  }
  return { type: "image", source: { type: "base64", media_type: mimeType, data } };
}

export function emailContext(meta) {
  if (!meta || !Object.keys(meta).length) return "Read the attached document and record it with the record_invoice tool.";
  const lines = [
    "The attached document arrived by email. Use the email details for context (they often carry the PO or site reference) but the document itself is the source of truth.",
    `From: ${meta.from || ""}`,
    `Subject: ${meta.subject || ""}`,
    `Date: ${meta.date || ""}`,
    `Attachment: ${meta.attachment_name || ""}`,
  ];
  const body = String(meta.body_text || "").trim();
  if (body) lines.push("Email body:\n" + body.slice(0, 2500));
  lines.push("Record the document with the record_invoice tool.");
  return lines.join("\n");
}

export function parseTextJson(text) {
  const cleaned = String(text || "").trim().replace(/^```(?:json)?\s*|\s*```$/g, "");
  try {
    return JSON.parse(cleaned);
  } catch {
    const start = cleaned.indexOf("{");
    const end = cleaned.lastIndexOf("}");
    if (start >= 0 && end > start) {
      try {
        return JSON.parse(cleaned.slice(start, end + 1));
      } catch {
        return null;
      }
    }
    return null;
  }
}

async function messages(apiKey, body) {
  const response = await fetch(`${API_BASE}/v1/messages`, {
    method: "POST",
    headers: { "x-api-key": apiKey, "anthropic-version": "2023-06-01", "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload && payload.error ? `${payload.error.type}: ${payload.error.message}` : `HTTP ${response.status}`;
    throw new Error(`Claude API refused the request (${detail})`);
  }
  return payload;
}

function toolResult(payload, toolName) {
  let record = null;
  const textParts = [];
  for (const block of payload.content || []) {
    if (block.type === "tool_use" && block.name === toolName) {
      record = { ...block.input };
      break;
    }
    if (block.type === "text") textParts.push(block.text);
  }
  if (!record) record = parseTextJson(textParts.join("\n"));
  const usage = {
    input_tokens: (payload.usage && payload.usage.input_tokens) || 0,
    output_tokens: (payload.usage && payload.usage.output_tokens) || 0,
    model: payload.model || "",
  };
  return { record, usage };
}

/** Returns { record, usage }. The record is normalised. Throws on API failure. */
export async function extractInvoice(env, { apiKey, model, attachment, mimeType, companyName, defaultCurrency, emailMeta, referenceText }) {
  if (fixtures.enabled(env)) return fixtures.extractInvoice({ attachment, mimeType, emailMeta });
  if (attachment.byteLength > MAX_ATTACHMENT_BYTES) throw new Error("Attachment is larger than 30 MB; split it or compress it first.");
  const query = [emailMeta?.from, emailMeta?.subject, emailMeta?.attachment_name, emailMeta?.body_text].filter(Boolean).join("\n");
  const notes = retrieve(referenceText, query);
  const payload = await messages(apiKey, {
    model,
    max_tokens: 8000,
    system: extractionSystemPrompt(companyName || "Glent Group", defaultCurrency || "GBP", notes),
    tools: [INVOICE_TOOL],
    tool_choice: { type: "auto" },
    messages: [{ role: "user", content: [mediaBlock(attachment, mimeType), { type: "text", text: emailContext(emailMeta) }] }],
  });
  const { record, usage } = toolResult(payload, INVOICE_TOOL.name);
  if (!record) throw new Error("The model did not return a structured invoice record.");
  return { record: normalise(record), usage };
}

function classificationContext(meta, attachments) {
  const lines = [
    `From: ${meta.from || ""}`,
    `To: ${meta.to || ""}`,
    `Subject: ${meta.subject || ""}`,
    `Date: ${meta.date || meta.received_at || ""}`,
    `Attachments: ${attachments.length ? attachments.map((a) => `${a.filename} (${a.mime_type}, ${Math.round(a.size / 1024)} KB)`).join("; ") : "none"}`,
  ];
  const body = String(meta.body_text || meta.snippet || "").trim();
  lines.push("Email body:\n" + (body ? body.slice(0, 3000) : "(empty)"));
  lines.push("Decide with the label_email tool.");
  return lines.join("\n");
}

/** Returns { verdict, document_kind, confidence, reason, usage }. Throws on API failure. */
export async function classifyEmail(env, { apiKey, model, emailMeta, attachments, companyName, referenceText }) {
  if (fixtures.enabled(env)) return fixtures.classifyEmail({ emailMeta, attachments });
  const query = [emailMeta?.from, emailMeta?.subject, emailMeta?.body_text, ...(attachments || []).map((a) => a.filename)].filter(Boolean).join("\n");
  const notes = retrieve(referenceText, query);
  const content = [];
  let total = 0;
  for (const att of (attachments || []).slice(0, CLASSIFY_MAX_ATTACHMENTS)) {
    if (total + att.size > CLASSIFY_MAX_TOTAL_BYTES) break;
    total += att.size;
    content.push(mediaBlock(att.data, att.mime_type));
  }
  content.push({ type: "text", text: classificationContext(emailMeta || {}, attachments || []) });
  const payload = await messages(apiKey, {
    model,
    max_tokens: 600,
    system: classificationSystemPrompt(companyName || "Glent Group", notes),
    tools: [CLASSIFY_TOOL],
    tool_choice: { type: "auto" },
    messages: [{ role: "user", content }],
  });
  const { record, usage } = toolResult(payload, CLASSIFY_TOOL.name);
  if (!record || !["invoice", "not_invoice"].includes(record.verdict)) throw new Error("The model did not return a verdict.");
  const confidence = parseFloat(record.confidence);
  return {
    verdict: record.verdict,
    document_kind: String(record.document_kind || "").slice(0, 80),
    confidence: Number.isFinite(confidence) ? Math.max(0, Math.min(1, confidence)) : 0,
    reason: String(record.reason || "").slice(0, 400),
    usage,
  };
}

/** Validate a key without spending tokens: list models. Returns [ok, message]. */
export async function checkApiKey(env, apiKey) {
  if (fixtures.enabled(env)) return [true, "Key works (fixture mode)."];
  try {
    const response = await fetch(`${API_BASE}/v1/models?limit=5`, {
      headers: { "x-api-key": apiKey, "anthropic-version": "2023-06-01" },
    });
    if (response.status === 401) return [false, "Anthropic rejected the key (401). Check it was pasted in full."];
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) return [false, `Claude API answered HTTP ${response.status}: ${(payload.error && payload.error.message) || ""}`];
    const names = (payload.data || []).map((m) => m.id);
    return [true, "Key works. Models visible: " + names.slice(0, 5).join(", ")];
  } catch (err) {
    return [false, `Could not reach the Claude API: ${err.message || err}`];
  }
}

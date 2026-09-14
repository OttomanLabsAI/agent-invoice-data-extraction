// The two agents and the inbox check: classify new mail, extract labelled mail.

import * as db from "./db.js";
import { extractInvoice, classifyEmail } from "./claude.js";
import * as gmail from "./gmail.js";
import { build } from "./sage_mapper.js";
import { hasClaude } from "./settings.js";

const safeName = (name) => String(name || "attachment").replace(/[^A-Za-z0-9._-]+/g, "_").slice(0, 120);

export const CLASSIFY_BATCH = 5;
export const EXTRACT_BATCH = 1;

/** Extract, map and store one attachment. Returns the new invoice id. */
export async function processAttachment(env, settings, { data, filename, mimeType, emailMeta, source = "gmail" }) {
  const meta = { ...(emailMeta || {}), attachment_name: filename };
  const key = `attachments/${meta.id || "upload-" + Date.now()}_${safeName(filename)}`;
  await env.FILES.put(key, data, { httpMetadata: { contentType: mimeType } });

  const base = {
    source,
    gmail_message_id: meta.id || null,
    gmail_thread_id: meta.thread_id || null,
    from_addr: meta.from || null,
    subject: meta.subject || null,
    received_at: meta.received_at || null,
    attachment_name: filename,
    attachment_key: key,
    mime_type: mimeType,
  };

  let extracted;
  let usage;
  try {
    ({ record: extracted, usage } = await extractInvoice(env, {
      apiKey: settings.anthropic_api_key,
      model: settings.extract_model || "claude-sonnet-5",
      attachment: data,
      mimeType,
      companyName: settings.company_name || "Glent Group",
      defaultCurrency: settings.default_currency || "GBP",
      emailMeta: meta,
      referenceText: settings.extract_reference_text || "",
    }));
  } catch (err) {
    console.error(`Extraction failed for ${filename}: ${err.message || err}`);
    return db.insertInvoice(env.DB, { ...base, status: "error", error: `${err.name || "Error"}: ${err.message || err}` });
  }

  const mapped = build(extracted, settings, { emailMeta: meta });
  const status = ["invoice", "credit_note"].includes(extracted.document_type) ? "review" : "not_invoice";
  return db.insertInvoice(env.DB, {
    ...base,
    status,
    extracted,
    sage: mapped,
    issues: mapped.issues,
    input_tokens: usage.input_tokens || 0,
    output_tokens: usage.output_tokens || 0,
  });
}

/** Rebuild the Sage payload for a stored invoice (after edits or settings changes). */
export function remap(invoice, settings, overrides) {
  const meta = {
    id: invoice.gmail_message_id,
    from: invoice.from_addr,
    subject: invoice.subject,
    received_at: invoice.received_at,
    attachment_name: invoice.attachment_name,
  };
  return build(invoice.extracted, settings, { emailMeta: meta, overrides: overrides || {} });
}

async function withRun(env, stage, summary, work) {
  if (!(await db.acquireRunLock(env.DB))) return { ...summary, ok: false, message: "A run is already in progress." };
  const runId = await db.startRun(env.DB, stage);
  try {
    await work();
    await db.finishRun(env.DB, runId, summary.message, summary.errors === 0);
  } catch (err) {
    summary.ok = false;
    summary.message = err instanceof gmail.GmailNotConnected ? err.message : `Run failed: ${err.name || "Error"}: ${err.message || err}`;
    if (!(err instanceof gmail.GmailNotConnected)) console.error(summary.message, err.stack || "");
    await db.finishRun(env.DB, runId, summary.message, false);
  } finally {
    await db.releaseRunLock(env.DB);
  }
  return summary;
}

/** Agent - Classification: look at up to `limit` new emails and label them. */
export async function classifyStep(env, settings, limit = CLASSIFY_BATCH) {
  const summary = { ok: true, stage: "classify", looked: 0, invoices: 0, others: 0, skipped: 0, errors: 0, remaining: false, message: "" };
  if (!hasClaude(settings)) return { ...summary, ok: false, message: "Add your Claude API key in Settings first." };
  return withRun(env, "classify", summary, async () => {
    const client = gmail.client(env, settings);
    const invoiceLabel = settings.classify_invoice_label || "Invoice Incoming";
    const otherLabel = settings.classify_other_label || "";
    let query = settings.gmail_query || "has:attachment is:unread";
    for (const label of [invoiceLabel, otherLabel, settings.gmail_processed_label]) {
      if (label) query += ` -label:${gmail.searchLabel(label)}`;
    }
    const perRun = Math.max(1, parseInt(settings.gmail_max_messages, 10) || 10);
    const batch = Math.max(1, Math.min(limit, perRun));
    const ids = await client.listMessages(query, batch + 1);
    summary.remaining = ids.length > batch;

    for (const id of ids.slice(0, batch)) {
      let meta;
      try {
        meta = await client.fetchMessage(id);
      } catch (err) {
        console.error(`Could not read message ${id}: ${err.message || err}`);
        summary.errors += 1;
        continue;
      }
      summary.looked += 1;
      const attachmentNames = meta.attachments.map((a) => a.filename).join(", ");
      const base = { gmail_message_id: id, from_addr: meta.from, subject: meta.subject, received_at: meta.received_at, attachments: attachmentNames };

      if (!gmail.senderAllowed(meta.from, settings.gmail_allowed_senders)) {
        summary.skipped += 1;
        let applied = "";
        if (otherLabel) {
          await client.modify(id, { addLabels: [otherLabel] });
          applied = otherLabel;
        }
        await db.recordClassification(env.DB, { ...base, verdict: "not_invoice", document_kind: "sender not allowed", confidence: 1, reason: "Sender is not in the allowed list.", label_applied: applied });
        continue;
      }

      let result;
      try {
        result = await classifyEmail(env, {
          apiKey: settings.anthropic_api_key,
          model: settings.classify_model || "claude-haiku-4-5-20251001",
          emailMeta: meta,
          attachments: meta.attachments,
          companyName: settings.company_name || "Glent Group",
          referenceText: settings.classify_reference_text || "",
        });
      } catch (err) {
        console.error(`Classification failed for ${id}: ${err.message || err}`);
        summary.errors += 1;
        continue; // left unlabelled so the next run tries again
      }

      const label = result.verdict === "invoice" ? invoiceLabel : otherLabel;
      let applied = "";
      if (label) {
        try {
          await client.modify(id, { addLabels: [label] });
          applied = label;
        } catch (err) {
          console.error(`Could not label message ${id}: ${err.message || err}`);
          summary.errors += 1;
        }
      }
      await db.recordClassification(env.DB, {
        ...base,
        verdict: result.verdict,
        document_kind: result.document_kind,
        confidence: result.confidence,
        reason: result.reason,
        label_applied: applied,
        model: result.usage.model,
        input_tokens: result.usage.input_tokens,
        output_tokens: result.usage.output_tokens,
      });
      if (result.verdict === "invoice") summary.invoices += 1;
      else summary.others += 1;
    }

    summary.message =
      `Looked at ${summary.looked} email(s): ${summary.invoices} labelled ${invoiceLabel}, ` +
      `${summary.others + summary.skipped} ${otherLabel || "left unlabelled"}, ${summary.errors} failed.` +
      (summary.remaining ? " More waiting." : "");
  });
}

/** Agent - Invoice Extraction: read up to `limit` labelled emails and extract every supported attachment. */
export async function extractStep(env, settings, limit = EXTRACT_BATCH) {
  const summary = { ok: true, stage: "extract", messages: 0, processed: 0, skipped: 0, errors: 0, remaining: false, message: "" };
  if (!hasClaude(settings)) return { ...summary, ok: false, message: "Add your Claude API key in Settings first." };
  return withRun(env, "extract", summary, async () => {
    const client = gmail.client(env, settings);
    const invoiceLabel = settings.classify_invoice_label || "Invoice Incoming";
    const processedLabel = settings.gmail_processed_label || "";
    let query = `label:${gmail.searchLabel(invoiceLabel)}`;
    if (processedLabel) query += ` -label:${gmail.searchLabel(processedLabel)}`;
    const perRun = Math.max(1, parseInt(settings.gmail_max_messages, 10) || 10);
    const batch = Math.max(1, Math.min(limit, perRun));
    const ids = await client.listMessages(query, batch + 1);
    summary.remaining = ids.length > batch;

    for (const id of ids.slice(0, batch)) {
      let meta;
      try {
        meta = await client.fetchMessage(id);
      } catch (err) {
        console.error(`Could not read message ${id}: ${err.message || err}`);
        summary.errors += 1;
        continue;
      }
      summary.messages += 1;

      for (const att of meta.attachments) {
        if (await db.alreadyProcessed(env.DB, id, att.filename)) {
          summary.skipped += 1;
          continue;
        }
        const invoiceId = await processAttachment(env, settings, { data: att.data, filename: att.filename, mimeType: att.mime_type, emailMeta: meta, source: "gmail" });
        const row = await db.getInvoice(env.DB, invoiceId);
        if (row && row.status === "error") summary.errors += 1;
        else summary.processed += 1;
      }
      if (!meta.attachments.length) {
        summary.skipped += 1;
        if (!(await db.alreadyProcessed(env.DB, id, "(no supported attachment)"))) {
          await db.insertInvoice(env.DB, {
            source: "gmail",
            gmail_message_id: id,
            gmail_thread_id: meta.thread_id,
            from_addr: meta.from,
            subject: meta.subject,
            received_at: meta.received_at,
            attachment_name: "(no supported attachment)",
            mime_type: "",
            status: "not_invoice",
            notes: "Email had no PDF or image attachment.",
          });
        }
      }
      try {
        await client.modify(id, { addLabels: processedLabel ? [processedLabel] : [], markRead: true });
      } catch (err) {
        console.error(`Could not label message ${id}: ${err.message || err}`);
      }
    }

    summary.message =
      `Checked ${summary.messages} email(s): ${summary.processed} processed, ${summary.skipped} skipped, ${summary.errors} failed.` +
      (summary.remaining ? " More waiting." : "");
  });
}

/** Check inbox now: one classification batch, then one extraction step. */
export async function checkInboxStep(env, settings) {
  const classified = await classifyStep(env, settings, CLASSIFY_BATCH);
  if (!classified.ok) return classified;
  const extracted = await extractStep(env, settings, EXTRACT_BATCH);
  if (!extracted.ok) return extracted;
  return {
    ok: true,
    stage: "inbox",
    remaining: classified.remaining || extracted.remaining,
    errors: classified.errors + extracted.errors,
    message: `Classification: ${classified.message} Extraction: ${extracted.message}`,
  };
}

/** The cron trigger: honour poll_minutes, then do a bounded amount of work. */
export async function runScheduled(env, settings) {
  const minutes = parseInt(settings.poll_minutes, 10) || 0;
  if (minutes <= 0) return { ok: true, message: "Automatic checks are off." };
  const last = Number((await db.getState(env.DB, "last_auto_run")) || 0);
  if (Date.now() - last < minutes * 60 * 1000 - 30 * 1000) return { ok: true, message: "Not due yet." };
  await db.setState(env.DB, "last_auto_run", String(Date.now()));
  const classified = await classifyStep(env, settings, CLASSIFY_BATCH);
  const extracted = classified.ok ? await extractStep(env, settings, 2) : { message: "" };
  const message = `Scheduled: ${classified.message} ${extracted.message}`.trim();
  console.log(message);
  return { ok: classified.ok, message };
}

import { html, raw, when, selected, checked, disabled, money, shortDate, timeOf, fmt2, plain, humanise, esc } from "./html.js";
import { continueNudge } from "./layout.js";
import { STATUSES, STATUS_LABELS } from "../db.js";
import { modelLabel, GENERIC_CODES } from "../settings.js";
import { TYPE_SIGNALS, EXPECTED_VAT } from "../invoice_types.js";

// --------------------------------------------------------------------------- Sign-in and setup

export function signInPage({ error, companyName }) {
  return html`<div class="pagehead"><div><h1>Sign in</h1><div class="meta">The invoice intake agent for ${companyName}'s accounts team.</div></div></div>
<section class="sheet" style="max-width:32rem">
  ${when(error, html`<div class="flash error">${error}</div>`)}
  <form method="post" action="/signin" autocomplete="off">
    <div class="field">
      <label for="password">App password</label>
      <div class="secret"><input id="password" name="password" type="password" autocomplete="current-password" autofocus required><button type="button" class="reveal" aria-label="Show" title="Show">${eye()}</button></div>
    </div>
    <div class="actions" style="margin-top:1rem"><button class="btn primary" type="submit">Sign in</button></div>
  </form>
</section>`;
}

export function setupPage() {
  return html`<div class="pagehead"><div><h1>One thing to set up first</h1><div class="meta">This app has no password yet, so it refuses every visitor.</div></div></div>
<section class="sheet" style="max-width:48rem">
  <p>In the Cloudflare dashboard open this Worker, go to <strong>Settings → Variables and Secrets</strong>, and add a <strong>secret</strong> named <code>APP_PASSWORD</code> with the password the accounts team will use. Deploy is not needed: the page works as soon as the secret is saved.</p>
  <p class="help">Everyone shares the one password. Changing it signs everyone out.</p>
</section>`;
}

export function notFoundPage() {
  return html`<div class="pagehead"><div><h1>Nothing filed under that address</h1><div class="meta">The page you asked for is not here.</div></div></div>
<div class="empty"><a class="btn primary" href="/">Back to the inbox</a></div>`;
}

export function errorPage(message) {
  return html`<div class="pagehead"><div><h1>Something went wrong</h1><div class="meta">${message}</div></div></div>
<div class="empty"><a class="btn primary" href="/">Back to the inbox</a></div>`;
}

// --------------------------------------------------------------------------- Inbox

export function dashboardPage({ invoices, counts, activeStatus, lastRun, gmailConnected, claudeReady, continueRun }) {
  const nudge = !claudeReady && !gmailConnected
    ? html`Nothing is connected yet. <a href="/settings">Open Settings</a> to paste your Claude API key and connect the Gmail account. You can also drop a PDF below to try the extraction straight away once the Claude key is in.`
    : !claudeReady
      ? html`Gmail is connected but there is no Claude API key. <a href="/settings">Add it in Settings</a> before checking the inbox.`
      : !gmailConnected
        ? html`Claude is ready but Gmail is not connected. <a href="/settings">Connect it in Settings</a>, or upload invoices by hand below.`
        : "";
  return html`
${when(nudge, html`<div class="nudge">${nudge}</div>`)}
${when(continueRun, continueNudge("/run", "mail"))}
<div class="pagehead">
  <div>
    <h1>Inbox</h1>
    <div class="meta">
      ${lastRun
        ? html`Last checked ${shortDate(lastRun.finished_at || lastRun.started_at)}${when(lastRun.finished_at, html` at ${timeOf(lastRun.finished_at)} UTC`)}${when(lastRun.summary, html` · ${lastRun.summary}`)}`
        : "The inbox has not been checked yet."}
    </div>
  </div>
  <div class="actions">
    <form method="post" action="/upload" enctype="multipart/form-data" class="upload">
      <input type="file" name="files" accept=".pdf,.png,.jpg,.jpeg,.webp" multiple required>
      <button class="btn" type="submit">Process upload</button>
    </form>
    <form method="post" action="/run" class="inline">
      <button class="btn primary" type="submit" data-run${disabled(!(claudeReady && gmailConnected))} title="Classifies new mail, then extracts the invoices">Check inbox now</button>
    </form>
  </div>
</div>

<div class="counts">
  <a href="/" class="${!activeStatus ? "active" : ""}"><strong>${counts.total}</strong> all</a>
  ${STATUSES.map((key) => html`<a href="/?status=${key}" class="${activeStatus === key ? "active" : ""}"><strong>${counts[key]}</strong> ${STATUS_LABELS[key].toLowerCase()}</a>`)}
  <a href="/export.csv${activeStatus ? `?status=${activeStatus}` : ""}" style="margin-left:auto; margin-right:0">Download log CSV</a>
</div>

${invoices.length
    ? html`<table class="ledger">
  <thead>
    <tr><th>Received</th><th>Supplier</th><th>Invoice no</th><th>Invoice date</th><th class="num">Net</th><th class="num">VAT</th><th class="num">Gross</th><th class="num">Checks</th><th>Status</th></tr>
  </thead>
  <tbody>
  ${invoices.map((inv) => {
    const rec = inv.extracted || {};
    const issues = inv.issues || [];
    return html`<tr>
      <td>${shortDate(inv.received_at || inv.created_at)}<span class="sub">${inv.source}</span></td>
      <td><a href="/invoice/${inv.id}">${(rec.supplier || {}).name || inv.from_addr || "Unknown supplier"}</a><span class="sub">${inv.subject || inv.attachment_name}</span></td>
      <td>${rec.invoice_number || "–"}${when(rec.document_type && rec.document_type !== "invoice", html`<span class="sub">${humanise(rec.document_type)}</span>`)}</td>
      <td>${shortDate(rec.invoice_date)}</td>
      <td class="num">${money(rec.net_total)}</td>
      <td class="num">${money(rec.vat_total)}</td>
      <td class="num">${money(rec.gross_total)}</td>
      <td class="num">${issues.length ? html`<span class="flagcount">${issues.length}</span>` : inv.status === "error" ? html`<span class="flagcount">!</span>` : "0"}</td>
      <td><span class="status ${inv.status}">${inv.status_label}</span>${when(inv.intacct_recordno, html`<span class="sub">Sage #${inv.intacct_recordno}</span>`)}</td>
    </tr>`;
  })}
  </tbody>
</table>`
    : html`<div class="empty">${activeStatus ? "No invoices with this status." : "No invoices yet. Check the inbox, or upload a PDF to see what the extraction produces."}</div>`}`;
}

// --------------------------------------------------------------------------- Invoice review

export function invoicePage({ inv, rec, sage, vendorId, projectId, billJson, billXml, categories, vatTreatments, documentTypes, sageReady, invoiceTypes = [], chosenType = "" }) {
  const supplier = rec.supplier || {};
  const bank = rec.bank_details || {};
  const cis = rec.cis || {};
  const retention = rec.retention || {};
  const issues = sage.issues || [];
  const notes = sage.notes || [];
  const sheet = sage.entry_sheet || {};
  const bill = sage.bill || {};
  const matched = sage.matched || {};
  const statusForm = (action, label, cls = "btn", extra = "") =>
    html`<form class="inline" method="post" action="/invoice/${inv.id}/status"${raw(extra)}><input type="hidden" name="action" value="${action}"><button class="${cls}" type="submit">${label}</button></form>`;

  return html`<div class="pagehead">
  <div>
    <h1>${supplier.name || inv.from_addr || "Unknown supplier"}${when(rec.invoice_number, html` · ${rec.invoice_number}`)}</h1>
    <div class="meta">
      <span class="status ${inv.status}">${inv.status_label}</span>
      · received ${shortDate(inv.received_at || inv.created_at)} from ${inv.from_addr || inv.source}
      ${when(inv.subject, html` · “${inv.subject}”`)}
      ${when(inv.input_tokens, html` · ${(inv.input_tokens || 0) + (inv.output_tokens || 0)} tokens`)}
      ${when(inv.intacct_recordno, html` · Sage record ${inv.intacct_recordno}`)}
    </div>
  </div>
  <div class="actions">
    <a class="btn quiet" href="/">Back to inbox</a>
    ${when(["review", "queried"].includes(inv.status), html`<form class="inline" method="post" action="/invoice/${inv.id}/status"><input type="hidden" name="action" value="approve"><button class="btn" type="submit"${disabled(issues.length)}${when(issues.length, raw(' title="Fix the checks first"'))}>Approve</button></form>`)}
    ${when(inv.status === "review", statusForm("query", "Query"))}
    ${when(inv.status === "approved", html`${statusForm("posted", "Mark keyed in by hand")}
      <form class="inline" method="post" action="/invoice/${inv.id}/push" onsubmit="return confirm('Create this bill in Sage Intacct now?')"><button class="btn primary" type="submit"${disabled(!sageReady)}${when(!sageReady, raw(' title="Sage connection not set up"'))}>Post to Sage</button></form>`)}
    ${when(["approved", "posted", "queried", "not_invoice", "error"].includes(inv.status), statusForm("reopen", "Reopen", "btn quiet"))}
    ${statusForm("delete", "Delete", "btn quiet", ` onsubmit="return confirm('Delete this record? The email stays labelled as processed.')"`)}
  </div>
</div>

${when(inv.status === "error", html`<div class="flash error">Extraction failed: ${inv.error}. Fix the cause (usually the API key or the file) and re-upload the document.</div>`)}

${rec && Object.keys(rec).length
    ? html`<div class="split">
  <div>
    <form method="post" action="/invoice/${inv.id}">
    <section class="sheet">
      <h2>What the document says</h2>
      ${when(issues.length || notes.length, html`<div style="margin-bottom:1rem">
        ${when(issues.length, html`<ul class="issues">${issues.map((i) => html`<li>${i}</li>`)}</ul>`)}
        ${when(notes.length, html`<ul class="notes">${notes.map((n) => html`<li>${n}</li>`)}</ul>`)}
      </div>`)}

      <div class="field"><label for="document_type">Document</label>
        <select id="document_type" name="document_type">${documentTypes.map((t) => html`<option value="${t}"${selected(rec.document_type, t)}>${humanise(t)}</option>`)}</select></div>
      <div class="field"><label for="supplier_name">Supplier</label>
        <div class="row"><input id="supplier_name" name="supplier_name" type="text" value="${supplier.name || ""}"><input class="w-m" name="supplier_vat" type="text" value="${supplier.vat_number || ""}" placeholder="VAT number"></div>
        ${when(supplier.address, html`<div class="hint">${supplier.address}${when(supplier.postcode && !String(supplier.address).includes(supplier.postcode), html`, ${supplier.postcode}`)}</div>`)}</div>
      <div class="field"><label for="vendor_id">Sage vendor ID<small>from Settings › Vendor map</small></label>
        <input id="vendor_id" name="vendor_id" type="text" value="${vendorId}" placeholder="e.g. V0123">
        ${when(matched.vendor_key, html`<div class="hint">Matched “${matched.vendor_key}” in the vendor map.</div>`)}</div>
      ${when(invoiceTypes.length, html`<div class="field"><label for="invoice_type">Invoice type<small>from the Invoice types tab</small></label>
        <select id="invoice_type" name="invoice_type">
          <option value=""${selected(chosenType, "")}>Automatic${sage.invoice_type && !chosenType ? ` - ${sage.invoice_type.name}` : ""}</option>
          ${invoiceTypes.map((t) => html`<option value="${t.id}"${selected(chosenType, t.id)}>${t.name}</option>`)}
        </select>
        ${when(sage.invoice_type, html`<div class="hint">${sage.invoice_type.name}: ${sage.invoice_type.how}. The type sets the GL and VAT coding, CIS and retention defaults, and which checks apply; <a href="/types#type-${sage.invoice_type.id}">edit it</a>.</div>`)}</div>`)}
      <div class="field"><label for="invoice_number">Invoice number</label><input id="invoice_number" name="invoice_number" type="text" value="${rec.invoice_number || ""}"></div>
      <div class="field"><label>Dates</label>
        <div class="row"><input name="invoice_date" type="date" value="${rec.invoice_date || ""}" aria-label="Invoice date"><input name="due_date" type="date" value="${rec.due_date || bill.WHENDUE || ""}" aria-label="Due date"></div>
        <div class="hint">Invoice date, due date${when(rec.payment_terms_text, html` · terms on document: ${rec.payment_terms_text}`)}</div></div>
      <div class="field"><label for="po_number">PO number</label>
        <div class="row"><input id="po_number" name="po_number" type="text" value="${rec.po_number || ""}"><input name="project_reference" type="text" value="${rec.project_reference || ""}" placeholder="Project / site reference"></div>
        ${when(rec.order_reference || rec.delivery_note_number, html`<div class="hint">${when(rec.order_reference, html`Order ref: ${rec.order_reference}`)}${when(rec.delivery_note_number, html` · Delivery note: ${rec.delivery_note_number}`)}</div>`)}</div>
      <div class="field"><label for="project_id">Sage project ID<small>from Settings › Project map</small></label>
        <input id="project_id" name="project_id" type="text" value="${projectId}" placeholder="optional"></div>
      <div class="field"><label for="description">Description</label><input id="description" name="description" type="text" value="${rec.description || ""}"></div>
      <div class="field"><label>Currency and VAT</label>
        <div class="row"><input class="w-s" name="currency" type="text" value="${rec.currency || "GBP"}" aria-label="Currency">
          <select name="vat_treatment" aria-label="VAT treatment">${vatTreatments.map((v) => html`<option value="${v}"${selected(rec.vat_treatment, v)}>${humanise(v)}</option>`)}</select></div></div>

      <table class="lines" id="lines">
        <colgroup><col><col class="c-qty"><col class="c-price"><col class="c-net"><col class="c-rate"><col class="c-vat"><col class="c-cat"><col class="c-rm"></colgroup>
        <thead><tr><th>Line</th><th class="num">Qty</th><th class="num">Unit price</th><th class="num">Net</th><th class="num">VAT %</th><th class="num">VAT</th><th>Category</th><th></th></tr></thead>
        <tbody>
        ${(rec.line_items || []).map((line) => html`<tr>
            <td><input name="line_desc" type="text" value="${line.description || ""}"><input name="line_unit" type="hidden" value="${line.unit || ""}"></td>
            <td><input name="line_qty" type="text" class="num" value="${plain(line.quantity)}" title="${line.unit || "quantity"}"></td>
            <td><input name="line_unit_price" type="text" class="num" value="${plain(line.unit_price)}"></td>
            <td><input name="line_net" type="text" class="num" data-auto="off" value="${fmt2(line.net_amount)}"></td>
            <td><input name="line_vat_rate" type="text" class="num" value="${plain(line.vat_rate)}"></td>
            <td><input name="line_vat" type="text" class="num" data-auto="off" value="${fmt2(line.vat_amount)}"></td>
            <td><select name="line_category">${categories.map((c) => html`<option value="${c}"${selected(line.category, c)}>${c}</option>`)}</select></td>
            <td class="rm"><button type="button" class="rm" aria-label="Remove line">×</button></td>
          </tr>`)}
        </tbody>
      </table>
      <template id="line-template">
        <tr>
          <td><input name="line_desc" type="text"><input name="line_unit" type="hidden" value=""></td>
          <td><input name="line_qty" type="text" class="num"></td>
          <td><input name="line_unit_price" type="text" class="num"></td>
          <td><input name="line_net" type="text" class="num"></td>
          <td><input name="line_vat_rate" type="text" class="num" value="20"></td>
          <td><input name="line_vat" type="text" class="num"></td>
          <td><select name="line_category">${categories.map((c) => html`<option value="${c}"${selected("materials", c)}>${c}</option>`)}</select></td>
          <td class="rm"><button type="button" class="rm" aria-label="Remove line">×</button></td>
        </tr>
      </template>
      <div style="margin-top:0.5rem"><button type="button" class="btn small" id="add-line">Add line</button></div>

      <div class="totals">
        <div><label for="net_total">Net total</label><input id="net_total" name="net_total" type="text" data-auto="off" value="${fmt2(rec.net_total)}"></div>
        <div><label for="vat_total">VAT total</label><input id="vat_total" name="vat_total" type="text" data-auto="off" value="${fmt2(rec.vat_total)}"></div>
        <div><label for="gross_total">Gross total</label><input id="gross_total" name="gross_total" type="text" data-auto="off" value="${fmt2(rec.gross_total)}"></div>
      </div>
      <div class="checkline">
        <label><input type="checkbox" name="cis_applicable"${checked(cis.applicable)}> CIS subcontractor</label>
        <label>Rate % <input name="cis_rate" type="text" value="${plain(cis.deduction_rate)}"></label>
        <label>Deduction <input name="cis_amount" type="text" value="${fmt2(cis.deduction_amount)}"></label>
        <label>Retention % <input name="retention_pct" type="text" value="${plain(retention.percentage)}"></label>
        <label>Retention <input name="retention_amount" type="text" value="${fmt2(retention.amount)}"></label>
      </div>
      <div class="field" style="margin-top:0.8rem"><label for="notes">Notes for accounts</label><textarea id="notes" name="notes" style="min-height:3.5rem">${inv.notes || ""}</textarea></div>
      <div class="actions" style="margin-top:1rem">
        <button class="btn primary" type="submit">Save changes</button>
        <span class="test-result">Saving rebuilds the Sage payload on the right.</span>
      </div>
    </section>
    </form>

    <section class="sheet">
      <h2>Sage Intacct payload</h2>
      <p class="help">What Post to Sage sends. Download it if you'd rather import through the Intacct web UI or another tool.</p>
      <div class="tabs">
        <button type="button" class="active" data-tab="pane-json">JSON</button>
        <button type="button" data-tab="pane-xml">XML gateway</button>
        <button type="button" data-tab="pane-raw">Raw extraction</button>
      </div>
      <div id="pane-json" class="tabpane active">
        <div class="actions" style="margin-bottom:0.5rem"><a class="btn small" href="/invoice/${inv.id}/sage.json">Download JSON</a><button type="button" class="btn small quiet" data-copy="json-src">Copy</button>
          <form class="inline" method="post" action="/invoice/${inv.id}/remap"><button class="btn small quiet" type="submit">Rebuild from current settings</button></form></div>
        <pre id="json-src">${billJson}</pre>
      </div>
      <div id="pane-xml" class="tabpane">
        <div class="actions" style="margin-bottom:0.5rem"><a class="btn small" href="/invoice/${inv.id}/sage.xml">Download XML</a><button type="button" class="btn small quiet" data-copy="xml-src">Copy</button></div>
        <pre id="xml-src">${billXml}</pre>
      </div>
      <div id="pane-raw" class="tabpane">
        <pre>${JSON.stringify(rec, null, 2)}</pre>
      </div>
    </section>
  </div>

  <aside class="side">
    ${when(inv.attachment_key, html`${inv.mime_type === "application/pdf"
      ? html`<iframe class="preview" src="/invoice/${inv.id}/file#toolbar=0" title="Invoice document"></iframe>`
      : html`<img class="preview" src="/invoice/${inv.id}/file" alt="Invoice image">`}
      <p class="help" style="margin:0.4rem 0 1rem"><a href="/invoice/${inv.id}/file" target="_blank" rel="noopener">Open ${inv.attachment_name}</a> in a new tab</p>`)}

    <div class="entry">
      <h2>Key into Sage</h2>
      <dl>
        ${(sheet.header || []).map(([label, value, sub]) => html`<dt>${label}</dt><dd class="${String(value || "").startsWith("(") ? "unmapped" : ""}">${value || "–"}${when(sub, html`<small>${sub}</small>`)}</dd>`)}
      </dl>
      ${when((bill.ITEMS || []).length, html`<div class="section">Bill lines</div>
      <table>
        <thead><tr><th>GL</th><th>Description</th><th class="num">Amount</th><th class="num">Tax</th></tr></thead>
        <tbody>
        ${(bill.ITEMS || []).map((item) => {
          const tax = (item.TAXENTRIES || [])[0];
          return html`<tr>
            <td class="${!item.ACCOUNTNO ? "unmapped" : ""}">${item.ACCOUNTNO || "(set GL)"}<small>${item._category}</small></td>
            <td>${item.ENTRYDESCRIPTION}</td>
            <td class="num">${item.TRX_AMOUNT}</td>
            <td class="num ${tax && !tax.DETAILID ? "unmapped" : ""}">${tax ? html`${tax.TRX_TAX}<small>${tax.DETAILID || "(set tax detail)"} · ${item._tax_label}</small>` : "–"}</td>
          </tr>`;
        })}
        </tbody>
      </table>`)}
      <div class="section">Totals</div>
      <table><tbody>
        ${(sheet.totals || []).map(([label, value]) => html`<tr class="${["Gross", "Amount payable"].includes(label) ? "total" : ""}"><td>${label}</td><td class="num">${value}</td></tr>`)}
      </tbody></table>
      ${when(sheet.payment, html`<div class="section">For the payment run</div>
      <dl>
        ${(sheet.payment || []).map(([label, value]) => when(value, html`<dt>${label}</dt><dd>${value}</dd>`))}
        ${when(!(bank.sort_code || bank.iban), html`<dt>Bank details</dt><dd class="unmapped">Not on the invoice - check against the vendor record before paying.</dd>`)}
      </dl>`)}
    </div>
  </aside>
</div>`
    : ""}`;
}

// --------------------------------------------------------------------------- Agent tabs

function modelSelect(id, value, models) {
  return html`<select id="${id}" name="${id}">${models.map(([v, label]) => html`<option value="${v}"${selected(value, v)}>${label}</option>`)}</select>`;
}

function ragField(id, value, info, help) {
  return html`<div class="field">
    <label for="${id}">Reference text<small>RAG retrieval</small></label>
    <div>
      <textarea id="${id}" name="${id}" style="min-height:14rem" placeholder="Paste notes, lists and examples here. Blank lines separate paragraphs.">${value || ""}</textarea>
      <div class="hint">${help} ${info.chars
        ? html`Currently ${info.chars.toLocaleString("en-GB")} characters in ${info.paragraphs} paragraph${info.paragraphs === 1 ? "" : "s"} - ${info.whole ? "sent whole with every email." : `over the ${info.maxChars.toLocaleString("en-GB")}-character budget, so only the paragraphs that share words with each email are sent.`}`
        : "Empty - nothing is added to the prompt yet."}</div>
    </div>
  </div>`;
}

function agentStatusLine(lastRun, connected, claudeReady) {
  if (!claudeReady) return html`Needs the Claude API key from <a href="/settings">Settings</a> before it can run.`;
  if (!connected) return html`Needs Gmail connected in <a href="/settings">Settings</a> before it can run.`;
  if (!lastRun) return "Has not run yet.";
  return html`Last run ${shortDate(lastRun.finished_at || lastRun.started_at)}${when(lastRun.finished_at, html` at ${timeOf(lastRun.finished_at)} UTC`)}${when(lastRun.summary, html` · ${lastRun.summary}`)}`;
}

export function classificationPage({ s, models, connected, claudeReady, recent, counts, lastRun, ragInfo, continueRun }) {
  const canRun = connected && claudeReady;
  return html`${when(continueRun, continueNudge("/agents/classification/run", "mail to classify"))}
<div class="pagehead">
  <div>
    <h1>Agent - Classification</h1>
    <div class="meta">Reads new mail in the invoices mailbox and labels each email <code>${s.classify_invoice_label}</code> or <code>${s.classify_other_label || "(nothing)"}</code>. It decides only; the extraction agent does the reading of invoice data. ${agentStatusLine(lastRun, connected, claudeReady)}</div>
  </div>
  <div class="actions">
    <button class="btn" type="submit" form="classification-form">Save</button>
    <button class="btn primary" type="submit" form="classification-form" name="action" value="run" data-run${disabled(!canRun)}>Classify now</button>
  </div>
</div>

<form id="classification-form" method="post" action="/agents/classification" autocomplete="off">
<section class="sheet">
  <h2>AI model</h2>
  <p class="help">Opus 5 is the default. Classification is a short call per email, so Sonnet 5 or Haiku 4.5 will do it for less if volume gets high. The key comes from Settings.</p>
  <div class="field"><label for="classify_model">Model</label>${modelSelect("classify_model", s.classify_model, models)}</div>
</section>

<section class="sheet">
  <h2>Reference text (RAG)</h2>
  <p class="help">What the agent should know when deciding: which suppliers send applications for payment, senders whose mail is never an invoice, how statements and remittances from particular suppliers look, anything it has got wrong before. The relevant paragraphs are retrieved and added to the instructions for every email.</p>
  ${ragField("classify_reference_text", s.classify_reference_text, ragInfo, "")}
</section>

<section class="sheet">
  <h2>What it reads</h2>
  <div class="field">
    <label for="gmail_query">Search</label>
    <input id="gmail_query" name="gmail_query" type="text" value="${s.gmail_query}">
    <div class="hint">Gmail search syntax. Emails already labelled by either agent are excluded automatically. Example: <code>has:attachment is:unread label:Invoices</code></div>
  </div>
  <div class="field">
    <label for="gmail_allowed_senders">Only from<small>optional</small></label>
    <input id="gmail_allowed_senders" name="gmail_allowed_senders" type="text" value="${s.gmail_allowed_senders}" placeholder="@supplier.co.uk, accounts@another.com">
    <div class="hint">Comma-separated addresses or domains. Blank means any sender. Mail from anyone else is labelled as not an invoice without being read.</div>
  </div>
  <div class="field">
    <label for="gmail_max_messages">Per run</label>
    <div><input id="gmail_max_messages" name="gmail_max_messages" type="number" min="1" max="100" value="${s.gmail_max_messages}" style="max-width:8rem"><div class="hint">emails looked at per click (in batches of five; the page keeps going while more are waiting)</div></div>
  </div>
</section>

<section class="sheet">
  <h2>Labels it applies</h2>
  <div class="field">
    <label for="classify_invoice_label">Invoice label</label>
    <input id="classify_invoice_label" name="classify_invoice_label" type="text" value="${s.classify_invoice_label}">
    <div class="hint">Added to every email judged to carry an invoice. The extraction agent reads exactly this label, so you can also apply it by hand in Gmail to push an email through. Created in the mailbox if it does not exist.</div>
  </div>
  <div class="field">
    <label for="classify_other_label">Everything else</label>
    <input id="classify_other_label" name="classify_other_label" type="text" value="${s.classify_other_label}">
    <div class="hint">Added to emails judged not to be invoices so they are never read twice. Leave blank to label nothing - then every run reads them again.</div>
  </div>
</section>
<div class="actions"><button class="btn primary" type="submit">Save</button></div>
</form>

<section class="sheet" id="decisions">
  <h2>Recent decisions</h2>
  <p class="help">${counts.total ? html`${counts.total} email${counts.total === 1 ? "" : "s"} classified so far: ${counts.invoice} invoice${counts.invoice === 1 ? "" : "s"}, ${counts.not_invoice} not.` : "Nothing classified yet."}</p>
  ${recent.length
    ? html`<table class="ledger">
    <thead><tr><th>Received</th><th>From</th><th>Subject</th><th>Verdict</th><th class="num">Confidence</th><th>Reason</th></tr></thead>
    <tbody>
    ${recent.map((c) => html`<tr>
      <td>${shortDate(c.received_at || c.created_at)}</td>
      <td>${c.from_addr}</td>
      <td>${c.subject}${when(c.attachments, html`<span class="sub">${c.attachments}</span>`)}</td>
      <td><span class="status ${c.verdict === "invoice" ? "approved" : "not_invoice"}">${c.verdict === "invoice" ? "Invoice" : "Not an invoice"}</span>${when(c.document_kind, html`<span class="sub">${c.document_kind}</span>`)}${when(c.label_applied, html`<span class="sub">label: ${c.label_applied}</span>`)}</td>
      <td class="num">${Math.round((c.confidence || 0) * 100)}%</td>
      <td>${c.reason}</td>
    </tr>`)}
    </tbody>
  </table>`
    : ""}
</section>`;
}

function schemaRows(schema, prefix = "") {
  const rows = [];
  for (const [name, def] of Object.entries(schema.properties || {})) {
    const path = prefix ? `${prefix}.${name}` : name;
    if (def.type === "object" && def.properties) {
      rows.push([path, def.description || "", true]);
      rows.push(...schemaRows(def, path));
    } else if (def.type === "array" && def.items && def.items.properties) {
      rows.push([path + "[]", def.description || "", true]);
      rows.push(...schemaRows(def.items, path + "[]"));
    } else {
      const type = Array.isArray(def.type) ? def.type.filter((t) => t !== "null").join("/") : def.type || "";
      const choice = def.enum ? ` one of: ${def.enum.join(", ")}` : "";
      rows.push([path, `${def.description || ""}${choice}`.trim() || type, false]);
    }
  }
  return rows;
}

export function extractionPage({ s, models, connected, claudeReady, lastRun, ragInfo, continueRun, schema, counts }) {
  const canRun = connected && claudeReady;
  return html`${when(continueRun, continueNudge("/agents/extraction/run", "invoices to extract"))}
<div class="pagehead">
  <div>
    <h1>Agent - Invoice Extraction</h1>
    <div class="meta">Reads every email labelled <code>${s.classify_invoice_label}</code>, sends each PDF or image to Claude with the invoice record schema, maps the result to a Sage Intacct bill and files it in the <a href="/">Inbox</a> for review. ${agentStatusLine(lastRun, connected, claudeReady)}</div>
  </div>
  <div class="actions">
    <button class="btn" type="submit" form="extraction-form">Save</button>
    <button class="btn primary" type="submit" form="extraction-form" name="action" value="run" data-run${disabled(!canRun)}>Extract now</button>
  </div>
</div>

<form id="extraction-form" method="post" action="/agents/extraction" autocomplete="off">
<section class="sheet">
  <h2>AI model</h2>
  <p class="help">Reads the whole document, so this is where model quality matters. Opus 5 is the default; Sonnet 5 is faster and cheaper and still plenty for most invoices. The key comes from Settings.</p>
  <div class="field"><label for="extract_model">Model</label>${modelSelect("extract_model", s.extract_model, models)}</div>
</section>

<section class="sheet">
  <h2>Reference text (RAG)</h2>
  <p class="help">What the agent should know when reading an invoice: how particular suppliers word things, which references are project or site codes, CIS and retention conventions, application-for-payment layouts, examples of invoices it has misread. The relevant paragraphs are retrieved and added to the instructions for every document.</p>
  ${ragField("extract_reference_text", s.extract_reference_text, ragInfo, "")}
</section>

<section class="sheet">
  <h2>Mail handling</h2>
  <div class="field">
    <label>Reads label</label>
    <div><code>${s.classify_invoice_label}</code><div class="hint">Set on the <a href="/agents/classification">classification agent</a>. Emails carrying it that have not been processed yet are read, ${s.gmail_max_messages} at most per run, one email per step.</div></div>
  </div>
  <div class="field">
    <label for="gmail_processed_label">Processed label</label>
    <input id="gmail_processed_label" name="gmail_processed_label" type="text" value="${s.gmail_processed_label}">
    <div class="hint">Added to each email after its attachments are read, and the email is marked read, so nothing is extracted twice. Created if it does not exist.</div>
  </div>
  <div class="field">
    <label for="poll_minutes">Automatic checks</label>
    <div><input id="poll_minutes" name="poll_minutes" type="number" min="0" max="1440" step="5" value="${s.poll_minutes}" style="max-width:8rem"><div class="hint">minutes between automatic runs of both agents, in steps of 5 (0 = only when you click). Each automatic run classifies a batch and extracts a couple of invoices; the next run picks up the rest.</div></div>
  </div>
</section>

<section class="sheet">
  <h2>What it extracts</h2>
  <p class="help">The record every document is read into. Totals are re-checked arithmetically after extraction; anything that cannot be mapped to Sage becomes a check on the invoice.</p>
  <table class="ledger">
    <thead><tr><th>Field</th><th>Meaning</th></tr></thead>
    <tbody>
    ${schemaRows(schema).map(([name, desc, group]) => html`<tr><td><code>${name}</code></td><td>${group ? html`<em>${desc || "group"}</em>` : desc}</td></tr>`)}
    </tbody>
  </table>
</section>
<div class="actions"><button class="btn primary" type="submit">Save</button></div>
</form>

<section class="sheet">
  <h2>So far</h2>
  <p class="help">${counts.total ? html`${counts.total} document${counts.total === 1 ? "" : "s"} in the Inbox: ${counts.review} needing review, ${counts.approved} approved, ${counts.posted} posted, ${counts.queried} queried, ${counts.not_invoice} not invoices, ${counts.error} failed.` : "Nothing extracted yet. Run the classification agent first, or upload a PDF on the Inbox."}</p>
</section>`;
}

// --------------------------------------------------------------------------- Agent tree

function runLine(run) {
  if (!run) return "Not run yet";
  return `Last run ${shortDate(run.finished_at || run.started_at)}${run.finished_at ? ` ${timeOf(run.finished_at)} UTC` : ""}`;
}

function node({ href, kind = "", name, lines = [], stat = "" }) {
  return html`<a class="node ${kind}" href="${href}">
    <span class="name">${name}</span>
    ${lines.filter(Boolean).map((line) => html`<span class="meta">${line}</span>`)}
    ${when(stat, html`<span class="stat">${stat}</span>`)}
  </a>`;
}

export function agentTreePage({ s, gmail, claudeReady, sageReady, classifyRun, extractRun, classCounts, invoiceCounts }) {
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const mailbox = node({
    href: "/settings#gmail",
    name: "Gmail mailbox",
    lines: [
      gmail.connected ? `Connected as ${gmail.email}` : gmail.error ? `Token problem - reconnect in Settings` : "Not connected - open Settings",
      claudeReady ? "Claude key in place" : "No Claude API key yet",
    ],
    stat: `search: ${s.gmail_query || "has:attachment is:unread"}`,
  });
  const classification = node({
    href: "/agents/classification",
    kind: "agent",
    name: "Agent - Classification",
    lines: [modelLabel(s.classify_model), runLine(classifyRun)],
    stat: classCounts.total ? `${plural(classCounts.total, "email")} classified: ${plural(classCounts.invoice, "invoice")}, ${classCounts.not_invoice} not` : "Nothing classified yet",
  });
  const extraction = node({
    href: "/agents/extraction",
    kind: "agent",
    name: "Agent - Invoice Extraction",
    lines: [modelLabel(s.extract_model), runLine(extractRun)],
    stat: invoiceCounts.total ? `${plural(invoiceCounts.total, "document")} read, ${invoiceCounts.error} failed` : "Nothing extracted yet",
  });
  const inbox = node({
    href: "/",
    name: "Inbox - review",
    lines: ["Check the fields and the Key into Sage sheet, then approve"],
    stat: `${invoiceCounts.review} needing review · ${invoiceCounts.approved} approved · ${invoiceCounts.queried} queried`,
  });
  const sageNode = node({
    href: "/settings#sage",
    name: "Sage Intacct",
    lines: [sageReady ? "Connected - approved bills can be posted as Draft" : "Not connected - key in from the entry sheet, or download JSON / XML / CSV"],
    stat: `${plural(invoiceCounts.posted, "bill")} posted or keyed in`,
  });
  const stopped = node({
    href: "/agents/classification#decisions",
    kind: "leaf",
    name: "Stops here",
    lines: ["Statements, remittances, quotes and the rest stay in the mailbox with this label"],
    stat: `${plural(classCounts.not_invoice, "email")} so far`,
  });

  return html`<div class="pagehead">
  <div>
    <h1>Agent tree</h1>
    <div class="meta">How an email travels through the agents. Click a box to open it; the red-edged boxes are the agents.</div>
  </div>
  <div class="actions">
    <a class="btn" href="/agents/classification">Open classification</a>
    <a class="btn" href="/agents/extraction">Open extraction</a>
  </div>
</div>

<div class="tree">
  <ul>
    <li>${mailbox}
      <ul>
        <li><span class="edge">new mail matching the search</span>${classification}
          <ul>
            <li><span class="edge">labels “${s.classify_invoice_label}”</span>${extraction}
              <ul>
                <li><span class="edge">files each document, labels “${s.gmail_processed_label || "processed"}”</span>${inbox}
                  <ul>
                    <li><span class="edge">approve, then post or key in</span>${sageNode}</li>
                  </ul>
                </li>
              </ul>
            </li>
            <li><span class="edge">labels “${s.classify_other_label || "(nothing)"}”</span>${stopped}</li>
          </ul>
        </li>
      </ul>
    </li>
  </ul>
</div>`;
}

// --------------------------------------------------------------------------- Invoice types

const tinyLabel = (id, text) => html`<label for="${id}" style="font-size:0.85rem;color:var(--ink-soft)">${text}</label>`;

function typeSheet(t, categories) {
  const f = (name) => `${t.id}__${name}`;
  return html`<section class="sheet type" id="type-${t.id}">
  <div class="typehead">
    <h2>${t.name}</h2>
    <button class="btn small quiet" type="submit" name="action" value="remove:${t.id}" formnovalidate onclick="return confirm('Remove the ${t.name} type? Invoices already coded keep their coding.')">Remove type</button>
  </div>
  <div class="field"><label for="${f("name")}">Name</label><input id="${f("name")}" name="${f("name")}" type="text" value="${t.name}" required></div>
  <div class="field"><label for="${f("description")}">What it covers</label><input id="${f("description")}" name="${f("description")}" type="text" value="${t.description}"></div>

  <div class="field">
    <label>Matched when</label>
    <div>
      <div class="hint" style="margin:0 0 0.4rem">In this order: the supplier is listed below; one of the ticked signals is on the invoice; the lines are mostly a ticked category. Any invoice can be moved to another type by hand on its review page.</div>
      <textarea id="${f("suppliers")}" name="${f("suppliers")}" placeholder="One supplier name per line, matched like the vendor map" style="min-height:3.5rem">${t.suppliers}</textarea>
      <div class="checks" style="margin-top:0.5rem">
        ${TYPE_SIGNALS.map(([key, label]) => html`<label><input type="checkbox" name="${f("sig_" + key)}"${checked(t.signals.includes(key))}> ${label}</label>`)}
      </div>
      <div class="checks" style="margin-top:0.3rem">
        ${categories.map((c) => html`<label><input type="checkbox" name="${f("cat_" + c)}"${checked(t.categories.includes(c))}> mostly ${c}</label>`)}
      </div>
    </div>
  </div>

  <div class="field">
    <label>GL account by line category<small>blank = Settings</small></label>
    <div>
      <div class="row">
        ${categories.map((c) => html`<div>${tinyLabel(f("gl_" + c), c)}<input id="${f("gl_" + c)}" name="${f("gl_" + c)}" type="text" value="${t.gl_map[c] || ""}"></div>`)}
      </div>
    </div>
  </div>
  <div class="field">
    <label>VAT tax details<small>blank = Settings</small></label>
    <div class="row">
      <div>${tinyLabel(f("vat_20"), "20% standard")}<input id="${f("vat_20")}" name="${f("vat_20")}" type="text" value="${t.vat_detail_map["20"] || ""}"></div>
      <div>${tinyLabel(f("vat_5"), "5% reduced")}<input id="${f("vat_5")}" name="${f("vat_5")}" type="text" value="${t.vat_detail_map["5"] || ""}"></div>
      <div>${tinyLabel(f("vat_0"), "0% / exempt")}<input id="${f("vat_0")}" name="${f("vat_0")}" type="text" value="${t.vat_detail_map["0"] || ""}"></div>
      <div>${tinyLabel(f("vat_rc"), "reverse charge")}<input id="${f("vat_rc")}" name="${f("vat_rc")}" type="text" value="${t.vat_detail_map.reverse_charge || ""}"></div>
    </div>
  </div>
  <div class="field">
    <label>Deductions</label>
    <div>
      <div class="checkline" style="margin-top:0.2rem">
        <label><input type="checkbox" name="${f("cis_applies")}"${checked(t.cis_applies)}> CIS applies</label>
        <label>Default rate % <input name="${f("cis_rate")}" type="text" value="${t.cis_rate || ""}"></label>
        <label>Retention % <input name="${f("retention_percent")}" type="text" value="${t.retention_percent || ""}"></label>
        <label>Terms days <input name="${f("terms_days")}" type="text" value="${t.terms_days === null ? "" : t.terms_days}"></label>
      </div>
      <div class="hint">Used only where the invoice is silent: a CIS deduction is computed on the labour lines at the default rate, retention on the net total, and the terms pick the Intacct term name. Blank retention or terms means only what the invoice shows.</div>
    </div>
  </div>
  <div class="field">
    <label>Checks</label>
    <div>
      <div class="checks">
        <label><input type="checkbox" name="${f("po_required")}"${checked(t.po_required)}> Needs a purchase order number</label>
        <label><input type="checkbox" name="${f("project_required")}"${checked(t.project_required)}> Needs a Sage project</label>
      </div>
      <div class="row" style="margin-top:0.5rem"><div>${tinyLabel(f("expected_vat"), "VAT treatment expected")}<select id="${f("expected_vat")}" name="${f("expected_vat")}">${EXPECTED_VAT.map(([v, label]) => html`<option value="${v}"${selected(t.expected_vat, v)}>${label}</option>`)}</select></div></div>
      <div class="hint">A missing PO or project blocks Approve; an unexpected VAT treatment is noted for the reviewer.</div>
    </div>
  </div>
  <div class="field">
    <label>Posting<small>blank = Settings</small></label>
    <div class="row">
      <div>${tinyLabel(f("location_id"), "Location ID")}<input id="${f("location_id")}" name="${f("location_id")}" type="text" value="${t.location_id}"></div>
      <div>${tinyLabel(f("department_id"), "Department ID")}<input id="${f("department_id")}" name="${f("department_id")}" type="text" value="${t.department_id}"></div>
      <div>${tinyLabel(f("sage_action"), "Create bills as")}<select id="${f("sage_action")}" name="${f("sage_action")}"><option value=""${selected(t.sage_action, "")}>As in Settings</option><option value="Draft"${selected(t.sage_action, "Draft")}>Draft</option><option value="Submit"${selected(t.sage_action, "Submit")}>Submit</option></select></div>
    </div>
  </div>
</section>`;
}

export function typesPage({ types, categories }) {
  return html`<div class="pagehead">
  <div>
    <h1>Invoice types</h1>
    <div class="meta">Different kinds of invoice need different coding. Each type overrides the Sage coding in <a href="/settings">Settings</a> for its kind, fills in what the invoice leaves out, and decides which checks block approval. Every invoice is matched to one type and can be moved by hand on its review page.</div>
  </div>
  <div class="actions">
    <button class="btn" type="submit" form="types-form" name="action" value="add" formnovalidate>Add a type</button>
    <button class="btn primary" type="submit" form="types-form">Save</button>
  </div>
</div>
<form id="types-form" method="post" action="/types" autocomplete="off">
  ${types.map((t) => typeSheet(t, categories))}
  <div class="actions"><button class="btn primary" type="submit">Save</button><button class="btn" type="submit" name="action" value="add" formnovalidate>Add a type</button></div>
</form>`;
}

// --------------------------------------------------------------------------- Settings

export function eye() {
  return raw(
    '<svg class="eye-on" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>' +
      '<svg class="eye-off" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 3l18 18"/><path d="M10.6 10.6a3 3 0 0 0 4.2 4.2"/><path d="M9.9 5.1A10.8 10.8 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-3.2 4.1"/><path d="M6.6 6.6C3.9 8.4 2 12 2 12s3.5 7 10 7a9.7 9.7 0 0 0 4.4-1"/></svg>',
  );
}

function secret(id, label, value, { hint = "", placeholder = "", small = "" } = {}) {
  return html`<div class="field">
  <label for="${id}">${label}${when(small, html`<small>${small}</small>`)}</label>
  <div class="secret">
    <input id="${id}" name="${id}" type="password" value="${value || ""}" autocomplete="new-password" spellcheck="false" placeholder="${placeholder}">
    <button type="button" class="reveal" aria-label="Show" title="Show">${eye()}</button>
  </div>
  ${when(hint, html`<div class="hint">${hint}</div>`)}
</div>`;
}

const smallLabel = (id, text) => html`<label for="${id}" style="font-size:0.85rem;color:var(--ink-soft)">${text}</label>`;

export function settingsPage({ s, gmail, redirectUri, vendorMapText, projectMapText, termsMapText, categories }) {
  return html`<div class="pagehead">
  <div>
    <h1>Settings</h1>
    <div class="meta">Keys are stored in this app's own database on Cloudflare, behind the sign-in. Paste, reveal to check, save.</div>
  </div>
  <div class="actions"><button class="btn primary" type="submit" form="settings-form">Save settings</button></div>
</div>

<form id="settings-form" method="post" action="/settings" autocomplete="off">

<section class="sheet">
  <h2>Claude</h2>
  <p class="help">Both agents use this key. Get it from the Claude Console; usage is billed per document (a typical two-page invoice is a few thousand tokens). Each agent picks its own model on its tab.</p>
  ${secret("anthropic_api_key", "API key", s.anthropic_api_key, { placeholder: "sk-ant-…" })}
  <div class="field">
    <label></label>
    <div><button type="button" class="btn small" id="test-claude">Test key</button><div class="test-result" id="test-claude-result"></div></div>
  </div>
</section>

<section class="sheet" id="gmail">
  <h2>Gmail</h2>
  <p class="help">
    In Google Cloud Console create a project, enable the <strong>Gmail API</strong>, then under <em>APIs &amp; Services › Credentials</em> create an <strong>OAuth client ID</strong> of type <strong>Web application</strong> and add <code>${redirectUri}</code> as an authorised redirect URI. Paste its client ID and secret here, save, then connect. While the consent screen is in testing mode, add the mailbox as a test user.
  </p>
  ${secret("google_client_id", "OAuth client ID", s.google_client_id, { placeholder: "…apps.googleusercontent.com" })}
  ${secret("google_client_secret", "OAuth client secret", s.google_client_secret, { placeholder: "GOCSPX-…" })}
  <div class="field">
    <label>Connection</label>
    <div>
      ${gmail.connected
        ? html`<span class="connected">Connected</span> as ${gmail.email}
        <button class="btn small quiet" type="submit" formaction="/gmail/disconnect" formmethod="post" formnovalidate>Disconnect</button>`
        : gmail.error
          ? html`<span class="disconnected">Token problem</span> — ${gmail.error}
        <button class="btn small" type="submit" name="action" value="connect_gmail" formnovalidate>Reconnect Gmail</button>`
          : html`<span class="disconnected">Not connected</span>
        <button class="btn small" type="submit" name="action" value="connect_gmail" formnovalidate>Connect Gmail</button>
        <div class="hint">Saves everything on this page, then opens Google's sign-in for the invoices mailbox.</div>`}
    </div>
  </div>
  <div class="field">
    <label>What is read</label>
    <div class="hint" style="grid-column:2;margin-top:0.4rem">The search, sender filter and labels live on the two agent tabs: <a href="/agents/classification">Classification</a> decides which mail carries invoices, <a href="/agents/extraction">Invoice Extraction</a> reads them.</div>
  </div>
</section>

<section class="sheet">
  <h2>Company</h2>
  <div class="field"><label for="company_name">Company name</label><input id="company_name" name="company_name" type="text" value="${s.company_name}"></div>
  <div class="field"><label for="default_currency">Base currency</label><input id="default_currency" name="default_currency" type="text" value="${s.default_currency}" style="max-width:8rem"></div>
</section>

<section class="sheet" id="sage">
  <h2>Sage Intacct connection</h2>
  <p class="help">Optional. With these filled in, an approved invoice can be posted straight into Intacct as an AP bill. Without them the app still gives you the bill ready to key in, plus JSON/XML/CSV downloads. Web Services must be enabled on the Intacct company and the sender ID authorised for it.</p>
  <div class="field"><label for="sage_endpoint">Endpoint</label><input id="sage_endpoint" name="sage_endpoint" type="text" value="${s.sage_endpoint}"></div>
  ${secret("sage_sender_id", "Web services sender ID", s.sage_sender_id)}
  ${secret("sage_sender_password", "Web services password", s.sage_sender_password)}
  ${secret("sage_company_id", "Company ID", s.sage_company_id)}
  ${secret("sage_user_id", "User ID", s.sage_user_id, { hint: "A dedicated Web Services user is best, so posts show under a recognisable name." })}
  ${secret("sage_user_password", "User password", s.sage_user_password)}
  <div class="field">
    <label for="sage_action">Create bills as</label>
    <select id="sage_action" name="sage_action" style="max-width:14rem">
      <option value="Draft"${selected(s.sage_action, "Draft")}>Draft (review inside Intacct)</option>
      <option value="Submit"${selected(s.sage_action, "Submit")}>Submit (post immediately)</option>
    </select>
  </div>
  <div class="field">
    <label></label>
    <div><button type="button" class="btn small" id="test-sage">Test connection</button><div class="test-result" id="test-sage-result"></div></div>
  </div>
</section>

<section class="sheet">
  <h2>Sage coding</h2>
  <p class="help">The company-wide coding. The <a href="/types">Invoice types</a> tab overrides GL accounts, VAT details, location and department per kind of invoice; blanks there fall back to what is here. The values shown to begin with are generic starting points for a UK contractor paying subcontractors and suppliers: Sage-style nominal codes and Intacct's standard UK VAT tax detail names. IDs must match what exists in your Intacct company exactly - copy them from the vendor, GL account, project and tax detail lists there, because Intacct rejects a bill whose account, vendor or tax detail it does not know.</p>
  <div class="field"><label for="sage_location_id">Location ID</label><input id="sage_location_id" name="sage_location_id" type="text" value="${s.sage_location_id}" placeholder="e.g. LON" style="max-width:14rem"><div class="hint">Blank is fine unless the Intacct company runs several locations; a wrong one makes Intacct reject the bill.</div></div>
  <div class="field"><label for="sage_department_id">Department ID</label><input id="sage_department_id" name="sage_department_id" type="text" value="${s.sage_department_id}" placeholder="e.g. MEP" style="max-width:14rem"></div>
  <div class="field"><label for="sage_tax_solution_id">Tax solution</label><input id="sage_tax_solution_id" name="sage_tax_solution_id" type="text" value="${s.sage_tax_solution_id}" style="max-width:20rem"></div>

  <div class="field">
    <label>GL account by line category</label>
    <div>
      <div class="row">
        ${categories.map((cat) => html`<div>${smallLabel(`gl_${cat}`, cat)}<input id="gl_${cat}" name="gl_${cat}" type="text" value="${(s.gl_map || {})[cat] || ""}"></div>`)}
      </div>
      <div class="row" style="margin-top:0.5rem">
        <div>${smallLabel("sage_default_gl", "fallback")}<input id="sage_default_gl" name="sage_default_gl" type="text" value="${s.sage_default_gl}"></div>
        <div>${smallLabel("sage_cis_gl", "CIS control")}<input id="sage_cis_gl" name="sage_cis_gl" type="text" value="${s.sage_cis_gl}"></div>
        <div>${smallLabel("sage_retention_gl", "retention control")}<input id="sage_retention_gl" name="sage_retention_gl" type="text" value="${s.sage_retention_gl}"></div>
      </div>
      <div class="hint">CIS deductions and retentions are added as negative lines to the control accounts when set; otherwise they are flagged for manual posting. Generic codes: ${GENERIC_CODES.map(([code, meaning]) => `${code} ${meaning}`).join(" · ")}.</div>
    </div>
  </div>

  <div class="field">
    <label>VAT tax details<small>Intacct tax detail IDs</small></label>
    <div>
      <div class="row">
        <div>${smallLabel("vat_20", "20% standard")}<input id="vat_20" name="vat_20" type="text" value="${(s.vat_detail_map || {})["20"] || ""}"></div>
        <div>${smallLabel("vat_5", "5% reduced")}<input id="vat_5" name="vat_5" type="text" value="${(s.vat_detail_map || {})["5"] || ""}"></div>
        <div>${smallLabel("vat_0", "0% / exempt")}<input id="vat_0" name="vat_0" type="text" value="${(s.vat_detail_map || {})["0"] || ""}"></div>
        <div>${smallLabel("vat_rc", "reverse charge")}<input id="vat_rc" name="vat_rc" type="text" value="${(s.vat_detail_map || {}).reverse_charge || ""}"></div>
      </div>
      <div class="hint">e.g. <code>UK Purchase Goods Standard Rate</code>, <code>UK Purchase Services Reverse Charge Standard Rate</code> - whatever your tax solution calls them.</div>
    </div>
  </div>

  <div class="field">
    <label for="terms_map">Payment terms<small>days = Intacct term name</small></label>
    <textarea id="terms_map" name="terms_map">${termsMapText}</textarea>
  </div>
  <div class="field">
    <label for="vendor_map">Vendor map<small>supplier name = vendor ID</small></label>
    <textarea id="vendor_map" name="vendor_map" placeholder="Kingspan Insulation Ltd = V0042&#10;Speedy Hire = V0117">${vendorMapText}</textarea>
    <div class="hint">Matching ignores case, punctuation and Ltd/Limited/PLC. Anything unmatched is flagged on the invoice, where you can type the ID directly. The entry shown to begin with is the fictional supplier on the sample invoice - remove it once real suppliers are in.</div>
  </div>
  <div class="field">
    <label for="project_map">Project map<small>site or PO prefix = project ID</small></label>
    <textarea id="project_map" name="project_map" placeholder="HEL18 = P-HEL18&#10;AMS01 = P-AMS01">${projectMapText}</textarea>
    <div class="hint">Matched against the project reference, PO number, site address and email subject. HEL18 is the fictional project on the sample invoice.</div>
  </div>
</section>

<div class="actions"><button class="btn primary" type="submit">Save settings</button></div>
</form>`;
}

export { esc };

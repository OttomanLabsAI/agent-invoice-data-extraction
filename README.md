# Invoice intake agent

Two agents on a Cloudflare Worker, working a shared Gmail inbox for Glent Group's accounts team. The first reads new mail and labels the invoices; the second reads each labelled invoice with Claude and turns it into an AP bill ready for Sage Intacct - with a review screen, a "key this into Sage" sheet, JSON/XML payloads and a CSV that drops into the purchase invoice log.

```
Gmail (invoices@…) ──► Agent - Classification labels "Invoice Incoming" ──► Agent - Invoice Extraction reads the PDF/image ──► Sage-shaped bill ──► review ──► post / export
```

It runs in the browser at the Worker's address. Everyone on the team signs in with one shared app password.

## Deploy it (once, by whoever looks after Cloudflare)

1. Connect this repository in the Cloudflare dashboard: **Workers & Pages → Create → Import a repository**. Keep the default deploy command (`npx wrangler deploy`). Every push to `main` deploys.
2. The first deploy creates the D1 database `invoice-agent` and the R2 bucket `invoice-agent-files` named in `wrangler.jsonc`. R2 has to be enabled on the account once (R2 in the dashboard, accept the terms). If the build reports that it cannot create them, run `npx wrangler d1 create invoice-agent` and `npx wrangler r2 bucket create invoice-agent-files`, put the printed `database_id` into `wrangler.jsonc`, and push again.
3. Open the Worker → **Settings → Variables and Secrets** and add a **secret** named `APP_PASSWORD`. Until it exists the app refuses every visitor and says so.
4. Open the Worker's address and sign in.

The Workers Paid plan is recommended: reading a large PDF into a request needs more CPU time than the free plan allows, and the paid plan also lifts the per-request limit on Gmail and Claude calls. Cloudflare Access can be put in front of the Worker later for per-person sign-in without any change to the app.

## Set up (Settings page)

Every key field is masked; the eye button reveals it so you can check a paste. Keys are stored in the app's own database on Cloudflare, behind the sign-in.

**Claude** - paste an API key from the Claude Console. *Test key* checks it without spending tokens. Each agent chooses its own model on its tab.

**Gmail** - the app talks to the Gmail API with OAuth, so it needs an OAuth client rather than a password:

1. Google Cloud Console → create a project → *APIs & Services → Library* → enable **Gmail API**.
2. *OAuth consent screen* → External → add the invoices mailbox as a test user (or publish the app if it is a Workspace account).
3. *Credentials → Create credentials → OAuth client ID → Web application* → add the redirect URI shown on the Settings page (`https://<your worker>/oauth/callback`). Paste the client ID and secret into Settings, save.
4. Click **Connect Gmail**, sign in as the invoices mailbox, allow access. The token is stored in the database and refreshes itself.

**Sage Intacct** - optional. With Web Services credentials (sender ID + password, company ID, user ID + password) an approved invoice can be posted straight in as an AP bill, as *Draft* by default so accounts can check it inside Intacct before it posts. Without them, everything still works: you get the entry sheet, JSON, the XML gateway body, and the CSV.

**Sage coding** - the maps that turn extracted lines into Intacct fields:

| Setting | Used for |
|---|---|
| Vendor map | supplier name on the invoice → `VENDORID` (fuzzy on case / punctuation / Ltd) |
| GL account by category | labour / materials / plant / subcontract / services / expenses / other → `ACCOUNTNO` |
| VAT tax details | 20 / 5 / 0 / reverse charge → Intacct tax `DETAILID` |
| Project map | site code or PO prefix → `PROJECTID` |
| Payment terms | days on the invoice → `TERMNAME` |
| CIS / retention control | deductions become negative lines to these accounts |
| Location / department | stamped on every line |

Anything that cannot be mapped is listed as a check on the invoice and blocks *Approve* until you fill it in (you can type a vendor or project ID straight on the invoice).

## The two agents

**Agent - Classification** reads new mail and decides, email by email, whether it carries an invoice. Its tab holds the model (Haiku 4.5 by default - it is a short call per email), the reference text, the Gmail search it sweeps (`has:attachment is:unread` by default), an optional sender allow-list, and the two labels it applies: `Invoice Incoming` for invoices and `Not an invoice` for everything else, so nothing is read twice. It never marks mail read. The tab lists its recent decisions with the reason for each. You can also apply the `Invoice Incoming` label by hand in Gmail to push an email through.

**Agent - Invoice Extraction** reads every email labelled `Invoice Incoming` that has not been processed, sends each PDF or image to Claude with the invoice record schema, maps the result to a Sage Intacct bill and files it in the Inbox for review. Its tab holds the model (Sonnet 5 by default), the reference text, the processed label (`Invoices/Processed`, added with the email marked read once its attachments are in), the automatic-check interval, and the full list of fields it extracts.

**Reference text (RAG)** - each agent has a free-text box for what it should know: supplier names and how they word things, which references are project codes, CIS and retention conventions, senders whose mail is never an invoice, examples it has got wrong. Text under about 16,000 characters is sent whole with every call; longer text is split into paragraphs and only the ones that share words with the email are retrieved and sent.

Both agents work in small batches per click (five emails classified, one email extracted) and the page keeps going by itself while more mail is waiting; *Stop* on the page halts it. With **Automatic checks** set to a number of minutes, a cron trigger runs both agents on that cadence without anyone clicking.

## Workflow

1. **Check inbox now** (or set automatic checks on the extraction tab) - or drag a PDF onto *Process upload* to test.
2. Each attachment appears in the Inbox as *Needs review* with the number of open checks.
3. Open it: the left side is the extracted data (editable, with the line editor recalculating new lines), the right side is the document and the **Key into Sage** sheet in the order of Intacct's bill screen.
4. **Save changes** rebuilds the Sage payload. **Approve** once the checks are clear.
5. **Post to Sage** (if connected) or **Mark keyed in by hand**; **Query** parks it.
6. **Download log CSV** on the Inbox for the purchase invoice log - one row per invoice, columns match the entry sheet.

## What gets extracted

For every invoice or credit note: supplier (name, address, VAT and company numbers, remittance email), invoice number and dates, payment terms, PO number and other references, project / site, one-line description, currency, every line with net / VAT rate / VAT / category, net-VAT-gross totals with a VAT breakdown, VAT treatment (including the domestic reverse charge for construction services), CIS labour/materials split and deduction, retention withheld, amount payable, bank details for the payment run, plus a confidence score and a list of things to check by hand. Totals are re-checked arithmetically after extraction.

Statements, remittances, quotes and marketing PDFs are filed under *Not an invoice* rather than mapped.

## Local development and tests

```bash
npm install
cp .dev.vars.example .dev.vars   # set APP_PASSWORD
npm run dev                      # http://localhost:8787 with a local database and bucket
npm test                         # unit tests: mapper, normalisation, retrieval, CSV, helpers
npm run smoke                    # end-to-end against wrangler dev with the fake mailbox and fake Claude
npm run check                    # wrangler deploy --dry-run
```

`npm run smoke` starts the Worker with `TEST_FIXTURES=1`, which swaps in a three-email fake mailbox and canned Claude answers; nothing external is called. Never set that variable on the deployed Worker.

## Files

```
src/index.js          routing, sign-in gate, security headers, cron entry
src/routes.js         page handlers (no business logic)
src/pipeline.js       the two agents and the inbox check
src/claude.js         Claude Messages API: extraction, classification, key check
src/gmail.js          Gmail REST API and OAuth
src/schema.js         record_invoice / label_email tool schemas and system prompts
src/normalise.js      coerce numbers, recompute totals, check arithmetic
src/rag.js            reference-text retrieval by paragraph
src/sage_mapper.js    record → APBILL / APADJUSTMENT payload, XML, entry sheet, log row
src/sage_client.js    Intacct XML gateway: test connection, create bill
src/settings.js       defaults, D1 storage, key=value map parsing, model list
src/db.js             D1 schema and queries: settings, state, invoices, runs, classifications
src/auth.js           app password and session cookie
src/fixtures.js       fake mailbox and fake Claude for the smoke test
src/views/            html helpers, layout with the tabs, every page
public/               assets served as-is: style.css (ledger-paper look), app.js, favicon
tests/                unit.test.js, smoke.js
samples/              a fictional subcontractor invoice to test with (make_sample.py regenerates it; Python + reportlab, dev only)
wrangler.jsonc        Worker, D1, R2, assets and cron configuration
```

## Things to know

- The Intacct field names follow the documented `APBILL` create object (`WHENCREATED`, `WHENDUE`, `VENDORID`, `RECORDID`, `DOCNUMBER`, `TERMNAME`, `TAXSOLUTIONID`, `APBILLITEMS/APBILLITEM` with `ACCOUNTNO`, `TRX_AMOUNT`, `LOCATIONID`, `DEPARTMENTID`, `PROJECTID`, `TAXENTRIES`). Credit notes map to `APADJUSTMENT` with negative amounts. The PO number goes in `DOCNUMBER` (reference); change `build` in `src/sage_mapper.js` if your Intacct uses PO matching through Purchasing instead.
- The Intacct push has been written against the XML gateway spec but not exercised against a live company - do the first post as *Draft* and compare against a bill keyed by hand.
- Attachments live in the R2 bucket and extracted data in the D1 database, both inside your Cloudflare account. Delete a row from the Inbox to remove both; the email keeps its labels.
- Only PDF, PNG, JPG and WEBP attachments are read; images under 20 KB are treated as logos and skipped.
- The cron trigger fires every five minutes; the automatic-check interval on the extraction tab decides whether a run is due, so anything under five minutes behaves as five.

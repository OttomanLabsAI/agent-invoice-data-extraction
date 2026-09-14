# Invoice intake agent

Watches a Gmail inbox for supplier invoices, reads each attachment with Claude, and turns it into an AP bill ready for Sage Intacct - with a review screen, a "key this into Sage" sheet, JSON/XML payloads and a CSV that drops into the purchase invoice log.

```
Gmail (invoices@…)  ──►  Claude reads the PDF/image  ──►  Sage-shaped bill  ──►  review  ──►  post / export
```

Runs locally on one machine at `http://localhost:8765`. Nothing is hosted; keys stay in `data/settings.json`.

## Run it

```bash
./run.sh          # macOS / Linux (creates .venv, installs, starts)
run.bat           # Windows
```

or by hand: `pip install -r requirements.txt` then `python app.py`. Python 3.11 or newer.

## Set up (Settings page)

Every key field is masked; the eye button reveals it so you can check a paste.

**Claude** - paste an API key from the Claude Console. *Test key* checks it without spending tokens. Sonnet 5 is the default and is plenty for invoices; Haiku 4.5 is cheaper if volume gets high.

**Gmail** - the app talks to the Gmail API with OAuth, so it needs an OAuth client rather than a password:

1. Google Cloud Console → create a project → *APIs & Services → Library* → enable **Gmail API**.
2. *OAuth consent screen* → External → add the invoices mailbox as a test user (or publish the app if it is a Workspace account).
3. *Credentials → Create credentials → OAuth client ID → Desktop app*. Paste the client ID and secret into Settings, save.
4. Click **Connect Gmail**, sign in as the invoices mailbox, allow access. The token lands in `data/google_token.json` and refreshes itself.

If Google complains about `redirect_uri_mismatch`, create the client as *Web application* instead and add the redirect URI shown on the Settings page.

The search box uses Gmail's own syntax (`has:attachment is:unread label:Invoices`, `from:@supplier.co.uk`, etc.). After a message is processed it is marked read and labelled `Invoices/Processed`, and that label is always excluded from the next search, so nothing is read twice.

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

## What gets extracted

For every invoice or credit note: supplier (name, address, VAT and company numbers, remittance email), invoice number and dates, payment terms, PO number and other references, project / site, one-line description, currency, every line with net / VAT rate / VAT / category, net-VAT-gross totals with a VAT breakdown, VAT treatment (including the domestic reverse charge for construction services), CIS labour/materials split and deduction, retention withheld, amount payable, bank details for the payment run, plus a confidence score and a list of things to check by hand. Totals are re-checked arithmetically after extraction.

Statements, remittances, quotes and marketing PDFs are filed under *Not an invoice* rather than mapped.

## Workflow

1. **Check inbox now** (or set a polling interval in Settings) - or drag a PDF onto *Process upload* to test.
2. Each attachment appears in the Inbox as *Needs review* with the number of open checks.
3. Open it: the left side is the extracted data (editable, with the line editor recalculating totals), the right side is the document and the **Key into Sage** sheet in the order of Intacct's bill screen.
4. **Save changes** rebuilds the Sage payload. **Approve** once the checks are clear.
5. **Post to Sage** (if connected) or **Mark keyed in by hand**; **Query** parks it.
6. **Download log CSV** on the Inbox for the purchase invoice log - one row per invoice, columns match the entry sheet.

## Files

```
app.py                 Flask app and routes
agent/config.py        settings defaults and storage (data/settings.json, 0600)
agent/gmail_client.py  OAuth, message + attachment fetch, labelling
agent/extractor.py     Claude call: PDF/image → record_invoice tool schema
agent/sage_mapper.py   record → APBILL / APADJUSTMENT payload, XML, entry sheet, log row
agent/sage_client.py   Intacct XML gateway: test connection, create bill
agent/pipeline.py      run loop, manual upload, background poller
agent/store.py         SQLite (data/invoices.db)
templates/, static/    the three pages
samples/               a fictional subcontractor invoice to test with
tests/smoke_test.py    end-to-end test with the Claude call mocked
public/                the project website, served by Cloudflare Workers (see Website below)
wrangler.jsonc         Cloudflare config for it; package.json carries wrangler
```

Set `INVOICE_AGENT_DATA=/path` to keep `data/` somewhere else (a synced folder, for example) and `INVOICE_AGENT_PORT` to change the port.

## Website

`public/` is a small static site - the front door for the accounts team: what the agent does in four steps, downloads, how to get it running and how to set it up, with screenshots of the three pages. Cloudflare Workers serves it as static assets (no build step; nothing outside `public/` is deployed), and every push to `main` deploys it. The app itself is not hosted - it stays on the accounts machine as described above, and the site holds no data.

The **Download** section offers the latest build as a zip of the `main` branch straight from GitHub, and an **Older versions** chooser. The chooser asks the GitHub API for the repository's releases (falling back to plain tags), lists them newest first, and points its button at the chosen tag's zip. With no releases published yet it says so; if GitHub cannot be reached it points at the releases page instead. Releases are created by hand from the release text in each push, so the chooser fills itself in as they are made.

```bash
npm install
npm run dev      # preview at the address wrangler prints
npm run check    # wrangler deploy --dry-run
```

The site uses the app's own `static/style.css` (copied to `public/assets/css/style.css`) plus `site.css` for layout and `assets/js/downloads.js` for the chooser. No external resources beyond the GitHub API calls: fonts fall back to system faces and the screenshots are local files.

A browser-hosted version of this app (a Cloudflare Worker with two agent tabs, invoice types and database storage) was built and released as v2.0 to v2.7 and then retired in favour of this local app plus download page. It lives on in those tags and the history.

## Things to know

- The Intacct field names follow the documented `APBILL` create object (`WHENCREATED`, `WHENDUE`, `VENDORID`, `RECORDID`, `DOCNUMBER`, `TERMNAME`, `TAXSOLUTIONID`, `APBILLITEMS/APBILLITEM` with `ACCOUNTNO`, `TRX_AMOUNT`, `LOCATIONID`, `DEPARTMENTID`, `PROJECTID`, `TAXENTRIES`). Credit notes map to `APADJUSTMENT` with negative amounts. The PO number goes in `DOCNUMBER` (reference); change `sage_mapper.build` if your Intacct uses PO matching through Purchasing instead.
- The Intacct push has been written against the XML gateway spec but not exercised against a live company - do the first post as *Draft* and compare against a bill keyed by hand.
- Attachments and extracted data are stored locally under `data/`. Delete a row from the Inbox to remove it; the email keeps its processed label.
- Only PDF, PNG, JPG and WEBP attachments are read; images under 20 KB are treated as logos and skipped.

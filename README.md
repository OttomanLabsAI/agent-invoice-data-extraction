# Invoice intake agent

Two Claude agents watch a Gmail inbox for supplier invoices. **Agent - Classification** reads each new email and labels it as an invoice or not; **Agent - Invoice Extraction** reads the labelled invoices and turns each one into an AP bill ready for Sage Intacct - with a review screen, a "key this into Sage" sheet, JSON/XML payloads and a CSV that drops into the purchase invoice log.

```
Gmail (invoices@…)  ──►  Classification: invoice or not?  ──►  Extraction: Claude reads the PDF/image  ──►  Sage-shaped bill  ──►  review  ──►  post / export
```

Runs locally on one machine at `http://localhost:8765`. Nothing is hosted; keys stay in `data/settings.json`.

## Run it

The easy way is the **updater**: download `invoice-agent-updater.zip` from the website, unzip it, keep the *Invoice agent* folder on the desktop and double-click `update.bat` (Windows) or `update.command` (Mac). A page opens in the browser showing the installed and latest versions; *Install the latest version* fetches the app into an `app` folder next to it, *Update* replaces it later (the `data` folder with the settings, the Gmail token and the invoices is carried over, the old version and the zip are deleted), *Older versions* installs any published version, and *Start the app* runs `run.bat` / `run.sh` for you. It needs Python 3 and nothing else; the files are in `updater/`.

By hand:

```bash
./run.sh          # macOS / Linux (creates .venv, installs, starts)
run.bat           # Windows
```

or `pip install -r requirements.txt` then `python app.py`. Python 3.11 or newer. The installed version is in the `VERSION` file.

## The tabs

| Tab | What it is for |
|---|---|
| Inbox | Every document that came in, with its status and open checks. *Check inbox now* runs both agents; *Process upload* reads a file by hand. |
| Agent tree | A clickable map of the mailbox, the two agents, the Inbox and Sage. Each box shows its model, last run and counts; clicking one opens it. |
| Agent - Classification | Model, reference text (RAG), the Gmail search, sender filter, batch size and the two labels. *Classify now* runs it alone; recent decisions are listed with their reasons. |
| Agent - Invoice Extraction | Model, reference text (RAG), the processed label, the timer for automatic runs and the record it fills in, field by field. *Extract now* runs it alone. |
| Invoice types | One sheet per kind of invoice: how it is matched, GL and VAT overrides, CIS / retention / terms defaults, checks and posting options. |
| Settings | Claude key, Gmail OAuth client and connection, company, Sage Intacct connection, and the company-wide Sage coding maps. |

## Set up

Every key field is masked; the eye button reveals it so you can check a paste.

**Claude** (Settings) - paste an API key from the Claude Console. *Test key* checks it without spending tokens. Both agents use the key; each picks its own model on its tab. Claude Opus 5 is the default; Sonnet 5 is faster and cheaper and still plenty for most invoices, Haiku 4.5 is the cheapest for classification.

**Gmail** (Settings) - the app talks to the Gmail API with OAuth, so it needs an OAuth client rather than a password:

1. Google Cloud Console → create a project → *APIs & Services → Library* → enable **Gmail API**.
2. *OAuth consent screen* → External → add the invoices mailbox as a test user (or publish the app if it is a Workspace account).
3. *Credentials → Create credentials → OAuth client ID → Desktop app*. Paste the client ID and secret into Settings, save.
4. Click **Connect Gmail**, sign in as the invoices mailbox, allow access. The token lands in `data/google_token.json` and refreshes itself.

If Google complains about `redirect_uri_mismatch`, create the client as *Web application* instead and add the redirect URI shown on the Settings page.

**Agent - Classification** - the search box uses Gmail's own syntax (`has:attachment is:unread` by default; `label:Invoices`, `from:@supplier.co.uk` and so on). Emails already labelled by either agent are excluded from every search. Each email judged to carry an invoice, credit note or application for payment gets the label `Invoice Incoming`; everything else gets `Not an invoice` (leave that blank to label nothing). Mail from senders outside the optional *Only from* list is labelled as not an invoice without being read. Applying `Invoice Incoming` by hand in Gmail pushes an email through to extraction.

**Agent - Invoice Extraction** - reads every email carrying the invoice label that has not been processed, sends each PDF or image attachment to Claude, and files the result in the Inbox. Processed emails are marked read and labelled `Invoices/Processed`. *Automatic checks* runs both agents every N minutes while the app is open (0 = only when you click).

**Instructions to the AI** - each agent's tab shows the prompt the model is given, word for word, and lets you edit it. *Restore default* brings the shipped wording back (so does saving an empty box). `{company_name}` and `{default_currency}` are filled in from Settings when the call is made. Below it, *What the AI receives* shows the prompt exactly as sent - instructions plus reference text - and the note that goes with each email, so there is no guessing what the model was told.

**Reference text (RAG)** - each agent also has a text box for your own notes, added to the instructions: which suppliers send applications for payment, senders whose mail is never an invoice, project and site codes, how particular suppliers word things, anything it has got wrong before and the right answer. It starts empty; the tab lists examples of what to add. Blank lines separate paragraphs. A text under 16,000 characters is sent whole with every call; a longer one is searched paragraph by paragraph and only the paragraphs that share words with the email (sender, subject, body, attachment names) are sent, in their original order, within the budget. The tab reports the size and whether it is sent whole.

**Sage Intacct** (Settings) - optional. With Web Services credentials (sender ID + password, company ID, user ID + password) an approved invoice can be posted straight in as an AP bill, as *Draft* by default so accounts can check it inside Intacct before it posts. Without them, everything still works: you get the entry sheet, JSON, the XML gateway body, and the CSV.

**Sage coding** (Settings) - the maps that turn extracted lines into Intacct fields. They come pre-filled with generic codes for a UK contractor; every ID is a starting point to replace with, or check against, the lists in your own Intacct.

| Setting | Used for | Generic default |
|---|---|---|
| Vendor map | supplier name on the invoice → `VENDORID` (fuzzy on case / punctuation / Ltd) | the sample supplier, `V0088` |
| GL account by category | labour / materials / plant / subcontract / services / expenses / other → `ACCOUNTNO` | 6000 / 5000 / 7700 / 6002 / 7603 / 7400 / 5002 |
| VAT tax details | 20 / 5 / 0 / reverse charge → Intacct tax `DETAILID` | Intacct's UK purchase goods names, reverse charge services |
| Project map | site code or PO prefix → `PROJECTID` | the sample project, `HEL18 = P-HEL18` |
| Payment terms | days on the invoice → `TERMNAME` | 0, 7, 14, 30, 45, 60, 90 days → Due on receipt, Net 7 … Net 90 |
| CIS / retention control | deductions become negative lines to these accounts | 2214 / 2215 |
| Location / department | stamped on every line | blank |

Anything that cannot be mapped is listed as a check on the invoice and blocks *Approve* until you fill it in (you can type a vendor or project ID straight on the invoice).

**Invoice types** - different kinds of invoice need different coding. Five generic types are set up:

| Type | Matched when | Coding and checks |
|---|---|---|
| Subcontractor | CIS shown, domestic reverse charge, or an application for payment; lines mostly labour or subcontract | every line to 6002, services VAT details, CIS at 20% on labour where the invoice is silent, project required, reverse charge expected |
| Materials supplier | lines mostly materials | materials 5000, delivery / expenses 5100, PO and project required, standard VAT expected |
| Plant hire | lines mostly plant | plant and labour 7700, services VAT details, PO and project required |
| Professional services | lines mostly services | services and labour 7603, services VAT details, standard VAT expected |
| Overheads | anything else | company-wide coding |

A type is matched in this order: chosen by hand on the review page, the supplier is listed on the type, one of its ticked signals is on the invoice, the lines are mostly one of its ticked categories, then the fallback. A type's GL and VAT entries override the company-wide maps only where filled in; its CIS rate, retention percentage and payment terms fill in only what the invoice leaves out (and each such default is noted on the invoice); its PO and project requirements block *Approve*; an unexpected VAT treatment is noted for the reviewer. Types can be added, renamed and removed on their tab; the review page shows which type matched and why, and can move the invoice to another type.

## What gets extracted

For every invoice or credit note: supplier (name, address, VAT and company numbers, remittance email), invoice number and dates, payment terms, PO number and other references, project / site, one-line description, currency, every line with net / VAT rate / VAT / category, net-VAT-gross totals with a VAT breakdown, VAT treatment (including the domestic reverse charge for construction services), CIS labour/materials split and deduction, retention withheld, amount payable, bank details for the payment run, plus a confidence score and a list of things to check by hand. Totals are re-checked arithmetically after extraction. The full record is listed on the extraction agent's tab.

Statements, remittances, quotes and marketing PDFs are normally stopped by the classification agent; anything that gets through is filed under *Not an invoice* rather than mapped. A statement of account is not an invoice even though it shows a total due: it lists invoices already issued, and those arrive separately. If a document asks for payment to new or changed bank details, the agent says so in its reason whatever the verdict.

**Invoices that are not a PDF.** Plenty of invoices never arrive as an attachment. The app reads:

| It arrives as | What happens |
|---|---|
| A PDF or image attachment | Sent to Claude as the document, as ever |
| Written out in the email itself | Read from the email text; the review page shows that text in place of the document |
| Inside a forwarded email (a `.eml` attachment), often with an empty covering note | The forwarded email is opened: its text is read, and any PDF or image inside it becomes a document in its own right |
| A timesheet, schedule or CSV beside the invoice | Read as text and given to the agents as background |
| Anything else (a `.msg` file, say) | Named for the reviewer, not read |

Logos and signature images under 20 KB are ignored, so an email whose only attachment is a logo is judged on its text.

## Workflow

1. **Check inbox now** on the Inbox runs classification and then extraction (or set a timer on the extraction tab) - or drag a PDF onto *Process upload* to test. Each agent can also be run alone from its tab.
2. Each attachment appears in the Inbox as *Needs review* with the number of open checks.
3. Open it: the left side is the extracted data (editable, with the line editor recalculating totals, and the invoice type it matched), the right side is the document and the **Key into Sage** sheet in the order of Intacct's bill screen.
4. **Save changes** rebuilds the Sage payload. **Approve** once the checks are clear.
5. **Post to Sage** (if connected) or **Mark keyed in by hand**; **Query** parks it.
6. **Download log CSV** on the Inbox for the purchase invoice log - one row per invoice, columns match the entry sheet.

## Files

```
app.py                 Flask app and routes
agent/config.py        settings defaults (generic Sage coding, both agents, invoice types) and storage (data/settings.json, 0600)
agent/gmail_client.py  OAuth, message + attachment fetch, labelling, the Mailbox wrapper the agents use
agent/mail_parts.py    what an email carries: forwarded .eml files, timesheets and spreadsheets read as text, documents dug out of a forward
agent/extractor.py     Claude calls: label_email tool for classification, record_invoice tool for extraction; prompts assembled from the templates
agent/rag.py           reference text retrieval: whole text when short, matching paragraphs when long
agent/prompts.py       the shipped system prompt for each agent, placeholder filling and the Restore default check
agent/invoice_types.py the generic types, matching order, coding overrides and defaults
agent/match.py         fuzzy name matching shared by the vendor / project maps and the types
agent/sage_mapper.py   record → APBILL / APADJUSTMENT payload, XML, entry sheet, log row
agent/sage_client.py   Intacct XML gateway: test connection, create bill
agent/pipeline.py      classification run, extraction run, manual upload, background poller
agent/store.py         SQLite (data/invoices.db): invoices, runs, classification decisions
templates/, static/    the six tabs
samples/               a fictional subcontractor invoice, and a forwarded email whose invoice is in the text
tests/smoke_test.py    end-to-end test with Gmail and both Claude calls faked, plus the updater with GitHub faked
updater/               the desktop updater: updater.py (local page + install + start), updater.html, update.bat, update.command, README.txt, make_zip.py
VERSION                the release number the updater compares with the one on GitHub
public/                the project website, served by Cloudflare Workers (see Website below)
wrangler.jsonc         Cloudflare config for it; package.json carries wrangler
```

Set `INVOICE_AGENT_DATA=/path` to keep `data/` somewhere else (a synced folder, for example) and `INVOICE_AGENT_PORT` to change the port.

## Website

`public/` is a small static site - the front door for the accounts team: what the agent does in four steps, the agent tree with a note on each agent, downloads, screenshots of the pages, how to get it running and how to set it up. Cloudflare Workers serves it as static assets (no build step; nothing outside `public/` is deployed), and every push to `main` deploys it. The app itself is not hosted - it stays on the accounts machine as described above, and the site holds no data.

The **Download** section offers the updater zip (built from `updater/` by `python updater/make_zip.py` into `public/downloads/`), the latest build as a zip of the `main` branch straight from GitHub, and an **Older versions** chooser. The chooser asks the GitHub API for the repository's releases (falling back to plain tags), lists them newest first, and points its button at the chosen tag's zip. With no releases published yet it says so; if GitHub cannot be reached it points at the releases page instead. Releases are created by hand from the release text in each push, so the chooser fills itself in as they are made.

```bash
npm install
npm run dev      # preview at the address wrangler prints
npm run check    # wrangler deploy --dry-run
```

The site uses the app's own `static/style.css` (copied to `public/assets/css/style.css`, which is how the agent tree on the site looks like the one in the app) plus `site.css` for layout and `assets/js/downloads.js` for the chooser. Assets are cached for a year, so the `?v=` query on their links is bumped whenever they change. No external resources beyond the GitHub API calls: fonts fall back to system faces and the screenshots are local files.

A browser-hosted version of this app (a Cloudflare Worker with database storage) was built and released as v2.0 to v2.7 and then retired in favour of this local app plus download page; its features - the two agent tabs, the agent tree, the generic Sage coding and the invoice types - now live in the local app.

## Things to know

- The Intacct field names follow the documented `APBILL` create object (`WHENCREATED`, `WHENDUE`, `VENDORID`, `RECORDID`, `DOCNUMBER`, `TERMNAME`, `TAXSOLUTIONID`, `APBILLITEMS/APBILLITEM` with `ACCOUNTNO`, `TRX_AMOUNT`, `LOCATIONID`, `DEPARTMENTID`, `PROJECTID`, `TAXENTRIES`). Credit notes map to `APADJUSTMENT` with negative amounts. The PO number goes in `DOCNUMBER` (reference); change `sage_mapper.build` if your Intacct uses PO matching through Purchasing instead.
- The Intacct push has been written against the XML gateway spec but not exercised against a live company - do the first post as *Draft* and compare against a bill keyed by hand.
- Attachments and extracted data are stored locally under `data/`. Delete a row from the Inbox to remove it; the email keeps its processed label.
- Only PDF, PNG, JPG and WEBP attachments are read; images under 20 KB are treated as logos and skipped. Classification sends at most three attachments (12 MB in all) per email to Claude.
- Settings saved by the single-agent version (v1.0) still load: its model becomes the extraction model and the types are added with their defaults.

# glent-invoice-agent

Local Flask app for Glent Group's accounts team: a Gmail inbox receives supplier invoices; **Agent - Classification** reads each new email with Claude and labels it `Invoice Incoming` or `Not an invoice`; **Agent - Invoice Extraction** reads the labelled attachments into a fixed schema, matches each to an invoice type and maps it to a Sage Intacct AP bill, reviewed on screen, then posted to Intacct or keyed in from the entry sheet. Six tabs: Inbox, Agent tree, the two agents, Invoice types, Settings. Single user, single machine, `http://localhost:8765`.

**Audience rule:** the end user is not technical. Anything that needs a terminal, pip, env vars or editing files beyond `run.sh` / `run.bat` is a regression. All configuration goes through the Settings page; keys are pasted, never typed into files.

## Commands

```bash
bash run.sh                 # venv + deps on first run, then starts the app (run.bat on Windows)
python app.py               # same without the venv wrapper
python tests/smoke_test.py  # end-to-end with Gmail faked and both Claude calls mocked; must print "All checks passed."
python samples/make_sample.py   # regenerates samples/sample-invoice.pdf (needs reportlab, dev only)
npm install && npm run check    # wrangler dry-run of the website in public/ (needs Node)
npm run dev                     # preview the website locally
```

Env: `INVOICE_AGENT_DATA` (data dir, default `./data`), `INVOICE_AGENT_PORT` (default 8765), `INVOICE_AGENT_HOST` (default 127.0.0.1 - keep it loopback).

## Layout

```
app.py                  routes only; no business logic
agent/config.py         DEFAULTS (generic Sage coding, both agents, invoice types), with_defaults() compat, load/save settings (data/settings.json, 0600), key=value map parsing, CLAUDE_MODELS, model_label
agent/gmail_client.py   OAuth flow, token load/refresh, list/fetch messages + attachments, labels (add_labels, search_label), Mailbox wrapper + connect()
agent/extractor.py      Claude Messages calls: classify_email (label_email tool) and extract_invoice (record_invoice tool) -> normalise(); reference text goes into the system prompts
agent/rag.py            reference-text retrieval: whole text under RAG_MAX_CHARS, otherwise the paragraphs sharing words with the email; describe() for the tabs
agent/invoice_types.py  LINE_CATEGORIES, DEFAULT_TYPES, normalise_type, resolve_type, effective_coding, apply_type_defaults
agent/match.py          norm / lookup / name_listed: fuzzy supplier matching shared by the maps and the types
agent/sage_mapper.py    normalised record + type -> APBILL / APADJUSTMENT payload, XML, "Key into Sage" sheet, CSV log row
agent/sage_client.py    Intacct XML gateway (getAPISession, create)
agent/pipeline.py       process_attachment, remap, classify_run, extract_run, run_once (both), background poller thread
agent/store.py          SQLite: invoices + runs (with stage) + classifications (data/invoices.db)
templates/              base (six tabs), dashboard (Inbox), invoice (review), agent_tree, agent_classification, agent_extraction, invoice_types, settings
static/                 style.css (ledger-paper look, tree, types), app.js (reveal toggles, line editor, tabs, tests)
samples/                fictional subcontractor invoice: reverse charge + CIS + retention, project HEL18
tests/smoke_test.py     mapper + types + rag + both agents over a FakeMailbox + every route
public/                 the project website (static; served by Cloudflare Workers, see below)
wrangler.jsonc          assets-only Cloudflare config; package.json + package-lock.json carry wrangler
```

Data flow: `pipeline.classify_run()` lists `gmail_query` minus the three labels, calls `extractor.classify_email()` per email, applies `classify_invoice_label` / `classify_other_label` and records a `classifications` row. `pipeline.extract_run()` lists `label:<invoice label> -label:<processed label>` and calls `process_attachment()` per attachment: `extractor.extract_invoice()` returns `(record, usage)` -> `extractor.normalise()` coerces numbers and recomputes totals -> `invoice_types.resolve_type()` + `apply_type_defaults()` (fills blanks only, flags each default) -> `sage_mapper.build(record, settings, overrides)` returns `{bill, issues, notes, entry_sheet, log_row, matched, invoice_type}` -> `store.insert_invoice()`; then the email is marked processed. `run_once()` is both in turn and writes an `inbox` run row. Edits on the review page go through `app.invoice_save` -> resolve/apply type -> `pipeline.remap()`, which reruns `build` only.

Invoice statuses: `review` -> `approved` -> `posted`; side states `queried`, `not_invoice`, `error`. Approve is refused while `issues` is non-empty.

## Website on Cloudflare

`public/` is the project's front door, not the app: a static page saying what the agent does in four steps, the agent tree (the app's `.tree` markup and CSS, static text, boxes linking to the explanations below it), a Download section (latest `main` zip from GitHub plus an older-versions chooser), screenshots of the seven pages, how to get it running and how to set it up. Cloudflare Workers serves it as static assets - `wrangler.jsonc` has no Worker script, `assets.directory` is `./public` and `not_found_handling` serves `404.html`. The repo is connected to Cloudflare Workers Builds, so **every push to `main` deploys it**. Nothing outside `public/` is deployed, so the Python app and `data/` never reach Cloudflare; the app stays local as designed.

- `public/index.html` copy follows README.md - keep the two in step when the workflow or setup changes.
- `public/assets/js/downloads.js` fills the older-versions chooser from the GitHub API (`/releases`, falling back to `/tags`, newest first by `vMAJOR.MINOR`) and points the button at `archive/refs/tags/<tag>.zip`. It needs no key (public repo, 60 requests/hour per visitor). Tags and releases are created by hand by the owner, never pushed from here, so the chooser shows "no older versions" until they exist.
- The site is assets-only: `wrangler.jsonc` has no Worker script and no bindings, so a deploy needs nothing created in the account. The v2.x browser version (Worker + D1) is retired; it stays in tags v2.0-v2.7 and in history, and its deploy lessons are that the Workers Builds token can create D1 databases by name but never R2 buckets.
- `public/assets/css/style.css` is a verbatim copy of `static/style.css`; `site.css` holds site-only layout (including the `.sheet .tree` overrides that stop the sheet's list spacing leaking into the tree). If the app stylesheet changes, copy it again.
- `public/assets/img/` holds screenshots of the seven pages (inbox, agent-tree, classification, extraction, review, invoice-types, settings), 1200 px wide, taken from the running app with Gmail faked and both Claude calls mocked (the review image is the line editor plus the Key into Sage sheet, because headless Chromium draws the PDF preview black; the tree image is the whole tree). The `<img>` width/height attributes carry the real pixel size.
- `public/404.html` uses root-relative asset paths so it is styled at any depth.
- `public/_headers`: security headers, `/assets/*` cached immutable for a year, HTML must-revalidate. Every asset link in the HTML carries `?v=<release>`; bump it whenever an asset changes, or the cached copy is served for a year.
- `package-lock.json` stays committed or Workers Builds cannot install.
- Verification before any push that touches the site: `npm run check` (wrangler dry run), then serve `public/` and render `index.html` and `404.html` in headless Chromium at desktop and phone widths and look at the screenshots - an unstyled page is the failure this catches. For the chooser, load the page in Playwright with the GitHub API routes stubbed (a releases list, then an empty list) and check both renderings. Check the tree section too: six boxes, zero list padding on `.tree > ul`, no horizontal overflow at 390 px.

## Contracts worth knowing before changing things

- **Extraction schema** is `extractor.INVOICE_TOOL["input_schema"]`. Add fields there, then in `normalise()`, then in the review form (`templates/invoice.html` + `app.invoice_save`), then in `sage_mapper` if Sage needs them. Line categories are `LINE_CATEGORIES` in `sage_mapper` (labour, materials, plant, subcontract, services, expenses, other) and drive the GL map.
- **Claude calls:** both use `tool_choice` `auto` with a text-JSON fallback. Do not force `tool_choice` - it is rejected when a model has thinking on (Fable 5.1 does). Do not send a `thinking` param. Model IDs live in `config.CLAUDE_MODELS`; `config.DEFAULT_MODEL` is `claude-opus-5` for both agents, each chosen on its own tab (`classify_model`, `extract_model`). The request shape was confirmed accepted by the API (401 with a bad key, not 400).
- **Classification schema** is `extractor.CLASSIFY_TOOL` (`label_email`: verdict invoice / not_invoice, document_kind, confidence, reason). `classify_email` sends at most `CLASSIFY_MAX_ATTACHMENTS` (3) attachments and `CLASSIFY_MAX_TOTAL_BYTES` (12 MB), raises `ValueError` without a verdict, and the run leaves that email unlabelled so the next run retries it. A sender outside `gmail_allowed_senders` is labelled as not an invoice without a call.
- **Reference text (RAG):** `rag.retrieve(text, query)` - the whole text when it is under `RAG_MAX_CHARS` (16,000), otherwise the paragraphs (blank-line separated) that share words with the email, in original order, within the budget. The query is sender + subject + attachment names + body. The text goes into the system prompt through `_reference_section`. Both tabs show `rag.describe()`.
- **Invoice types** live in `settings["invoice_types"]` (list of dicts, `invoice_types.normalise_type` fills missing fields; `DEFAULT_TYPES` are subcontractor, materials, plant, professional, overheads). `resolve_type(rec, settings, overrides)` order: `overrides["invoice_type"]` -> supplier listed on a type -> signals (cis, reverse_charge, application_for_payment) -> dominant line category -> the type with `other` (else the last). `effective_coding` layers the type's non-blank GL / VAT / location / department / action over Settings. `apply_type_defaults` runs in `process_attachment` and `invoice_save` only (not in `build`), touches blanks only and appends a flag per default. `build` adds project-required / PO-required issues, expected-VAT notes, the type row in the entry sheet, `log_row["Invoice type"]` (in `LOG_COLUMNS`) and returns `invoice_type {id, name, how}`. The types form names fields `<id>__<field>`; `app._read_type` reads them; add / `remove:<id>` are `action` values.
- **Settings compatibility:** `config.with_defaults()` maps an old `claude_model` to `extract_model`, normalises stored types, merges stored maps over the defaults and drops unknown keys. The Settings POST and the two agent POSTs touch only the keys present in the form, so one page never resets another's values.
- **Intacct payload** follows the documented `APBILL` create object. `RECORDID` = supplier's invoice number, `DOCNUMBER` = PO, `TERMNAME` from the terms map, `TAXSOLUTIONID` + per-line `TAXENTRIES` assume the Intacct Taxes application with a UK VAT solution. Credit notes -> `APADJUSTMENT` with negative amounts. CIS deductions and retention are extra negative lines to the control accounts in Settings; if those are blank they surface as issues instead.
- **Gmail:** Desktop-app OAuth client, loopback redirect `http://localhost:8765/oauth/callback` (the URI shown on the Settings page is built from the request host). Scope `gmail.modify`. Three labels: `classify_invoice_label` (default `Invoice Incoming`, never blank), `classify_other_label` (default `Not an invoice`, blank = label nothing) and `gmail_processed_label` (`Invoices/Processed`). Nested labels are created parent-first. The classification search is `gmail_query` minus all three labels; the extraction search is `label:<invoice> -label:<processed>`; both use Gmail's hyphenated form from `gmail_client.search_label` (`re.sub(r'[\s/]+', '-', label)`). The agents talk to Gmail through `gmail_client.connect()` -> `Mailbox` (list / fetch / add_labels / mark_processed) so tests replace `connect` with a fake. Dedupe key is `(gmail_message_id, attachment_name)`; `classifications` upserts on `gmail_message_id`.
- **Runs:** `runs.stage` is `classify`, `extract` or `inbox` (`init_db` adds the column to old databases); the Inbox shows the last `inbox` run, each agent tab its own. A partial failure keeps `ok: True` in the returned summary (the message says how many failed) but writes `ok = 0` on the run row.
- **Secrets** are plain text in `data/settings.json` (0600) and `data/google_token.json`. `data/` is gitignored. Never log or flash a key.

## Bugs already fixed - do not reintroduce

- *Connect Gmail* must be a submit button of the settings form (`name="action" value="connect_gmail"`), so the pasted client ID/secret are saved before the redirect. It was a plain link once and silently dropped the values.
- The OAuth callback must be built with the same PKCE `code_verifier` the sign-in step generated (`session["oauth_code_verifier"]`), or Google returns `invalid_grant: Missing code verifier`.
- Jinja precedence: `a or b | filter` filters only `b`. Bracket it.
- `pipeline.run_once` builds the `-label:` exclusion on its own line before the f-string. A backslash inside an f-string expression is a SyntaxError before Python 3.12 and took the whole app down on 3.11.
- The line-item editor table is `table-layout: fixed` with `<col>` widths in CSS; unit is a hidden input per row so `getlist` columns stay aligned. `app.invoice_save` reads columns through `col(name, i)` to tolerate uneven lists.
- `site.css`'s `.sheet ul` / `.sheet li` have the same specificity as the app's `.tree ul` / `.tree li` and load later, so the tree inside a sheet on the website needs the `.sheet .tree` overrides or its connectors drift.
- Playwright's `boundingBox()` is viewport-relative; a `fullPage` clip needs document coordinates (`getBoundingClientRect().top + window.scrollY`), or a screenshot taken after a click that scrolled the page starts at the top of the document.

## State at handover (15 Sep 2026)

Working and checked: the full app with the v2 features ported in (two agent tabs with model and reference text, Opus 5 default, agent tree, generic Sage coding, invoice types), smoke test green on Python 3.11, a headless-Chromium click-through of all six tabs with Gmail faked and both Claude calls mocked (the scripts live in the session scratchpad, not the repo: a launcher that patches `gmail_client.connect`, `connection_status`, `classify_email` and `extract_invoice`, then Playwright through inbox -> tree -> classify now -> extract now -> upload -> review -> type change -> approve -> types add/remove -> settings). Gmail OAuth was connected end to end from the Settings page on Fid's machine in v1.0 and that code is unchanged. The site deploys as assets only; the older-versions chooser was verified against stubbed GitHub responses because no tags or releases existed yet.

Not yet exercised: either Claude call on a real email or invoice (only mocked in tests - run the sample PDF through *Process upload* with a live key first, then *Classify now* on the real mailbox with a small batch), and the Intacct push - written to the XML gateway spec, never sent to a live company. First real post should be `Draft`, compared against a hand-keyed bill; expect to adjust `TAXSOLUTIONID` / tax detail names / whether PO matching should go through Purchasing rather than `DOCNUMBER`.

Settings defaults are generic contractor codes (GL 5000-7700, CIS 2214, retention 2215, Intacct's UK VAT detail names, vendor and project maps with the sample supplier and project) and the five generic types. Real IDs come from Glent's Intacct lists; the sample maps end to end out of the box so the review page can be shown with no issues.

## Next steps, in rough order

1. Run real invoices through it with the Glent mailbox; tune both system prompts in `extractor.py` and the reference texts on what they get wrong (UK construction specifics: reverse charge wording, CIS labour/materials splits, retention, application-for-payment vs invoice; statements and remittances that look like invoices).
2. Fill the coding maps and the types from Intacct, then do the Draft post test.
3. Purchase invoice log: the CSV export (`sage_mapper.LOG_COLUMNS`) should match the columns of `Purchase_Invoice_Log.xlsm` (tabs Invoice Log / Not Posted / Posted / Payment Run / Paid / Queried). Longer term the workbook sync - status changes and picking up posted bills from Intacct - belongs in this app, not in Excel VBA.
4. Packaging for the accounts team without a Python install (PyInstaller one-folder build with `run` launcher, or a small installer). `run.sh` / `run.bat` are the interim.
5. Nice-to-haves: per-status CSV export, `not_invoice` triage, a "continue" loop when a classification batch leaves more mail waiting, wide tables scrolling at phone width (the app is a desktop tool; its ledger tables overflow at 390 px as they did in v1.0).

## Git and release policy

- Author identity for every commit: `Fid` / `fid_kk@proton.me` (set with `git config user.name/user.email` in this repo before the first commit).
- Every push to `main` is a release. Versions are an ascending `vMAJOR.MINOR` sequence starting at `v1.0`; minor bump per push, major reserved for a ground-up overhaul.
- Commits: descriptive imperative first line, short prose body. No AI-attribution trailers, model names, session links or tooling identifiers in commits, titles or code comments. (The model IDs in `config.py` are application configuration and stay.)
- Never push tags. Put the release text (Tag / Title / Description) in the reply so the GitHub release can be created by hand, and append a line to the ledger below.
- Before every push: `python tests/smoke_test.py` green, start the app and click through Inbox -> sample upload -> review -> Settings, and confirm `git status` shows nothing under `data/`. If `public/` changed, also `npm run check` and the render check described above.

### Release ledger

| Tag | Date | Summary |
|---|---|---|
| v1.0 | 14 Sep 2026 | First release: the app as handed over, plus the project website served from public/ on Cloudflare Workers. Smoke test now passes on Python 3.11. |
| v2.0 | 14 Sep 2026 | Ground-up rebuild as a Cloudflare Worker that runs in the browser: D1 + R2 storage, app-password sign-in, and two agent tabs - Classification labels invoices in the mailbox, Invoice Extraction reads them - each with its own model and reference text. |
| v2.1 | 14 Sep 2026 | Claude Opus 5 is the default model for both agents. |
| v2.2 | 14 Sep 2026 | Agent tree tab: a clickable map of the mailbox, the two agents, the Inbox and Sage, with each box showing its model, last run and counts. |
| v2.3 | 14 Sep 2026 | Sage coding pre-filled with generic contractor defaults: nominal codes per category, CIS and retention accounts, UK VAT detail names, payment terms, and the sample supplier and project. |
| v2.4 | 14 Sep 2026 | Invoice types: five generic kinds of invoice, each with its own GL and VAT overrides, CIS and retention defaults, terms and PO / project checks, matched automatically and changeable on the review page. |
| v2.5 | 14 Sep 2026 | Deploy fix: the R2 binding no longer names its bucket, so wrangler creates one on the first deploy instead of failing on a bucket that does not exist. |
| v2.6 | 14 Sep 2026 | Deploy fix, second round: the build token cannot create R2 buckets, so the bucket is created once by hand and named in the config again. |
| v2.7 | 14 Sep 2026 | Attachments move into the D1 database in 512 KB pieces and the R2 bucket is gone, so a deploy creates everything it needs by itself. |
| v3.0 | 14 Sep 2026 | The browser version is retired. The address is a plain site again: what the app does in four steps, a download of the latest main build and a chooser for older tagged versions, with the local app restored from v1.0 as what gets downloaded. |
| v3.1 | 15 Sep 2026 | The browser version's features come back into the local app: Agent - Classification and Agent - Invoice Extraction tabs, each with its model and reference text, Claude Opus 5 as the default, the Agent tree, generic contractor Sage coding, and invoice types. The website gets the agent tree and screenshots of every page. |

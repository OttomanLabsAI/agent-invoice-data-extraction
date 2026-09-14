# glent-invoice-agent

Local Flask app for Glent Group's accounts team: a Gmail inbox receives supplier invoices, each attachment is read by the Claude API into a fixed schema, mapped to a Sage Intacct AP bill, reviewed on screen, then posted to Intacct or keyed in from the entry sheet. Single user, single machine, `http://localhost:8765`.

**Audience rule:** the end user is not technical. Anything that needs a terminal, pip, env vars or editing files beyond `run.sh` / `run.bat` is a regression. All configuration goes through the Settings page; keys are pasted, never typed into files.

## Commands

```bash
bash run.sh                 # venv + deps on first run, then starts the app (run.bat on Windows)
python app.py               # same without the venv wrapper
python tests/smoke_test.py  # end-to-end with the Claude call mocked; must print "All checks passed."
python samples/make_sample.py   # regenerates samples/sample-invoice.pdf (needs reportlab, dev only)
npm install && npm run check    # wrangler dry-run of the website in public/ (needs Node)
npm run dev                     # preview the website locally
```

Env: `INVOICE_AGENT_DATA` (data dir, default `./data`), `INVOICE_AGENT_PORT` (default 8765), `INVOICE_AGENT_HOST` (default 127.0.0.1 - keep it loopback).

## Layout

```
app.py                  routes only; no business logic
agent/config.py         DEFAULTS, load/save settings (data/settings.json, 0600), key=value map parsing, CLAUDE_MODELS
agent/gmail_client.py   OAuth flow, token load/refresh, list/fetch messages + attachments, labelling
agent/extractor.py      Claude Messages call: PDF/image + email context -> record_invoice tool -> normalise()
agent/sage_mapper.py    normalised record -> APBILL / APADJUSTMENT payload, XML, "Key into Sage" sheet, CSV log row
agent/sage_client.py    Intacct XML gateway (getAPISession, create)
agent/pipeline.py       process_attachment, remap, run_once (Gmail sweep), background poller thread
agent/store.py          SQLite: invoices + runs (data/invoices.db)
templates/              base, dashboard (Inbox), invoice (review), settings
static/                 style.css (ledger-paper look), app.js (reveal toggles, line editor, tabs, tests)
samples/                fictional subcontractor invoice: reverse charge + CIS + retention, project HEL18
tests/smoke_test.py     mapper + pipeline + every route
public/                 the project website (static; served by Cloudflare Workers, see below)
wrangler.jsonc          assets-only Cloudflare config; package.json + package-lock.json carry wrangler
```

Data flow: `extractor.extract_invoice()` returns `(record, usage)` -> `extractor.normalise()` coerces numbers and recomputes totals -> `sage_mapper.build(record, settings, overrides)` returns `{bill, issues, notes, entry_sheet, log_row, matched_vendor_key, matched_project_key}` -> `store.insert_invoice()`. Edits on the review page go through `app.invoice_save` -> `pipeline.remap()`, which reruns `build` only.

Invoice statuses: `review` -> `approved` -> `posted`; side states `queried`, `not_invoice`, `error`. Approve is refused while `issues` is non-empty.

## Website on Cloudflare

`public/` is the project's front door, not the app: a static page saying what the agent does, how to get it running and how to set it up, with screenshots of the three pages. Cloudflare Workers serves it as static assets - `wrangler.jsonc` has no Worker script, `assets.directory` is `./public` and `not_found_handling` serves `404.html`. The repo is connected to Cloudflare Workers Builds, so **every push to `main` deploys it**. Nothing outside `public/` is deployed, so the Python app and `data/` never reach Cloudflare; the app stays local as designed.

- `public/index.html` copy follows README.md - keep the two in step when the workflow or setup changes.
- `public/assets/css/style.css` is a verbatim copy of `static/style.css`; `site.css` holds site-only layout. If the app stylesheet changes, copy it again.
- `public/assets/img/` holds screenshots of the three pages, taken from the running app with the sample invoice and the Claude call mocked (the review image is the line editor plus the Key into Sage sheet, because headless Chromium draws the PDF preview black).
- `public/404.html` uses root-relative asset paths so it is styled at any depth.
- `public/_headers`: security headers, `/assets/*` cached immutable for a year (rename a file to bust), HTML must-revalidate.
- `package-lock.json` stays committed or Workers Builds cannot install.
- Verification before any push that touches the site: `npm run check` (wrangler dry run), then serve `public/` and render `index.html` and `404.html` in headless Chromium at desktop and phone widths and look at the screenshots - an unstyled page is the failure this catches.

## Contracts worth knowing before changing things

- **Extraction schema** is `extractor.INVOICE_TOOL["input_schema"]`. Add fields there, then in `normalise()`, then in the review form (`templates/invoice.html` + `app.invoice_save`), then in `sage_mapper` if Sage needs them. Line categories are `LINE_CATEGORIES` in `sage_mapper` (labour, materials, plant, subcontract, services, expenses, other) and drive the GL map.
- **Claude call:** `tool_choice` is `auto` with a text-JSON fallback. Do not force `tool_choice` - it is rejected when a model has thinking on (Fable 5.1 does). Do not send a `thinking` param. Model IDs live in `config.CLAUDE_MODELS`; default `claude-sonnet-5`. The request shape was confirmed accepted by the API (401 with a bad key, not 400).
- **Intacct payload** follows the documented `APBILL` create object. `RECORDID` = supplier's invoice number, `DOCNUMBER` = PO, `TERMNAME` from the terms map, `TAXSOLUTIONID` + per-line `TAXENTRIES` assume the Intacct Taxes application with a UK VAT solution. Credit notes -> `APADJUSTMENT` with negative amounts. CIS deductions and retention are extra negative lines to the control accounts in Settings; if those are blank they surface as issues instead.
- **Gmail:** Desktop-app OAuth client, loopback redirect `http://localhost:8765/oauth/callback` (the URI shown on the Settings page is built from the request host). Scope `gmail.modify`. Processed mail is labelled `gmail_processed_label` (nested labels are created parent-first) and excluded from the next sweep with `-label:` using Gmail's hyphenated form (`re.sub(r'[\s/]+', '-', label)`). Dedupe key is `(gmail_message_id, attachment_name)`.
- **Secrets** are plain text in `data/settings.json` (0600) and `data/google_token.json`. `data/` is gitignored. Never log or flash a key.

## Bugs already fixed - do not reintroduce

- *Connect Gmail* must be a submit button of the settings form (`name="action" value="connect_gmail"`), so the pasted client ID/secret are saved before the redirect. It was a plain link once and silently dropped the values.
- The OAuth callback must be built with the same PKCE `code_verifier` the sign-in step generated (`session["oauth_code_verifier"]`), or Google returns `invalid_grant: Missing code verifier`.
- Jinja precedence: `a or b | filter` filters only `b`. Bracket it.
- `pipeline.run_once` builds the `-label:` exclusion on its own line before the f-string. A backslash inside an f-string expression is a SyntaxError before Python 3.12 and took the whole app down on 3.11.
- The line-item editor table is `table-layout: fixed` with `<col>` widths in CSS; unit is a hidden input per row so `getlist` columns stay aligned. `app.invoice_save` reads columns through `col(name, i)` to tolerate uneven lists.

## State at handover (14 Sep 2026)

Working and checked: full app, smoke test green (on Python 3.11 as well), packaged copy boots and serves, Gmail OAuth connected end to end from the Settings page on Fid's machine.

Not yet exercised: Claude extraction on a real invoice (only mocked in tests - run the sample PDF through *Process upload* with a live key first), and the Intacct push - written to the XML gateway spec, never sent to a live company. First real post should be `Draft`, compared against a hand-keyed bill; expect to adjust `TAXSOLUTIONID` / tax detail names / whether PO matching should go through Purchasing rather than `DOCNUMBER`.

Settings defaults are placeholders (GL 5100-7000, `LON`/`MEP`, vendor map with the sample supplier). Real IDs come from Glent's Intacct lists.

## Next steps, in rough order

1. Run real invoices through it with the Glent mailbox; tune the system prompt in `extractor.py` on what it gets wrong (UK construction specifics: reverse charge wording, CIS labour/materials splits, retention, application-for-payment vs invoice).
2. Fill the coding maps from Intacct, then do the Draft post test.
3. Purchase invoice log: the CSV export (`sage_mapper.LOG_COLUMNS`) should match the columns of `Purchase_Invoice_Log.xlsm` (tabs Invoice Log / Not Posted / Posted / Payment Run / Paid / Queried). Longer term the workbook sync - status changes and picking up posted bills from Intacct - belongs in this app, not in Excel VBA.
4. Packaging for the accounts team without a Python install (PyInstaller one-folder build with `run` launcher, or a small installer). `run.sh` / `run.bat` are the interim.
5. Nice-to-haves already stubbed: allowed-sender filter, polling interval, per-status CSV export, `not_invoice` triage.

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

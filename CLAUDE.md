# glent-invoice-agent

Cloudflare Worker for Glent Group's accounts team: a Gmail inbox receives supplier invoices, a classification agent labels the invoices, an extraction agent reads each attachment with the Claude API into a fixed schema, the record is mapped to a Sage Intacct AP bill, reviewed on screen, then posted to Intacct or keyed in from the entry sheet. Runs in the browser behind one shared app password.

**Audience rule:** the end user is not technical. Anything that needs a terminal, wrangler, or editing files is a regression for them. All configuration goes through the Settings page and the two agent tabs; keys are pasted, never typed into files. The one exception is the person who deploys: they set the `APP_PASSWORD` secret in the Cloudflare dashboard once.

## Commands

```bash
npm install
cp .dev.vars.example .dev.vars   # APP_PASSWORD for local dev
npm run dev                      # wrangler dev on http://localhost:8787, local D1 + R2
npm test                         # node --test tests/unit.test.js
npm run smoke                    # tests/smoke.js: wrangler dev with TEST_FIXTURES=1, every route over HTTP; must end "All checks passed."
npm run check                    # wrangler deploy --dry-run
```

## Layout

```
src/index.js          fetch + scheduled entry, router, sign-in gate, security headers
src/routes.js         handlers only; no business logic (GET /agents is the tree, /agents/<name> the agent tabs)
src/pipeline.js       processAttachment, remap, classifyStep, extractStep, checkInboxStep, runScheduled
src/claude.js         Messages API via fetch: extractInvoice, classifyEmail, checkApiKey; base64 media blocks
src/gmail.js          OAuth (web client), token refresh, REST client: list/fetch/attachments/labels/modify
src/schema.js         INVOICE_TOOL, CLASSIFY_TOOL, LINE_CATEGORIES/DOCUMENT_TYPES/VAT_TREATMENTS, system prompts
src/normalise.js      normalise(), totalsReconcile()
src/rag.js            retrieve(text, query): whole text under RAG_MAX_CHARS, else paragraph retrieval
src/sage_mapper.js    build(record, settings, {emailMeta, overrides}), billToXml, publicBill, LOG_COLUMNS
src/sage_client.js    Intacct XML gateway (getAPISession, create), regex-parsed responses
src/settings.js       DEFAULTS, loadSettings/saveSettings (D1 settings table), parseMap/formatMap, CLAUDE_MODELS
src/db.js             D1 schema (settings, state, invoices, runs, classifications) and queries; run lock
src/auth.js           APP_PASSWORD check, HMAC session cookie, same-origin check for posts
src/fixtures.js       fake mailbox + fake Claude, only when env.TEST_FIXTURES === "1"
src/views/html.js     html`` tag that escapes interpolations; raw(); money/shortDate filters
src/views/layout.js   page frame with the five tabs (Inbox, Agent tree, the two agents, Settings); ASSET_VERSION; continueNudge
src/views/pages.js    sign-in, setup, inbox, invoice review, agent tree, both agent tabs, settings, 404
public/               static assets: assets/css/style.css, assets/js/app.js, favicon.svg, robots.txt (Disallow), _headers
tests/unit.test.js    mapper, normalise, rag, csv, settings, helpers
tests/smoke.js        end-to-end over HTTP against wrangler dev (fixture mode)
samples/              fictional subcontractor invoice: reverse charge + CIS + retention, project HEL18
wrangler.jsonc        main src/index.js, assets ./public (binding ASSETS), D1 DB "invoice-agent", R2 FILES "invoice-agent-files", cron */5
```

Data flow: `classifyStep` sweeps `gmail_query` minus the three labels, calls `claude.classifyEmail` per email (attachments included, Haiku by default), applies `classify_invoice_label` / `classify_other_label`, records a row in `classifications`. `extractStep` sweeps `label:<invoice label> -label:<processed label>`, calls `claude.extractInvoice` per attachment -> `normalise()` -> `sage_mapper.build()` -> `db.insertInvoice()`, then adds the processed label and marks the email read. Edits on the review page go through `routes.invoiceSave` -> `pipeline.remap()`, which reruns `build` only. Attachments are R2 objects under `attachments/<message id or upload-ts>_<name>`; the row keeps `attachment_key`.

Invoice statuses: `review` -> `approved` -> `posted`; side states `queried`, `not_invoice`, `error`. Approve is refused while `issues` is non-empty.

## Contracts worth knowing before changing things

- **Extraction schema** is `INVOICE_TOOL.input_schema` in `src/schema.js`. Add fields there, then in `normalise()`, then in the review form (`views/pages.js` invoicePage + `routes.invoiceSave`), then in `sage_mapper` if Sage needs them. The extraction tab renders the schema, so it documents itself.
- **Claude calls** go through `fetch` in `src/claude.js` with `anthropic-version: 2023-06-01`. `tool_choice` is `auto` with a text-JSON fallback. Do not force `tool_choice` - it is rejected when a model has thinking on (Fable 5.1 does). Do not send a `thinking` param. Model IDs live in `settings.CLAUDE_MODELS`; the default for both agents is `settings.DEFAULT_MODEL` (`claude-opus-5`), chosen per agent on its tab.
- **Reference text (RAG):** `rag.retrieve(text, query)` returns the whole text under 16,000 characters, otherwise the paragraphs (blank-line separated) sharing the most words with the email, in original order, within the budget. It is appended to the system prompt as "Reference notes from the accounts team".
- **Batches:** `CLASSIFY_BATCH` 5 and `EXTRACT_BATCH` 1 per request keep each request short. A step reports `remaining` by asking Gmail for one more message than the batch; the page then shows the continue nudge (`?continue=1`) and `app.js` re-posts after three seconds unless stopped. The cron handler (`runScheduled`) classifies one batch and extracts two, gated by `poll_minutes` and the `last_auto_run` state key.
- **Run lock** is the `run_lock` state row, taken with a conditional UPDATE (`db.acquireRunLock`); a lock older than ten minutes is treated as stale.
- **Intacct payload** follows the documented `APBILL` create object. `RECORDID` = supplier's invoice number, `DOCNUMBER` = PO, `TERMNAME` from the terms map, `TAXSOLUTIONID` + per-line `TAXENTRIES` assume the Intacct Taxes application with a UK VAT solution. Credit notes -> `APADJUSTMENT` with negative amounts. CIS deductions and retention are extra negative lines to the control accounts in Settings; if those are blank they surface as issues instead. Responses are parsed with regexes (no XML parser in Workers).
- **Gmail:** Web-application OAuth client, redirect `https://<host>/oauth/callback` (the URI on the Settings page is built from the request origin). Scope `gmail.modify`. Labels are created parent-first for nested names, and searched with Gmail's hyphenated form (`gmail.searchLabel`: `[\s/]+` -> `-`). Dedupe key is `(gmail_message_id, attachment_name)`. Classification never marks read; extraction marks read + processed label.
- **Auth:** `APP_PASSWORD` is a Worker secret. Session cookie = `<expiry>.<HMAC>` keyed from the password, 30 days, HttpOnly, SameSite=Lax, Secure on https. Posts are refused unless `sec-fetch-site`/`Origin` say same-origin. With no secret set, every path shows the setup page (503).
- **Secrets at rest:** API keys and the Gmail token are plain text in D1 (settings/state tables), the same trust model as the old local `settings.json`: only the Cloudflare account and the Worker can read them. Never log or flash a key.
- **Fixture mode** (`TEST_FIXTURES=1`) swaps `claude.js` and `gmail.js` for `fixtures.js`: three emails (invoice, statement, no attachment), canned record, label state kept in the `fixture_mailbox` state row. Only `wrangler dev --var` sets it.
- **Assets** are cached immutably for a year; bump `ASSET_VERSION` in `views/layout.js` whenever `style.css` or `app.js` change.
- **D1:** create tables with `db.batch` of single statements (`ensureSchema`), not `exec` - `exec` splits on newlines.

## Bugs already fixed - do not reintroduce

- *Connect Gmail* must be a submit button of the settings form (`name="action" value="connect_gmail"`), so the pasted client ID/secret are saved before the redirect. It was a plain link once and silently dropped the values. The agent tabs follow the same rule: *Classify now* / *Extract now* submit the tab's form with `action=run`, so the reference text is saved before the run.
- The OAuth callback checks the `state` against the `oauth_state` cookie set at sign-in time; without it Google's redirect could be replayed.
- The line-item editor table is `table-layout: fixed` with `<col>` widths in CSS; unit is a hidden input per row so `getAll` columns stay aligned. `routes.invoiceSave` reads columns through `col(name, i)` to tolerate uneven lists.
- Settings posts only touch keys present in the form (`applyFields`), so saving one tab never resets another tab's numbers to defaults.
- The PDF preview is an iframe on the same origin, so page responses carry `X-Frame-Options: SAMEORIGIN`, not `DENY`.

## State at handover (14 Sep 2026)

Working and checked: unit tests and the HTTP smoke test green, the whole app clicked through in headless Chromium against `wrangler dev` (sign-in, inbox, both agent tabs with a run each, review page, settings), `wrangler deploy --dry-run` clean.

Not yet exercised: a real deploy on the owner's Cloudflare account (first deploy provisions the D1 database and R2 bucket by name; if that fails, create them and set `database_id`), Gmail OAuth with a Web-application client on the deployed host, Claude extraction on a real invoice (fixture-only in tests - run the sample PDF through *Process upload* with a live key first), and the Intacct push - written to the XML gateway spec, never sent to a live company. First real post should be `Draft`, compared against a hand-keyed bill; expect to adjust `TAXSOLUTIONID` / tax detail names / whether PO matching should go through Purchasing rather than `DOCNUMBER`.

Settings defaults are placeholders (`LON`/`MEP`, vendor map empty). Real IDs come from Glent's Intacct lists.

## Next steps, in rough order

1. Deploy, set `APP_PASSWORD`, connect the real mailbox, run real invoices through both agents; tune the reference texts and, if needed, the system prompts in `src/schema.js` on what they get wrong (UK construction specifics: reverse charge wording, CIS labour/materials splits, retention, application-for-payment vs invoice).
2. Fill the coding maps from Intacct, then do the Draft post test.
3. Purchase invoice log: the CSV export (`LOG_COLUMNS`) should match the columns of `Purchase_Invoice_Log.xlsm` (tabs Invoice Log / Not Posted / Posted / Payment Run / Paid / Queried). Longer term the workbook sync - status changes and picking up posted bills from Intacct - belongs in this app.
4. Per-person sign-in through Cloudflare Access in front of the Worker, if the shared password becomes a problem.
5. Nice-to-haves: per-status CSV export tabs, a retrieval preview on the agent tabs (which paragraphs a given email would pull), `not_invoice` triage.

## Git and release policy

- Author identity for every commit: `Fid` / `fid_kk@proton.me` (set with `git config user.name/user.email` in this repo before the first commit).
- Every push to `main` is a release. Versions are an ascending `vMAJOR.MINOR` sequence; minor bump per push, major reserved for a ground-up overhaul.
- Commits: descriptive imperative first line, short prose body. No AI-attribution trailers, model names, session links or tooling identifiers in commits, titles or code comments. (The model IDs in `settings.js` are application configuration and stay.)
- Never push tags. Put the release text (Tag / Title / Description) in the reply so the GitHub release can be created by hand, and append a line to the ledger below.
- Before every push: `npm test` and `npm run smoke` green, `npm run check` clean, start `npm run dev` and click through sign-in -> Inbox -> sample upload -> review -> Agent tree -> both agent tabs -> Settings, and confirm `git status` shows no `.dev.vars` or `.wrangler/`.

### Release ledger

| Tag | Date | Summary |
|---|---|---|
| v1.0 | 14 Sep 2026 | First release: the local Python app as handed over, plus a project website served from public/ on Cloudflare Workers. Smoke test passes on Python 3.11. |
| v2.0 | 14 Sep 2026 | Ground-up rebuild as a Cloudflare Worker that runs in the browser: D1 + R2 storage, app-password sign-in, and two agent tabs - Classification labels invoices in the mailbox, Invoice Extraction reads them - each with its own model and reference text. |
| v2.1 | 14 Sep 2026 | Claude Opus 5 is the default model for both agents. |
| v2.2 | 14 Sep 2026 | Agent tree tab: a clickable map of the mailbox, the two agents, the Inbox and Sage, with each box showing its model, last run and counts. |

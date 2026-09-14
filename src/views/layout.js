import { html, raw } from "./html.js";

/** Bump when style.css or app.js change: assets are cached immutably for a year. */
export const ASSET_VERSION = "2.0";

export const TABS = [
  ["inbox", "/", "Inbox"],
  ["classification", "/agents/classification", "Agent - Classification"],
  ["extraction", "/agents/extraction", "Agent - Invoice Extraction"],
  ["settings", "/settings", "Settings"],
];

export function layout({ title, companyName, active, flashes = [], body, signedIn = true }) {
  return html`<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${title} · ${companyName}</title>
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <link rel="stylesheet" href="/assets/css/style.css?v=${ASSET_VERSION}">
</head>
<body>
  <header class="topbar">
    <a class="wordmark" href="/">${companyName} <span>invoice intake</span></a>
    <nav>
      ${signedIn
        ? TABS.map(([key, href, label]) => html`<a href="${href}" class="${active === key ? "active" : ""}">${label}</a>`)
        : ""}
      ${signedIn ? html`<form method="post" action="/signout" class="inline"><button type="submit" class="linklike">Sign out</button></form>` : ""}
    </nav>
  </header>
  <main>
    ${flashes.map(([category, message]) => html`<div class="flash ${category}">${message}</div>`)}
    ${body}
  </main>
  <script src="/assets/js/app.js?v=${ASSET_VERSION}"></script>
</body>
</html>`;
}

export const page = (opts) => String(layout(opts));

/** The auto-continue nudge shown after a run step that left more mail waiting. */
export function continueNudge(action, what) {
  return html`<div class="nudge" id="continue-nudge">
  More ${what} waiting. Continuing in <span data-countdown>3</span>s…
  <form method="post" action="${action}" class="inline" data-continue="1">
    <button class="btn small" type="submit">Continue now</button>
    <button class="btn small quiet" type="button" data-stop>Stop</button>
  </form>
</div>`;
}

export const nothing = raw("");

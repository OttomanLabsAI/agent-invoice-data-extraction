// Worker entry: routing, sign-in gate, security headers, and the cron trigger.

import { ensureSchema } from "./db.js";
import { loadSettings } from "./settings.js";
import * as auth from "./auth.js";
import * as routes from "./routes.js";
import { runScheduled } from "./pipeline.js";
import { page } from "./views/layout.js";
import { errorPage } from "./views/pages.js";

const ROUTES = [
  ["GET", "/", routes.dashboard],
  ["POST", "/run", routes.runNow],
  ["POST", "/upload", routes.upload],
  ["GET", "/invoice/:id", routes.invoiceDetail],
  ["POST", "/invoice/:id", routes.invoiceSave],
  ["POST", "/invoice/:id/status", routes.invoiceStatus],
  ["POST", "/invoice/:id/remap", routes.invoiceRemap],
  ["POST", "/invoice/:id/push", routes.invoicePush],
  ["GET", "/invoice/:id/file", routes.invoiceFile],
  ["GET", "/invoice/:id/sage.json", routes.invoiceSageJson],
  ["GET", "/invoice/:id/sage.xml", routes.invoiceSageXml],
  ["GET", "/export.csv", routes.exportCsv],
  ["GET", "/agents", routes.agentTree],
  ["GET", "/agents/classification", routes.classificationTab],
  ["POST", "/agents/classification", routes.classificationSave],
  ["POST", "/agents/classification/run", routes.classificationRun],
  ["GET", "/agents/extraction", routes.extractionTab],
  ["POST", "/agents/extraction", routes.extractionSave],
  ["POST", "/agents/extraction/run", routes.extractionRun],
  ["GET", "/settings", routes.settingsPage],
  ["POST", "/settings", routes.settingsSave],
  ["POST", "/settings/test/claude", routes.testClaude],
  ["POST", "/settings/test/sage", routes.testSage],
  ["GET", "/gmail/connect", routes.gmailConnect],
  ["GET", "/oauth/callback", routes.gmailCallback],
  ["POST", "/gmail/disconnect", routes.gmailDisconnect],
  ["POST", "/signout", routes.signOut],
].map(([method, pattern, handler]) => ({
  method,
  handler,
  keys: [...pattern.matchAll(/:(\w+)/g)].map((m) => m[1]),
  regex: new RegExp("^" + pattern.replace(/\./g, "\\.").replace(/:(\w+)/g, "([^/]+)") + "$"),
}));

function match(method, pathname) {
  for (const route of ROUTES) {
    if (route.method !== method) continue;
    const m = pathname.match(route.regex);
    if (!m) continue;
    const params = {};
    route.keys.forEach((key, i) => (params[key] = decodeURIComponent(m[i + 1])));
    return { handler: route.handler, params };
  }
  return null;
}

const ASSET_PREFIXES = ["/assets/", "/favicon.svg", "/robots.txt"];

function withSecurityHeaders(response, request) {
  const headers = new Headers(response.headers);
  headers.set("x-content-type-options", "nosniff");
  headers.set("x-frame-options", "SAMEORIGIN");
  headers.set("referrer-policy", "strict-origin-when-cross-origin");
  if (!headers.has("cache-control")) headers.set("cache-control", "no-store");
  if (auth.isSecure(request)) headers.set("strict-transport-security", "max-age=31536000; includeSubDomains");
  return new Response(response.body, { status: response.status, headers });
}

async function handle(request, env) {
  const url = new URL(request.url);
  if (ASSET_PREFIXES.some((p) => url.pathname.startsWith(p))) return env.ASSETS.fetch(request);

  await ensureSchema(env.DB);
  const settings = await loadSettings(env.DB);
  const ctx = { request, env, url, settings, params: {} };

  if (!auth.configured(env)) return routes.setupNeeded(ctx);

  if (url.pathname === "/signin") {
    if (request.method === "POST") return routes.signIn(ctx);
    return (await auth.hasSession(env, request)) ? Response.redirect(url.origin + "/", 303) : routes.signInForm(ctx);
  }
  if (!(await auth.hasSession(env, request))) {
    if (request.method === "GET" || request.method === "HEAD") return Response.redirect(url.origin + "/signin", 303);
    return new Response("Sign in first.", { status: 401 });
  }
  if (request.method === "POST" && !auth.sameOrigin(request)) return new Response("Cross-site request refused.", { status: 403 });

  const found = match(request.method === "HEAD" ? "GET" : request.method, url.pathname);
  if (!found) return routes.notFound(ctx);
  ctx.params = found.params;
  return found.handler(ctx);
}

export default {
  async fetch(request, env, ctx) {
    try {
      return withSecurityHeaders(await handle(request, env), request);
    } catch (err) {
      console.error(err && err.stack ? err.stack : err);
      const body = page({ title: "Error", companyName: "Glent Group", active: "", flashes: [], body: errorPage(String((err && err.message) || err)), signedIn: true });
      return withSecurityHeaders(new Response(body, { status: 500, headers: { "content-type": "text/html; charset=utf-8" } }), request);
    }
  },

  async scheduled(controller, env, ctx) {
    ctx.waitUntil(
      (async () => {
        await ensureSchema(env.DB);
        const settings = await loadSettings(env.DB);
        await runScheduled(env, settings);
      })(),
    );
  },
};

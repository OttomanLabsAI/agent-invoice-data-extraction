// One shared app password (the APP_PASSWORD secret) and an HMAC-signed session
// cookie derived from it, so changing the password signs everyone out.

const enc = new TextEncoder();
const SESSION_DAYS = 30;

function b64url(bytes) {
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function sign(secret, data) {
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return b64url(new Uint8Array(await crypto.subtle.sign("HMAC", key, enc.encode(data))));
}

export const configured = (env) => Boolean(env.APP_PASSWORD);

export function readCookies(request) {
  const out = {};
  for (const part of (request.headers.get("cookie") || "").split(";")) {
    const idx = part.indexOf("=");
    if (idx > 0) out[part.slice(0, idx).trim()] = decodeURIComponent(part.slice(idx + 1).trim());
  }
  return out;
}

export function cookie(name, value, { maxAge, secure } = {}) {
  let text = `${name}=${encodeURIComponent(value)}; Path=/; HttpOnly; SameSite=Lax`;
  if (maxAge !== undefined) text += `; Max-Age=${maxAge}`;
  if (secure) text += "; Secure";
  return text;
}

export const isSecure = (request) => new URL(request.url).protocol === "https:";

export async function passwordMatches(env, given) {
  const a = await sign("pw:" + env.APP_PASSWORD, String(given || ""));
  const b = await sign("pw:" + env.APP_PASSWORD, env.APP_PASSWORD);
  return a === b;
}

export async function newSession(env) {
  const exp = Date.now() + SESSION_DAYS * 24 * 60 * 60 * 1000;
  return `${exp}.${await sign("session:" + env.APP_PASSWORD, String(exp))}`;
}

export async function hasSession(env, request) {
  const value = readCookies(request).session || "";
  const [exp, sig] = value.split(".");
  if (!exp || !sig || Number(exp) < Date.now()) return false;
  const expected = await sign("session:" + env.APP_PASSWORD, exp);
  return (await sign("cmp", sig)) === (await sign("cmp", expected));
}

export const SESSION_MAX_AGE = SESSION_DAYS * 24 * 60 * 60;

/** Reject cross-site form posts. Same-origin requests carry no Origin at all or a matching one. */
export function sameOrigin(request) {
  const site = request.headers.get("sec-fetch-site");
  if (site && !["same-origin", "none"].includes(site)) return false;
  const origin = request.headers.get("origin");
  if (origin && origin !== "null") {
    try {
      return new URL(origin).host === new URL(request.url).host;
    } catch {
      return false;
    }
  }
  return true;
}

export const randomToken = () => b64url(crypto.getRandomValues(new Uint8Array(24)));

// Sage Intacct Web Services (XML gateway). Only used when the five Intacct
// credentials are filled in; everything else works without it.

import { billToXml, xmlEscape } from "./sage_mapper.js";

export const DEFAULT_ENDPOINT = "https://api.intacct.com/ia/xml/xmlgw.phtml";

export class SageError extends Error {}

const controlId = () => crypto.randomUUID().replace(/-/g, "").slice(0, 24);

function requestXml(settings, functionBody) {
  const esc = xmlEscape;
  return `<?xml version="1.0" encoding="UTF-8"?>
<request>
  <control>
    <senderid>${esc(settings.sage_sender_id)}</senderid>
    <password>${esc(settings.sage_sender_password)}</password>
    <controlid>${controlId()}</controlid>
    <uniqueid>false</uniqueid>
    <dtdversion>3.0</dtdversion>
    <includewhitespace>false</includewhitespace>
  </control>
  <operation>
    <authentication>
      <login>
        <userid>${esc(settings.sage_user_id)}</userid>
        <companyid>${esc(settings.sage_company_id)}</companyid>
        <password>${esc(settings.sage_user_password)}</password>
      </login>
    </authentication>
    <content>
      <function controlid="${controlId()}">
${functionBody}
      </function>
    </content>
  </operation>
</request>`;
}

// The gateway's responses are small and regular, so a few targeted regexes
// are enough to read them without an XML parser (Workers have none).
function section(xml, name) {
  const m = xml.match(new RegExp(`<${name}(?:\\s[^>]*)?>([\\s\\S]*?)</${name}>`, "i"));
  return m ? m[1] : "";
}

function textOf(xml, name) {
  const m = xml.match(new RegExp(`<${name}(?:\\s[^>]*)?>([^<]*)</${name}>`, "i"));
  return m ? m[1].trim() : "";
}

function errors(xml) {
  const messages = [];
  for (const m of xml.matchAll(/<error>([\s\S]*?)<\/error>/gi)) {
    const parts = ["errorno", "description", "description2", "correction"].map((k) => textOf(m[1], k)).filter(Boolean);
    if (parts.length) messages.push(parts.join(" "));
  }
  return messages.join("; ") || "no error detail returned";
}

async function call(settings, functionBody) {
  const endpoint = settings.sage_endpoint || DEFAULT_ENDPOINT;
  let response;
  try {
    response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "x-intacct-xml-request" },
      body: requestXml(settings, functionBody),
    });
  } catch (err) {
    throw new SageError(`Could not reach Intacct: ${err.message || err}`);
  }
  const body = await response.text();
  if (response.status !== 200) throw new SageError(`Intacct returned HTTP ${response.status}: ${body.slice(0, 300)}`);
  if (!/<response/i.test(body)) throw new SageError("Intacct sent back something that is not XML.");

  const controlStatus = textOf(section(body, "control"), "status");
  if (controlStatus && controlStatus.toLowerCase() !== "success") throw new SageError("Intacct rejected the request: " + errors(body));
  const authStatus = textOf(section(section(body, "operation"), "authentication"), "status");
  if (authStatus && authStatus.toLowerCase() !== "success") throw new SageError("Intacct login failed: " + errors(body));
  const result = section(section(body, "operation"), "result");
  if (!result) throw new SageError("Intacct response had no result block: " + errors(body));
  if (textOf(result, "status").toLowerCase() !== "success") throw new SageError("Intacct returned an error: " + errors(result));
  return result;
}

export async function testConnection(settings) {
  try {
    const result = await call(settings, "        <getAPISession/>");
    const sessionId = textOf(result, "sessionid") || "(none)";
    const endpoint = textOf(result, "endpoint");
    return [true, `Connected. Session ${sessionId.slice(0, 8)}... via ${endpoint || "default endpoint"}.`];
  } catch (err) {
    return [false, err.message || String(err)];
  }
}

/** Create the AP bill (or adjustment). Returns { recordno, object }. */
export async function postBill(settings, bill) {
  const body = billToXml(bill, "  ").split("\n").map((line) => "        " + line).join("\n");
  const result = await call(settings, body);
  const obj = bill._object || "APBILL";
  const recordno = textOf(result, "RECORDNO");
  return { recordno, object: obj };
}

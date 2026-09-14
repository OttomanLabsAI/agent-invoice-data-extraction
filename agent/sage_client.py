"""Sage Intacct Web Services (XML gateway).

Only used when the five Intacct credentials are filled in on the Settings page.
Everything else in the app works without it - the bill payload can always be
downloaded as JSON/XML or keyed in by hand from the entry sheet.
"""

from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET

import requests

from .sage_mapper import bill_to_xml

DEFAULT_ENDPOINT = "https://api.intacct.com/ia/xml/xmlgw.phtml"


class SageError(Exception):
    pass


def _control_id() -> str:
    return uuid.uuid4().hex[:24]


def _request_xml(settings: dict, function_body: str) -> str:
    from xml.sax.saxutils import escape as esc

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<request>
  <control>
    <senderid>{esc(settings['sage_sender_id'])}</senderid>
    <password>{esc(settings['sage_sender_password'])}</password>
    <controlid>{_control_id()}</controlid>
    <uniqueid>false</uniqueid>
    <dtdversion>3.0</dtdversion>
    <includewhitespace>false</includewhitespace>
  </control>
  <operation>
    <authentication>
      <login>
        <userid>{esc(settings['sage_user_id'])}</userid>
        <companyid>{esc(settings['sage_company_id'])}</companyid>
        <password>{esc(settings['sage_user_password'])}</password>
      </login>
    </authentication>
    <content>
      <function controlid="{_control_id()}">
{function_body}
      </function>
    </content>
  </operation>
</request>"""


def _call(settings: dict, function_body: str, timeout: int = 60) -> ET.Element:
    endpoint = settings.get("sage_endpoint") or DEFAULT_ENDPOINT
    xml_body = _request_xml(settings, function_body)
    resp = requests.post(
        endpoint,
        data=xml_body.encode("utf-8"),
        headers={"Content-Type": "x-intacct-xml-request"},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise SageError(f"Intacct returned HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise SageError(f"Intacct sent back something that is not XML: {exc}") from exc

    control_status = root.findtext("./control/status")
    if control_status and control_status.lower() != "success":
        raise SageError("Intacct rejected the request: " + _errors(root))

    auth_status = root.findtext("./operation/authentication/status")
    if auth_status and auth_status.lower() != "success":
        raise SageError("Intacct login failed: " + _errors(root))

    result = root.find("./operation/result")
    if result is None:
        raise SageError("Intacct response had no result block: " + _errors(root))
    if (result.findtext("status") or "").lower() != "success":
        raise SageError("Intacct returned an error: " + _errors(result))
    return result


def _errors(node: ET.Element) -> str:
    messages = []
    for err in node.iter("error"):
        parts = [err.findtext("errorno") or "", err.findtext("description") or "", err.findtext("description2") or "", err.findtext("correction") or ""]
        messages.append(" ".join(p for p in parts if p).strip())
    return "; ".join(m for m in messages if m) or "no error detail returned"


def test_connection(settings: dict) -> tuple[bool, str]:
    try:
        result = _call(settings, "        <getAPISession/>")
        session_id = result.findtext("./data/api/sessionid") or "(none)"
        endpoint = result.findtext("./data/api/endpoint") or ""
        return True, f"Connected. Session {session_id[:8]}... via {endpoint or 'default endpoint'}."
    except SageError as exc:
        return False, str(exc)
    except requests.RequestException as exc:
        return False, f"Could not reach Intacct: {exc}"


def post_bill(settings: dict, bill: dict) -> dict:
    """Create the AP bill (or adjustment). Returns {'recordno': ..., 'object': ...}."""
    body = bill_to_xml(bill, indent="  ")
    body = "\n".join("        " + line for line in body.splitlines())
    result = _call(settings, body)
    obj = bill.get("_object", "APBILL")
    recordno = result.findtext(f"./data/{obj.lower()}/RECORDNO") or result.findtext(f"./data/{obj}/RECORDNO") or ""
    if not recordno:
        # Fall back to any RECORDNO in the result
        for node in result.iter():
            if node.tag.upper() == "RECORDNO" and (node.text or "").strip():
                recordno = node.text.strip()
                break
    return {"recordno": recordno, "object": obj}

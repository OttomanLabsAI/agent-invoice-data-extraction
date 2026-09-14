// What the two agents ask Claude for: the invoice record schema (extraction) and
// the label decision (classification), plus their system prompts.

export const LINE_CATEGORIES = ["labour", "materials", "plant", "subcontract", "services", "expenses", "other"];
export const DOCUMENT_TYPES = ["invoice", "credit_note", "application_for_payment", "proforma", "statement", "remittance", "other"];
export const VAT_TREATMENTS = ["standard", "reduced", "zero", "exempt", "reverse_charge", "mixed", "unknown"];

const money = (desc) => ({ type: ["number", "null"], description: desc + " Plain number, no currency symbol, 2dp." });
const text = (desc) => ({ type: ["string", "null"], description: desc });

export const INVOICE_TOOL = {
  name: "record_invoice",
  description:
    "Record the contents of one supplier document (invoice, credit note, application for payment) " +
    "as structured data ready to be posted as an accounts payable bill in Sage Intacct.",
  input_schema: {
    type: "object",
    properties: {
      document_type: { type: "string", enum: DOCUMENT_TYPES },
      supplier: {
        type: "object",
        properties: {
          name: text("Legal / trading name of the supplier exactly as printed."),
          address: text("Supplier postal address on one line."),
          postcode: text("Supplier postcode."),
          country: text("Supplier country, default United Kingdom if a UK address."),
          vat_number: text("Supplier VAT registration number, e.g. GB123456789."),
          company_number: text("Companies House registration number if printed."),
          email: text("Accounts / remittance email address if printed."),
          phone: text("Phone number if printed."),
        },
      },
      bill_to: {
        type: "object",
        properties: {
          name: text("Customer name the invoice is addressed to."),
          address: text("Customer address on one line."),
        },
      },
      invoice_number: text("Supplier's invoice / credit note number exactly as printed."),
      invoice_date: text("Invoice date as YYYY-MM-DD."),
      due_date: text("Payment due date as YYYY-MM-DD if printed or derivable from terms."),
      payment_terms_days: { type: ["integer", "null"], description: "Payment terms in days, e.g. 30." },
      payment_terms_text: text("Payment terms exactly as printed, e.g. '30 days from invoice date'."),
      po_number: text("Customer purchase order number quoted on the invoice."),
      order_reference: text("Any other order / contract / quote reference quoted."),
      delivery_note_number: text("Delivery note or ticket number(s) if quoted."),
      project_reference: text("Project, job, site or contract name/code the work relates to (e.g. a data centre site code)."),
      site_address: text("Site / delivery address if different from the bill-to address."),
      description: text("One-line summary of what was supplied, suitable as a bill description in Sage."),
      currency: { type: "string", description: "ISO 4217 code, e.g. GBP, EUR." },
      line_items: {
        type: "array",
        description: "Every billable line. Keep the supplier's order.",
        items: {
          type: "object",
          properties: {
            description: text("Line description."),
            quantity: { type: ["number", "null"] },
            unit: text("Unit of measure, e.g. each, m, hrs, day, tonne."),
            unit_price: money("Unit price ex VAT."),
            net_amount: money("Line total ex VAT."),
            vat_rate: { type: ["number", "null"], description: "VAT rate percent for the line, e.g. 20, 5, 0." },
            vat_amount: money("VAT on the line."),
            category: { type: "string", enum: LINE_CATEGORIES },
          },
          required: ["description", "net_amount", "category"],
        },
      },
      net_total: money("Total ex VAT."),
      vat_total: money("Total VAT charged."),
      gross_total: money("Total inc VAT."),
      discount_amount: money("Settlement or other discount shown, if any."),
      amount_due: money("Amount payable after any CIS deduction, retention or discount, if shown separately from the gross total."),
      vat_breakdown: {
        type: "array",
        description: "One entry per VAT rate used.",
        items: {
          type: "object",
          properties: {
            rate: { type: ["number", "null"] },
            net: money("Net at this rate."),
            vat: money("VAT at this rate."),
          },
        },
      },
      vat_treatment: {
        type: "string",
        enum: VAT_TREATMENTS,
        description: "reverse_charge when the invoice says the customer must account for VAT under the domestic reverse charge for construction services.",
      },
      cis: {
        type: "object",
        description: "Construction Industry Scheme details if the invoice is from a subcontractor.",
        properties: {
          applicable: { type: "boolean" },
          labour_amount: money("Labour element subject to CIS deduction."),
          materials_amount: money("Materials element (not deductible)."),
          deduction_rate: { type: ["number", "null"], description: "CIS deduction rate percent (0, 20 or 30) if stated." },
          deduction_amount: money("CIS deduction shown, if any."),
        },
        required: ["applicable"],
      },
      retention: {
        type: "object",
        properties: {
          applicable: { type: "boolean" },
          percentage: { type: ["number", "null"] },
          amount: money("Retention withheld on this invoice."),
        },
        required: ["applicable"],
      },
      bank_details: {
        type: "object",
        properties: {
          account_name: text("Account name."),
          bank_name: text("Bank name."),
          sort_code: text("Sort code as printed, e.g. 12-34-56."),
          account_number: text("Account number."),
          iban: text("IBAN if printed."),
          bic: text("BIC / SWIFT if printed."),
        },
      },
      totals_reconcile: { type: "boolean", description: "true if net_total + vat_total = gross_total and the lines add up to net_total (within 0.02)." },
      confidence: { type: "number", description: "0 to 1. Your confidence that the key fields (supplier, number, dates, totals) are correct." },
      flags: {
        type: "array",
        items: { type: "string" },
        description: "Anything the accounts team should check by hand: unreadable text, missing PO, totals that do not add up, duplicate risk, handwritten amendments, multiple invoices in one file, etc.",
      },
    },
    required: [
      "document_type", "supplier", "invoice_number", "invoice_date", "currency",
      "line_items", "net_total", "vat_total", "gross_total", "vat_treatment",
      "cis", "retention", "totals_reconcile", "confidence", "flags",
    ],
  },
};

export const CLASSIFY_TOOL = {
  name: "label_email",
  description:
    "Decide whether this email is a supplier invoice, credit note or application for payment that the accounts team must process.",
  input_schema: {
    type: "object",
    properties: {
      verdict: { type: "string", enum: ["invoice", "not_invoice"] },
      document_kind: {
        type: "string",
        description:
          "What the email and its attachments actually are: invoice, credit note, application for payment, statement, remittance advice, quote, purchase order, delivery note, marketing, personal, other.",
      },
      confidence: { type: "number", description: "0 to 1." },
      reason: { type: "string", description: "One sentence for the accounts team explaining the decision." },
    },
    required: ["verdict", "document_kind", "confidence", "reason"],
  },
};

function referenceSection(referenceText) {
  const text = (referenceText || "").trim();
  if (!text) return "";
  return `

Reference notes from the accounts team. Treat them as authoritative background (supplier names, project codes, house conventions, examples); the document itself is still the source of truth for what is printed on it:
---
${text}
---`;
}

export function extractionSystemPrompt(companyName, defaultCurrency, referenceText = "") {
  return `You are the accounts payable assistant for ${companyName}, a UK construction contractor delivering MEP and civils packages on data-centre and infrastructure projects. Suppliers and subcontractors email their invoices to a shared mailbox; your job is to read each document and capture exactly what the accounts team keys into Sage Intacct when posting an AP bill.

Extract every field you can read directly from the document. Never invent a value: if something is not printed, leave it null and add a flag. Specifically:
- Dates: output YYYY-MM-DD. If only payment terms are printed (e.g. "30 days"), derive due_date from invoice_date and say so in a flag.
- Amounts: plain numbers, no currency symbols or thousands separators. Default currency ${defaultCurrency} unless the document clearly shows another.
- Lines: capture every billable line with its net amount and VAT rate. Classify each line as labour, materials, plant, subcontract, services, expenses or other. A subcontractor's "supply and fit" line is subcontract; hire of equipment is plant; consumables and deliveries are materials.
- UK VAT: identify the treatment. If the invoice says "reverse charge", "domestic reverse charge applies", "customer to pay VAT to HMRC" or shows VAT at 0 with a reverse-charge note, set vat_treatment to reverse_charge and vat_total to 0 (the VAT is accounted for by the customer).
- CIS: if the invoice separates labour and materials, or shows a CIS deduction, or the supplier is clearly a subcontractor doing site work, fill the cis block. Materials are never subject to CIS deduction.
- Retention: capture any retention percentage or amount withheld.
- References: purchase order numbers, delivery notes, project or site codes and contract references matter a lot for coding the bill - capture every one you can see.
- Check the arithmetic: lines should sum to net_total and net_total + vat_total should equal gross_total. If they do not, still record what is printed and set totals_reconcile to false with a flag explaining the difference.
- If the file is not an invoice or credit note (a statement, remittance advice, quote, delivery note, marketing), set document_type accordingly and keep the rest minimal.
- If a file contains more than one invoice, extract the first and flag that others exist.

Use the record_invoice tool for your answer.${referenceSection(referenceText)}`;
}

export function classificationSystemPrompt(companyName, referenceText = "") {
  return `You are the mailbox classifier for ${companyName}, a UK construction contractor. Suppliers and subcontractors email a shared accounts mailbox. Read each email and its attachments and decide whether it carries a document the accounts team must process as a purchase invoice.

Count as an invoice: supplier invoices, credit notes, applications for payment (AFPs / payment applications from subcontractors), pro-forma invoices that are due for payment, and invoices arriving as images or scans.
Do not count: statements of account, remittance advices, quotes and estimates, purchase orders, order acknowledgements, delivery notes and tickets, timesheets, marketing, newsletters, personal mail, internal mail, and anything with no readable document at all.

Judge from the attachments first and the email text second. An email whose only attachment is a logo or signature image carries no document. If an email contains both an invoice and other paperwork, it counts as an invoice.

Use the label_email tool for your answer: verdict, what kind of document it is, your confidence and a one-sentence reason.${referenceSection(referenceText)}`;
}

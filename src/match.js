// Fuzzy name matching shared by the vendor / project maps and the invoice types.

const LEGAL_SUFFIXES = /\b(ltd|limited|plc|llp|llc|inc|co|company|uk|group|holdings|services|the)\b/g;

export function norm(name) {
  let text = String(name || "").toLowerCase();
  text = text.replace(/[^a-z0-9 ]+/g, " ");
  text = text.replace(LEGAL_SUFFIXES, " ");
  return text.replace(/\s+/g, " ").trim();
}

/** Returns [value, matchedKey]. Exact normalised match first, then containment either way. */
export function lookup(mapping, ...candidates) {
  if (!mapping || !Object.keys(mapping).length) return ["", ""];
  const normalised = new Map();
  for (const [k, v] of Object.entries(mapping)) if (k && v) normalised.set(norm(k), [k, v]);
  const cands = candidates.filter(Boolean).map(norm);
  for (const cand of cands) {
    if (normalised.has(cand)) {
      const [key, value] = normalised.get(cand);
      return [value, key];
    }
  }
  for (const cand of cands) {
    for (const [nkey, [key, value]] of normalised) {
      if (nkey.length >= 3 && (cand.includes(nkey) || nkey.includes(cand))) return [value, key];
    }
  }
  return ["", ""];
}

/** True when the supplier name matches one of the listed names, by the same rules as the vendor map. */
export function nameListed(name, names) {
  const mapping = Object.fromEntries(names.filter(Boolean).map((n) => [n, "x"]));
  return lookup(mapping, name)[0] === "x";
}

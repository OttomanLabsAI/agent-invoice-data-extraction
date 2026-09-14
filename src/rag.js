// Reference-text retrieval for the agents. Short texts are sent whole; a long
// text is split into paragraphs and only the ones that share words with the
// email (subject, sender, body, attachment names) are sent, up to a budget.

export const RAG_MAX_CHARS = 16000;

const STOP = new Set(
  ("a an and are as at be by for from has have in is it its of on or that the this to was were will with you your we our " +
    "not no if then than so do does did can could would should may might into onto over under please find attached see " +
    "regards thanks thank hi hello dear re fw fwd").split(" "),
);

export function tokens(text) {
  const words = String(text || "").toLowerCase().match(/[a-z0-9][a-z0-9'.\-]*/g) || [];
  return words.filter((w) => w.length >= 2 && !STOP.has(w));
}

export function paragraphs(text) {
  return String(text || "")
    .split(/\r?\n\s*\r?\n/)
    .map((b) => b.trim())
    .filter(Boolean);
}

/** The whole text when it fits, otherwise the best-matching paragraphs in their original order. */
export function retrieve(text, query, maxChars = RAG_MAX_CHARS) {
  const whole = String(text || "").trim();
  if (!whole) return "";
  if (whole.length <= maxChars) return whole;

  const wanted = new Set(tokens(query));
  const scored = paragraphs(whole).map((block, index) => {
    const seen = new Set();
    for (const word of tokens(block)) if (wanted.has(word)) seen.add(word);
    return { index, block, score: seen.size };
  });
  scored.sort((a, b) => b.score - a.score || a.block.length - b.block.length || a.index - b.index);

  const chosen = [];
  let used = 0;
  for (const entry of scored) {
    if (entry.score === 0 && chosen.length) break;
    if (used + entry.block.length + 2 > maxChars) continue;
    chosen.push(entry);
    used += entry.block.length + 2;
  }
  if (!chosen.length) return whole.slice(0, maxChars);
  chosen.sort((a, b) => a.index - b.index);
  return chosen.map((e) => e.block).join("\n\n");
}

export function describe(text, maxChars = RAG_MAX_CHARS) {
  const whole = String(text || "").trim();
  const count = paragraphs(whole).length;
  return { chars: whole.length, paragraphs: count, whole: whole.length <= maxChars, maxChars };
}

// Attachments stored in D1 as chunked BLOB rows, so the app needs no R2 bucket
// and the first deploy has nothing to create by hand. D1 caps a row at 2 MB,
// so a file is split into CHUNK_BYTES pieces keyed by (key, seq).

export const CHUNK_BYTES = 512 * 1024;
const BATCH = 20;

export function splitChunks(bytes, size = CHUNK_BYTES) {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  const chunks = [];
  for (let offset = 0; offset < view.length; offset += size) chunks.push(view.subarray(offset, Math.min(offset + size, view.length)));
  if (!chunks.length) chunks.push(new Uint8Array(0));
  return chunks;
}

export function joinChunks(chunks) {
  const views = chunks.map((c) => (c instanceof Uint8Array ? c : new Uint8Array(c)));
  const out = new Uint8Array(views.reduce((n, v) => n + v.length, 0));
  let offset = 0;
  for (const v of views) {
    out.set(v, offset);
    offset += v.length;
  }
  return out;
}

export async function putFile(db, key, bytes, contentType) {
  const chunks = splitChunks(bytes);
  const total = bytes.byteLength;
  await db.prepare("DELETE FROM files WHERE key = ?").bind(key).run();
  for (let i = 0; i < chunks.length; i += BATCH) {
    const statements = chunks.slice(i, i + BATCH).map((chunk, j) =>
      db
        .prepare("INSERT INTO files (key, seq, content_type, size, data) VALUES (?, ?, ?, ?, ?)")
        .bind(key, i + j, contentType || "application/octet-stream", total, chunk.slice().buffer),
    );
    await db.batch(statements);
  }
}

/** Returns { bytes, contentType, size } or null. */
export async function getFile(db, key) {
  const { results } = await db.prepare("SELECT seq, content_type, size, data FROM files WHERE key = ? ORDER BY seq").bind(key).all();
  if (!results || !results.length) return null;
  return { bytes: joinChunks(results.map((r) => r.data)), contentType: results[0].content_type, size: results[0].size };
}

export async function deleteFile(db, key) {
  await db.prepare("DELETE FROM files WHERE key = ?").bind(key).run();
}

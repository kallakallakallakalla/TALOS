// The CRM's mail search on Netlify: GET /crm/api/search?q=... (netlify.toml routes it here).
// Same matching as desk.search_mails in crm/desk.py: newest first, at most 30, the query
// found in the mail's id, sender, subject or text. The index is written by
// crm/netlify_build.py during the build and bundled into this function.
import mails from '../../crm/.netlify-build/mails.json' with { type: 'json' };

const LIMIT = 30;

export default async request => {
  const q = (new URL(request.url).searchParams.get('q') || '').slice(0, 200).trim().toLowerCase();
  const hits = [];
  if (q) {
    for (const { hit, text } of mails) {
      if (text.some(t => t.includes(q))) {
        hits.push(hit);
        if (hits.length >= LIMIT) break;
      }
    }
  }
  return new Response(JSON.stringify({ mails: hits }), {
    headers: { 'content-type': 'application/json', 'cache-control': 'no-cache' },
  });
};

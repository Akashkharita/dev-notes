// worker/index.js
// Cloudflare Worker — RAG chat endpoint
// Deploy with: wrangler deploy
// Secrets needed: GEMINI_API_KEY, CHAT_PASSWORD

const GITHUB_RAW  = 'https://raw.githubusercontent.com/Akashkharita/dev-notes/main/public/index.json';
const EMBED_MODEL = 'text-embedding-004';
const CHAT_MODEL  = 'gemini-2.0-flash';
const TOP_K       = 6;

// Cosine similarity
function cosineSim(a, b) {
  let dot = 0, na = 0, nb = 0;
  for (let i = 0; i < a.length; i++) { dot += a[i]*b[i]; na += a[i]**2; nb += b[i]**2; }
  return dot / (Math.sqrt(na) * Math.sqrt(nb));
}

async function geminiEmbed(text, apiKey) {
  const r = await fetch(
    `https://generativelanguage.googleapis.com/v1beta/models/${EMBED_MODEL}:embedContent?key=${apiKey}`,
    { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ model:`models/${EMBED_MODEL}`, content:{ parts:[{text}] }, taskType:'RETRIEVAL_QUERY' }) }
  );
  const d = await r.json();
  return d.embedding.values;
}

async function geminiChat(messages, context, apiKey) {
  const system = `You are a developer assistant with access to Akash Kharita's daily commit notes.
Answer questions about his work using only the context below. Be concise and specific.
If the answer isn't in the context, say so.

CONTEXT:
${context}`;

  const contents = messages.map(m => ({
    role: m.role === 'assistant' ? 'model' : 'user',
    parts: [{ text: m.content }]
  }));

  const r = await fetch(
    `https://generativelanguage.googleapis.com/v1beta/models/${CHAT_MODEL}:generateContent?key=${apiKey}`,
    { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ system_instruction:{ parts:[{text:system}] }, contents }) }
  );
  const d = await r.json();
  return d.candidates?.[0]?.content?.parts?.[0]?.text || 'No response generated.';
}

let cachedIndex = null;
let cacheTime   = 0;
const CACHE_TTL = 60 * 60 * 1000; // 1 hour

async function getIndex() {
  if (cachedIndex && (Date.now() - cacheTime) < CACHE_TTL) return cachedIndex;
  const r = await fetch(GITHUB_RAW, { cf:{ cacheTtl: 3600 } });
  cachedIndex = await r.json();
  cacheTime = Date.now();
  return cachedIndex;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const cors = {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type, X-Chat-Password',
    };

    if (request.method === 'OPTIONS') return new Response(null, { headers: cors });

    if (url.pathname === '/chat' && request.method === 'POST') {
      // Password check
      const pw = request.headers.get('X-Chat-Password') || '';
      if (pw !== env.CHAT_PASSWORD) {
        return new Response(JSON.stringify({error:'Unauthorized'}),
          { status:401, headers:{...cors,'Content-Type':'application/json'} });
      }

      const { messages } = await request.json();
      const query = messages[messages.length - 1].content;

      try {
        const [queryEmbedding, index] = await Promise.all([
          geminiEmbed(query, env.GEMINI_API_KEY),
          getIndex(),
        ]);

        // Retrieve top-K chunks
        const scored = index
          .map(chunk => ({ ...chunk, score: cosineSim(queryEmbedding, chunk.embedding) }))
          .sort((a,b) => b.score - a.score)
          .slice(0, TOP_K);

        const context = scored
          .map(c => `[${c.date}]\n${c.text}`)
          .join('\n\n---\n\n');

        const answer = await geminiChat(messages, context, env.GEMINI_API_KEY);
        return new Response(JSON.stringify({ answer }),
          { headers:{...cors,'Content-Type':'application/json'} });

      } catch(e) {
        return new Response(JSON.stringify({error: e.message}),
          { status:500, headers:{...cors,'Content-Type':'application/json'} });
      }
    }

    return new Response('Not found', { status:404 });
  }
};

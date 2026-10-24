// Azure TV sync gateway. Only one named GitHub Actions workflow may upload.
const ISSUER = 'https://token.actions.githubusercontent.com';
const AUDIENCE = 'azure-tv-sync-v1';
const REPO = 'FeiRune12/iptv-brasileira';
const REPO_ID = '1427806223';
const WORKFLOW = REPO + '/.github/workflows/azure-tv-auto.yml@refs/heads/main';
const STATE_KEY = '__azure_tv_sync/state.json';
const PLAYLIST_KEY = 'playlist.m3u8';
let jwksCache = {until: 0, keys: []};

function asBytes(part) {
  const raw = part.replace(/-/g, '+').replace(/_/g, '/');
  const bin = atob(raw + '='.repeat((4 - raw.length % 4) % 4));
  return Uint8Array.from(bin, c => c.charCodeAt(0));
}
function json(value, status = 200) {
  return new Response(JSON.stringify(value), {status, headers:{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store'}});
}
async function authorized(request) {
  const m = /^Bearer (\S+)$/i.exec(request.headers.get('authorization') || '');
  if (!m) return false;
  const token = m[1];
  if (token.length > 12000) return false;
  const parts = token.split('.');
  if (parts.length !== 3) return false;
  let header, payload;
  try {
    header = JSON.parse(new TextDecoder().decode(asBytes(parts[0])));
    payload = JSON.parse(new TextDecoder().decode(asBytes(parts[1])));
  } catch { return false; }
  if (header.alg !== 'RS256' || typeof header.kid !== 'string') return false;
  if (payload.iss !== ISSUER || payload.aud !== AUDIENCE) return false;
  if (payload.repository !== REPO || String(payload.repository_id) !== REPO_ID) return false;
  if (payload.ref !== 'refs/heads/main') return false;
  if (payload.workflow_ref !== WORKFLOW && payload.job_workflow_ref !== WORKFLOW) return false;
  if (!['schedule','workflow_dispatch','push'].includes(payload.event_name)) return false;
  const now = Math.floor(Date.now() / 1000);
  if (!Number.isInteger(payload.exp) || now >= payload.exp) return false;
  if (!Number.isInteger(payload.nbf) || now + 30 < payload.nbf) return false;
  if (!Number.isInteger(payload.iat) || payload.iat > now + 30) return false;
  try {
    if (jwksCache.until < Date.now()) {
      const response = await fetch(ISSUER + '/.well-known/jwks', {headers:{Accept:'application/json'}});
      if (!response.ok) return false;
      const data = await response.json();
      if (!Array.isArray(data.keys)) return false;
      jwksCache = {until:Date.now()+3600000, keys:data.keys};
    }
    const jwk = jwksCache.keys.find(k => k.kid === header.kid && k.kty === 'RSA' && (!k.alg || k.alg === 'RS256'));
    if (!jwk) return false;
    const key = await crypto.subtle.importKey('jwk', jwk, {name:'RSASSA-PKCS1-v1_5', hash:'SHA-256'}, false, ['verify']);
    return await crypto.subtle.verify('RSASSA-PKCS1-v1_5', key, asBytes(parts[2]), new TextEncoder().encode(parts[0]+'.'+parts[1]));
  } catch { return false; }
}
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === '/health' && request.method === 'GET') return json({service:'azure-tv-sync',ready:true});
    if (!(await authorized(request))) return json({error:'Unauthorized'}, 401);
    try {
      if (request.method === 'GET' && url.pathname === '/state') {
        const [state, live] = await Promise.all([env.PLAYLIST.get(STATE_KEY),env.PLAYLIST.head(PLAYLIST_KEY)]);
        return json({state:state?await state.json():null,liveBytes:live?.size||0});
      }
      if (request.method === 'POST' && url.pathname === '/restore') {
        // One-time emergency restoration of the original catalog.
        const key = '__azure_tv_sync/backups/2026-10-24T19-43-16-467Z.m3u8';
        const expectedOriginal = 111000682;
        const expectedCurrent = 297823822;
        const [backupHead, current] = await Promise.all([env.PLAYLIST.head(key), env.PLAYLIST.head(PLAYLIST_KEY)]);
        if (!backupHead || backupHead.size !== expectedOriginal) return json({error:'Original backup missing/mismatched'},409);
        if (!current || current.size !== expectedCurrent) return json({error:'Live playlist changed; refusing unsafe restore',liveBytes:current?.size},409);
        const original = await env.PLAYLIST.get(key);
        if (!original) return json({error:'Cannot read backup'},500);
        const saved = await env.PLAYLIST.put(PLAYLIST_KEY, original.body, {
          httpMetadata:{contentType:'application/vnd.apple.mpegurl'}
        });
        const verify = await env.PLAYLIST.head(PLAYLIST_KEY);
        if (!saved || !verify || verify.size !== expectedOriginal) return json({error:'Restore verification failed',size:verify?.size},500);
        await env.PLAYLIST.put(STATE_KEY,JSON.stringify({restoredFrom:key,bytes:verify.size,restoredAt:new Date().toISOString(),syncPaused:true}),{httpMetadata:{contentType:'application/json'}});
        return json({ok:true,restoredBytes:verify.size,sourceBackup:key});
      }
      if (request.method === 'POST' && url.pathname === '/start') {
        const live = await env.PLAYLIST.get(PLAYLIST_KEY);
        let backup = null;
        if (live) {
          backup = '__azure_tv_sync/backups/' + new Date().toISOString().replace(/[:.]/g,'-') + '.m3u8';
          const saved = await env.PLAYLIST.put(backup, live.body, {httpMetadata:{contentType:'application/vnd.apple.mpegurl'}});
          if (saved.size !== live.size) return json({error:'Backup size mismatch'},500);
        }
        const upload = await env.PLAYLIST.createMultipartUpload(PLAYLIST_KEY,{httpMetadata:{contentType:'application/vnd.apple.mpegurl'}});
        return json({uploadId:upload.uploadId,backup});
      }
      if (request.method === 'POST' && url.pathname === '/part') {
        const uploadId = url.searchParams.get('uploadId') || '';
        const num = Number(url.searchParams.get('number'));
        if (uploadId.length < 5 || uploadId.length > 2048 || !Number.isInteger(num) || num < 1 || num > 200) return json({error:'Invalid part'},400);
        const len = Number(request.headers.get('content-length') || 0);
        if (len > 9000000) return json({error:'Part too large'},413);
        const buf = await request.arrayBuffer();
        if (buf.byteLength > 9000000 || buf.byteLength === 0) return json({error:'Invalid part body'},413);
        const upload = env.PLAYLIST.resumeMultipartUpload(PLAYLIST_KEY,uploadId);
        const part = await upload.uploadPart(num, buf);
        return json({partNumber:part.partNumber,etag:part.etag});
      }
      if (request.method === 'POST' && url.pathname === '/finish') {
        const body = await request.json();
        if (typeof body.uploadId !== 'string' || !Array.isArray(body.parts) || body.parts.length < 2 || body.parts.length > 200) return json({error:'Bad multipart request'},400);
        if (!body.parts.every((p,i)=>p.partNumber===i+1 && typeof p.etag==='string')) return json({error:'Invalid part order'},400);
        if (!body.meta || !Number.isInteger(body.meta.total) || body.meta.total < 10000 || !body.meta.sources || typeof body.meta.sources !== 'object') return json({error:'Invalid metadata'},400);
        const upload = env.PLAYLIST.resumeMultipartUpload(PLAYLIST_KEY,body.uploadId);
        const published = await upload.complete(body.parts);
        if (published.size < 10000000) return json({error:'Published object unexpectedly small, restore backup'},500);
        await env.PLAYLIST.put(STATE_KEY, JSON.stringify({...body.meta,bytes:published.size,updatedAt:new Date().toISOString()}),{httpMetadata:{contentType:'application/json'}});
        // Keep the first historical backup and the two most recent ones.
        try {
          const listing = await env.PLAYLIST.list({prefix:'__azure_tv_sync/backups/',limit:1000});
          const backups = listing.objects.map(o=>o.key).sort();
          if (!listing.truncated && backups.length > 3) {
            await env.PLAYLIST.delete(backups.slice(1, -2));
          }
        } catch (ignored) { /* A failed cleanup must not undo a successful publish. */ }
        return json({ok:true,bytes:published.size});
      }
      if (request.method === 'POST' && url.pathname === '/abort') {
        const body = await request.json();
        if (typeof body.uploadId !== 'string') return json({error:'Invalid uploadId'},400);
        await env.PLAYLIST.resumeMultipartUpload(PLAYLIST_KEY,body.uploadId).abort();
        return json({ok:true});
      }
      return json({error:'Not Found'},404);
    } catch (error) {
      return json({error:String(error?.message||error).slice(0,350)},500);
    }
  }
};
// Azure TV sync gateway. Only one named GitHub Actions workflow may upload.
const ISSUER = 'https://token.actions.githubusercontent.com';
const AUDIENCE = 'azure-tv-sync-v1';
const REPO = 'FeiRune12/iptv-brasileira';
const REPO_ID = '1427806223';
const WORKFLOW = REPO + '/.github/workflows/azure-tv-promote.yml@refs/heads/main';
const STATE_KEY = '__azure_tv_preview/state.json';
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

const PREVIEW_KEY = '__azure_tv_preview/playlist.m3u8';
const MINI_KEY = '__azure_tv_preview/channels-test.m3u8';
const SAFE_HEADERS = {'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'};
async function allowPermanentKey(url, bucket) {
  const token = url.searchParams.get('access_key') || '';
  if (!/^aztv_[0-9a-f]{64}$/.test(token)) return false;
  const buf = await crypto.subtle.digest('SHA-256',new TextEncoder().encode(token));
  const hex = Array.from(new Uint8Array(buf), x=>x.toString(16).padStart(2,'0')).join('');
  return !!(await bucket.head('__azure_tv_keys/'+hex));
}
function fail(msg, status) {return json({error:msg},status);}

const ORIGINAL_KEY='playlist.m3u8';
const STAGED_KEY='__azure_tv_preview/playlist.m3u8';
const STAGED_STATE='__azure_tv_preview/state.json';
const BACKUP_KEY='__azure_tv_sync/backups/favorites-before-119-channel-update-20261024.m3u8';
const MARKER='__azure_tv_sync/promotions/20261024-approved119.json';
const EXPECTED_ORIGINAL={size:111000682,etag:'ebd1587511849e575e093055fe56e5f0'};
const EXPECTED_STAGE={size:111000186,etag:'bc7adc9ed3e33540d89c01849d1eee10-14'};
const EXPECTED_COUNT=119;

function fail(code,msg) {return json({ok:false,error:msg},code);}
export default {
  async fetch(request,env) {
    if (!(await authorized(request))) return fail(401,'Unauthorized');
    const path=new URL(request.url).pathname;
    const method=request.method;
    if (method==='GET' && path==='/health') return json({ready:true,purpose:'one-time, 119 channels'});
    if (method==='GET' && path==='/state') {
      const [original,stage,metadata,marker]=await Promise.all([
        env.PLAYLIST.head(ORIGINAL_KEY),env.PLAYLIST.head(STAGED_KEY),
        env.PLAYLIST.get(STAGED_STATE),env.PLAYLIST.get(MARKER)
      ]);
      return json({original:{size:original?.size,etag:original?.etag},
        staged:{size:stage?.size,etag:stage?.etag},
        metadata:metadata?await metadata.json():null,
        alreadyPromoted:!!marker,marker:marker?await marker.json():null});
    }
    if (method==='GET' && (path==='/original'||path==='/candidate'||path==='/published')) {
      const key=path==='/candidate'?STAGED_KEY:ORIGINAL_KEY;
      const obj=await env.PLAYLIST.get(key);
      if (!obj) return fail(404,'Object not found');
      return new Response(obj.body,{headers:{
        'Content-Type':'application/vnd.apple.mpegurl','Cache-Control':'private,no-store',
        'X-Azure-Bytes':String(obj.size),'X-Azure-Etag':obj.etag
      }});
    }
    if (method!=='POST'||path!=='/promote') return fail(404,'No route');
    if (await env.PLAYLIST.head(MARKER)) return fail(409,'One-time operation already completed');
    let body;
    try {body=await request.json();}catch{return fail(400,'Invalid JSON');}
    if (!body || body.confirmation!=='PROMOTE_119_URL_ONLY'
        || body.count!==EXPECTED_COUNT
        || !/^[0-9a-f]{64}$/.test(body.originalSha256||'')
        || !/^[0-9a-f]{64}$/.test(body.candidateSha256||'')
        || body.originalSha256===body.candidateSha256) return fail(400,'Explicit checked confirmation absent');
    let current=await env.PLAYLIST.head(ORIGINAL_KEY);
    let stage=await env.PLAYLIST.head(STAGED_KEY);
    if (!current||current.size!==EXPECTED_ORIGINAL.size||current.etag!==EXPECTED_ORIGINAL.etag) return fail(409,'Live playlist unexpected: aborting');
    if (!stage||stage.size!==EXPECTED_STAGE.size||stage.etag!==EXPECTED_STAGE.etag) return fail(409,'Staged playlist changed: aborting');
    const metaObj=await env.PLAYLIST.get(STAGED_STATE);
    if (!metaObj) return fail(409,'Preview validation report missing');
    const meta=await metaObj.json();
    if (meta.urlsChanged!==EXPECTED_COUNT ||
        meta.originalEtag!==EXPECTED_ORIGINAL.etag ||
        meta.originalBytes!==EXPECTED_ORIGINAL.size ||
        meta.previewBytes!==EXPECTED_STAGE.size ||
        meta.entries!==431730||meta.protectedVOD!==431062) return fail(409,'Preview validation report mismatch');
    if (await env.PLAYLIST.head(BACKUP_KEY)) return fail(409,'Safety backup already exists, operation aborted');
    // Back up active playlist before touching it. R2 put makes copy visible atomically.
    const src=await env.PLAYLIST.get(ORIGINAL_KEY);
    if (!src) return fail(409,'Could not read original');
    const backup=await env.PLAYLIST.put(BACKUP_KEY,src.body,{httpMetadata:{contentType:'application/vnd.apple.mpegurl'}});
    const verifiedBackup=await env.PLAYLIST.head(BACKUP_KEY);
    if (!backup||!verifiedBackup||verifiedBackup.size!==EXPECTED_ORIGINAL.size||
        verifiedBackup.etag!==EXPECTED_ORIGINAL.etag) return fail(500,'Backup verification failed: live unchanged');
    // Check again for conflicting writers just before replacement.
    current=await env.PLAYLIST.head(ORIGINAL_KEY);
    stage=await env.PLAYLIST.head(STAGED_KEY);
    if (!current||current.etag!==EXPECTED_ORIGINAL.etag||!stage||stage.etag!==EXPECTED_STAGE.etag) return fail(409,'Objects changed during backup: live unchanged');
    let startedReplacement=false;
    try {
      const candidate=await env.PLAYLIST.get(STAGED_KEY);
      if (!candidate) return fail(409,'Candidate disappeared');
      startedReplacement=true;
      const saved=await env.PLAYLIST.put(ORIGINAL_KEY,candidate.body,{httpMetadata:{contentType:'application/vnd.apple.mpegurl'}});
      const visible=await env.PLAYLIST.head(ORIGINAL_KEY);
      if (!saved||!visible||visible.size!==EXPECTED_STAGE.size) throw new Error('Size verification failed after copying candidate');
      const promotion={
        changes:EXPECTED_COUNT,originalSha256:body.originalSha256,
        candidateSha256:body.candidateSha256,backup:BACKUP_KEY,
        before:{bytes:EXPECTED_ORIGINAL.size,etag:EXPECTED_ORIGINAL.etag},
        after:{bytes:visible.size,etag:visible.etag},
        at:new Date().toISOString(),automaticFuturePromotions:false
      };
      await env.PLAYLIST.put(MARKER,JSON.stringify(promotion),{httpMetadata:{contentType:'application/json'}});
      return json({ok:true,...promotion});
    } catch(error) {
      if (startedReplacement) {
        try {
          const restore=await env.PLAYLIST.get(BACKUP_KEY);
          if (restore) await env.PLAYLIST.put(ORIGINAL_KEY,restore.body,{httpMetadata:{contentType:'application/vnd.apple.mpegurl'}});
          const afterRestore=await env.PLAYLIST.head(ORIGINAL_KEY);
          return fail(500,'Publish failed. Rollback: '+(afterRestore?.etag===EXPECTED_ORIGINAL.etag?'verified':'requires manual inspection')+'. '+String(error).slice(0,150));
        } catch(e) { return fail(500,'Publish failed and automatic rollback failed. Backup key: '+BACKUP_KEY); }
      }
      return fail(500,'Not published: '+String(error).slice(0,150));
    }
  }
};

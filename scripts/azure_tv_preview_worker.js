// Azure TV sync gateway. Only one named GitHub Actions workflow may upload.
const ISSUER = 'https://token.actions.githubusercontent.com';
const AUDIENCE = 'azure-tv-sync-v1';
const REPO = 'FeiRune12/iptv-brasileira';
const REPO_ID = '1427806223';
const WORKFLOW = REPO + '/.github/workflows/azure-tv-preview.yml@refs/heads/main';
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
const SAFE_HEADERS = {'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'};
async function allowPermanentKey(url, bucket) {
  const token = url.searchParams.get('access_key') || '';
  if (!/^aztv_[0-9a-f]{64}$/.test(token)) return false;
  const buf = await crypto.subtle.digest('SHA-256',new TextEncoder().encode(token));
  const hex = Array.from(new Uint8Array(buf), x=>x.toString(16).padStart(2,'0')).join('');
  return !!(await bucket.head('__azure_tv_keys/'+hex));
}
function fail(msg, status) {return json({error:msg},status);}
export default {
  async fetch(request,env) {
    const url=new URL(request.url), method=request.method;
    if (url.pathname==='/health' && method==='GET') return json({ready:true,name:'azure-tv-preview'});
    try {
      if (url.pathname==='/playlist-preview.m3u8' && (method==='GET'||method==='HEAD')) {
        if (!(await allowPermanentKey(url,env.PLAYLIST))) return new Response('Acesso negado',{status:401,headers:SAFE_HEADERS});
        const obj=method==='HEAD' ? await env.PLAYLIST.head(PREVIEW_KEY) : await env.PLAYLIST.get(PREVIEW_KEY);
        if (!obj) return new Response('Playlist de teste ainda nao gerada',{status:404,headers:SAFE_HEADERS});
        return new Response(method==='HEAD'?null:obj.body,{headers:{...SAFE_HEADERS,'Content-Type':'application/vnd.apple.mpegurl; charset=utf-8','Content-Disposition':'attachment; filename="Azure_TV_Preview.m3u8"'}});
      }
      if (!(await authorized(request))) return fail('Unauthorized',401);
      if (url.pathname==='/original' && method==='GET') {
        const obj=await env.PLAYLIST.get(PLAYLIST_KEY);
        if (!obj) return fail('Original missing',404);
        return new Response(obj.body,{headers:{...SAFE_HEADERS,'Content-Type':'application/vnd.apple.mpegurl; charset=utf-8','X-Original-Size':String(obj.size),'X-Original-Etag':obj.etag}});
      }
      if (url.pathname==='/state' && method==='GET') {
        const [orig,preview,state,checked]=await Promise.all([env.PLAYLIST.head(PLAYLIST_KEY),env.PLAYLIST.head(PREVIEW_KEY),env.PLAYLIST.get(STATE_KEY),env.PLAYLIST.get('__azure_tv_preview/checked.json')]);
        return json({original:{size:orig?.size||0,etag:orig?.etag||null},preview:{size:preview?.size||0},metadata:state?await state.json():null,checked:checked?await checked.json():null});
      }
      if (url.pathname==='/checked' && method==='POST') {
        const body=await request.json();
        if (!body || typeof body.sourceCommit!=='string' || !body.sources || typeof body.sources!=='object') return fail('Invalid source state',400);
        if (!Object.entries(body.sources).every(([k,v])=>/^CanaisBR\\d{2}\\.m3u8$/i.test(k)&&/^[a-f0-9]{40}$/.test(v))) return fail('Invalid source files',400);
        await env.PLAYLIST.put('__azure_tv_preview/checked.json',JSON.stringify({
          sourceCommit:body.sourceCommit,sources:body.sources,verifiedAt:new Date().toISOString(),
          urlUpdatesFound:Number(body.urlUpdatesFound)||0
        }),{httpMetadata:{contentType:'application/json'}});
        return json({ok:true});
      }
      if (url.pathname==='/start' && method==='POST') {
        const original=await env.PLAYLIST.head(PLAYLIST_KEY);
        if (!original) return fail('No original available',404);
        const upload=await env.PLAYLIST.createMultipartUpload(PREVIEW_KEY,{httpMetadata:{contentType:'application/vnd.apple.mpegurl'}});
        return json({uploadId:upload.uploadId,originalBytes:original.size,originalEtag:original.etag});
      }
      if (url.pathname==='/part' && method==='POST') {
        const id=url.searchParams.get('uploadId')||'', num=Number(url.searchParams.get('number'));
        if (id.length<5||id.length>2048||!Number.isInteger(num)||num<1||num>60) return fail('Invalid part',400);
        const declared=Number(request.headers.get('content-length')||0);
        if (declared>9*1024*1024) return fail('Too large',413);
        const body=await request.arrayBuffer();
        if (body.byteLength===0||body.byteLength>9*1024*1024) return fail('Invalid part size',413);
        const obj=env.PLAYLIST.resumeMultipartUpload(PREVIEW_KEY,id);
        const r=await obj.uploadPart(num,body);
        return json({partNumber:r.partNumber,etag:r.etag});
      }
      if (url.pathname==='/finish' && method==='POST') {
        const body=await request.json();
        const {uploadId,parts,meta}=body;
        if (typeof uploadId!=='string'||!Array.isArray(parts)||parts.length<2||parts.length>60) return fail('Invalid parts',400);
        if (!parts.every((x,i)=>x.partNumber===i+1&&typeof x.etag==='string')) return fail('Unordered parts',400);
        if (!meta||!Number.isInteger(meta.entries)||meta.entries<10000||!Number.isInteger(meta.urlsChanged)||meta.urlsChanged<0||typeof meta.structureHash!=='string'||typeof meta.sourceCommit!=='string') return fail('Metadata invalid',400);
        const current=await env.PLAYLIST.head(PLAYLIST_KEY);
        if (!current||current.etag!==meta.originalEtag||current.size!==meta.originalBytes) return fail('Original changed during operation',409);
        const upload=env.PLAYLIST.resumeMultipartUpload(PREVIEW_KEY,uploadId);
        const preview=await upload.complete(parts);
        if (preview.size<current.size*0.8||preview.size>current.size*1.2) return fail('Preview size outside safety range',500);
        const result={...meta,previewBytes:preview.size,createdAt:new Date().toISOString()};
        await env.PLAYLIST.put(STATE_KEY,JSON.stringify(result),{httpMetadata:{contentType:'application/json'}});
        return json({ok:true,previewBytes:preview.size,urlsChanged:meta.urlsChanged});
      }
      if (url.pathname==='/abort' && method==='POST') {
        const body=await request.json();
        if (typeof body.uploadId!=='string') return fail('Invalid upload ID',400);
        await env.PLAYLIST.resumeMultipartUpload(PREVIEW_KEY,body.uploadId).abort();
        return json({ok:true});
      }
      return fail('Not found',404);
    } catch(e) {
      return fail(String(e?.message||e).slice(0,250),500);
    }
  }
};

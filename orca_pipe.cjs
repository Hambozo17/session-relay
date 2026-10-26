'use strict';
// A single authenticated local Orca RPC request. Tokens stay inside this process.
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const crypto = require('node:crypto');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { input += chunk; });
process.stdin.on('end', () => {
  let request, metadata;
  try {
    request = JSON.parse(input);
    metadata = JSON.parse(fs.readFileSync(path.join(process.env.APPDATA, 'orca', 'orca-runtime.json'), 'utf8'));
    if (!request.method || !metadata.authToken) throw Error('Runtime unavailable');
  } catch (_) { process.stdout.write(JSON.stringify({ok:false,error:{code:'unavailable',message:'Orca runtime is unavailable.'}})); return; }
  const transport = metadata.transports.find(t => t.kind === 'named-pipe');
  if (!transport || !transport.endpoint.startsWith('\\\\.\\pipe\\orca-')) {
    process.stdout.write(JSON.stringify({ok:false,error:{code:'unavailable',message:'Orca local pipe is unavailable.'}})); return;
  }
  // Some Windows metadata spells the prefix with doubled backslashes.
  const endpoint = transport.endpoint.replace(/^\\{4}\.\\{2}pipe\\{2}/, '\\\\.\\pipe\\');
  const id = crypto.randomUUID();
  const socket = net.connect(endpoint);
  let buffer = '', settled = false;
  const finish = result => { if(settled)return; settled=true; clearTimeout(timer); socket.destroy(); process.stdout.write(JSON.stringify(result)); };
  const timer = setTimeout(() => finish({ok:false,error:{code:'uncertain',message:'Orca did not confirm this request.'}}), request.timeout_ms || 20000);
  socket.setEncoding('utf8');
  socket.on('connect', () => socket.write(JSON.stringify({id,authToken:metadata.authToken,method:request.method,params:request.params ?? null,
    ...(request.envelope || {})})+'\n'));
  socket.on('error', () => finish({ok:false,error:{code:'unavailable',message:'Could not reach the Orca local runtime.'}}));
  socket.on('data', chunk => {
    buffer += chunk;
    if(buffer.length > 32*1024*1024) return finish({ok:false,error:{code:'too_large',message:'Orca response exceeded the supported size.'}});
    while(buffer.includes('\n')) {
      const pos=buffer.indexOf('\n'), line=buffer.slice(0,pos); buffer=buffer.slice(pos+1);
      let result; try{result=JSON.parse(line);}catch(_){return finish({ok:false,error:{code:'invalid',message:'Invalid Orca response.'}});}
      if(result._keepalive)continue;
      if(result.id!==id)continue;
      if(result._meta?.runtimeId && result._meta.runtimeId!==metadata.runtimeId)return finish({ok:false,error:{code:'uncertain',message:'Orca restarted during this request.'}});
      finish(result);
    }
  });
});

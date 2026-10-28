'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {Bridge, summarize, applyPatches} = require('./desktop_bridge.cjs');
const id = '123e4567-e89b-42d3-a456-426614174000';
const operation = '987e6543-e21b-42d3-a456-426614174000';

test('opted-in stopped continuation works without Goal and never grants approval', async () => {
  for (const goal of [null,'blocked','completed']) {
    const bridge=new Bridge(()=>{});let sends=0;
    bridge.fresh=async()=>({owner:'owner',summary:{turn_id:'old',status:'idle',runtime_status:'idle',goal_status:goal,settings_fingerprint:'same'}});
    bridge.request=async(_,params)=>{sends++;assert.equal(params.turnStart.context.inheritThreadSettings,true);assert.equal(params.turnStart.context.usePermissionSelection,false)};
    bridge.waitFor=async(_,predicate)=>{
      const row={turn_id:'new',status:'running',goal_status:goal,settings_fingerprint:'same',client_user_message_id:operation};assert.equal(predicate(row),true);return row;
    };
    const request={threadId:id,clientUserMessageId:operation,input:[{type:'text',text:'continue'}]};
    await bridge.resume({op:'continue_stopped',thread:id,operation_id:operation,expected_turn:'old',settings_fingerprint:'same',request});
    await assert.rejects(bridge.resume({op:'continue_stopped',thread:id,operation_id:operation,expected_turn:'old',settings_fingerprint:'same',request}),{kind:'uncertain'});
    assert.equal(sends,1);
  }
});

test('stopped continuation preserves running turns, manual stops, approvals and quota routing', async () => {
  for (const change of [{status:'running',runtime_status:'active'},{status:'interrupted'},{waiting:true},
      {goal_status:'paused'},{status:'failed',error_kind:'usageLimitExceeded'}]) {
    const bridge=new Bridge(()=>{});let sends=0;
    bridge.fresh=async()=>({owner:'owner',summary:{turn_id:'old',status:'idle',runtime_status:'idle',goal_status:null,settings_fingerprint:'same',...change}});
    bridge.request=async()=>{sends++};
    await assert.rejects(bridge.resume({op:'continue_stopped',thread:id,operation_id:operation,expected_turn:'old',settings_fingerprint:'same',request:{threadId:id,clientUserMessageId:operation,input:[{type:'text',text:'continue'}]}}),{kind:'state_changed'});
    assert.equal(sends,0);
  }
});

test('owner discovery retries transient reads and preserves unresolved ownership', async () => {
  for (const succeeds of [true,false]) {
    const bridge=new Bridge(()=>{});bridge.compatible=true;
    const attempts=[];
    bridge.ownerFor=async (_,timeout)=>{
      attempts.push(timeout);
      if (attempts.length===1 || !succeeds) { const e=new Error();e.kind='timeout';throw e; }
      return null;
    };
    const result=await bridge.scan([id],0);
    assert.equal(attempts.length,2);assert.ok(attempts[1]>=5000);
    assert.equal(result.unresolved.length,succeeds?0:1);
    assert.equal(result.diagnostics.owner_null,succeeds?1:0);
    assert.equal(bridge.attempts.size,0);
  }
});

test('a delayed owner snapshot is refreshed without a mutation', async () => {
  const bridge=new Bridge(()=>{});bridge.compatible=true;
  bridge.ownerFor=async()=>'owner';let follows=0;
  bridge.follow=()=>{
    if (++follows===2) bridge.states.set(id,{owner:'owner',state:{id,turns:[],threadRuntimeStatus:'idle'}});
  };
  const result=await bridge.scan([id],0);
  assert.equal(follows,2);assert.equal(result.unresolved.length,0);
  assert.equal(result.sessions.length,1);assert.equal(bridge.attempts.size,0);
});

test('full-access descendant root removal preserves coverage and raw settings', () => {
  const state={id,turns:[],latestModel:'chosen',latestReasoningEffort:'xhigh',
    permissions:{sandboxPolicy:{type:'dangerFullAccess'},runtimeWorkspaceRoots:['C:/work','C:/work/project']}};
  const before=summarize(state,'owner');
  assert.equal(before.settings.permissions.runtimeWorkspaceRoots.length,2);
  state.permissions.runtimeWorkspaceRoots=['C:/work'];
  assert.equal(summarize(state,'owner').settings_fingerprint,before.settings_fingerprint);
  for(const roots of [['C:/work/project'],['C:/work2'],['C:/work','C:/work2']]) {
    state.permissions.runtimeWorkspaceRoots=roots;
    assert.notEqual(summarize(state,'owner').settings_fingerprint,before.settings_fingerprint);
  }
  state.permissions.sandboxPolicy={type:'workspaceWrite'};
  state.permissions.runtimeWorkspaceRoots=['C:/work','C:/work/project'];
  const restricted=summarize(state,'owner').settings_fingerprint;
  state.permissions.runtimeWorkspaceRoots=['C:/work'];
  assert.notEqual(summarize(state,'owner').settings_fingerprint,restricted);
});

test('implicit effort matches the same-model collaboration effort without changing the raw snapshot', () => {
  const state={id,turns:[],latestModel:'selected',latestReasoningEffort:null,
    latestCollaborationMode:{mode:'default',settings:{model:'selected',reasoning_effort:'xhigh'}}};
  const before=summarize(state,'owner');
  state.latestReasoningEffort='xhigh';
  assert.equal(summarize(state,'owner').settings_fingerprint,before.settings_fingerprint);
  assert.equal(before.settings.effort,undefined);
  state.latestReasoningEffort='high';
  assert.notEqual(summarize(state,'owner').settings_fingerprint,before.settings_fingerprint);
  state.latestReasoningEffort=null;state.latestCollaborationMode.settings.model='different';
  const changed=summarize(state,'owner');state.latestReasoningEffort='xhigh';
  assert.notEqual(summarize(state,'owner').settings_fingerprint,changed.settings_fingerprint);
});

test('exact built-in Default instructions remain equivalent across automatic Goal restart', () => {
  const instructions=require('./desktop-settings-policy.json').default_instructions[0];
  const state={id,turns:[],latestModel:'selected',latestReasoningEffort:'xhigh',
    latestCollaborationMode:{mode:'default',settings:{model:'selected',reasoning_effort:'xhigh',developer_instructions:null}}};
  const baseline=summarize(state,'owner').settings_fingerprint;
  state.latestCollaborationMode.settings.developer_instructions=instructions.replace(/\n/g,'\r\n');
  assert.equal(summarize(state,'owner').settings_fingerprint,baseline);
  assert.ok(summarize(state,'owner').settings.collaborationMode.settings.developer_instructions);
  state.latestCollaborationMode.settings.developer_instructions+='\nCustom instruction';
  assert.notEqual(summarize(state,'owner').settings_fingerprint,baseline);
  state.latestCollaborationMode.settings.developer_instructions=instructions;
  state.latestCollaborationMode.mode='plan';
  assert.notEqual(summarize(state,'owner').settings_fingerprint,baseline);
});

test('owner comes from coordinator envelope rather than handler result', async () => {
  const bridge = new Bridge(() => {});
  bridge.request = async () => ({handledByClientId:'actual-owner', result:{supportsUntrustedAppInput:true}});
  assert.equal(await bridge.ownerFor(id), 'actual-owner');
  bridge.request = async () => { const e = new Error(); e.kind='no_owner'; throw e; };
  assert.equal(await bridge.ownerFor(id), null);
});

test('UI usageLimited Goal status matches database without changing chosen model', () => {
  const result = summarize({id, threadRuntimeStatus:'idle', threadGoal:{status:'usageLimited'},
    latestModel:'selected-model', latestModelReasoningEffort:'xhigh', turns:[]}, 'owner');
  assert.equal(result.goal_status, 'usage_limited');
  assert.equal(result.model, 'selected-model');
  assert.equal(result.effort, 'xhigh');
});

test('interrupt accepts installed nested acknowledgement and binds exact turn', async () => {
  const bridge = new Bridge(() => {});
  const summary = {turn_id:'old-turn', status:'running', runtime_status:'active', waiting:false, settings_fingerprint:'fingerprint'};
  bridge.fresh = async () => ({owner:'owner', summary});
  bridge.request = async (method, params) => {
    assert.equal(method, 'thread-follower-interrupt-turn');
    assert.equal(params.expectedTurnId, 'old-turn');
    return {result:{method, result:{interruptedTurnId:'old-turn', ok:true}}};
  };
  bridge.waitFor = async (_, predicate) => {
    const stopped = {...summary, status:'interrupted', runtime_status:'idle'};
    assert.equal(predicate(stopped), true); return stopped;
  };
  const result = await bridge.interrupt({thread:id, operation_id:operation, expected_turn:'old-turn', settings_fingerprint:'fingerprint'});
  assert.equal(result.session.status, 'interrupted');
});

test('a rejected native pause is reconciled from its exact postcondition without repeating it', async () => {
  const bridge=new Bridge(()=>{});
  const before={turn_id:'turn',status:'running',runtime_status:'active',waiting:false,goal_status:'active',settings_fingerprint:'same'};
  bridge.fresh=async()=>({owner:'owner',summary:before});
  let sends=0;bridge.request=async()=>{sends++;const cause=new Error('native handler failed after interruption');cause.kind='rejected';throw cause};
  bridge.waitFor=async(_,predicate)=>{
    const stopped={...before,status:'interrupted',runtime_status:'idle'};
    assert.equal(predicate(stopped),true);
    for(const changes of [{turn_id:'other'},{settings_fingerprint:'changed'},{goal_status:'paused'},{waiting:true},{status:'running'}]) assert.equal(predicate({...stopped,...changes}),false);
    return stopped;
  };
  const command={thread:id,operation_id:operation,expected_turn:'turn',settings_fingerprint:'same'};
  const result=await bridge.interrupt(command);
  assert.equal(result.confirmation,'verified-postcondition-after-request-failure');assert.equal(sends,1);
  await assert.rejects(bridge.interrupt(command),{kind:'uncertain'});assert.equal(sends,1);
});

test('pre-send manual interruption and unconfirmed rejected pause never create a receipt', async () => {
  const bridge=new Bridge(()=>{});let sends=0;
  const before={turn_id:'turn',status:'interrupted',runtime_status:'idle',goal_status:null,settings_fingerprint:'same'};
  bridge.fresh=async()=>({owner:'owner',summary:before});
  bridge.request=async()=>{sends++;const cause=new Error();cause.kind='rejected';throw cause};
  const command={thread:id,operation_id:operation,expected_turn:'turn',settings_fingerprint:'same'};
  await assert.rejects(bridge.interrupt(command),{kind:'state_changed'});assert.equal(sends,0);
  before.status='running';before.runtime_status='active';
  bridge.waitFor=async()=>{const cause=new Error();cause.kind='uncertain';throw cause};
  await assert.rejects(bridge.interrupt(command),{kind:'uncertain'});assert.equal(sends,1);
  await assert.rejects(bridge.interrupt(command),{kind:'uncertain'});assert.equal(sends,1);
});

test('a fast completed continuation is confirmed once, unrelated turns are rejected', async () => {
  const bridge = new Bridge(() => {});
  bridge.fresh = async () => ({owner:'owner', summary:{turn_id:'old-turn',status:'interrupted',runtime_status:'idle',goal_status:null,settings_fingerprint:'fingerprint'}});
  let sends=0;
  bridge.request = async (method, params) => {
    sends++; assert.equal(method,'thread-follower-start-turn');
    assert.equal(params.turnStart.context.inheritThreadSettings,true);
  };
  bridge.waitFor = async (_, predicate) => {
    const done={turn_id:'new-turn',client_user_message_id:operation,status:'idle',settings_fingerprint:'fingerprint',goal_status:null};
    assert.equal(predicate({...done,client_user_message_id:'unrelated'}),false);
    assert.equal(predicate(done),true); return done;
  };
  const command={thread:id,operation_id:operation,expected_turn:'old-turn',settings_fingerprint:'fingerprint',request:{threadId:id,clientUserMessageId:operation,input:[{type:'text',text:'continue'}]}};
  await bridge.resume(command);
  await assert.rejects(bridge.resume(command), {kind:'uncertain'});
  assert.equal(sends,1);
});

test('state patches reject prototype pollution', () => {
  assert.throws(() => applyPatches({}, [{op:'add',path:['__proto__','polluted'],value:true}]));
  assert.equal({}.polluted,undefined);
});

test('installed canonical history exposes codexErrorInfo quota failure without a Goal', () => {
  const turn={turnId:'failed-turn',turnStartedAtMs:1234,status:'failed',
    error:{message:'private error body',codexErrorInfo:'usageLimitExceeded',additionalDetails:null}};
  const state={id,threadRuntimeStatus:{type:'idle'},turnHistory:{kind:'canonical',history:{
    entitiesByKey:{entry:turn},islands:[{entries:[{value:'entry'}]}]}}};
  const row=summarize(state,'owner');
  assert.equal(row.status,'failed');assert.equal(row.error_kind,'usageLimitExceeded');
  assert.equal(row.turn_id,'failed-turn');assert.equal(row.turn_started_at_ms,1234);
  assert.equal(JSON.stringify(row).includes('private error body'),false);
  turn.error.codexErrorInfo={type:'unauthorized',additionalDetails:'private'};
  assert.equal(summarize(state,'owner').error_kind,'unauthorized');
});

test('a quota failure without Goal can continue with original settings', async () => {
  const bridge=new Bridge(()=>{});
  bridge.fresh=async()=>({owner:'owner',summary:{turn_id:'failed-turn',status:'failed',
    runtime_status:'idle',goal_status:null,error_kind:'usageLimitExceeded',settings_fingerprint:'fingerprint'}});
  let sends=0;bridge.request=async()=>{sends++};
  bridge.waitFor=async(_,predicate)=>{
    const row={turn_id:'new-turn',status:'running',goal_status:null,client_user_message_id:operation,settings_fingerprint:'fingerprint'};
    assert.equal(predicate(row),true);return row;
  };
  await bridge.resume({thread:id,operation_id:operation,expected_turn:'failed-turn',settings_fingerprint:'fingerprint',
    request:{threadId:id,clientUserMessageId:operation,input:[{type:'text',text:'continue'}]}});
  assert.equal(sends,1);
});

test('only the same thread generated visualization root is neutral under full access', () => {
  const path=require('node:path'),os=require('node:os');
  const root=path.join(os.homedir(),'.codex','visualizations','2026','10','09',id);
  const state={id,latestModel:'selected-model',latestModelReasoningEffort:'xhigh',turns:[],
    permissions:{sandboxPolicy:{type:'dangerFullAccess'},runtimeWorkspaceRoots:['C:/project']}};
  const original=summarize(state,'owner').settings_fingerprint;
  state.permissions.runtimeWorkspaceRoots.push(root);
  assert.equal(summarize(state,'owner').settings_fingerprint,original);
  state.permissions={runtimeWorkspaceRoots:state.permissions.runtimeWorkspaceRoots,sandboxPolicy:state.permissions.sandboxPolicy};
  assert.equal(summarize(state,'owner').settings_fingerprint,original);
  assert.equal(summarize(state,'owner').settings.permissions.runtimeWorkspaceRoots.length,2);
  state.permissions.runtimeWorkspaceRoots.push('C:/other-project');
  assert.notEqual(summarize(state,'owner').settings_fingerprint,original);
  state.permissions.runtimeWorkspaceRoots=['C:/project'];
  state.permissions.sandboxPolicy={type:'workspaceWrite'};
  const restricted=summarize(state,'owner').settings_fingerprint;
  state.permissions.runtimeWorkspaceRoots.push(root);
  assert.notEqual(summarize(state,'owner').settings_fingerprint,restricted);
  state.permissions.sandboxPolicy={type:'dangerFullAccess'};
  state.permissions.runtimeWorkspaceRoots=['C:/project',root.replace(id,operation)];
  assert.notEqual(summarize(state,'owner').settings_fingerprint,original);
});

"""Exercise the complete Orca handoff against deterministic native RPC responses."""
import contextlib
import itertools
import pathlib
import tempfile
import unittest
from unittest.mock import patch
import orca_adapter as module


class NativeFixture:
    def __init__(self, root, *, error='quota', outcome='running', goal=False, auto_goal=False,
            custom_shell=False, direct_pane=False, post_policy_mismatch=False,
            missing_fresh_context=False,post_context_changes=None):
        self.root=root;self.stage='original';self.outcome=outcome;self.auto_goal=auto_goal
        self.custom_shell=custom_shell
        self.direct_pane=direct_pane
        self.post_policy_mismatch=post_policy_mismatch
        self.missing_fresh_context=missing_fresh_context
        self.post_context_changes=post_context_changes or {}
        self.calls=[];self.pending='';self.submissions=0
        self.source=root/'old/sessions/2026/test.jsonl';self.source.parent.mkdir(parents=True)
        self.source.write_text('{"type":"session_meta","payload":{"id":"chat"}}\n')
        self.target={'key':'new','orca_id':'native-new','orca_home':str(root/'new')}
        (root/'new').mkdir();(root/'new/auth.json').write_text('{}')
        self.original={'id':'chat','status':'failed','error_kind':error,'turn_id':'old-turn','event_pos':10,
            'model':'chosen-model','effort':'xhigh','model_provider':'openai','approval_policy':'never',
            'sandbox_policy':{'type':'danger-full-access'},'waiting':False,'is_child':False,'connected':True,
            'permission_profile':{'type':'disabled'},
            'active_permission_profile':{'id':':danger-full-access'},'_context_turn_id':'old-turn',
            'cwd':str(root),'home':str(root/'old'),'path':str(self.source),'account_key':'old',
            'pty_id':'pty','terminal':'pane','incarnation':'inc','goal_status':'usage_limited' if goal else None,
            'worktree':'work','tab_id':'tab','leaf_id':'leaf'}
        self.goals={}
        if goal:
            g={'goal_id':'existing-goal','status':'usage_limited','token_budget':1000,'tokens_used':75,
                'time_used_seconds':20,'created_at_ms':1,'updated_at_ms':2}
            self.goals[str(root/'old')]=g
            self.original['_goal']=dict(g)
        else:self.original['_goal']=None

    def get(self, sid):
        row=dict(self.original)
        if self.stage in ('launched','status','submitted'):
            row.update(account_key='new',home=self.target['orca_home'],path=str(self.root/'new/sessions/2026/test.jsonl'))
            if self.auto_goal:row.update(status='running',turn_id='automatic-turn',goal_status='active')
        return row

    def transcript(self, path):
        row=dict(self.original)
        if self.stage=='submitted' and self.outcome!='missing':
            row.update(turn_id='continued-turn',status=self.outcome,event_pos=20)
        elif self.auto_goal and self.stage in ('launched','status'):
            row.update(turn_id='automatic-turn',status='running',event_pos=20)
        new_turn=row.get('turn_id')!=self.original.get('turn_id')
        if new_turn:
            row['_context_turn_id']=('old-turn' if self.missing_fresh_context else row['turn_id'])
            if self.post_policy_mismatch:
                row.update(sandbox_policy={'type':'read-only'},permission_profile=None,
                    active_permission_profile=None)
            row.update(self.post_context_changes)
        return row

    def read_goal(self, home, sid):
        row=self.goals.get(str(home));return dict(row) if row else None

    def transfer_goal(self, old, new, sid):
        if str(old) in self.goals:self.goals[str(new)]=dict(self.goals[str(old)])

    @contextlib.contextmanager
    def database(self, path):
        fixture=self
        class Connection:
            def execute(self, sql, params):
                g=fixture.goals[str(pathlib.Path(path).parent)]
                if "status='paused'" in sql and 'updated_at_ms=?' in sql:
                    g.update(status='paused',updated_at_ms=params[0])
                else:g['status']='active'
                return type('Result',(),{'rowcount':1})()
        yield Connection()

    def call(self, method, params=None, **kwargs):
        self.calls.append((method,params))
        if method=='accounts.selectCodexForTarget':return {}
        if method=='accounts.list':return {'codex':{'activeAccountId':'native-new'}}
        if method=='terminal.agentStatus':return {'agentStatus':{'state':'idle'}}
        if method=='terminal.isRunningAgent':return {'isRunningAgent':self.stage!='quit'}
        if method=='terminal.inspectProcess':return {'process':{'foregroundProcess':None if self.direct_pane else 'pwsh.exe','hasChildProcesses':False}}
        if method=='terminal.wait':return {'wait':{'handle':params['terminal'],'condition':'exit','satisfied':self.direct_pane,'status':'exited' if self.direct_pane else 'running'}}
        if method=='terminal.create':
            self.stage='launched'
            return {'terminal':{'handle':'replacement','incarnationId':'replacement-inc','ptyId':'replacement-pty','worktreeId':'work','tabId':'tab','paneKey':'tab:leaf'}}
        if method=='terminal.read':
            if self.stage=='quit':tail=['Token usage: total=123','To continue this session, run codex resume','chat']+([] if self.direct_pane else ['repo ❯' if self.custom_shell else 'PS C:\\test>'])
            elif self.stage=='status':tail=['chat','new-label','Ask Codex']
            elif self.auto_goal and self.stage=='launched':tail=['Working']
            else:tail=['Ask Codex']
            return {'terminal':{'source':'screen','tail':tail}}
        if method=='terminal.send':
            if params.get('agentPrompt'):
                self.submissions+=1
                if self.outcome=='rejected':return {'send':{'accepted':False}}
                self.stage='submitted'
            elif 'text' in params:self.pending=params['text']
            elif params.get('enter'):
                if self.pending=='/quit':self.stage='quit'
                elif self.pending=='/status':self.stage='status'
                elif '$env:CODEX_HOME=' in self.pending:self.stage='launched'
            return {'send':{'accepted':True}}
        raise AssertionError(method)


class OrcaRecovery(unittest.TestCase):
    def run_case(self, *, error='quota', outcome='running', goal=False, auto_goal=False,
            custom_shell=False, direct_pane=False, native_profile=True,
            post_policy_mismatch=False,missing_fresh_context=False,workspace_policy=None,
            post_context_changes=None):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder);f=NativeFixture(root,error=error,outcome=outcome,goal=goal,
                auto_goal=auto_goal,custom_shell=custom_shell,direct_pane=direct_pane,
                post_policy_mismatch=post_policy_mismatch,missing_fresh_context=missing_fresh_context,
                post_context_changes=post_context_changes)
            if workspace_policy is not None:
                f.original.update(sandbox_policy={'type':'workspace-write',**workspace_policy},
                    permission_profile=None,active_permission_profile=None)
                (root/'new/config.toml').write_text(
                    'sandbox_mode = "workspace-write"\n[sandbox_workspace_write]\n'
                    'network_access = true\nwritable_roots = ["C:/destination-extra"]\n'
                    'exclude_tmpdir_env_var = false\nexclude_slash_tmp = false\n',encoding='utf-8')
            if not native_profile:f.target={'key':'old'}
            clock=itertools.count()
            with contextlib.ExitStack() as stack:
                for name,value in [('LOCALDATA',root),('identity',lambda *_:{'key':'new' if native_profile else 'old'}),
                    ('protected_json',lambda *_:{}),('inherit_project_trust',lambda *_:None),
                    ('read_transcript',f.transcript),('read_goal',f.read_goal),('transfer_goal',f.transfer_goal),
                    ('database_connection',f.database),('codex_binary',lambda:'C:/codex.exe')]:
                    stack.enter_context(patch.object(module,name,value))
                stack.enter_context(patch.object(module.time,'sleep',lambda *_:None))
                stack.enter_context(patch.object(module.time,'monotonic',lambda:next(clock)))
                a=module.OrcaAdapter(object(),f);a.get=f.get;phases=[];events=[]
                def journal(phase,data):
                    phases.append(phase);events.append((phase,data))
                target=dict(f.target,label='new-label')
                expect_failure=(outcome in ('missing','failed','rejected') or post_policy_mismatch
                    or missing_fresh_context or post_context_changes)
                if expect_failure:
                    with self.assertRaises(RuntimeError):a.rotate(['chat'],target,journal)
                    self.assertNotIn('session-recovered',phases)
                else:
                    self.assertEqual(a.rotate(['chat'],target,journal),['chat'])
                    self.assertIn('session-recovered',phases)
                if direct_pane and outcome not in ('missing','failed','rejected'):
                    created=next(data for phase,data in events if phase=='resume-process-created')
                    self.assertEqual(created.get('pty_id'),'replacement-pty')
                self.assertEqual(f.submissions,0 if auto_goal else 1)
                if not native_profile:self.assertFalse(any(m.startswith('accounts.') for m,_ in f.calls))
                commands=[p['text'] for m,p in f.calls if m=='terminal.send' and '$env:CODEX_HOME=' in p.get('text','')]
                commands += [p['command'] for m,p in f.calls if m=='terminal.create']
                self.assertEqual(len(commands),1)
                self.assertIn("'chosen-model'",commands[0]);self.assertIn('xhigh',commands[0]);self.assertIn("'--no-daemon'",commands[0])
                if workspace_policy is not None:
                    for field,value in workspace_policy.items():
                        cli_value='[]' if field=='writable_roots' else str(value).lower()
                        self.assertIn("'sandbox_workspace_write."+field+'='+cli_value+"'",commands[0])
                prompts=[p for m,p in f.calls if m=='terminal.send' and p.get('agentPrompt')]
                if prompts:
                    self.assertEqual(prompts[0]['text'],'/goal resume' if goal else 'continue')
                    self.assertTrue(prompts[0]['enter']);self.assertTrue(prompts[0]['requireWriteSettlement'])
                if goal:
                    g=f.goals[str(root/'new')]
                    self.assertEqual((g['goal_id'],g['token_budget'],g['tokens_used'],g['time_used_seconds']),('existing-goal',1000,75,20))

    def test_quota_error_switches_and_starts_new_turn(self):self.run_case()
    def test_destination_expanded_workspace_config_is_overridden_by_original_policy(self):
        self.run_case(workspace_policy={'network_access':False,'writable_roots':[],
            'exclude_tmpdir_env_var':True,'exclude_slash_tmp':True})
    def test_login_error_switches_and_starts_new_turn(self):self.run_case(error='login')
    def test_fast_completed_turn_is_confirmed(self):self.run_case(outcome='idle')
    def test_accepted_but_missing_turn_is_not_success(self):self.run_case(outcome='missing')
    def test_failed_new_turn_is_not_success(self):self.run_case(outcome='failed')
    def test_rejected_prompt_is_not_success(self):self.run_case(outcome='rejected')
    def test_existing_goal_is_resumed_with_budget_preserved(self):self.run_case(goal=True)
    def test_goal_automatic_resume_does_not_send_duplicate(self):self.run_case(goal=True,auto_goal=True)
    def test_fast_completed_turn_with_changed_permissions_never_records_recovery(self):
        self.run_case(outcome='idle',post_policy_mismatch=True)
    def test_fresh_turn_must_preserve_model_effort_and_approval(self):
        for changes in ({'model':'other-model'},{'effort':'low'},
                {'approval_policy':'on-request'}):
            with self.subTest(changes=changes):
                self.run_case(outcome='idle',post_context_changes=changes)
    def test_automatic_goal_with_changed_permissions_never_sends_or_records_success(self):
        self.run_case(goal=True,auto_goal=True,post_policy_mismatch=True)
    def test_new_turn_without_fresh_context_remains_unrecovered(self):
        self.run_case(outcome='idle',missing_fresh_context=True)
    def test_automatic_goal_without_fresh_context_never_sends_or_records_success(self):
        self.run_case(goal=True,auto_goal=True,missing_fresh_context=True)
    def test_exit_footer_with_custom_shell_prompt_launches_resume(self):self.run_case(custom_shell=True)
    def test_root_process_exit_without_shell_prompt_recreates_same_pane_and_continues(self):self.run_case(direct_pane=True)
    def test_same_account_reset_with_no_orca_profile_reuses_exact_current_home(self):self.run_case(native_profile=False)
    def test_exited_same_account_pane_with_no_orca_profile_is_recreated(self):self.run_case(native_profile=False,direct_pane=True)

    def test_unsupported_permission_policies_reject_before_account_or_terminal_mutations(self):
        invalid=[
            lambda f:f.original.update(approval_policy='on-failure'),
            lambda f:f.original.update(approval_policy='untrusted'),
            lambda f:f.original.update(sandbox_policy={'type':'workspace-write',
                'writable_roots':['C:/destination-extra']},permission_profile=None,
                active_permission_profile=None),
            lambda f:f.original.update(sandbox_policy={'type':'workspace-write',
                'network_access':'false'},permission_profile=None,active_permission_profile=None),
            lambda f:f.original.update(sandbox_policy={'type':'workspace-write',
                'custom_restriction':True},permission_profile=None,active_permission_profile=None),
            lambda f:f.original.update(active_permission_profile={'id':'custom-profile'}),
            lambda f:f.original.update(sandbox_policy={'type':'workspace-write'},
                permission_profile={'type':'managed','network':'restricted','file_system':{
                    'type':'restricted','entries':[{'path':{'type':'glob_pattern',
                        'pattern':'**/*.secret'},'access':'deny'}]}},active_permission_profile=None),
        ]
        for mutate in invalid:
            with self.subTest(policy=mutate),tempfile.TemporaryDirectory() as folder:
                root=pathlib.Path(folder);f=NativeFixture(root);mutate(f);trust=[]
                with contextlib.ExitStack() as stack:
                    for name,value in [('LOCALDATA',root),('identity',lambda *_:{'key':'new'}),
                        ('protected_json',lambda *_:{}),('inherit_project_trust',lambda *_:trust.append(True)),
                        ('read_transcript',f.transcript),('read_goal',f.read_goal),
                        ('transfer_goal',f.transfer_goal),('database_connection',f.database),
                        ('codex_binary',lambda:'C:/codex.exe')]:
                        stack.enter_context(patch.object(module,name,value))
                    stack.enter_context(patch.object(module.time,'sleep',lambda *_:None))
                    a=module.OrcaAdapter(object(),f);a.get=f.get
                    with self.assertRaises(RuntimeError):
                        a.rotate(['chat'],dict(f.target,label='new-label'),lambda *_:None)
                self.assertEqual(trust,[])
                self.assertFalse(any(method=='accounts.selectCodexForTarget' for method,_ in f.calls))
                self.assertFalse(any(method=='terminal.send' for method,_ in f.calls))

    def test_fresh_original_permission_change_rejects_before_account_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder);f=NativeFixture(root);trust=[];count=[]
            def fresh_get(sid):
                count.append(sid);row=f.get(sid)
                if len(count)==2:row['approval_policy']='on-request'
                return row
            with contextlib.ExitStack() as stack:
                for name,value in [('LOCALDATA',root),('identity',lambda *_:{'key':'new'}),
                    ('protected_json',lambda *_:{}),('inherit_project_trust',lambda *_:trust.append(True)),
                    ('read_transcript',f.transcript),('read_goal',f.read_goal),
                    ('transfer_goal',f.transfer_goal),('database_connection',f.database),
                    ('codex_binary',lambda:'C:/codex.exe')]:
                    stack.enter_context(patch.object(module,name,value))
                a=module.OrcaAdapter(object(),f);a.get=fresh_get
                with self.assertRaises(RuntimeError):
                    a.rotate(['chat'],dict(f.target,label='new-label'),lambda *_:None)
            self.assertEqual(trust,[])
            self.assertFalse(any(method=='accounts.selectCodexForTarget' for method,_ in f.calls))
            self.assertFalse(any(method=='terminal.send' for method,_ in f.calls))

    def test_live_or_unverified_pane_is_never_recreated_from_footer_alone(self):
        class Rpc:
            def call(self,method,params):
                if method=='terminal.wait':return {'wait':{'handle':'pane','condition':'exit','satisfied':False,'status':'running'}}
                raise AssertionError('No create is allowed: '+method)
        a=module.OrcaAdapter(object(),Rpc())
        with self.assertRaisesRegex(RuntimeError,'no longer confirmed exited'):
            a.replace_exited_pane({'id':'chat','worktree':'work','tab_id':'tab','leaf_id':'leaf'},
                {'terminal':'pane','incarnation':'inc'},{'key':'new'},'destination','command',lambda *_:None)

    def test_exact_wrapped_exit_footer_requires_fresh_power_shell_process(self):
        sid='123e4567-e89b-42d3-a456-426614174000'
        screen={'source':'screen','tail':['Token usage: total=123','To continue this session, run codex resume '+sid[:24],sid[24:],'repo ❯']}
        process={'foregroundProcess':'C:/Program Files/PowerShell/7/pwsh.exe','hasChildProcesses':False,
            'foregroundProcessEvidence':{'verdict':'live','ptyId':'pane','ptyIncarnationId':'inc','capturedAgeMs':0}}
        self.assertTrue(module.shell_exit_confirmed(screen,sid,process,'pane','inc'))
        for changes in ({'foregroundProcess':'codex.exe'},{'foregroundProcess':'bash.exe'},
                {'foregroundProcess':None},{'hasChildProcesses':True},{'verdict':'unverifiable'},
                {'foregroundProcessEvidence':dict(process['foregroundProcessEvidence'],ptyIncarnationId='other')},
                {'foregroundProcessEvidence':dict(process['foregroundProcessEvidence'],capturedAgeMs=3000)}):
            self.assertFalse(module.shell_exit_confirmed(screen,sid,dict(process,**changes),'pane','inc'))
        self.assertFalse(module.shell_exit_confirmed(dict(screen,source='stream'),sid,process,'pane','inc'))
        self.assertFalse(module.shell_exit_confirmed(screen,'other-session',process,'pane','inc'))
        self.assertFalse(module.shell_exit_confirmed(dict(screen,draft='user input'),sid,process,'pane','inc'))

    def test_unverifiable_text_write_never_sends_enter_or_replays_text(self):
        class Rpc:
            def __init__(self):self.calls=[]
            def call(self,method,params):
                self.calls.append(params)
                return {'send':{'accepted':True,'writeSettlement':{'outcome':'unverifiable','bytesHandedToTransport':True}}}
        rpc=Rpc();adapter=module.OrcaAdapter(object(),rpc)
        with self.assertRaises(RuntimeError):adapter.submit_local('pane','inc','$env:CODEX_HOME=...')
        self.assertEqual(len(rpc.calls),1)
        self.assertNotIn('enter',rpc.calls[0])

    def test_unavailable_process_observation_preserves_exact_live_shell_prompt_proof(self):
        screen={'source':'screen','tail':['codex resume chat','PS C:\\test>']}
        process={'foregroundProcess':'codex','hasChildProcesses':True,
            'foregroundProcessEvidence':{'verdict':'unverifiable','reason':'process_table_unreadable'}}
        self.assertTrue(module.shell_exit_confirmed(screen,'chat',process,'pane','inc'))
        self.assertFalse(module.shell_exit_confirmed(dict(screen,tail=['codex resume chat','Ask Codex']), 'chat',process,'pane','inc'))

    def test_explicit_idle_repair_requires_fresh_depleted_quota(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder);f=NativeFixture(root);f.original.update(status='idle',error_kind=None)
            class Accounts:
                def find(self,key):return {'key':key}
                def probe(self,*args,**kwargs):return {'valid':True,'five_remaining':80,'weekly_remaining':80}
            with patch.object(module,'LOCALDATA',root):
                a=module.OrcaAdapter(Accounts(),f);a.get=f.get
                with self.assertRaisesRegex(RuntimeError,'freshly verified depleted'):
                    a.rotate(['chat'],f.target,lambda *_:None,repair_depleted_idle=True)
                self.assertFalse(f.calls)

    def test_attested_idle_pane_without_provider_hook_remains_monitored(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder);home=root/'home';path=home/'sessions/chat.jsonl';path.parent.mkdir(parents=True);path.write_text('{}');(home/'auth.json').write_text('{}')
            class Rpc:
                incarnation='inc'
                def call(self,method,params=None):
                    if method=='session.tabs.listAll':return {'snapshots':[]}
                    if method=='terminal.list':return {'terminals':[{'handle':'pane','incarnationId':self.incarnation,'connected':True,'title':'Resume','ptyId':'pty','worktreeId':'work'}]}
                    if method=='terminal.read':return {'terminal':{'source':'screen','tail':['Ask Codex to do anything']}}
                    if method=='terminal.agentStatus':return {'agentStatus':{'isRunningAgent':True,'status':'idle'}}
                    raise AssertionError(method)
            rpc=Rpc();info={'id':'chat','path':str(path),'status':'idle','turn_id':'before','model':'chosen','effort':'xhigh'}
            with patch.object(module,'LOCALDATA',root),patch.object(module,'read_transcript',return_value=info),patch.object(module,'read_goal',return_value=None),patch.object(module,'protected_json',return_value={}),patch.object(module,'identity',return_value={'key':'verified'}):
                a=module.OrcaAdapter(object(),rpc);a.bindings={'chat':{'terminal':'pane','incarnation':'inc','home':str(home),'path':str(path),'key':'verified','status_verified':True}}
                self.assertEqual(a.discover()[0]['account_key'],'verified')
                rpc.incarnation='different';self.assertEqual(a.discover(),[])
                rpc.incarnation='inc';a.bindings['chat']['status_verified']=False;self.assertEqual(a.discover(),[])

    def test_bound_transcript_refreshes_permission_profile_and_context_turn_id(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder);source_home=root/'source';target_home=root/'target'
            path=source_home/'sessions/chat.jsonl';path.parent.mkdir(parents=True)
            path.write_text('{}',encoding='utf-8')
            (source_home/'auth.json').write_text('{}',encoding='utf-8')
            (target_home/'auth.json').parent.mkdir(parents=True)
            (target_home/'auth.json').write_text('{}',encoding='utf-8')
            stale={'id':'chat','path':str(path),'status':'running','turn_id':'fresh-turn',
                'model':'chosen','effort':'xhigh','approval_policy':'never',
                'sandbox_policy':{'type':'danger-full-access'},
                'permission_profile':{'type':'disabled'},
                'active_permission_profile':{'id':':danger-full-access'},
                '_context_turn_id':'old-turn'}
            fresh=dict(stale,sandbox_policy={'type':'workspace-write'},
                permission_profile={'type':'managed','current':'profile'},
                active_permission_profile={'id':':workspace'},
                _context_turn_id='fresh-turn')
            class Rpc:
                def call(self,method,params=None):
                    if method=='session.tabs.listAll':return {'snapshots':[{'worktree':'work','tabs':[{
                        'id':'tab','ptyId':'pty','title':'Chat','agentStatus':{
                            'agentType':'codex','state':'idle','providerSession':{
                                'id':'chat','transcriptPath':str(path)}}}]}]}
                    if method=='terminal.list':return {'terminals':[{'handle':'pane','incarnationId':'inc',
                        'connected':True,'ptyId':'pty','worktreeId':'work','tabId':'tab'}]}
                    if method=='terminal.read':return {'terminal':{'source':'screen','tail':['Ask Codex']}}
                    raise AssertionError(method)
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(module,'LOCALDATA',root))
                stack.enter_context(patch.object(module,'read_transcript',side_effect=[stale,fresh]))
                stack.enter_context(patch.object(module,'read_goal',return_value=None))
                stack.enter_context(patch.object(module,'protected_json',return_value={}))
                stack.enter_context(patch.object(module,'identity',side_effect=[{'key':'old'},{'key':'new'}]))
                adapter=module.OrcaAdapter(object(),Rpc())
                adapter.bindings={'chat':{'terminal':'pane','incarnation':'inc','home':str(target_home),
                    'path':str(path),'source_path':str(path),'key':'new','status_verified':True}}
                row=adapter.discover()[0]
            self.assertEqual(row['permission_profile'],fresh['permission_profile'])
            self.assertEqual(row['active_permission_profile'],fresh['active_permission_profile'])
            self.assertEqual(row['_context_turn_id'],'fresh-turn')

    def test_exited_quota_pane_stays_discoverable_but_manual_stop_does_not(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder);home=root/'home';path=home/'sessions/chat.jsonl';path.parent.mkdir(parents=True);path.write_text('{}');(home/'auth.json').write_text('{}')
            class Rpc:
                exited=True
                def call(self,method,params=None):
                    if method=='session.tabs.listAll':return {'snapshots':[]}
                    if method=='terminal.list':return {'terminals':[{'handle':'pane','incarnationId':'inc','connected':False,'ptyId':'pty','worktreeId':'work'}]}
                    if method=='terminal.read':return {'terminal':{'source':'screen','tail':['Token usage: total=123','To continue this session, run:','codex resume chat']}}
                    if method=='terminal.wait':return {'wait':{'handle':'pane','condition':'exit','satisfied':self.exited,'status':'exited' if self.exited else 'running'}}
                    raise AssertionError(method)
            rpc=Rpc();info={'id':'chat','path':str(path),'status':'failed','error_kind':'quota','turn_id':'before','model':'chosen','effort':'xhigh'}
            with patch.object(module,'LOCALDATA',root),patch.object(module,'read_transcript',side_effect=lambda *_:dict(info)),patch.object(module,'read_goal',return_value=None),patch.object(module,'protected_json',return_value={}),patch.object(module,'identity',return_value={'key':'verified'}):
                a=module.OrcaAdapter(object(),rpc);a.bindings={'chat':{'terminal':'pane','incarnation':'inc','home':str(home),'path':str(path),'key':'verified','status_verified':True}}
                row=a.discover()[0];self.assertTrue(row['_exited_pane']);self.assertEqual(row['error_kind'],'quota')
                rpc.exited=False;self.assertEqual(a.discover(),[])
                rpc.exited=True;info.update(status='interrupted',error_kind='user_interrupt');self.assertEqual(a.discover(),[])


if __name__=='__main__':unittest.main()

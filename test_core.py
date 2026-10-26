import json, pathlib, sqlite3, tempfile, unittest
from unittest.mock import patch
from accounts import quota_windows, transfer_goal, read_goal, database_connection, atomic_json
from controller import available, needs_switch
from transcript import read_transcript, resume_arguments, permission_policy_equal, powershell_quote

class StatePublication(unittest.TestCase):
    def test_windows_sharing_failure_retries_without_exposing_partial_document(self):
        import os
        with tempfile.TemporaryDirectory() as folder:
            path=pathlib.Path(folder)/'state.json';atomic_json(path,{'old':True});replace=os.replace;calls=[]
            def briefly_locked(source,dest):
                calls.append(source)
                if len(calls)<3:
                    self.assertEqual(json.loads(path.read_text()),{'old':True})
                    raise PermissionError('reader temporarily denies delete sharing')
                replace(source,dest)
            with patch('accounts.os.replace',side_effect=briefly_locked),patch('accounts.time.sleep'):
                atomic_json(path,{'new':True})
            self.assertEqual(json.loads(path.read_text()),{'new':True})
            self.assertEqual(len(list(path.parent.glob('*.tmp'))),0)

    def test_persistent_write_denial_retains_old_document_and_raises_for_journal(self):
        with tempfile.TemporaryDirectory() as folder:
            path=pathlib.Path(folder)/'journal.json';atomic_json(path,{'original':True})
            with patch('accounts.os.replace',side_effect=PermissionError('locked')),patch('accounts.time.sleep'):
                with self.assertRaises(PermissionError):atomic_json(path,{'replacement':True})
            self.assertEqual(json.loads(path.read_text()),{'original':True})
            self.assertEqual(len(list(path.parent.glob('*.tmp'))),0)

class Quotas(unittest.TestCase):
    def window(self, plan='plus', five=90, week=40):
        return quota_windows({'rateLimits': {'planType':plan, 'primary':{'usedPercent':five,'windowDurationMins':300,'resetsAt':100},'secondary':{'usedPercent':week,'windowDurationMins':10080,'resetsAt':200}}})
    def test_threshold_and_weekly_exhaustion(self):
        self.assertTrue(available(self.window(),2))
        self.assertFalse(available(self.window(five=98),2))
        self.assertFalse(available(self.window(week=100),2))
        self.assertTrue(needs_switch({'status':'running'},self.window(five=98),2))
    def test_monthly_is_not_five_hour(self):
        q=quota_windows({'rateLimits':{'planType':'free','primary':{'usedPercent':0,'windowDurationMins':43200}}})
        self.assertFalse(q['valid']);self.assertIsNone(q['five_remaining'])
    def test_pro_weekly_only(self):
        q=quota_windows({'rateLimits':{'planType':'pro','primary':{'usedPercent':20,'windowDurationMins':10080}}})
        self.assertTrue(available(q,2))
    def test_designated_weekly_fallback_requires_real_weekly_allowance(self):
        raw={'rateLimits':{'planType':'plus','primary':{'usedPercent':20,'windowDurationMins':10080}}}
        self.assertFalse(available(quota_windows(raw),2))
        q=quota_windows(raw,weekly_fallback=True)
        self.assertTrue(q['weekly_only']);self.assertTrue(q['fallback_only']);self.assertTrue(available(q,2))
        self.assertFalse(available(quota_windows({},weekly_fallback=True),2))
        self.assertFalse(available(quota_windows(dict(raw,ordinaryUsageAllowed=False),weekly_fallback=True),2))
        raw['rateLimits']['primary']['usedPercent']=100
        self.assertFalse(available(quota_windows(raw,weekly_fallback=True),2))
    def test_free_is_excluded_even_with_supported_usage_windows(self):
        self.assertFalse(available(self.window(plan='free',five=0,week=0),2))
    def test_user_stops_and_goal_budget_are_respected(self):
        for s in ({'status':'idle'},{'status':'interrupted'},{'status':'running','waiting':True},{'status':'running','goal_status':'paused'},{'status':'running','goal_status':'budget_limited'}):
            self.assertFalse(needs_switch(s,self.window(five=100),2))
        self.assertTrue(needs_switch({'status':'failed','error_kind':'login'}, {},2))
        self.assertFalse(needs_switch({'status':'interrupted','goal_status':'usage_limited','error_kind':'user_interrupt'},self.window(five=100),2))
    def test_screen_banner_requires_fresh_exhausted_allowance(self):
        row={'status':'failed','error_kind':'quota','_screen_quota':True}
        self.assertFalse(needs_switch(row,self.window(five=20),2))
        self.assertTrue(needs_switch(row,self.window(five=100),2))
    def test_desktop_native_quota_codes_without_goal(self):
        for code in ('usageLimitExceeded','rate_limit_exceeded'):
            self.assertTrue(needs_switch({'status':'failed','error_kind':code,'goal_status':None},{},2))
        self.assertFalse(needs_switch({'status':'failed','error_kind':'network_error'},{},2))
    def test_proxy_quota_never_rotates_openai_login(self):
        for row in ({'status':'running','model':'google-antigravity/gemini-3.8-flash'},
                    {'status':'failed','goal_status':'usage_limited','model_provider':'google-antigravity','error_kind':'quota'}):
            self.assertFalse(needs_switch(row,self.window(five=100),2))

class GoalTransfer(unittest.TestCase):
    def test_preserves_accounting_and_rejects_newer_goal(self):
        with tempfile.TemporaryDirectory() as root:
            old=pathlib.Path(root)/'old';new=pathlib.Path(root)/'new';old.mkdir();new.mkdir()
            with database_connection(old/'goals_1.sqlite') as c:
                c.execute('CREATE TABLE _sqlx_migrations (version INTEGER, description TEXT)')
                c.execute('CREATE TABLE thread_goals (thread_id TEXT PRIMARY KEY, goal_id TEXT, objective TEXT, status TEXT, token_budget INTEGER, tokens_used INTEGER, time_used_seconds INTEGER, created_at_ms INTEGER, updated_at_ms INTEGER)')
                c.execute('INSERT INTO thread_goals VALUES (?,?,?,?,?,?,?,?,?)',('s','g','Finish the work','active',10000,700,23,1,2))
            transfer_goal(old,new,'s')
            self.assertEqual(read_goal(old,'s'),read_goal(new,'s'))
            with database_connection(new/'goals_1.sqlite') as c:c.execute('UPDATE thread_goals SET tokens_used=701')
            with self.assertRaises(RuntimeError):transfer_goal(old,new,'s')

class Resume(unittest.TestCase):
    def test_preserves_model_and_permission_policy(self):
        d={'id':'s','model':'chosen-model','effort':'high','sandbox_policy':{'type':'read-only'},'approval_policy':'on-request'}
        args=resume_arguments(d)
        self.assertIn('chosen-model',args);self.assertIn('read-only',args);self.assertNotIn('danger-full-access',args)
        d['sandbox_policy']={'type':'unsupported'}
        with self.assertRaises(RuntimeError):resume_arguments(d)

    def test_workspace_write_resume_explicitly_overrides_all_four_fields(self):
        for network in (False,True):
            for exclude_tmp in (False,True):
                for exclude_slash in (False,True):
                    with self.subTest(network=network,exclude_tmp=exclude_tmp,exclude_slash=exclude_slash):
                        info={'id':'s','model':'chosen','effort':'high','approval_policy':'never',
                            'sandbox_policy':{'type':'workspace-write','network_access':network,
                                'writable_roots':[],'exclude_tmpdir_env_var':exclude_tmp,
                                'exclude_slash_tmp':exclude_slash}}
                        args=resume_arguments(info)
                        overrides=[args[i+1] for i,value in enumerate(args[:-1])
                            if value=='-c' and args[i+1].startswith('sandbox_workspace_write.')]
                        self.assertEqual(overrides,[
                            'sandbox_workspace_write.network_access='+str(network).lower(),
                            'sandbox_workspace_write.writable_roots=[]',
                            'sandbox_workspace_write.exclude_tmpdir_env_var='+str(exclude_tmp).lower(),
                            'sandbox_workspace_write.exclude_slash_tmp='+str(exclude_slash).lower(),
                        ])

    def test_workspace_profile_and_legacy_defaults_normalize_semantically(self):
        entries=[
            {'path':{'type':'special','value':{'kind':'root'}},'access':'read'},
            {'path':{'type':'special','value':{'kind':'project_roots'}},'access':'write'},
            {'path':{'type':'special','value':{'kind':'slash_tmp'}},'access':'write'},
            {'path':{'type':'special','value':{'kind':'tmpdir'}},'access':'write'},
        ]
        entries.extend({'path':{'type':'special','value':{'kind':'project_roots','subpath':name}},
            'access':'read','missing_path_behavior':'skip'} for name in ('.git','.agents','.codex','.aws'))
        original={'id':'s','model':'chosen','effort':'high','approval_policy':'on-request',
            'sandbox_policy':{'type':'workspace-write'},
            'permission_profile':{'type':'managed','network':'restricted',
                'file_system':{'type':'restricted','entries':entries}},
            'active_permission_profile':{'id':':workspace','extends':None}}
        normalized=dict(original,sandbox_policy={'type':'workspace-write','network_access':False,
            'writable_roots':[],'exclude_tmpdir_env_var':False,'exclude_slash_tmp':False},
            active_permission_profile=None)
        self.assertTrue(permission_policy_equal(original,normalized))
        args=resume_arguments(original)
        self.assertIn('sandbox_workspace_write.network_access=false',args)

    def test_unsupported_approval_and_unrepresentable_permission_fields_reject(self):
        base={'id':'s','model':'chosen','effort':'high','approval_policy':'never',
            'sandbox_policy':{'type':'workspace-write'}}
        bad=[
            dict(base,approval_policy='on-failure'),
            dict(base,approval_policy='untrusted'),
            dict(base,sandbox_policy={'type':'workspace-write','writable_roots':['C:/extra']}),
            dict(base,sandbox_policy={'type':'workspace-write','network_access':'false'}),
            dict(base,sandbox_policy={'type':'workspace-write','custom_restriction':True}),
            dict(base,sandbox_policy={'type':'read-only','network_access':False}),
            dict(base,active_permission_profile={'id':'custom-safe','extends':None}),
            dict(base,permission_profile={'type':'external','network':'restricted'}),
            dict(base,permission_profile={'type':'managed','network':'restricted',
                'file_system':{'type':'restricted','entries':[{'path':{'type':'glob_pattern',
                    'pattern':'**/*.secret'},'access':'deny'}]}}),
        ]
        for info in bad:
            with self.subTest(info=info),self.assertRaises(RuntimeError):resume_arguments(info)

    def test_disabled_permission_profile_remains_supported_for_full_access(self):
        info={'id':'s','model':'chosen','effort':'high','approval_policy':'never',
            'sandbox_policy':{'type':'danger-full-access'},
            'permission_profile':{'type':'disabled'},
            'active_permission_profile':{'id':':danger-full-access'}}
        self.assertIn('danger-full-access',resume_arguments(info))

    def test_shell_quotes_do_not_expand_user_text(self):
        self.assertEqual(powershell_quote("x'`$()"),"'x''`$()'")
    def test_last_event_detects_usage_failure(self):
        with tempfile.TemporaryDirectory() as root:
            file=pathlib.Path(root)/'session.jsonl'
            file.write_text('\n'.join(json.dumps(r) for r in [
                {'type':'session_meta','payload':{'id':'s','cwd':'C:/test'}},
                {'type':'event_msg','payload':{'type':'task_started','turn_id':'t'}},
                {'type':'event_msg','payload':{'type':'error','message':'You have hit your usage limit'}}]))
            self.assertEqual(read_transcript(file)['error_kind'],'quota')

    def test_turn_context_id_distinguishes_copied_settings_from_fresh_turn_settings(self):
        with tempfile.TemporaryDirectory() as root:
            file=pathlib.Path(root)/'session.jsonl'
            file.write_text('\n'.join(json.dumps(r) for r in [
                {'type':'session_meta','payload':{'id':'s','cwd':'C:/test'}},
                {'type':'turn_context','payload':{'turn_id':'old','model':'chosen','effort':'high',
                    'approval_policy':'never','sandbox_policy':{'type':'danger-full-access'},
                    'permission_profile':{'type':'disabled'}}},
                {'type':'event_msg','payload':{'type':'task_started','turn_id':'new'}},
            ])+'\n')
            copied=read_transcript(file)
            self.assertEqual(copied['turn_id'],'new')
            self.assertEqual(copied['_context_turn_id'],'old')
            self.assertEqual(copied['permission_profile'],{'type':'disabled'})
            with file.open('a') as out:
                out.write(json.dumps({'type':'turn_context','payload':{'turn_id':'new','model':'chosen',
                    'effort':'high','approval_policy':'never',
                    'sandbox_policy':{'type':'danger-full-access'},
                    'permission_profile':{'type':'disabled'}}})+'\n')
            fresh=read_transcript(file)
            self.assertEqual(fresh['_context_turn_id'],'new')

    def test_explicit_quota_login_aborts_and_manual_stop_are_distinguished(self):
        with tempfile.TemporaryDirectory() as root:
            file=pathlib.Path(root)/'session.jsonl'
            for reason,expected in [('usageLimitExceeded','quota'),({'type':'unauthorized'},'login'),
                                    ('interrupted','user_interrupt'),(None,'user_interrupt'),('network_error','user_interrupt')]:
                with self.subTest(reason=reason):
                    file.write_text('\n'.join(json.dumps(r) for r in [
                        {'type':'session_meta','payload':{'id':'s','cwd':'C:/test'}},
                        {'type':'event_msg','payload':{'type':'task_started','turn_id':'t'}},
                        {'type':'event_msg','payload':{'type':'turn_aborted','reason':reason}}]))
                    row=read_transcript(file)
                    self.assertEqual(row['error_kind'],expected)
                    self.assertEqual(needs_switch(row,{'five_remaining':90,'weekly_remaining':90},2),expected!='user_interrupt')

    def test_long_turn_keeps_identity_when_task_start_is_outside_tail(self):
        with tempfile.TemporaryDirectory() as root:
            file=pathlib.Path(root)/'session.jsonl'
            rows=[{'type':'session_meta','payload':{'id':'s','cwd':'C:/test'}},
                  {'type':'event_msg','payload':{'type':'task_started','turn_id':'long-turn'}},
                  {'type':'response_item','payload':{'padding':'x'*2_100_000}},
                  {'type':'turn_context','payload':{'turn_id':'long-turn','model':'chosen-model','effort':'high'}}]
            file.write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
            row=read_transcript(file)
            self.assertEqual(row['turn_id'],'long-turn');self.assertEqual(row['status'],'running')
            with file.open('a') as f:f.write(json.dumps({'type':'event_msg','payload':{'type':'turn_aborted','reason':'interrupted'}})+'\n')
            self.assertEqual(read_transcript(file)['status'],'interrupted')

    def test_long_completed_turn_backfills_latest_settings_without_reviving_user_stop(self):
        with tempfile.TemporaryDirectory() as root:
            file=pathlib.Path(root)/'session.jsonl'
            rows=[{'type':'session_meta','payload':{'id':'s','cwd':'C:/test'}},
                  {'type':'turn_context','payload':{'turn_id':'older','model':'old','effort':'high'}},
                  {'type':'turn_context','payload':{'turn_id':'newer','model':'chosen','effort':'xhigh','approval_policy':'never','sandbox_policy':{'type':'danger-full-access'}}},
                  {'type':'event_msg','payload':{'type':'task_started','turn_id':'newer'}},
                  {'type':'response_item','payload':{'padding':'x'*3_100_000}},
                  {'type':'event_msg','payload':{'type':'task_complete'}}]
            file.write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
            row=read_transcript(file)
            self.assertEqual((row['turn_id'],row['model'],row['effort'],row['status']),('newer','chosen','xhigh','idle'))
            with file.open('a') as f:f.write(json.dumps({'type':'event_msg','payload':{'type':'turn_aborted','reason':'interrupted'}})+'\n')
            row=read_transcript(file);self.assertEqual(row['status'],'interrupted');self.assertFalse(needs_switch(row,{'five_remaining':0},2))

if __name__=='__main__':unittest.main()

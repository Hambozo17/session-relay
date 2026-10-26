import contextlib
import json
import pathlib
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import controller
import orca_adapter


class Accounts:
    def __init__(self, root, app):
        self.records=[{'key':'only','home':str(root),'orca_home':str(root),
            'orca_id':'native-only','label':'only','sources':['Orca']}]
        self.quotas={'only':self.quota(0,100)}
    def quota(self, remaining, reset):
        return {'valid':True,'five_remaining':remaining,'weekly_remaining':90,
            'updated_at':time.time(),'reset_at':reset,'weekly_reset_at':10000}
    def inventory(self):return self.records
    def find(self,key):return self.records[0] if key=='only' else None
    def current_desktop_key(self):return 'only'
    def probe(self,record,force=False,home=None):return self.quotas[record['key']]


class Adapter:
    def __init__(self,rows,app=None):self.rows=rows;self.app=app;self.calls=[];self.recovery_receipts={}
    def discover(self):return self.rows
    def rotate(self,ids,target,journal):
        self.calls.append(('rotate',list(ids),target['key']))
        if self.app=='orca':
            context={row['id']:dict(row) for row in self.rows if row['id'] in ids}
            journal('orca-recovery-context',{'context':context})
            for row in self.rows:
                if row['id'] in ids:
                    journal('resume-process-created',{'session':row['id'],'target':target['key'],
                        'terminal':'terminal-'+target['key'],'incarnation':'inc-'+target['key']})
        for row in self.rows:
            if row['id'] in ids:
                changes={'status':'running','turn_id':'resumed-'+row['turn_id'],'error_kind':None,
                    'account_key':target['key']}
                if self.app=='orca':
                    changes.update(terminal='terminal-'+target['key'],incarnation='inc-'+target['key'],
                        pty_id='pty-'+target['key'],connected=True,
                        _turn_started_at_ms=int(time.time()*1000))
                row.update(changes)
    def resume_in_place(self,ids,journal):self.rotate(ids,{'key':'only'},journal)


class CodexGoalAdapter:
    def __init__(self, row):
        self.rows=[row]
        self.calls=[]
        self.recovery_receipts={}

    def discover(self):return self.rows

    def rotate(self, ids, target, journal, **_kwargs):
        self.calls.append(('rotate',list(ids),target['key']))
        for row in self.rows:
            if row['id'] not in ids:continue
            goal=dict(row['_goal_record'])
            context={'turn':row['turn_id'],'settings':row['_settings_fingerprint'],
                'settings_snapshot':row.get('_settings'),'goal_record':dict(goal)}
            journal('desktop-recovery-context',{'context':{row['id']:context}})
            goal.update(status='usage_limited',tokens_used=goal['tokens_used']+1,
                time_used_seconds=goal['time_used_seconds']+1)
            row.update(status='idle',error_kind=None,goal_status='usage_limited',
                turn_id=f"{target['key']}-turn-{len(self.calls)}",account_key=target['key'],
                _goal_record=goal,_turn_started_at_ms=int((time.time()+1)*1000))


class QuotaLoop(unittest.TestCase):
    @contextlib.contextmanager
    def environment(self,app):
        with tempfile.TemporaryDirectory() as folder,contextlib.ExitStack() as stack:
            root=pathlib.Path(folder);accounts=Accounts(root,app)
            rows=[{'id':'chat','status':'failed','error_kind':'quota','turn_id':'initial',
                'account_key':'only','home':str(root),'connected':True,'supported':True}]
            adapter=Adapter(rows,app);other='codex' if app=='orca' else 'orca'
            cfg={app:{'enabled':True,'sessions':['chat']},other:{'enabled':False},'included_resets':False}
            for name,value in [('LOCALDATA',root),('STATUS',root/'status.json'),('JOURNAL',root/'journal.json')]:
                stack.enter_context(patch.object(controller,name,value))
            stack.enter_context(patch.object(controller,'config',return_value=cfg))
            c=controller.Controller(accounts,{app:adapter,other:Adapter([],other)});c.refresh_started=time.time()
            try:yield c,accounts,adapter,root,cfg
            finally:c.refresh_pool.shutdown(wait=True)

    def test_exhaustion_reset_resume_repeat_and_restart_in_both_apps(self):
        for app in ('orca','codex'):
            with self.subTest(app=app),self.environment(app) as (c,accounts,adapter,root,cfg):
                c.cycle();self.assertEqual(adapter.calls,[]);self.assertTrue(c.keep_awake)
                self.assertIn('stay queued',json.loads((root/'status.json').read_text())['messages'][app])
                accounts.quotas['only']=accounts.quota(99,200)
                c.cycle();self.assertEqual(len(adapter.calls),1)
                adapter.rows[0].update(status='failed',error_kind='quota',turn_id='new-failure')
                c.cycle();self.assertEqual(len(adapter.calls),1)  # Stale healthy quota cannot cause a send loop.
                restarted=controller.Controller(accounts,c.adapters);restarted.refresh_started=time.time()
                try:
                    restarted.cycle();self.assertEqual(len(adapter.calls),1)
                    accounts.quotas['only']=accounts.quota(0,200)
                    restarted.cycle();self.assertEqual(len(adapter.calls),1)
                    accounts.quotas['only']=accounts.quota(100,300)
                    restarted.cycle();self.assertEqual(len(adapter.calls),2)
                    self.assertEqual(cfg[app]['sessions'],['chat'])
                finally:restarted.refresh_pool.shutdown(wait=True)

    def test_stale_failed_login_becomes_a_destination_but_recent_failure_waits(self):
        now=time.time()
        self.assertTrue(controller.destination_needs_check({'valid':False,'updated_at':now-61},2))
        self.assertFalse(controller.destination_needs_check({'valid':False,'updated_at':now},2))
        self.assertTrue(controller.destination_needs_check({},2))

    def test_all_accounts_depleted_then_another_account_recovers(self):
        with self.environment('orca') as (c,accounts,adapter,root,_cfg):
            accounts.records.append(dict(accounts.records[0],key='second',orca_id='native-second'))
            accounts.quotas['second']=accounts.quota(0,100)
            c.cycle();self.assertEqual(adapter.calls,[])
            accounts.quotas['second']=accounts.quota(100,300)
            c.cycle();self.assertEqual(adapter.calls,[('rotate',['chat'],'second')])

    def test_verified_screen_failure_survives_reset_and_monitor_restart(self):
        with self.environment('orca') as (c,accounts,adapter,root,_cfg):
            adapter.rows[0]['_screen_quota']=True
            c.cycle();self.assertEqual(adapter.calls,[])
            restarted=controller.Controller(accounts,c.adapters);restarted.refresh_started=time.time()
            try:
                accounts.quotas['only']=accounts.quota(100,200)
                restarted.cycle();self.assertEqual(len(adapter.calls),1)
            finally:restarted.refresh_pool.shutdown(wait=True)

    def test_healthy_old_screen_banner_alone_does_not_resume_a_chat(self):
        with self.environment('orca') as (c,accounts,adapter,root,_cfg):
            adapter.rows[0]['_screen_quota']=True
            accounts.quotas['only']=accounts.quota(100,200)
            c.cycle();self.assertEqual(adapter.calls,[])

    def test_a_fast_new_failure_is_not_mistaken_for_a_pre_switch_failure(self):
        row={'id':'chat','status':'failed','error_kind':'quota','account_key':'new',
            'turn_id':'new-turn','_turn_started_at_ms':1800}
        op={'app':'codex','state':'complete','outgoing':'old','target':'new','started_at':1.5,
            'updated_at':2,'originals':{'chat':{'turn':'old-turn'}}}
        self.assertFalse(controller.quota_failure_before_handoff(row,[op]))
        self.assertFalse(controller.quota_failure_before_handoff(row,[dict(op,originals={})]))
        old=dict(row,turn_id='old-turn',_turn_started_at_ms=1000)
        self.assertTrue(controller.quota_failure_before_handoff(old,[op]))

    def test_orca_completed_target_failures_are_persisted_and_block_bounces_until_reset(self):
        with tempfile.TemporaryDirectory() as folder,contextlib.ExitStack() as stack:
            root=pathlib.Path(folder);now=time.time()
            class MultiAccounts:
                def __init__(self):
                    self.records=[
                        {'key':'A','home':str(root/'A'),'orca_home':str(root/'A'),'orca_id':'native-A','label':'A','sources':['Orca']},
                        {'key':'B','home':str(root/'B'),'orca_home':str(root/'B'),'orca_id':'native-B','label':'B','sources':['Orca']},
                    ]
                    self.quotas={'A':self.quota(),'B':self.quota()}
                def quota(self,generation=0):
                    return {'valid':True,'plan':'plus','ordinary_allowed':True,'five_remaining':80,
                        'weekly_remaining':70,'updated_at':time.time(),'reset_at':None,
                        'weekly_reset_at':None,'_allowance_generation':generation}
                def inventory(self):return self.records
                def find(self,key):return next((record for record in self.records if record['key']==key),None)
                def current_desktop_key(self):return None
                def probe(self,record,force=False,home=None):return self.quotas[record['key']]
            accounts=MultiAccounts()
            settings={'cwd':'C:/project','model':'gpt-test','effort':'high','model_provider':'openai',
                'approval_policy':'on-request','sandbox_policy':{'type':'workspace-write',
                    'network_access':False,'writable_roots':[],
                    'exclude_tmpdir_env_var':False,'exclude_slash_tmp':False}}
            row={'id':'chat','status':'failed','error_kind':'quota','turn_id':'turn-B-failure',
                '_turn_started_at_ms':int(now*1000),'account_key':'B','home':str(root/'B'),
                'connected':True,'supported':True,'provider':'codex','terminal':'terminal-B',
                'incarnation':'inc-B','pty_id':'pty-B',**settings}
            op_b={'id':'completed-A-to-B','app':'orca','state':'complete','outgoing':'A','target':'B',
                'sessions':['chat'],'started_at':now-20,'updated_at':now-10,
                'target_quota':{'allowance_generation':0,'reset_at':None,'weekly_reset_at':None},
                'orca_recovery_context':{'chat':dict(row,turn_id='turn-before-B',status='running',
                    account_key='A',sandbox_policy={'type':'workspace-write'})},
                'orca_target_panes':{'chat':{'target':'B','terminal':'terminal-B','incarnation':'inc-B','pty_id':'pty-B'}}}
            operations=root/'operations';operations.mkdir()
            (operations/'completed-A-to-B.json').write_text(json.dumps(op_b),encoding='utf-8')
            journal_path=root/'journal.json';journal_path.write_text(json.dumps(op_b),encoding='utf-8')
            cfg={'threshold':2,'included_resets':False,'orca':{'enabled':True,'sessions':['chat']},
                'codex':{'enabled':False,'sessions':[]}}
            for name,value in [('LOCALDATA',root),('STATUS',root/'status.json'),('JOURNAL',journal_path)]:
                stack.enter_context(patch.object(controller,name,value))
            stack.enter_context(patch.object(controller,'config',return_value=cfg))
            adapter=Adapter([row],'orca');other=Adapter([],'codex')
            self.assertIsNotNone(controller.quota_failure_after_handoff(row,op_b))
            self.assertIsNone(controller.quota_failure_after_handoff(
                dict(row,sandbox_policy=dict(row['sandbox_policy'],network_access=True)),op_b))
            first=controller.Controller(accounts,{'orca':adapter,'codex':other});first.refresh_started=time.time()
            try:
                self.assertEqual(controller.quota_failed_target_keys('orca',[row],first.recovery_attempts,accounts.quotas),set())
                first.cycle()
                self.assertEqual(adapter.calls,[('rotate',['chat'],'A')])
                saved_b=json.loads((operations/'completed-A-to-B.json').read_text(encoding='utf-8'))
                self.assertEqual(saved_b['state'],'complete')
                self.assertEqual(saved_b['quota_failed_sessions']['chat']['turn'],'turn-B-failure')

                completed_a=max((op for op in first.recovery_attempts if op.get('target')=='A'),
                    key=lambda op:op.get('started_at',0))
                row.update(status='failed',error_kind='quota',turn_id='turn-A-failure',
                    _turn_started_at_ms=int((completed_a['started_at']+5)*1000))
                first.cycle()
                self.assertEqual(len(adapter.calls),1)
                self.assertEqual(controller.quota_failed_target_keys('orca',[row],first.recovery_attempts,accounts.quotas),{'A','B'})
            finally:
                first.refresh_pool.shutdown(wait=True)

            restarted=controller.Controller(accounts,{'orca':adapter,'codex':other});restarted.refresh_started=time.time()
            try:
                restarted.cycle()
                self.assertEqual(len(adapter.calls),1)
                accounts.quotas['B']=accounts.quota(generation=1)
                restarted.cycle()
                self.assertEqual(adapter.calls[-1],('rotate',['chat'],'B'))
            finally:
                restarted.refresh_pool.shutdown(wait=True)


    def test_codex_idle_usage_limited_handoffs_block_bounces_until_each_target_resets(self):
        for budget in (None, 100):
            with self.subTest(budget=budget):
                self._codex_idle_goal_recovery_case(budget)

    def _codex_idle_goal_recovery_case(self, budget):
        with tempfile.TemporaryDirectory() as folder,contextlib.ExitStack() as stack:
            root=pathlib.Path(folder)
            class MultiAccounts:
                def __init__(self):
                    self.records=[{'key':key,'home':str(root/key),'label':key,'sources':['Codex']}
                        for key in ('A','B')]
                    self.quotas={key:self.quota() for key in ('A','B')}
                def quota(self,generation=0):
                    return {'valid':True,'plan':'plus','ordinary_allowed':True,'five_remaining':80,
                        'weekly_remaining':70,'updated_at':time.time(),'reset_at':None,
                        'weekly_reset_at':None,'_allowance_generation':generation}
                def inventory(self):return self.records
                def find(self,key):return next((record for record in self.records if record['key']==key),None)
                def current_desktop_key(self):return None
                def probe(self,record,force=False,home=None):return self.quotas[record['key']]
            accounts=MultiAccounts()
            goal={'goal_id':'goal-1','status':'active','token_budget':budget,'tokens_used':40,
                'time_used_seconds':30,'created_at_ms':100,'deferred':False}
            row={'id':'chat','status':'failed','error_kind':'quota','goal_status':'active',
                'turn_id':'initial-A','_turn_started_at_ms':int((time.time()-10)*1000),
                'account_key':'A','provider':'codex','model_provider':'openai','model':'gpt-test',
                'effort':'high','_owner':'owner-1','_settings_fingerprint':'settings-1',
                '_settings':{'model':'gpt-test','effort':'high'},'_goal_record':goal,
                '_goal_deferred':False,'_goal_mismatch':False,'_ephemeral':False,
                'waiting':False,'is_child':False,'supported':True}
            cfg={'threshold':2,'included_resets':False,
                'codex':{'enabled':True,'sessions':['chat']},
                'orca':{'enabled':False,'sessions':[]}}
            for name,value in [('LOCALDATA',root),('STATUS',root/'status.json'),('JOURNAL',root/'journal.json')]:
                stack.enter_context(patch.object(controller,name,value))
            stack.enter_context(patch.object(controller,'config',return_value=cfg))
            adapter=CodexGoalAdapter(row);other=Adapter([],'orca')
            first=controller.Controller(accounts,{'codex':adapter,'orca':other})
            first.refresh_started=time.time()
            try:
                first.cycle()
                self.assertEqual(adapter.calls,[('rotate',['chat'],'B')])
                completed_b=next(op for op in first.completed_handoffs if op.get('target')=='B')
                self.assertEqual(row['status'],'idle')
                self.assertEqual(row['goal_status'],'usage_limited')
                receipt=controller.quota_failure_after_handoff(row,completed_b)
                self.assertEqual(receipt['error_kind'],'quota')

                rejects=[
                    ('old turn',dict(row,turn_id=completed_b['recovery_context']['chat']['turn'])),
                    ('manual or unverified provider',dict(row,provider=None)),
                    ('approval pending',dict(row,waiting=True)),
                    ('child',dict(row,is_child=True)),
                    ('proxy provider',dict(row,model_provider='google-antigravity')),
                    ('paused goal',dict(row,goal_status='paused')),
                    ('completed goal',dict(row,goal_status='complete')),
                    ('budget limited goal',dict(row,goal_status='budget_limited')),
                    ('deferred goal flag',dict(row,_goal_deferred=True)),
                    ('ephemeral',dict(row,_ephemeral=True)),
                    ('mismatched goal flag',dict(row,_goal_mismatch=True)),
                    ('settings changed',dict(row,_settings_fingerprint='other-settings')),
                    ('owner missing',dict(row,_owner=None)),
                    ('post-handoff timestamp missing',dict(row,_turn_started_at_ms=None)),
                    ('turn predates handoff',dict(row,_turn_started_at_ms=int(completed_b['started_at']*1000))),
                    ('native goal missing',dict(row,_goal_record=None)),
                    ('goal budget missing',dict(row,_goal_record={
                        key:value for key,value in row['_goal_record'].items() if key!='token_budget'})),
                    ('goal identity changed',dict(row,_goal_record=dict(row['_goal_record'],goal_id='other'))),
                    ('goal budget changed',dict(row,_goal_record=dict(row['_goal_record'],token_budget=101))),
                    ('goal progress regressed',dict(row,_goal_record=dict(row['_goal_record'],tokens_used=39))),
                ]
                for label,bad_row in rejects:
                    self.assertIsNone(controller.quota_failure_after_handoff(bad_row,completed_b),label)
                missing_id_before=dict(completed_b['recovery_context']['chat'])
                missing_id_before['goal_record']=dict(
                    missing_id_before['goal_record'],goal_id='')
                missing_id_operation=dict(completed_b,recovery_context=dict(
                    completed_b['recovery_context'],chat=missing_id_before))
                missing_id_row=dict(row,_goal_record=dict(row['_goal_record'],goal_id=''))
                self.assertIsNone(controller.quota_failure_after_handoff(
                    missing_id_row,missing_id_operation),'missing native Goal ID')
                self.assertIsNone(controller.quota_failure_after_handoff(
                    row,dict(completed_b,state='in-progress')),'handoff is not complete')
                self.assertIsNone(controller.quota_failure_after_handoff(
                    row,dict(completed_b,recovery_context={})), 'manual turn without native recovery context')
                self.assertIsNone(controller.quota_failure_after_handoff(
                    row,dict(completed_b,app='orca')), 'Orca idle Goal')

                first.cycle()
                self.assertEqual(adapter.calls,[('rotate',['chat'],'B'),('rotate',['chat'],'A')])
                self.assertEqual(row['account_key'],'A')
                first.cycle()
                self.assertEqual(len(adapter.calls),2)
            finally:
                first.refresh_pool.shutdown(wait=True)

            saved=[json.loads(path.read_text(encoding='utf-8'))
                for path in (root/'operations').glob('*.json')]
            receipts={op['target']:op['quota_failed_sessions']['chat'] for op in saved
                if op.get('state')=='complete' and op.get('quota_failed_sessions',{}).get('chat')}
            self.assertEqual(set(receipts),{'A','B'})
            self.assertEqual(receipts['A']['target_quota']['allowance_generation'],0)
            self.assertEqual(receipts['B']['target_quota']['allowance_generation'],0)

            restarted=controller.Controller(accounts,{'codex':adapter,'orca':other})
            restarted.refresh_started=time.time()
            try:
                restarted.cycle()
                self.assertEqual(len(adapter.calls),2)
                self.assertEqual(controller.quota_failed_target_keys(
                    'codex',[row],restarted.recovery_attempts,accounts.quotas),{'A','B'})
                accounts.quotas['B']=accounts.quota(generation=1)
                restarted.cycle()
                self.assertEqual(adapter.calls[-1],('rotate',['chat'],'B'))
                self.assertEqual(controller.quota_failed_target_keys(
                    'codex',[row],restarted.recovery_attempts,accounts.quotas),{'A'})
            finally:
                restarted.refresh_pool.shutdown(wait=True)

    def test_new_selection_is_read_again_before_a_switch(self):
        with self.environment('orca') as (c,accounts,adapter,root,cfg):
            accounts.quotas['only']=accounts.quota(100,200)
            original=accounts.probe
            def deselect(*args,**kwargs):
                cfg['orca']['sessions']=[]
                return original(*args,**kwargs)
            accounts.probe=deselect
            c.cycle();self.assertEqual(adapter.calls,[])

    def test_original_account_is_rechecked_immediately_before_recovery(self):
        with self.environment('orca') as (c,accounts,adapter,root,_cfg):
            accounts.quotas['only']=accounts.quota(100,200);calls=[]
            def probe(record,force=False,home=None):
                calls.append(record['key'])
                if len(calls)>1:accounts.quotas['only']=accounts.quota(0,200)
                return accounts.quotas['only']
            accounts.probe=probe
            c.cycle();self.assertEqual(adapter.calls,[]);self.assertGreaterEqual(len(calls),2)

    def test_no_same_account_retry_for_user_stops_approvals_or_paused_goals(self):
        row={'id':'chat','status':'failed','error_kind':'quota','account_key':'only'}
        for changes in ({'status':'interrupted'},{'status':'running'},{'status':'idle'},
                {'waiting':True},{'goal_status':'paused'},{'goal_status':'budget_limited'},
                {'model_provider':'google-antigravity'},{'is_child':True}):
            self.assertFalse(controller.same_account_retry_allowed('orca',[dict(row,**changes)],{},[]),changes)

    def test_slow_login_does_not_hold_back_later_healthy_refreshes(self):
        with self.environment('orca') as (c,accounts,_,root,_cfg):
            gate=threading.Event();healthy=threading.Event();count=[]
            accounts.records=[{'key':'slow'},{'key':'healthy'}];accounts.quotas={}
            def probe(record,*args):
                key=record['key'];count.append(key)
                if key=='slow':gate.wait(3)
                else:healthy.set()
                accounts.quotas[key]={'updated_at':time.time()}
            accounts.probe=probe
            try:
                c.refresh_accounts(set());self.assertTrue(healthy.wait(1))
                accounts.quotas['healthy']['updated_at']=time.time()-61
                healthy.clear();c.refresh_accounts(set());self.assertTrue(healthy.wait(1))
                self.assertEqual(count.count('slow'),1);self.assertEqual(count.count('healthy'),2)
            finally:gate.set()

    def test_account_refresh_scheduler_runs_without_a_monitor_cycle(self):
        with self.environment('orca') as (c,accounts,_adapter,root,_cfg):
            checked=threading.Event()
            accounts.quotas['only']['updated_at']=time.time()-61
            def probe(*_args):checked.set();return accounts.quotas['only']
            accounts.probe=probe
            health=threading.Thread(target=c.health_loop)
            health.start()
            try:self.assertTrue(checked.wait(2))
            finally:c._health_stop.set();health.join(timeout=3)

    def test_accepted_continuation_failure_can_retry_but_uncertain_send_stays_blocked(self):
        before={'model':'chosen','effort':'xhigh','cwd':'repo','_goal':None,'turn_id':'old',
            'approval_policy':'never','sandbox_policy':{'type':'danger-full-access'},
            'permission_profile':{'type':'disabled'}}
        op={'app':'orca','phase':'continuation-accepted','target':'only','sessions':['chat'],
            'metadata':{'session':'chat','terminal':'pane','incarnation':'inc','turn':'old'},
            'orca_recovery_context':{'chat':before}}
        row=dict(before,id='chat',account_key='only',status='failed',error_kind='quota',
            turn_id='new',_context_turn_id='new',connected=True,terminal='pane',incarnation='inc')
        self.assertTrue(controller.orca_continuation_failed(op,[row]))
        self.assertFalse(controller.orca_continuation_failed(dict(op,phase='send-continuation'),[row]))
        for changes in ({'status':'interrupted'},{'waiting':True},{'turn_id':'old'},
                {'model':'other'},{'account_key':'other'},{'incarnation':'other'},
                {'_context_turn_id':None},{'_context_turn_id':'old'},
                {'approval_policy':'on-request'},{'sandbox_policy':{'type':'read-only'}}):
            self.assertFalse(controller.orca_continuation_failed(op,[dict(row,**changes)]),changes)
        sibling=dict(before,id='sibling',turn_id='untouched',account_key='old',terminal='sibling-pane',incarnation='inc')
        batch=dict(op,sessions=['chat','sibling'],orca_recovery_context={'chat':before,'sibling':sibling})
        self.assertTrue(controller.orca_continuation_failed(batch,[row,sibling]))
        self.assertFalse(controller.orca_continuation_failed(batch,[row,dict(sibling,turn_id='changed')]))
        self.assertFalse(controller.orca_continuation_failed(dict(batch,sessions=['sibling','chat']),[row,sibling]))

    def test_saved_orca_selector_deduplicates_and_keeps_live_state_without_authorizing_recovery(self):
        sid='123e4567-e89b-42d3-a456-426614174000'
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder);home=root/'home';home.mkdir()
            with orca_adapter.database_connection(home/'state_5.sqlite') as db:
                db.execute('CREATE TABLE threads (id TEXT,title TEXT,updated_at INTEGER,archived INTEGER,source TEXT)')
                db.executemany('INSERT INTO threads VALUES (?,?,?,?,?)',[
                    (sid,'Saved chat',100,0,'cli'),
                    ('223e4567-e89b-42d3-a456-426614174000','Child',100,0,'{"subagent":{}}'),
                    ('323e4567-e89b-42d3-a456-426614174000','Archived',100,1,'cli')])
            accounts=Accounts(home,'orca')
            with patch.object(orca_adapter,'LOCALDATA',root):
                a=orca_adapter.OrcaAdapter(accounts,object())
                rows=a.selectable_sessions([]);self.assertEqual([r['id'] for r in rows],[sid])
                self.assertFalse(rows[0]['attached']);self.assertEqual(rows[0]['status'],'Open in Orca')
                live={'id':sid,'title':'Live title','status':'failed','error_kind':'quota','supported':True}
                rows=a.selectable_sessions([live]);self.assertEqual(len(rows),1)
                self.assertEqual(rows[0]['title'],'Live title');self.assertTrue(rows[0]['attached'])


if __name__=='__main__':unittest.main()

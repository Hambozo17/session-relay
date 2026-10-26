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
    def __init__(self, gate=None):
        self.records=[{'key':key,'home':'C:/saved/'+key,'label':key,'sources':['Orca']} for key in ('active','invalid')]
        self.quotas={}; self.gate=gate; self.calls=[]
    def inventory(self): return self.records
    def find(self,key): return next((r for r in self.records if r['key']==key),None)
    def current_desktop_key(self): return None
    def probe(self,record,force=False,home=None):
        self.calls.append(record['key'])
        if record['key']=='invalid' and self.gate: self.gate.wait(5)
        result={'valid':record['key']=='active','five_remaining':80,'weekly_remaining':80,'updated_at':time.time()}
        self.quotas[record['key']]=result; return result

class Adapter:
    def __init__(self,rows): self.rows=rows; self.recovery_receipts={}
    def discover(self): return self.rows

class Monitor(unittest.TestCase):
    def test_orca_reconciliation_keeps_unverified_resumed_turns_blocked_across_restart(self):
        original={'id':'chat','turn_id':'before','model':'chosen','effort':'high',
            'approval_policy':'never','sandbox_policy':{'type':'danger-full-access'},
            'permission_profile':{'type':'disabled'},'cwd':'C:/work','_goal':None}
        invalid=[{'_context_turn_id':None},{'_context_turn_id':'before'},
            {'model':'changed'},{'effort':'low'},{'approval_policy':'on-request'},
            {'sandbox_policy':{'type':'read-only'}},
            {'permission_profile':{'type':'external'}}]
        for status in ('running','failed'):
            for changed in invalid:
                with self.subTest(status=status,changed=changed), tempfile.TemporaryDirectory() as folder:
                    root=pathlib.Path(folder);(root/'operations').mkdir()
                    operation={'id':'orca-resume','app':'orca','state':'needs-attention',
                        'phase':'continuation-accepted','sessions':['chat'],'target':'active',
                        'originals':{'chat':{'turn':'before','terminal':'old-pane'}},
                        'metadata':{'session':'chat','turn':'before','terminal':'new-pane','incarnation':'new-inc'},
                        'orca_recovery_context':{'chat':original},
                        'target_quota':{'allowance_generation':0}}
                    path=root/'operations/orca-resume.json'
                    path.write_text(json.dumps(operation))
                    row=dict(original,status=status,turn_id='after',_context_turn_id='after',
                        error_kind='quota' if status=='failed' else None,provider='codex',
                        account_key='active',connected=True,supported=True,home='C:/active',
                        terminal='new-pane',incarnation='new-inc',waiting=False,is_child=False)
                    row.update(changed)
                    cfg={'orca':{'enabled':True,'sessions':['chat']},'codex':{'enabled':False}}
                    with patch.object(controller,'LOCALDATA',root),patch.object(controller,'STATUS',root/'status.json'),patch.object(controller,'JOURNAL',root/'journal.json'),patch.object(controller,'config',return_value=cfg):
                        adapters={'orca':Adapter([row]),'codex':Adapter([])}
                        current=controller.Controller(Accounts(),adapters);current.refresh_started=time.time()
                        try:
                            for _ in range(2):
                                current.cycle()
                                self.assertEqual(len(current.unresolved),1)
                                self.assertEqual(json.loads(path.read_text())['state'],'needs-attention')
                        finally:current.refresh_pool.shutdown(wait=True)
                        restarted=controller.Controller(Accounts(),adapters);restarted.refresh_started=time.time()
                        try:
                            restarted.cycle()
                            self.assertEqual(len(restarted.unresolved),1)
                            self.assertIn('orca',restarted.attention)
                            row.update(original,turn_id='after',_context_turn_id='after')
                            restarted.cycle()
                            self.assertFalse(restarted.unresolved)
                            self.assertNotIn('orca',restarted.attention)
                            expected='reconciled-after-quota-failure' if status=='failed' else 'reconciled-before-continuation'
                            self.assertEqual(json.loads(path.read_text())['state'],expected)
                        finally:restarted.refresh_pool.shutdown(wait=True)

    def test_completed_later_turn_retires_failed_quit_without_resuming_idle_chat(self):
        operation={'phase':'desktop-close-intent','outgoing':'old','target':'new','sessions':['chat'],'updated_at':10,
            'recovery_context':{'chat':{'turn':'old-turn','settings':'same','goal_record':{'status':'usage_limited'}}}}
        row={'id':'chat','status':'idle','turn_id':'later','_turn_started_at_ms':20000,'account_key':'new',
            '_owner':'owner','supported':True,'_settings_fingerprint':'same','goal_status':None,'_goal_record':None}
        self.assertTrue(controller.desktop_close_superseded_by_completed_turn(operation,[row],'new'))
        for changes in ({'status':'interrupted'},{'status':'running'},{'goal_status':'paused'},
                {'_settings_fingerprint':'other'},{'_turn_started_at_ms':9000},{'waiting':True},
                {'turn_id':'old-turn'},{'_owner':None},{'_goal_record':{'status':'active'}}):
            self.assertFalse(controller.desktop_close_superseded_by_completed_turn(operation,[dict(row,**changes)],'new'))
        self.assertFalse(controller.desktop_close_superseded_by_completed_turn(dict(operation,phase='desktop-continuation-intent'),[row],'new'))
    def test_later_quota_turn_on_third_account_retires_old_startup_but_not_uncertain_send(self):
        goal={'goal_id':'original','status':'active','token_budget':1000,'tokens_used':10,'time_used_seconds':1}
        op={'phase':'desktop-open-thread-intent','updated_at':10,'target':'second','outgoing':'first','sessions':['chat'],
            'recovery_context':{'chat':{'turn':'before','settings':'same','goal_record':goal}}}
        row={'id':'chat','account_key':'third','_owner':'owner','supported':True,'status':'failed','error_kind':'usageLimitExceeded',
            'turn_id':'after','_turn_started_at_ms':20000,'goal_status':'usage_limited','_settings_fingerprint':'same',
            '_goal_record':dict(goal,status='usage_limited',tokens_used=20)}
        self.assertTrue(controller.desktop_restart_superseded_by_later_turns(op,[row],'third'))
        for changes in ({'status':'interrupted'},{'error_kind':'network_error'},{'turn_id':'before'},
                {'_turn_started_at_ms':9000},{'_settings_fingerprint':'changed'},{'waiting':True},
                {'is_child':True},{'supported':False},{'goal_status':'paused'},
                {'_goal_record':dict(goal,status='usage_limited',goal_id='different')},
                {'_goal_record':dict(goal,status='usage_limited',token_budget=2000)},
                {'_goal_record':dict(goal,status='usage_limited',tokens_used=1)},
                {'model_provider':'google-antigravity'}):
            self.assertFalse(controller.desktop_restart_superseded_by_later_turns(op,[dict(row,**changes)],'third'),changes)
        self.assertFalse(controller.desktop_restart_superseded_by_later_turns(dict(op,phase='desktop-continuation-intent'),[row],'third'))
        self.assertFalse(controller.desktop_restart_superseded_by_later_turns(dict(op,sessions=['chat','missing']),[row],'third'))

    def test_old_startup_failure_no_longer_blocks_current_selected_quota_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder);(root/'operations').mkdir()
            op={'id':'stale','app':'codex','state':'needs-attention','phase':'desktop-open-thread-intent',
                'updated_at':10,'outgoing':'first','target':'second','sessions':['chat'],
                'recovery_context':{'chat':{'turn':'before','settings':'same','goal_record':None}}}
            (root/'operations/stale.json').write_text(json.dumps(op))
            row={'id':'chat','account_key':'active','_owner':'owner','supported':True,'status':'failed','error_kind':'usageLimitExceeded',
                'turn_id':'after','_turn_started_at_ms':20000,'goal_status':None,'_settings_fingerprint':'same','_goal_record':None}
            class ResumeAdapter(Adapter):
                def rotate(self,ids,target,journal):self.rotated=(ids,target['key'])
            accounts=Accounts();accounts.current_desktop_key=lambda:'active'
            target={'key':'new','home':'new','label':'new','sources':[]};accounts.records.append(target)
            cfg={'codex':{'enabled':True,'sessions':['chat']},'orca':{'enabled':False}}
            with patch.object(controller,'LOCALDATA',root),patch.object(controller,'STATUS',root/'status.json'),patch.object(controller,'JOURNAL',root/'journal.json'),patch.object(controller,'config',return_value=cfg),patch.object(controller,'choose_destination',return_value=(target,False)):
                adapter=ResumeAdapter([row]);c=controller.Controller(accounts,{'orca':Adapter([]),'codex':adapter});c.refresh_started=time.time()
                try:
                    c.cycle();self.assertEqual(adapter.rotated,(['chat'],'new'))
                    self.assertFalse(c.attention);self.assertFalse(c.unresolved)
                    saved=json.loads((root/'operations/stale.json').read_text())
                    self.assertEqual(saved['reconciliation_evidence']['turns'],{'chat':'after'})
                finally:c.refresh_pool.shutdown(wait=True)

    def test_locked_status_display_does_not_cancel_durable_handoff_intent(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder);real_write=controller.atomic_json
            def write(path,value):
                if path==root/'status.json':raise PermissionError('locked display')
                real_write(path,value)
            with patch.object(controller,'LOCALDATA',root),patch.object(controller,'STATUS',root/'status.json'),patch.object(controller,'JOURNAL',root/'journal.json'),patch.object(controller,'atomic_json',side_effect=write):
                c=controller.Controller(Accounts(),{})
                try:
                    c.publish({},{});c.journal={'id':'durable','app':'codex','state':'in-progress'}
                    c.journal_phase('desktop-open-thread-intent',{'session':'chat'})
                    self.assertEqual(json.loads((root/'journal.json').read_text())['phase'],'desktop-open-thread-intent')
                    self.assertEqual(c.stage,'desktop-open-thread-intent')
                finally:c.refresh_pool.shutdown(wait=True)

    def test_quit_phase_keeps_pause_evidence_and_settled_goal_accounting(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder)
            with patch.object(controller,'LOCALDATA',root),patch.object(controller,'STATUS',root/'status.json'),patch.object(controller,'JOURNAL',root/'journal.json'):
                c=controller.Controller(accounts=object(),adapters={})
                try:
                    c.journal={'id':'receipts','app':'codex','state':'in-progress','interruption_receipts':{'chat':{'confirmation':'verified-postcondition-after-request-failure','goal_record':{'tokens_used':1}}}}
                    c.journal_phase('desktop-close-intent',{'originals':{'chat':{'turn':'turn','settings':'same','goal':'active','relay_interrupted':True}},'context':{'chat':{'goal_record':{'tokens_used':2,'goal_id':'same'}}}})
                    receipt=json.loads((root/'journal.json').read_text())['interruption_receipts']['chat']
                    self.assertEqual(receipt['confirmation'],'verified-postcondition-after-request-failure')
                    self.assertEqual(receipt['goal_record']['tokens_used'],2)
                finally:c.refresh_pool.shutdown(wait=True)

    def test_new_running_turn_releases_old_pause_without_authorizing_same_turn_manual_stop(self):
        goal={'goal_id':'same','status':'active','tokens_used':1,'time_used_seconds':1,'token_budget':100}
        operation={'phase':'desktop-interrupt-intent','outgoing':'old','metadata':{'session':'chat','turn':'before','settings':'same','goal_record':goal}}
        row={'id':'chat','account_key':'old','_owner':'owner','status':'running','turn_id':'after','goal_status':'active','_settings_fingerprint':'same','_goal_record':dict(goal,tokens_used=2)}
        self.assertTrue(controller.desktop_pause_superseded_by_running(operation,[row],'old'))
        for changes in ({'turn_id':'before'},{'status':'interrupted'},{'waiting':True},{'_settings_fingerprint':'other'},{'_goal_record':dict(goal,goal_id='other')},{'goal_status':'paused'}):
            self.assertFalse(controller.desktop_pause_superseded_by_running(operation,[dict(row,**changes)],'old'))
        self.assertFalse(controller.desktop_pause_superseded_by_running(dict(operation,phase='desktop-continuation-intent'),[row],'old'))

    def test_first_confirmed_pause_survives_second_pause_failure_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder)
            cfg={'codex':{'enabled':False},'orca':{'enabled':False}}
            row={'id':'master','account_key':'active','status':'interrupted','turn_id':'turn','_settings_fingerprint':'same','goal_status':'active'}
            with patch.object(controller,'LOCALDATA',root),patch.object(controller,'STATUS',root/'status.json'),patch.object(controller,'JOURNAL',root/'journal.json'),patch.object(controller,'config',return_value=cfg):
                c=controller.Controller(Accounts(),{'orca':Adapter([]),'codex':Adapter([row])})
                try:
                    c.journal={'id':'two-pauses','app':'codex','outgoing':'active','target':'new','state':'in-progress','sessions':['master','gemini']}
                    c.journal_phase('desktop-interrupt-confirmed',{'session':'master','turn':'turn','settings':'same','goal':'active','account':'active'})
                    c.journal_phase('desktop-interrupt-intent',{'session':'gemini','turn':'next','settings':'other'})
                    c.journal['state']='needs-attention';c.save_journal()
                    restarted=controller.Controller(Accounts(),{'orca':Adapter([]),'codex':Adapter([row])})
                    try:
                        restarted.cycle()
                        self.assertEqual(restarted.adapters['codex'].recovery_receipts,{'master':('turn','same')})
                        self.assertEqual(controller.confirmed_interruptions(restarted.unresolved[0])[0]['session'],'master')
                    finally:restarted.refresh_pool.shutdown(wait=True)
                finally:c.refresh_pool.shutdown(wait=True)

    def test_depleted_restart_target_reenters_account_selection_without_resuming_on_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder);(root/'operations').mkdir()
            operation={'id':'depleted','app':'codex','state':'needs-attention','phase':'desktop-open-thread-intent',
                'outgoing':'old','target':'active','sessions':['chat'],
                'recovery_context':{'chat':{'turn':'old','settings':'same','goal_record':None}},
                'interruption_receipts':{'chat':{'turn':'old','settings':'same','relay_interrupted':True}}}
            (root/'operations/depleted.json').write_text(json.dumps(operation))
            rows=[{'id':'chat','status':'interrupted','turn_id':'old','account_key':'active','supported':True,'_owner':'owner','_settings_fingerprint':'same','_goal_record':None}]
            class StoppedAdapter(Adapter):
                def resume_in_place(self,*args,**kwargs):raise AssertionError('Do not continue on a depleted target')
                def rotate(self,*args):raise AssertionError('No usable account is available yet')
            accounts=Accounts();accounts.current_desktop_key=lambda:'active'
            accounts.probe=lambda *args,**kwargs:{'valid':True,'five_remaining':1,'weekly_remaining':80}
            cfg={'codex':{'enabled':True,'sessions':['chat']},'orca':{'enabled':False},'included_resets':False}
            with patch.object(controller,'LOCALDATA',root),patch.object(controller,'STATUS',root/'status.json'),patch.object(controller,'JOURNAL',root/'journal.json'),patch.object(controller,'config',return_value=cfg):
                adapter=StoppedAdapter(rows);c=controller.Controller(accounts,{'orca':Adapter([]),'codex':adapter});c.refresh_started=time.time()
                try:
                    c.cycle();self.assertFalse(c.unresolved);self.assertFalse(c.attention)
                    self.assertEqual(adapter.recovery_receipts['chat'],('old','same'))
                    status=json.loads((root/'status.json').read_text())
                    self.assertIn('Waiting for an available account',status['messages']['codex'])
                finally:c.refresh_pool.shutdown(wait=True)

    def test_new_usage_failure_after_verified_restart_releases_stall_but_not_manual_stop(self):
        goal={'goal_id':'original','status':'active','token_budget':1000,'tokens_used':10,'time_used_seconds':1}
        op={'phase':'desktop-open-thread-intent','target':'active','sessions':['master','chat'],
            'recovery_context':{'master':{'turn':'original-master','settings':'same','goal_record':goal},'chat':{'turn':'original-chat','settings':'same','goal_record':None}},
            'interruption_receipts':{'chat':{'turn':'original-chat','settings':'same','relay_interrupted':True}}}
        rows=[{'id':'master','status':'failed','error_kind':'usageLimitExceeded','turn_id':'new-master','goal_status':'active','_goal_record':dict(goal,tokens_used=20)},
              {'id':'chat','status':'interrupted','turn_id':'original-chat','_goal_record':None}]
        for row in rows:row.update(account_key='active',supported=True,_owner='owner',_settings_fingerprint='same')
        self.assertTrue(controller.desktop_restart_superseded_by_usage(op,rows,'active'))
        for changes in ({'status':'interrupted'},{'error_kind':'network_error'},{'_settings_fingerprint':'changed'},{'goal_status':'paused'},{'model_provider':'google-antigravity'}):
            self.assertFalse(controller.desktop_restart_superseded_by_usage(op,[dict(rows[0],**changes),rows[1]],'active'))
        self.assertFalse(controller.desktop_restart_superseded_by_usage(dict(op,phase='desktop-continuation-intent'),rows,'active'))
        self.assertFalse(controller.desktop_restart_superseded_by_usage(op,[rows[0],dict(rows[1],turn_id='manual-stop')],'active'))

    def test_monitor_finishes_delayed_restart_while_native_goal_runs_without_duplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder);(root/'operations').mkdir()
            goal={'goal_id':'goal','status':'active','token_budget':1000,'tokens_used':10,'time_used_seconds':2}
            operation={'id':'delayed','app':'codex','state':'needs-attention','phase':'desktop-open-thread-intent',
                'outgoing':'old','target':'active','sessions':['master','chat'],
                'recovery_context':{'master':{'turn':'old-goal','settings':'same','goal_record':goal},'chat':{'turn':'old-chat','settings':'same','goal_record':None}},
                'interruption_receipts':{'chat':{'turn':'old-chat','settings':'same','relay_interrupted':True}}}
            (root/'operations/delayed.json').write_text(json.dumps(operation))
            rows=[{'id':'master','status':'running','goal_status':'active','turn_id':'new-goal','_goal_record':dict(goal,tokens_used=20)},
                  {'id':'chat','status':'interrupted','turn_id':'old-chat','_goal_record':None}]
            for row in rows:row.update(account_key='active',_owner='owner',supported=True,_settings_fingerprint='same')
            class ResumeAdapter(Adapter):
                def resume_in_place(self,ids,journal,recovery_operation=None):
                    self.resumed=ids
                    self.rows[1].update(status='running',turn_id='new-chat')
                    journal('desktop-session-recovered',{'session':'chat','target':'active','turn':'new-chat'})
                def rotate(self,*_):raise AssertionError('A delayed restart must not restart the working Goal again')
            cfg={'codex':{'enabled':True,'sessions':['master','chat']},'orca':{'enabled':False}}
            accounts=Accounts();accounts.current_desktop_key=lambda:'active'
            with patch.object(controller,'LOCALDATA',root),patch.object(controller,'STATUS',root/'status.json'),patch.object(controller,'JOURNAL',root/'journal.json'),patch.object(controller,'config',return_value=cfg):
                adapter=ResumeAdapter(rows);c=controller.Controller(accounts,{'orca':Adapter([]),'codex':adapter});c.refresh_started=time.time()
                try:
                    c.cycle();self.assertEqual(adapter.resumed,['chat'])
                    self.assertFalse(c.unresolved);self.assertFalse(c.attention)
                    self.assertEqual(c.journal['state'],'complete')
                    self.assertEqual(rows[0]['turn_id'],'new-goal')
                finally:c.refresh_pool.shutdown(wait=True)

    def test_new_quota_failure_releases_old_pause_without_authorizing_manual_stop(self):
        op={'phase':'desktop-interrupt-intent','outgoing':'old','metadata':{'session':'chat','turn':'old-turn','settings':'settings'}}
        row={'id':'chat','account_key':'old','_owner':'owner','status':'failed','error_kind':'usageLimitExceeded',
             'goal_status':'usage_limited','turn_id':'new-turn','_settings_fingerprint':'settings'}
        self.assertTrue(controller.desktop_pause_superseded_by_quota(op,[row],'old'))
        for field,value in [('status','interrupted'),('error_kind','network_error'),('turn_id','old-turn'),
                            ('goal_status','paused'),('_settings_fingerprint','changed'),('waiting',True),('account_key','new')]:
            with self.subTest(field=field):
                self.assertFalse(controller.desktop_pause_superseded_by_quota(op,[dict(row,**{field:value})],'old'))
        self.assertFalse(controller.desktop_pause_superseded_by_quota(op,[row],'new'))
        self.assertFalse(controller.desktop_pause_superseded_by_quota(dict(op,phase='desktop-continuation-intent'),[row],'old'))

    def test_attested_orca_alias_uses_target_account_and_keeps_later_manual_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder);old=root/'old';new=root/'new';old.mkdir();new.mkdir()
            for home in (old,new):(home/'auth.json').write_text('{}')
            source=old/'chat.jsonl';source.write_text('fixture');destination=new/'chat.jsonl'
            __import__('os').link(source,destination)
            base={'id':'chat','path':str(source),'status':'failed','error_kind':'quota','turn_id':'old-turn'}
            latest=dict(base,path=str(destination),status='idle',error_kind=None,turn_id='new-turn')
            class Rpc:
                def call(self,method,params=None):
                    if method=='session.tabs.listAll':return {'snapshots':[{'worktree':'work','tabs':[{'id':'tab','ptyId':'pty','agentStatus':{'agentType':'codex','state':'done','providerSession':{'id':'chat','transcriptPath':str(source)}}}]}]}
                    if method=='terminal.list':return {'terminals':[{'ptyId':'pty','handle':'pane','incarnationId':'inc','connected':True}]}
                    if method=='terminal.read':return {'terminal':{'tail':['Ask Codex']}}
                    raise AssertionError(method)
            with patch.object(orca_adapter,'LOCALDATA',root),patch.object(orca_adapter,'read_transcript',side_effect=lambda p:dict(base if str(p)==str(source) else latest)),patch.object(orca_adapter,'protected_json',side_effect=lambda p:{'key':'old' if pathlib.Path(p).parent==old else 'new'}),patch.object(orca_adapter,'identity',side_effect=lambda x:x),patch.object(orca_adapter,'read_goal',return_value=None):
                a=orca_adapter.OrcaAdapter(Accounts(),Rpc())
                a.bindings={'chat':{'terminal':'pane','incarnation':'inc','source_path':str(source),'home':str(new),'path':str(destination),'key':'new','verified_at':time.time()}}
                row=a.discover()[0]
                self.assertEqual(row['account_key'],'new');self.assertEqual(row['status'],'idle');self.assertEqual(row['turn_id'],'new-turn')
                latest.update(status='interrupted',error_kind='user_interrupt')
                row=a.discover()[0];self.assertFalse(controller.needs_switch(row,{'five_remaining':0},2))
                # A different inode with matching text is insufficient evidence.
                destination.unlink();destination.write_text('fixture')
                self.assertNotEqual(a.discover()[0]['account_key'],'new')

    def test_in_place_recovery_requires_handoff_after_failed_turn(self):
        row={'status':'failed','error_kind':'usageLimitExceeded','account_key':'new','_turn_started_at_ms':1000}
        operation={'app':'codex','state':'complete','target':'new','outgoing':'old','updated_at':2}
        self.assertTrue(controller.quota_failure_before_handoff(row,[operation]))
        self.assertFalse(controller.quota_failure_before_handoff(dict(row,_turn_started_at_ms=3000),[operation]))
        self.assertFalse(controller.quota_failure_before_handoff(dict(row,error_kind='unauthorized'),[operation]))
        self.assertFalse(controller.quota_failure_before_handoff(row,[dict(operation,outgoing='new')]))
        self.assertFalse(controller.quota_failure_before_handoff(row,[dict(operation,state='needs-attention')]))

    def test_monitor_continues_old_codex_failure_on_healthy_switched_account(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder);(root/'operations').mkdir()
            op={'id':'handoff','app':'codex','state':'complete','target':'active','outgoing':'old','updated_at':2}
            (root/'operations/handoff.json').write_text(json.dumps(op))
            cfg={'codex':{'enabled':True,'sessions':['chat'],'preserve_running':True},'orca':{'enabled':False}}
            rows=[{'id':'chat','status':'failed','error_kind':'usageLimitExceeded','account_key':'active','_turn_started_at_ms':1000},
                  {'id':'sibling','status':'running','account_key':'active'}]
            class ResumeAdapter(Adapter):
                def resume_in_place(self,ids,journal):self.resumed=ids
                def rotate(self,*_):raise AssertionError('healthy running Goals must not be restarted')
            with patch.object(controller,'LOCALDATA',root),patch.object(controller,'STATUS',root/'status.json'),patch.object(controller,'JOURNAL',root/'journal.json'),patch.object(controller,'config',return_value=cfg):
                adapter=ResumeAdapter(rows);c=controller.Controller(Accounts(),{'orca':Adapter([]),'codex':adapter})
                try:
                    c.cycle();self.assertEqual(adapter.resumed,['chat'])
                    self.assertEqual(c.journal['state'],'complete');self.assertEqual(c.journal['outgoing'],c.journal['target'])
                finally:c.refresh_pool.shutdown(wait=True)

    def test_delayed_desktop_recovery_requires_original_model_goal_and_new_turn(self):
        goal={'goal_id':'original-goal','status':'usage_limited','token_budget':1000,'tokens_used':73,
              'time_used_seconds':12,'created_at_ms':1,'deferred':False}
        operation={'target':'new','sessions':['chat'],'recovery_context':{
            'chat':{'turn':'old-turn','settings':'exact-model-settings','goal_record':goal}}}
        row={'id':'chat','account_key':'new','_owner':'owner','waiting':False,'status':'running','turn_id':'new-turn',
             '_settings_fingerprint':'exact-model-settings','_goal_record':dict(goal,status='active',tokens_used=100)}
        self.assertTrue(controller.desktop_recovery_confirmed(operation,[row],'new'))
        for field,value in [('turn_id','old-turn'),('_settings_fingerprint','different-model'),('account_key','wrong'),('waiting',True)]:
            with self.subTest(field=field):
                self.assertFalse(controller.desktop_recovery_confirmed(operation,[dict(row,**{field:value})],'new'))
        changed=dict(row,_goal_record=dict(goal,status='active',goal_id='different-goal'))
        self.assertFalse(controller.desktop_recovery_confirmed(operation,[changed],'new'))
        self.assertFalse(controller.desktop_recovery_confirmed(operation,[],'new'))
        operation.update(phase='desktop-continuation-intent',metadata={'session':'chat','operation':'exact-message'})
        self.assertFalse(controller.desktop_recovery_confirmed(operation,[row],'new'))
        row['_client_user_message_id']='exact-message'
        self.assertTrue(controller.desktop_recovery_confirmed(operation,[row],'new'))

    def test_confirmed_pause_survives_failed_normal_quit_and_controller_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder);(root/'operations').mkdir()
            operation={'id':'op','state':'reconciled-before-continuation','app':'codex','phase':'desktop-close-intent','outgoing':'active',
                'metadata':{'originals':{'proxy':{'turn':'turn','settings':'settings','goal':'active','relay_interrupted':True}}}}
            (root/'operations/op.json').write_text(json.dumps(operation))
            cfg={'orca':{'enabled':False},'codex':{'enabled':False}}
            row={'id':'proxy','status':'interrupted','turn_id':'turn','_settings_fingerprint':'settings','goal_status':'active','account_key':'active'}
            with patch.object(controller,'LOCALDATA',root), patch.object(controller,'STATUS',root/'status.json'), patch.object(controller,'JOURNAL',root/'journal.json'), patch.object(controller,'config',return_value=cfg):
                adapter=Adapter([row]);c=controller.Controller(Accounts(),{'orca':Adapter([]),'codex':adapter})
                try:
                    c.cycle();self.assertEqual(adapter.recovery_receipts,{'proxy':('turn','settings')})
                    row['turn_id']='user-changed-turn';adapter.recovery_receipts.clear()
                    c.cycle();self.assertEqual(adapter.recovery_receipts,{})
                finally:c.refresh_pool.shutdown(wait=True)

    def test_rejected_local_write_never_sends_enter(self):
        class Rpc:
            def __init__(self):self.calls=[]
            def call(self,method,params):self.calls.append(params);return {'send':{'accepted':False}}
        rpc=Rpc();adapter=orca_adapter.OrcaAdapter(Accounts(),rpc)
        with self.assertRaises(RuntimeError):adapter.submit_local('terminal','incarnation','/quit')
        self.assertEqual(len(rpc.calls),1);self.assertNotIn('enter',rpc.calls[0])
    def test_failed_orca_chat_does_not_block_healthy_sibling(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder);(root/'operations').mkdir()
            operation={'id':'op','state':'needs-attention','app':'orca','phase':'quit-codex','sessions':['broken'],'target':'target','metadata':{'terminal':'old'}}
            (root/'operations/op.json').write_text(json.dumps(operation))
            cfg={'orca':{'enabled':True,'sessions':['broken','healthy']},'codex':{'enabled':False}}
            rows=[{'id':'healthy','status':'running','account_key':'active','home':'C:/active'}]
            with patch.object(controller,'LOCALDATA',root), patch.object(controller,'STATUS',root/'status.json'), patch.object(controller,'JOURNAL',root/'journal.json'), patch.object(controller,'config',return_value=cfg):
                accounts=Accounts();c=controller.Controller(accounts,{'orca':Adapter(rows),'codex':Adapter([])})
                try:
                    c.cycle();self.assertIn('active',accounts.calls)
                    self.assertEqual(json.loads((root/'status.json').read_text())['messages']['orca'],'Monitoring 1 selected chat(s)')
                finally:c.refresh_pool.shutdown(wait=True)

    def test_exit_prompt_ignores_blank_terminal_rows(self):
        self.assertTrue(orca_adapter.shell_exit_confirmed({'source':'screen','tail':['codex resume chat','PS C:\\work>','','']},'chat'))
        self.assertTrue(orca_adapter.shell_exit_confirmed({'source':'screen','tail':['Session ID: chat','PS C:\\work>']},'chat'))
        self.assertFalse(orca_adapter.shell_exit_confirmed({'source':'screen','tail':['codex resume chat','Ask Codex','']},'chat'))

    def test_background_invalid_login_does_not_block_active_chat(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder);gate=threading.Event();accounts=Accounts(gate)
            cfg={'threshold':2,'orca':{'enabled':True,'sessions':['chat']},'codex':{'enabled':False,'sessions':[]}}
            with patch.object(controller,'LOCALDATA',root), patch.object(controller,'STATUS',root/'status.json'), patch.object(controller,'JOURNAL',root/'journal.json'), patch.object(controller,'config',return_value=cfg):
                c=controller.Controller(accounts,{'orca':Adapter([{'id':'chat','status':'running','account_key':'active','home':'C:/active'}]),'codex':Adapter([])})
                try:
                    started=time.monotonic();c.cycle()
                    self.assertLess(time.monotonic()-started,1)
                    self.assertIn('active',accounts.calls)
                    self.assertTrue(c.refresh_futures)
                    self.assertEqual(json.loads((root/'status.json').read_text())['messages']['orca'],'Monitoring 1 selected chat(s)')
                finally:gate.set();c.refresh_pool.shutdown(wait=True)

    def test_crash_before_interrupt_delivery_cannot_resume_user_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder);(root/'operations').mkdir()
            operation={'id':'op','state':'in-progress','app':'codex','phase':'desktop-interrupt-intent',
                'metadata':{'session':'chat','turn':'turn','settings':'settings','account':'active'}}
            (root/'operations/op.json').write_text(json.dumps(operation))
            cfg={'orca':{'enabled':False},'codex':{'enabled':True,'sessions':['chat']}}
            row={'id':'chat','status':'interrupted','turn_id':'turn','_settings_fingerprint':'settings','goal_status':'active','account_key':'active'}
            with patch.object(controller,'LOCALDATA',root), patch.object(controller,'STATUS',root/'status.json'), patch.object(controller,'JOURNAL',root/'journal.json'), patch.object(controller,'config',return_value=cfg):
                adapter=Adapter([row]);c=controller.Controller(Accounts(),{'orca':Adapter([]),'codex':adapter})
                try:
                    c.cycle();self.assertEqual(adapter.recovery_receipts,{})
                    self.assertIn('codex',c.attention)
                    self.assertEqual(json.loads((root/'operations/op.json').read_text())['state'],'in-progress')
                finally:c.refresh_pool.shutdown(wait=True)

    def test_orca_old_quota_banner_cannot_override_later_user_interrupt(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder);(root/'auth.json').write_text('{}');path=root/'sessions/chat.jsonl';path.parent.mkdir();path.write_text('')
            class Rpc:
                def call(self,method,params=None):
                    if method=='session.tabs.listAll':return {'snapshots':[{'worktree':'work','tabs':[{'id':'tab','ptyId':'pty','agentStatus':{'agentType':'codex','state':'working','providerSession':{'id':'chat','transcriptPath':str(path)}}}]}]}
                    if method=='terminal.list':return {'terminals':[{'ptyId':'pty','handle':'handle','incarnationId':'inc','connected':True}]}
                    if method=='terminal.read':return {'terminal':{'tail':["You've hit your usage limit"]}}
                    raise AssertionError(method)
            info={'id':'chat','cwd':'C:/work','path':str(path),'status':'interrupted','error_kind':'user_interrupt','turn_id':'turn'}
            with patch.object(orca_adapter,'LOCALDATA',root), patch.object(orca_adapter,'read_transcript',return_value=info), patch.object(orca_adapter,'identity',return_value={'key':'account'}), patch.object(orca_adapter,'protected_json',return_value={}), patch.object(orca_adapter,'read_goal',return_value=None):
                a=orca_adapter.OrcaAdapter(Accounts(),Rpc());row=a.discover()[0]
                self.assertEqual(row['status'],'interrupted');self.assertEqual(row['error_kind'],'user_interrupt')
                self.assertFalse(controller.needs_switch(row,{'five_remaining':0,'weekly_remaining':80},2))
                # Cached "working" cannot erase an authoritative current error.
                for kind in ('quota','login'):
                    info.update(status='failed',error_kind=kind)
                    Rpc.call_original=Rpc.call
                    def clean_screen(rpc,method,params=None):
                        if method=='terminal.read':return {'terminal':{'tail':['Ask Codex']}}
                        return Rpc.call_original(rpc,method,params)
                    with patch.object(Rpc,'call',clean_screen):
                        row=a.discover()[0]
                        self.assertEqual(row['status'],'failed');self.assertEqual(row['error_kind'],kind)
                        self.assertTrue(controller.needs_switch(row,{'five_remaining':80,'weekly_remaining':80},2))

if __name__=='__main__':unittest.main()

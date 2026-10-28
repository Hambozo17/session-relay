import copy,pathlib,tempfile,unittest
from unittest.mock import patch
from auto_continue import AutoContinue
from controller import available,quota_failed_target_keys,needs_switch
from desktop_adapter import DesktopUncertainError,DesktopStoppedStateError


class Accounts:
    def __init__(self):self.q={'valid':True,'plan':'plus','five_remaining':80,'weekly_remaining':80,'_allowance_generation':7}
    def current_desktop_key(self):return 'account'
    def find(self,key):return {'key':key} if key=='account' else None
    def probe_desktop(self,record,force):return self.q


class Native:
    def __init__(self):self.calls=[];self.error=None
    def continue_stopped(self,row,message_id):
        self.calls.append((row['turn_id'],message_id))
        if self.error:raise self.error
        return {'turn_id':'new-'+str(len(self.calls)),'client_user_message_id':message_id,'settings_fingerprint':'same'}


def chat(**changes):
    row={'id':'chat','status':'idle','_runtime_status':'idle','_owner':'owner','turn_id':'old',
        '_settings_fingerprint':'same','supported':True,'account_key':'account','goal_status':None}
    row.update(changes);return row


class ContinueTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.path=pathlib.Path(self.folder.name)/'auto.json'
        self.accounts=Accounts();self.native=Native();self.cfg={'enabled':True,'continue_on_stop':True,'sessions':['chat']}
    def tearDown(self):self.folder.cleanup()
    def drive(self,manager,row):return manager.drive([row],self.cfg,self.accounts,self.native,2,set(),available)

    def test_no_goal_and_blocked_work_continue_without_changing_goal_data(self):
        for goal in (None,'blocked','complete'):
            with self.subTest(goal=goal):
                path=self.path.with_name(str(goal)+'.json');manager=AutoContinue(path);row=chat(goal_status=goal,_goal_record={'status':goal,'token_budget':None})
                original=copy.deepcopy(row);self.assertEqual(self.drive(manager,row),1);self.assertEqual(row,original)

    def test_running_manual_stop_approval_pause_and_foreign_provider_are_preserved(self):
        for changed in ({'status':'running'},{'status':'interrupted'},{'waiting':True},{'_goal_deferred':True},
                {'goal_status':'paused'},{'_goal_mismatch':True},{'is_child':True},{'_ephemeral':True},
                {'model_provider':'google-antigravity'},{'supported':False},{'_owner':None},
                {'_runtime_status':'active'},{'error_kind':'quota','status':'failed'}):
            with self.subTest(changed=changed):self.assertEqual(self.drive(AutoContinue(self.path),chat(**changed)),0)
        self.assertEqual(self.native.calls,[])

    def test_unknown_delivery_survives_restart_until_exact_native_message_is_seen(self):
        now=[1000.0]
        with patch('auto_continue.time.time',side_effect=lambda:now[0]):
            manager=AutoContinue(self.path);self.native.error=DesktopUncertainError('unknown')
            self.drive(manager,chat());self.assertEqual(manager.pending_ids(),{'chat'})
            restarted=AutoContinue(self.path);now[0]+=400;self.drive(restarted,chat());self.assertEqual(len(self.native.calls),1)
            receipt=restarted.data['sessions']['chat'];wrong=chat(turn_id='new',_client_user_message_id='other')
            restarted.reconcile([wrong],'account');self.assertEqual(restarted.pending_ids(),{'chat'})
            accepted=chat(turn_id='new',_client_user_message_id=receipt['message_id'],status='running')
            restarted.reconcile([accepted],'account');self.assertEqual(restarted.pending_ids(),set())
            self.drive(restarted,accepted);self.assertEqual(len(self.native.calls),1)

    def test_safe_preflight_rejection_can_retry_after_three_minutes(self):
        now=[1000.0]
        with patch('auto_continue.time.time',side_effect=lambda:now[0]):
            manager=AutoContinue(self.path);self.native.error=DesktopStoppedStateError('changed')
            self.drive(manager,chat());self.assertFalse(manager.pending_ids())
            now[0]+=179;self.drive(manager,chat());self.assertEqual(len(self.native.calls),1)
            now[0]+=1;self.native.error=None;self.assertEqual(self.drive(manager,chat()),1)
            self.assertEqual(len(self.native.calls),2)

    def test_confirmed_new_stopped_turn_waits_three_minutes_between_messages(self):
        now=[1000.0]
        with patch('auto_continue.time.time',side_effect=lambda:now[0]):
            manager=AutoContinue(self.path);self.drive(manager,chat())
            now[0]+=179;self.drive(manager,chat(turn_id='new-1'));self.assertEqual(len(self.native.calls),1)
            now[0]+=1;self.drive(manager,chat(turn_id='new-1'));self.assertEqual(len(self.native.calls),2)

    def test_owned_quota_failure_bans_only_its_allowance_window(self):
        manager=AutoContinue(self.path);self.drive(manager,chat());receipt=manager.data['sessions']['chat']
        failed=chat(status='failed',turn_id='new-1',_client_user_message_id=receipt['message_id'],error_kind='quota')
        manager.reconcile([failed],'account');manager.reconcile([failed],'account')
        self.assertEqual(len(manager.failures()),1)
        self.assertEqual(quota_failed_target_keys('codex',[failed],manager.failures(),{'account':self.accounts.q}),{'account'})
        self.accounts.q['_allowance_generation']=8
        self.assertEqual(quota_failed_target_keys('codex',[failed],manager.failures(),{'account':self.accounts.q}),set())

    def test_quota_recovery_confirmation_shares_cooldown_after_monitor_restart(self):
        import controller
        class RecoveringAccounts(Accounts):
            def __init__(self):
                super().__init__();self.records=[{'key':'account','home':'saved','label':'saved','sources':[]}]
                self.quotas={'account':self.q}
            def inventory(self):return self.records
            def probe(self,*args,**kwargs):return self.q
        class Adapter(Native):
            def __init__(self,rows):
                super().__init__();self.rows=rows;self.recovery_receipts={};self.recoveries=0
            def discover(self):return copy.deepcopy(self.rows)
            def resume_in_place(self,ids,callback):return self.rotate(ids,{'key':'account'},callback)
            def rotate(self,ids,target,callback,**kwargs):
                self.recoveries+=1
                self.rows[0].update(status='idle',error_kind=None,turn_id='quota-recovered',_runtime_status='idle')
                callback('desktop-session-recovered',{'session':'chat','target':'account','turn':'quota-recovered'})
        root=self.path.parent;cfg={'codex':self.cfg,'orca':{'enabled':False}}
        accounts=RecoveringAccounts();native=Adapter([chat(status='failed',error_kind='quota',_continue_on_stop=True)])
        now=[1000.0]
        with patch.object(controller,'LOCALDATA',root),patch.object(controller,'STATUS',root/'status.json'),patch.object(controller,'JOURNAL',root/'journal.json'),patch.object(controller,'config',return_value=cfg),patch.object(controller.Controller,'refresh_accounts',return_value=None),patch('auto_continue.time.time',side_effect=lambda:now[0]):
            first=controller.Controller(accounts,{'codex':native,'orca':Adapter([])})
            try:
                first.cycle();self.assertEqual(native.recoveries,1)
                self.assertEqual(first.journal['recovered_at']['chat'],1000)
            finally:first.refresh_pool.shutdown(wait=True)
            restarted=controller.Controller(accounts,{'codex':native,'orca':Adapter([])})
            try:
                now[0]=1005;restarted.cycle();self.assertEqual(native.calls,[])
                now[0]=1179;restarted.cycle();self.assertEqual(native.calls,[])
                now[0]=1180;restarted.cycle();self.assertEqual(len(native.calls),1)
            finally:restarted.refresh_pool.shutdown(wait=True)

    def test_old_and_partially_confirmed_recovery_journals_share_cooldown(self):
        for operation in ({'state':'complete','sessions':['chat']},
                {'state':'needs-attention','sessions':['chat','other'],'recovered_sessions':['chat']}):
            with self.subTest(operation=operation):
                manager=AutoContinue(self.path);operation.update(app='codex',updated_at=1000)
                with patch('auto_continue.time.time',return_value=1005):
                    self.assertEqual(manager.drive([chat()],self.cfg,self.accounts,self.native,2,set(),available,[operation]),0)
        self.assertEqual(self.native.calls,[])

    def test_disabled_selection_and_depleted_account_do_not_send(self):
        manager=AutoContinue(self.path);self.cfg['continue_on_stop']=False;self.drive(manager,chat())
        self.cfg['continue_on_stop']=True;self.cfg['sessions']=[];self.drive(manager,chat())
        self.cfg['sessions']=['chat'];self.accounts.q['five_remaining']=0;self.drive(manager,chat())
        self.assertEqual(self.native.calls,[])

    def test_corrupt_receipts_fail_closed(self):
        self.path.write_text('{broken');manager=AutoContinue(self.path)
        self.assertTrue(manager.invalid);self.drive(manager,chat());self.assertEqual(self.native.calls,[])

    def test_no_goal_stopped_chat_uses_other_allowance_when_current_account_is_depleted(self):
        quota={'valid':True,'five_remaining':0,'weekly_remaining':50}
        self.assertFalse(needs_switch(chat(),quota,2))
        self.assertTrue(needs_switch(chat(_continue_on_stop=True),quota,2))
        self.assertTrue(needs_switch(chat(status='failed',error_kind='quota',goal_status='blocked',_continue_on_stop=True),quota,2))
        self.assertFalse(needs_switch(chat(goal_status='paused',_continue_on_stop=True),quota,2))

    def test_waiting_for_all_account_resets_keeps_no_goal_monitor_awake(self):
        import controller
        class EmptyAccounts(Accounts):
            def __init__(self):
                super().__init__();self.q['five_remaining']=0
                self.records=[{'key':'account','home':'saved','label':'saved','sources':[]}];self.quotas={'account':self.q}
            def inventory(self):return self.records
            def probe(self,*args,**kwargs):return self.q
        class Adapter:
            def __init__(self,rows):self.rows=rows;self.recovery_receipts={}
            def discover(self):return self.rows
        root=self.path.parent;cfg={'codex':self.cfg,'orca':{'enabled':False}}
        with patch.object(controller,'LOCALDATA',root),patch.object(controller,'STATUS',root/'status.json'),patch.object(controller,'JOURNAL',root/'journal.json'),patch.object(controller,'config',return_value=cfg),patch.object(controller.Controller,'refresh_accounts',return_value=None):
            c=controller.Controller(EmptyAccounts(),{'codex':Adapter([chat(_continue_on_stop=True)]),'orca':Adapter([])})
            try:c.cycle();self.assertTrue(c.keep_awake)
            finally:c.refresh_pool.shutdown(wait=True)

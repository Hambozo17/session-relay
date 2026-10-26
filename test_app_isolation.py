import json
import pathlib
import tempfile
import time
import unittest
from unittest.mock import patch
import accounts
import controller
import orca_adapter
from local_rpc import RpcError


GOOD={'valid':True,'five_remaining':80,'weekly_remaining':90}
BAD={'valid':True,'five_remaining':0,'weekly_remaining':90}


class DestinationAccounts:
    def __init__(self, fresh=None):
        self.records=[{'key':key,'home':'saved/'+key,'orca_home':'orca/'+key,'orca_id':key,
            'label':key,'sources':['Orca']} for key in ('outgoing','separate','shared')]
        self.quotas={r['key']:dict(GOOD) for r in self.records};self.fresh=fresh or {};self.calls=[]
    def probe(self,r,force=False,home=None):self.calls.append(r['key']);return self.fresh.get(r['key'],self.quotas[r['key']])
    def inventory(self):return self.records
    def find(self,key):return next((r for r in self.records if r['key']==key),None)
    def current_desktop_key(self):return 'shared'


class Isolation(unittest.TestCase):
    def test_five_hour_tier_including_shared_precedes_separate_weekly(self):
        m=DestinationAccounts();m.quotas['separate']=dict(GOOD,five_remaining=None,weekly_only=True)
        target,shared=controller.choose_destination(m,'orca','outgoing',{'shared'},set(),2)
        self.assertEqual(target['key'],'shared');self.assertTrue(shared)
        self.assertEqual(m.calls,['shared'])

    def test_weekly_fallback_only_after_fresh_five_hour_checks_fail(self):
        m=DestinationAccounts({'shared':BAD});m.quotas['separate']=dict(GOOD,five_remaining=None,weekly_only=True)
        target,shared=controller.choose_destination(m,'codex','outgoing',{'shared'},set(),2)
        self.assertEqual(target['key'],'separate');self.assertFalse(shared)
        self.assertEqual(m.calls,['shared','separate'])
        m.calls=[]
        target,shared=controller.choose_destination(m,'orca','outgoing',{'shared'},set(),2,five_hour_only=True)
        self.assertIsNone(target);self.assertFalse(shared)
        self.assertNotIn('separate',m.calls)

    def test_fresh_tier_change_cannot_promote_weekly_ahead_of_five_hour(self):
        weekly=dict(GOOD,five_remaining=None,weekly_only=True)
        m=DestinationAccounts({'separate':weekly})
        target,_=controller.choose_destination(m,'codex','outgoing',set(),set(),2)
        self.assertEqual(target['key'],'shared');self.assertEqual(m.calls,['separate','shared'])

    def test_free_candidates_never_probed_or_selected(self):
        m=DestinationAccounts();m.quotas['separate']=dict(GOOD,plan='free')
        target,_=controller.choose_destination(m,'orca','outgoing',set(),set(),2)
        self.assertEqual(target['key'],'shared');self.assertEqual(m.calls,['shared'])

    def test_reset_due_five_hour_is_rechecked_before_weekly_fallback(self):
        m=DestinationAccounts({'shared':GOOD});now=time.time()
        m.quotas['shared']=dict(BAD,updated_at=now-20,reset_at=now-10)
        m.quotas['separate']=dict(GOOD,five_remaining=None,weekly_only=True)
        target,_=controller.choose_destination(m,'orca','outgoing',{'shared'},set(),2)
        self.assertEqual(target['key'],'shared');self.assertEqual(m.calls,['shared'])
        # A post-reset response still showing depletion waits for refresh.
        q=dict(BAD,updated_at=now,reset_at=now-10)
        self.assertFalse(controller.destination_needs_check(q,2))

    def test_weekly_returns_only_working_selected_sessions_to_fresh_five_hour(self):
        class Adapter:
            recovery_receipts={}
            def __init__(self,rows):self.rows=rows;self.switches=[]
            def discover(self):return self.rows
            def rotate(self,ids,target,journal):self.switches.append((ids,target['key']))
        weekly=dict(GOOD,five_remaining=None,weekly_only=True)
        for recovered in (True,False):
            with self.subTest(recovered=recovered),tempfile.TemporaryDirectory() as folder:
                root=pathlib.Path(folder)
                m=DestinationAccounts({'outgoing':weekly,'separate':GOOD if recovered else BAD,'shared':BAD})
                m.quotas['outgoing']=weekly
                rows=[dict(id=sid,account_key='outgoing',status=status,goal_status=goal,model_provider=provider,model='unchanged')
                    for sid,status,goal,provider in [('working','running','active','openai'),('idle','idle','active','openai'),
                        ('manual','interrupted','active','openai'),('paused','running','paused','openai'),
                        ('gemini','running','active','google-antigravity')]]
                orca=Adapter(rows);desktop=Adapter([])
                cfg={'orca':{'enabled':True,'sessions':[r['id'] for r in rows]},'codex':{'enabled':False},'included_resets':True}
                with patch.object(controller,'LOCALDATA',root),patch.object(controller,'STATUS',root/'status.json'),patch.object(controller,'JOURNAL',root/'journal.json'),patch.object(controller,'config',return_value=cfg),patch('reset_bank.restore_allowance') as reset:
                    c=controller.Controller(m,{'orca':orca,'codex':desktop});c.refresh_started=time.time()
                    try:
                        before=json.dumps(rows,sort_keys=True);c.cycle()
                        self.assertEqual(orca.switches,[(['working'],'separate')] if recovered else [])
                        self.assertEqual(desktop.switches,[]);reset.assert_not_called()
                        self.assertEqual(before,json.dumps(rows,sort_keys=True))
                    finally:c.refresh_pool.shutdown(wait=True)

    def test_every_selected_orca_chat_recovers_independently_with_desktop_unavailable(self):
        class ClosedDesktop:
            recovery_receipts={}
            def discover(self):raise RuntimeError('Desktop unavailable')
            def rotate(self,*args):raise AssertionError('Orca recovery must not mutate Desktop')
        class Orca:
            recovery_receipts={}
            def __init__(self,rows):self.rows=rows;self.switches=[]
            def discover(self):return self.rows
            def rotate(self,ids,target,journal):self.switches.append((ids,target['key']))
        rows=[dict(id=sid,status=status,goal_status=goal,error_kind=error,account_key='outgoing',model='user-model')
            for sid,status,goal,error in (
                ('goal','failed','usage_limited','quota'),
                ('quota','failed',None,'quota'),
                ('login','failed',None,'login'),
                ('threshold','running',None,None),
                ('manual','interrupted','active',None),
                ('paused','failed','paused','quota'),
                ('blocked','failed','blocked','quota'),
                ('unselected','failed',None,'quota'))]
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder);m=DestinationAccounts({'outgoing':BAD});orca=Orca(rows)
            cfg={'orca':{'enabled':True,'sessions':[s['id'] for s in rows if s['id']!='unselected']},
                 'codex':{'enabled':False}}
            with patch.object(controller,'LOCALDATA',root),patch.object(controller,'STATUS',root/'status.json'),patch.object(controller,'JOURNAL',root/'journal.json'),patch.object(controller,'config',return_value=cfg):
                c=controller.Controller(m,{'orca':orca,'codex':ClosedDesktop()});c.refresh_started=time.time()
                try:
                    original=json.dumps(rows,sort_keys=True);c.cycle()
                    self.assertEqual(orca.switches,[(['goal','quota','login','threshold'],'separate')])
                    self.assertEqual(json.dumps(rows,sort_keys=True),original)
                    self.assertEqual(c.journal['state'],'complete')
                    self.assertTrue(c.keep_awake)
                finally:c.refresh_pool.shutdown(wait=True)

    def test_codex_recovers_with_orca_closed_but_not_with_unknown_live_ownership(self):
        class UnavailableOrca:
            def __init__(self,running):self.running=running
            def call(self,*_args,**_kwargs):raise RpcError('unavailable','runtime closed')
            def application_running(self):return self.running
        class Desktop:
            recovery_receipts={}
            def __init__(self):self.switches=[]
            def discover(self):return [{'id':'master','status':'failed','error_kind':'quota',
                'goal_status':'usage_limited','account_key':'outgoing'}]
            def rotate(self,ids,target,journal):self.switches.append((ids,target['key']))
        for running in (False,True,None):
            with self.subTest(running=running),tempfile.TemporaryDirectory() as folder:
                root=pathlib.Path(folder);m=DestinationAccounts({'outgoing':BAD});desktop=Desktop()
                cfg={'codex':{'enabled':True,'sessions':['master']},'orca':{'enabled':False}}
                with patch.object(controller,'LOCALDATA',root),patch.object(controller,'STATUS',root/'status.json'),patch.object(controller,'JOURNAL',root/'journal.json'),patch.object(controller,'config',return_value=cfg),patch.object(orca_adapter,'LOCALDATA',root):
                    other=orca_adapter.OrcaAdapter(m,UnavailableOrca(running))
                    c=controller.Controller(m,{'orca':other,'codex':desktop});c.refresh_started=time.time()
                    try:
                        c.cycle()
                        self.assertEqual(desktop.switches,[(['master'],'separate')] if running is False else [])
                        self.assertTrue(c.keep_awake)
                    finally:c.refresh_pool.shutdown(wait=True)

    def test_saved_orca_inventory_survives_a_restart_with_orca_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder);appdata=root/'appdata';relay=root/'relay'
            home=appdata/'orca/codex-accounts/profile/home';home.mkdir(parents=True)
            accounts.atomic_json(home/'auth.json',{'key':'saved','account_id':'id','label':'saved'})
            class Rpc:
                closed=False
                def call(self,*_args,**_kwargs):
                    if self.closed:raise RpcError('unavailable','closed')
                    return {'codex':{'accounts':[{'id':'profile','managedHomeRuntime':'host'}]}}
            rpc=Rpc()
            with patch.object(accounts,'APPDATA',appdata),patch.object(accounts,'LOCALDATA',relay),patch.object(accounts,'identity',side_effect=lambda a:a):
                self.assertEqual(accounts.AccountManager(rpc).inventory()[0]['key'],'saved')
                rpc.closed=True
                self.assertEqual(accounts.AccountManager(rpc).inventory()[0]['key'],'saved')
                (home/'auth.json').unlink()
                self.assertEqual(accounts.AccountManager(rpc).inventory(),[])

    def test_prefers_separate_account_even_when_shared_has_more_allowance(self):
        m=DestinationAccounts();m.quotas['shared']['five_remaining']=100
        target,shared=controller.choose_destination(m,'codex','outgoing',{'shared'},set(),2)
        self.assertEqual(target['key'],'separate');self.assertFalse(shared);self.assertEqual(m.calls,['separate'])

    def test_last_usable_shared_account_is_freshly_checked(self):
        m=DestinationAccounts({'separate':BAD})
        target,shared=controller.choose_destination(m,'orca','outgoing',{'shared'},set(),2)
        self.assertEqual(target['key'],'shared');self.assertTrue(shared);self.assertEqual(m.calls,['separate','shared'])

    def test_unknown_ownership_and_failed_shared_check_never_qualify(self):
        for uncertain,fresh in [({'shared'},{'separate':BAD}), (set(),{'separate':BAD,'shared':BAD})]:
            m=DestinationAccounts(fresh)
            target,shared=controller.choose_destination(m,'orca','outgoing',{'shared'},uncertain,2)
            self.assertIsNone(target);self.assertFalse(shared)

    def test_switch_mutates_only_the_affected_app_with_multiple_other_chats(self):
        class Adapter:
            def __init__(self,rows):self.rows=rows;self.switches=[];self.recovery_receipts={}
            def discover(self):return self.rows
            def rotate(self,ids,target,journal):self.switches.append((ids,target['key']))
        for affected_app in ('codex','orca'):
            with self.subTest(app=affected_app),tempfile.TemporaryDirectory() as folder:
                root=pathlib.Path(folder);other='orca' if affected_app=='codex' else 'codex'
                failed={'id':'failed','status':'failed','error_kind':'quota','account_key':'outgoing','home':'saved/outgoing'}
                healthy=[{'id':str(i),'status':'running','account_key':'shared','home':'saved/shared','model':'user-model'} for i in range(3)]
                adapters={affected_app:Adapter([failed]),other:Adapter(healthy)}
                cfg={affected_app:{'enabled':True,'sessions':['failed']},other:{'enabled':True,'sessions':['0','1','2']}}
                m=DestinationAccounts();m.fresh['outgoing']=BAD
                with patch.object(controller,'LOCALDATA',root),patch.object(controller,'STATUS',root/'status.json'),patch.object(controller,'JOURNAL',root/'journal.json'),patch.object(controller,'config',return_value=cfg):
                    c=controller.Controller(m,adapters);c.refresh_started=time.time()
                    try:
                        original=json.dumps(healthy,sort_keys=True);c.cycle()
                        self.assertEqual(adapters[affected_app].switches,[(['failed'],'separate')])
                        self.assertEqual(adapters[other].switches,[]);self.assertEqual(json.dumps(healthy,sort_keys=True),original)
                    finally:c.refresh_pool.shutdown(wait=True)

    def test_desktop_activation_and_destination_probe_never_write_orca(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder);desktop=root/'desktop';desktop.mkdir();old=root/'orca-old';new=root/'orca-new'
            for p in (old,new):p.mkdir()
            accounts.atomic_json(old/'auth.json',{'key':'old','last_refresh':'1'})
            accounts.atomic_json(new/'auth.json',{'key':'new','last_refresh':'1'})
            accounts.atomic_json(desktop/'auth.json',{'key':'old','last_refresh':'2'})
            before={p:(p/'auth.json').read_bytes() for p in (old,new)}
            class NoOrcaMutations:
                def call(self,*_args,**_kwargs):raise AssertionError('Desktop must not call Orca')
            with patch.object(accounts,'LOCALDATA',root/'relay'),patch.object(accounts,'REAL_HOME',desktop),patch.object(accounts,'identity',side_effect=lambda auth:{'key':auth['key']}),patch.object(accounts,'protect_backup',side_effect=lambda p,raw:accounts.atomic_json(p,json.loads(raw))):
                m=accounts.AccountManager(NoOrcaMutations())
                target={'key':'new','home':str(new),'homes':[str(new)]};m.records=[target]
                self.assertEqual(m.activate_desktop(target),'new')
                self.assertEqual(accounts.load_json(root/'relay/desktop-auth/old.json')['last_refresh'],'2')
                def probe(record,force,home):
                    self.assertEqual(home,root/'relay/desktop-probes/new')
                    accounts.atomic_json(home/'auth.json',{'key':'new','last_refresh':'3'})
                    return dict(GOOD)
                with patch.object(m,'_probe',side_effect=probe):self.assertTrue(m.probe_desktop(target)['valid'])
                self.assertEqual(m._desktop_auth(target)['last_refresh'],'3')
            for p,raw in before.items():self.assertEqual((p/'auth.json').read_bytes(),raw)


if __name__=='__main__':unittest.main()

import unittest
import pathlib
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
from accounts import atomic_json, load_json
from reset_bank import expected_scope, all_weekly_exhausted, restore_allowance

class ResetScope(unittest.TestCase):
    def test_scope_matches_native_javascript_serialization(self):
        snapshot={'codex':{'activeAccountId':'a','accounts':[{'id':'a','updatedAt':123}]},
                  'rateLimits':{'codex':{'provider':'codex','status':'ok','updatedAt':456,
                    'session':{'usedPercent':100.0,'windowMinutes':300,'resetsAt':1000},'weekly':None,
                    'rateLimitResetCredits':{'availableCount':1,'credits':[{'status':'available','expiresAt':2000,'grantedAt':500}]}}}}
        scope=expected_scope(snapshot,'a')
        self.assertEqual(scope['offerRevision'],'v1:[1,null,null,[["available",2000,500]],[100,300,1000],null,456]')
        snapshot['codex']['activeAccountId']='other';self.assertIsNone(expected_scope(snapshot,'a'))

class WeeklyPolicy(unittest.TestCase):
    def setUp(self):
        self.records=[{'key':'a'},{'key':'other-app'}]
        self.quotas={r['key']:{'valid':True,'weekly_remaining':0,'five_remaining':0,'updated_at':1000} for r in self.records}

    def test_every_account_weekly_zero_allows_reset(self):
        self.assertTrue(all_weekly_exhausted(self.records,self.quotas,now=1001))

    def test_five_hour_zero_with_weekly_available_never_allows_reset(self):
        self.quotas['a']['weekly_remaining']=39
        self.assertFalse(all_weekly_exhausted(self.records,self.quotas,now=1001))

    def test_two_percent_weekly_is_still_available(self):
        self.quotas['other-app']['weekly_remaining']=2
        self.assertFalse(all_weekly_exhausted(self.records,self.quotas,now=1001))

    def test_reserved_account_is_included_in_weekly_rule(self):
        self.quotas['other-app']['weekly_remaining']=84
        self.assertFalse(all_weekly_exhausted(self.records,self.quotas,now=1001))

    def test_unknown_invalid_and_stale_quota_cannot_authorize_reset(self):
        for field,value in [('valid',False),('updated_at',0),('weekly_remaining',None),('weekly_remaining',float('nan'))]:
            with self.subTest(field=field,value=value):
                original=self.quotas['a'][field];self.quotas['a'][field]=value
                self.assertFalse(all_weekly_exhausted(self.records,self.quotas,now=1001))
                self.quotas['a'][field]=original
        self.assertFalse(all_weekly_exhausted([],{},now=1001))

    def test_disabled_preference_prevents_native_reset_calls(self):
        with tempfile.TemporaryDirectory() as directory, patch('reset_bank.LOCALDATA',pathlib.Path(directory)):
            atomic_json(pathlib.Path(directory)/'config.json',{'included_resets':False})
            calls=[]
            accounts=SimpleNamespace(records=self.records,quotas=self.quotas,rpc=SimpleNamespace(call=lambda *a,**k:calls.append(a)))
            with self.assertRaisesRegex(RuntimeError,'0% weekly'):
                restore_allowance(accounts,self.records,set(),2,lambda *a:False)
            self.assertEqual(calls,[])

    def test_previous_reset_waits_for_allowance_without_spending_another(self):
        with tempfile.TemporaryDirectory() as directory, patch('reset_bank.LOCALDATA',pathlib.Path(directory)):
            ledger=pathlib.Path(directory)/'included-reset.json'
            atomic_json(ledger,{'key':'a','state':'awaiting-allowance','outcome':'reset'})
            calls=[]
            accounts=SimpleNamespace(records=self.records,quotas=self.quotas,probe=lambda *a:{'valid':True,'weekly_remaining':0},
                rpc=SimpleNamespace(call=lambda *a,**k:calls.append(a)))
            with self.assertRaisesRegex(RuntimeError,'no additional reset'):
                restore_allowance(accounts,self.records,set(),2,lambda quota,threshold:quota['weekly_remaining']>threshold)
            self.assertEqual(calls,[])
            self.assertEqual(load_json(ledger)['state'],'awaiting-allowance')

    def test_natural_weekly_reset_found_on_refresh_prevents_consumption(self):
        with tempfile.TemporaryDirectory() as directory, patch('reset_bank.LOCALDATA',pathlib.Path(directory)):
            atomic_json(pathlib.Path(directory)/'config.json',{'included_resets':True})
            for quota in self.quotas.values():quota['updated_at']=time.time()
            calls=[]
            def probe(record,force):
                self.quotas[record['key']].update(weekly_remaining=100,updated_at=time.time())
                return self.quotas[record['key']]
            accounts=SimpleNamespace(records=self.records,quotas=self.quotas,probe=probe,
                rpc=SimpleNamespace(call=lambda *a,**k:calls.append(a)))
            with self.assertRaisesRegex(RuntimeError,'weekly allowance'):
                restore_allowance(accounts,self.records,set(),2,lambda *a:False)
            self.assertEqual(calls,[])

if __name__=='__main__':unittest.main()

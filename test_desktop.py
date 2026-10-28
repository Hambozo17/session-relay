import pathlib
import tempfile
import unittest
from unittest.mock import patch
from contextlib import closing
from types import SimpleNamespace
import desktop_adapter

from desktop_adapter import (
    DesktopAdapter,
    DesktopAdapterError,
    DesktopProtocolError,
    DesktopUncertainError,
    quit_window,
    pending_desktop_recovery,
)


THREAD = "123e4567-e89b-42d3-a456-426614174000"
OLD_KEY = "old-account"
NEW_KEY = "new-account"
FINGERPRINT = "a" * 64
OTHER_THREAD = "987e6543-e21b-42d3-a456-426614174000"


@unittest.skipUnless(desktop_adapter.os.name=='nt','Windows package discovery')
class PackageUpdateTests(unittest.TestCase):
    def test_removed_cached_store_package_is_resolved_again(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder)
            old=root/'OpenAI.Codex_26.1021.3705.0_x64__publisher'
            new=root/'OpenAI.Codex_26.1023.6123.0_x64__publisher'
            archive=new/'app/resources/app.asar';archive.parent.mkdir(parents=True);archive.write_bytes(b'fixture')
            with patch.object(desktop_adapter,'_PACKAGE_LOCATION',old),patch.object(desktop_adapter.subprocess,'run',
                    return_value=SimpleNamespace(returncode=0,stdout=str(new))) as query:
                self.assertEqual(desktop_adapter._installed_package_location(),new)
                self.assertEqual(desktop_adapter._installed_package_location(),new)
                self.assertEqual(query.call_count,1)

    def test_removed_package_query_failure_does_not_poison_next_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            old=pathlib.Path(folder)/'removed'
            with patch.object(desktop_adapter,'_PACKAGE_LOCATION',old),patch.object(desktop_adapter.subprocess,'run',
                    side_effect=__import__('subprocess').TimeoutExpired('query',8)):
                with self.assertRaises(DesktopAdapterError):desktop_adapter._installed_package_location()
                self.assertIsNone(desktop_adapter._PACKAGE_LOCATION)

    def test_registered_update_replaces_an_old_package_even_while_old_files_remain(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder);old=root/'OpenAI.Codex_1_x64__publisher';new=root/'OpenAI.Codex_2_x64__publisher'
            for package in (old,new):
                archive=package/'app/resources/app.asar';archive.parent.mkdir(parents=True);archive.write_bytes(b'fixture')
                (package/'app/ChatGPT.exe').write_bytes(b'fixture')
                (package/'AppxManifest.xml').write_text('<Package><Application Id="App" Executable="app/ChatGPT.exe"/><Protocol Name="codex"/></Package>')
            control=desktop_adapter._WindowsDesktopControl()
            control._package={'root':old.resolve(),'exe':old/'app/ChatGPT.exe','family':'old','app_id':'App'}
            with patch.object(desktop_adapter,'_PACKAGE_LOCATION',old),patch.object(desktop_adapter,'_PACKAGE_CHECKED_AT',0),patch.object(desktop_adapter.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=str(new))):
                self.assertEqual(control.package()['root'],new.resolve())


class QuitWindowTests(unittest.TestCase):
    def test_overlay_before_minimized_main_does_not_receive_quit(self):
        # Styles captured from the actual failed handoff: overlay enumerated first.
        windows=[(526002,0x14000000,0x2800A8),(1574902,0x34C70000,0x240100)]
        self.assertEqual(quit_window(windows,526002),1574902)

    def test_overlay_only_inventory_cannot_send_keys(self):
        with self.assertRaises(DesktopAdapterError):
            quit_window([(1,0x14000000,0x2800A8),(2,0x14C70000,0x08000000)],1)

    def test_foreground_main_preferred_over_other_main(self):
        self.assertEqual(quit_window([(1,0x14C70000,0x240100),(2,0x14C70000,0x240100)],2),2)

    def test_maximized_custom_frame_main_is_supported(self):
        self.assertEqual(quit_window([(1,0x14000000,0x2800A8),(2,0x15070000,0x240100)],1),2)


def make_row(**overrides):
    row = {
        "id": THREAD,
        "owner": "owner-1",
        "title": "A local test chat",
        "cwd": "C:/work",
        "turn_id": "turn-1",
        "status": "running",
        "runtime_status": "active",
        "waiting": False,
        "waiting_reasons": [],
        "is_child": False,
        "ephemeral": False,
        "model": "gpt-test",
        "effort": "high",
        "settings": {
            "model": "gpt-test",
            "effort": "high",
            "cwd": "C:/work",
            "permissions": {"mode": "locked"},
            "approvalPolicy": "on-request",
            "sandboxPolicy": {"type": "workspaceWrite"},
        },
        "settings_fingerprint": FINGERPRINT,
        "goal_status": None,
        "error_kind": None,
        "client_user_message_id": None,
    }
    row.update(overrides)
    return row


class FakeAccounts:
    def __init__(self, *, switch_ok=True):
        self.key = OLD_KEY
        self.switch_ok = switch_ok
        self.source = {"key": OLD_KEY, "home": "C:/saved/old"}
        self.target = {"key": NEW_KEY, "home": "C:/saved/new"}
        self.records = {OLD_KEY: self.source, NEW_KEY: self.target}
        self.activations = 0

    def current_desktop_key(self):
        return self.key

    def find(self, key):
        return self.records.get(key)

    def activate_desktop(self, record):
        self.activations += 1
        if self.switch_ok:
            self.key = record["key"]
        return record["key"]


class FakeBridge:
    def __init__(self, rows=None, *, protocol_ok=True, resume_error=None, reopen_fingerprint=None, on_interrupt=None):
        self.rows = [dict(row) for row in (rows or [make_row()])]
        self.protocol_ok = protocol_ok
        self.resume_error = resume_error
        self.reopen_fingerprint = reopen_fingerprint
        self.on_interrupt = on_interrupt
        self.closed = False
        self.started = False
        self.calls = []

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    def call(self, command, timeout=20):
        self.calls.append(dict(command))
        op = command["op"]
        if op == "scan":
            rows = [dict(row) for row in self.rows if row["id"] in command["threads"]]
            if self.reopen_fingerprint:
                for row in rows:
                    row["settings_fingerprint"] = self.reopen_fingerprint
            return {"compatible": self.protocol_ok, "sessions": rows, "unresolved": []}
        if op == "interrupt":
            row = next(row for row in self.rows if row["id"] == command["thread"])
            row.update(status="interrupted", runtime_status="idle")
            if self.on_interrupt:
                self.on_interrupt()
            return {"session": dict(row), "owner": row["owner"]}
        if op == "resume":
            if self.resume_error:
                raise self.resume_error
            row = next(row for row in self.rows if row["id"] == command["thread"])
            row.update(status="running", runtime_status="active", turn_id="turn-2",
                       client_user_message_id=command["operation_id"])
            return {"session": dict(row), "owner": row["owner"]}
        raise AssertionError("unexpected bridge operation")


class FakeAppControl:
    def __init__(self, on_launch=None):
        self.closed = 0
        self.launched = 0
        self.opened = []
        self.on_launch = on_launch

    def close_app(self):
        self.closed += 1

    def launch_app(self):
        self.launched += 1
        if self.on_launch:
            self.on_launch()

    def open_thread(self, thread_id):
        self.opened.append(thread_id)


class DesktopAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.temp.name)
        state = __import__("sqlite3").connect(self.home / "state_5.sqlite")
        state.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, cwd TEXT, model TEXT, reasoning_effort TEXT, preview TEXT)")
        state.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)", (THREAD, "Saved test title", "C:/work", "gpt-test", "high", "must-not-read"))
        state.commit()
        state.close()
        self._write_goal(None)

    def tearDown(self):
        self.temp.cleanup()

    def _write_goal(self, status, budget=1000):
        path = self.home / "goals_1.sqlite"
        if path.exists():
            path.unlink()
        db = __import__("sqlite3").connect(path)
        db.execute("CREATE TABLE thread_goals (thread_id TEXT PRIMARY KEY, goal_id TEXT, objective TEXT, status TEXT, token_budget INTEGER, tokens_used INTEGER, time_used_seconds INTEGER, created_at_ms INTEGER, updated_at_ms INTEGER)")
        db.execute("CREATE TABLE thread_goal_continuation_deferrals (thread_id TEXT PRIMARY KEY)")
        if status is not None:
            db.execute("INSERT INTO thread_goals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (THREAD, "goal-1", "private objective", status, budget, 73, 12, 1, 2))
        db.commit()
        db.close()

    def _insert_thread(self, thread_id):
        db = __import__("sqlite3").connect(self.home / "state_5.sqlite")
        db.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)", (thread_id, "Other local test chat", "C:/work", "gpt-test", "high", "must-not-read"))
        db.commit()
        db.close()

    def _adapter(self, bridge, *, switch_ok=True, bridges=None):
        accounts = FakeAccounts(switch_ok=switch_ok)
        app = FakeAppControl()
        if bridges is None:
            bridges = [bridge]
        adapter = DesktopAdapter(
            accounts,
            codex_home=self.home,
            bridge_factory=lambda: bridges.pop(0),
            app_control=app,
            recovery_timeout=0,
        )
        adapter._bridge = bridge
        return adapter, accounts, app

    def test_invalid_installed_protocol_fails_closed(self):
        bridge = FakeBridge(protocol_ok=False)
        adapter, _accounts, _app = self._adapter(bridge)
        with self.assertRaises(DesktopProtocolError):
            adapter.discover()
        self.assertFalse(any(call["op"] in ("interrupt", "resume") for call in bridge.calls))

    def test_monitor_prioritizes_selected_old_goal_and_recent_threads(self):
        bridge=FakeBridge([make_row()]);adapter,accounts,app=self._adapter(bridge)
        catalog={f'123e4567-e89b-42d3-a456-{i:012d}':{'updated_at':i} for i in range(100)}
        catalog[THREAD]={'updated_at':-1};old_goal=next(iter(catalog))
        def saved():
            adapter._goal_rows={old_goal:{'status':'usage_limited'}}
            return catalog
        with patch.object(adapter,'_catalog',side_effect=saved),patch('accounts.load_json',return_value={'codex':{'sessions':[THREAD]}}):
            adapter.discover()
        requested=bridge.calls[-1]['threads']
        self.assertIn(THREAD,requested);self.assertIn(old_goal,requested)
        self.assertIn('123e4567-e89b-42d3-a456-000000000099',requested)
        self.assertNotIn('123e4567-e89b-42d3-a456-000000000010',requested)
        self.assertEqual(app.closed,0);self.assertEqual(accounts.activations,0)

    def test_in_place_recovery_accepts_only_a_confirmed_relay_pause(self):
        bridge=FakeBridge([make_row(status='interrupted',runtime_status='idle')])
        adapter,accounts,app=self._adapter(bridge)
        with self.assertRaises(DesktopAdapterError):adapter.resume_in_place([THREAD],lambda *_:None)
        self.assertFalse(any(call['op']=='resume' for call in bridge.calls))
        adapter.recovery_receipts[THREAD]=('turn-1',FINGERPRINT)
        self.assertEqual(adapter.resume_in_place([THREAD],lambda *_:None),[THREAD])
        self.assertEqual(len([c for c in bridge.calls if c['op']=='resume']),1)
        self.assertEqual(app.closed,0);self.assertEqual(accounts.activations,0)

    def test_stopped_continuation_rechecks_selection_and_leaves_goal_database_alone(self):
        class ContinueBridge(FakeBridge):
            def call(self,command,timeout=20):
                if command['op']!='continue_stopped':return super().call(command,timeout)
                self.calls.append(dict(command));row=self.rows[0]
                row.update(status='running',runtime_status='active',turn_id='continued',client_user_message_id=command['operation_id'])
                return {'session':dict(row)}
        bridge=ContinueBridge([make_row(status='idle',runtime_status='idle')]);adapter,accounts,app=self._adapter(bridge)
        cfg={'codex':{'enabled':True,'continue_on_stop':True,'sessions':[THREAD]}}
        with patch('accounts.load_json',return_value=cfg):
            expected=adapter._scan(adapter._catalog())[0]
            result=adapter.continue_stopped(expected,OTHER_THREAD)
            self.assertEqual(result['turn_id'],'continued')
            request=next(call for call in bridge.calls if call['op']=='continue_stopped')
            self.assertIn('does not grant new permissions or human approval',request['request']['input'][0]['text'])
            self.assertEqual(adapter._read_goals(),{})
            self.assertEqual(app.closed,0);self.assertEqual(accounts.activations,0)
            cfg['codex']['continue_on_stop']=False
            with self.assertRaises(desktop_adapter.DesktopStoppedStateError):adapter.continue_stopped(expected,OTHER_THREAD)
            self.assertEqual(len([c for c in bridge.calls if c['op']=='continue_stopped']),1)

    def test_stopped_continuation_respects_switches_changed_during_preflight(self):
        for stage in ('scan','request'):
            for change in ({'enabled':False},{'continue_on_stop':False},{'sessions':[]}):
                with self.subTest(stage=stage,change=change):
                    bridge=FakeBridge([make_row(status='idle',runtime_status='idle')])
                    adapter,accounts,app=self._adapter(bridge)
                    cfg={'codex':{'enabled':True,'continue_on_stop':True,'sessions':[THREAD]}}
                    with patch('accounts.load_json',return_value=cfg):
                        expected=adapter._scan(adapter._catalog())[0]
                        original=adapter._scan if stage=='scan' else adapter._continuation_request
                        def changed(*args,**kwargs):
                            cfg['codex'].update(change)
                            return original(*args,**kwargs)
                        attribute='_scan' if stage=='scan' else '_continuation_request'
                        with patch.object(adapter,attribute,side_effect=changed):
                            with self.assertRaises(desktop_adapter.DesktopStoppedStateError):
                                adapter.continue_stopped(expected,OTHER_THREAD)
                    self.assertFalse(any(c['op']=='continue_stopped' for c in bridge.calls))
                    self.assertEqual(app.closed,0);self.assertEqual(accounts.activations,0)

    def test_opted_in_idle_chat_can_switch_accounts_without_a_goal_or_with_blocked_goal(self):
        for goal in (None,'blocked'):
            with self.subTest(goal=goal):
                self._write_goal(goal)
                before=FakeBridge([make_row(status='idle',runtime_status='idle',goal_status=goal)])
                after=FakeBridge([make_row(status='idle',runtime_status='idle',goal_status=goal)])
                adapter,accounts,app=self._adapter(before,bridges=[after])
                cfg={'codex':{'enabled':True,'continue_on_stop':True,'sessions':[THREAD]}}
                with patch('accounts.load_json',return_value=cfg):
                    self.assertEqual(adapter.rotate([THREAD],accounts.find(NEW_KEY),lambda *_:None),[THREAD])
                    request=next(call for call in after.calls if call['op']=='resume')
                    self.assertTrue(request['allow_stopped'])
                    self.assertEqual((adapter._read_goals().get(THREAD) or {}).get('status'),goal)

    def test_restart_continuation_respects_live_revocation_after_launch_and_intent(self):
        self._restart_revocation()

    def test_blocked_goal_revocation_is_cancelled_before_goal_eligibility_can_block_recovery(self):
        self._restart_revocation('blocked')

    def _restart_revocation(self,goal=None):
        for stage in ('launch','intent'):
            for change in ({'enabled':False},{'continue_on_stop':False},{'sessions':[]}):
                with self.subTest(stage=stage,change=change):
                    self._write_goal(goal)
                    before=FakeBridge([make_row(status='idle',runtime_status='idle',goal_status=goal)])
                    after=FakeBridge([make_row(status='idle',runtime_status='idle',goal_status=goal)])
                    adapter,accounts,app=self._adapter(before,bridges=[after]);phases=[]
                    cfg={'codex':{'enabled':True,'continue_on_stop':True,'sessions':[THREAD]}}
                    if stage=='launch':app.on_launch=lambda:cfg['codex'].update(change)
                    def journal(phase,metadata):
                        phases.append(phase)
                        if stage=='intent' and phase=='desktop-continuation-intent':cfg['codex'].update(change)
                    with patch('accounts.load_json',return_value=cfg):
                        with self.assertRaises(desktop_adapter.DesktopStoppedStateError):
                            adapter.rotate([THREAD],accounts.find(NEW_KEY),journal)
                    self.assertFalse(any(c['op']=='resume' for c in after.calls))
                    self.assertIn('desktop-continuation-cancelled',phases)
                    self.assertFalse(adapter._attempted_sends)

    def test_quota_failed_stopped_continuation_is_not_recorded_as_recovery(self):
        class FailedBridge(FakeBridge):
            def call(self,command,timeout=20):
                result=super().call(command,timeout)
                if command['op']=='resume':
                    result['session'].update(status='failed',error_kind='usageLimitExceeded',runtime_status='idle')
                return result
        before=FakeBridge([make_row(status='idle',runtime_status='idle')])
        after=FailedBridge([make_row(status='idle',runtime_status='idle')])
        adapter,accounts,app=self._adapter(before,bridges=[after]);phases=[]
        cfg={'codex':{'enabled':True,'continue_on_stop':True,'sessions':[THREAD]}}
        with patch('accounts.load_json',return_value=cfg):
            with self.assertRaisesRegex(DesktopAdapterError,'usage/login failure'):
                adapter.rotate([THREAD],accounts.find(NEW_KEY),lambda phase,data:phases.append(phase))
        self.assertEqual(phases[-1],'desktop-continuation-intent')
        self.assertNotIn('desktop-session-recovered',phases)

    def test_first_failed_continuation_reconciles_with_unsent_opted_in_idle_sibling(self):
        import controller,time
        self._insert_thread(OTHER_THREAD)
        class FailedBridge(FakeBridge):
            def call(self,command,timeout=20):
                result=super().call(command,timeout)
                if command['op']=='resume':
                    row=next(r for r in self.rows if r['id']==command['thread'])
                    row.update(status='failed',error_kind='usageLimitExceeded',runtime_status='idle',
                        turn_started_at_ms=(time.time()+1)*1000)
                    result['session']=dict(row)
                return result
        rows=[make_row(status='idle',runtime_status='idle'),make_row(id=OTHER_THREAD,status='idle',runtime_status='idle')]
        before=FakeBridge(rows);after=FailedBridge(rows);adapter,accounts,app=self._adapter(before,bridges=[after])
        cfg={'codex':{'enabled':True,'continue_on_stop':True,'sessions':[THREAD,OTHER_THREAD]}}
        with patch.object(controller,'LOCALDATA',self.home),patch.object(controller,'STATUS',self.home/'status.json'),patch.object(controller,'JOURNAL',self.home/'journal.json'):
            c=controller.Controller(accounts,{})
            c.journal={'id':'idle-siblings','app':'codex','state':'in-progress','sessions':[THREAD,OTHER_THREAD],
                'target':NEW_KEY,'outgoing':OLD_KEY,'phase':'prepare','updated_at':time.time(),'target_quota':{}}
            try:
                with patch('accounts.load_json',return_value=cfg):
                    with self.assertRaisesRegex(DesktopAdapterError,'usage/login failure'):
                        adapter.rotate([THREAD,OTHER_THREAD],accounts.find(NEW_KEY),c.journal_phase)
                    fresh=adapter._scan(adapter._catalog())
                saved=controller.load_json(self.home/'operations/idle-siblings.json')
                self.assertEqual([call['thread'] for call in after.calls if call['op']=='resume'],[THREAD])
                self.assertTrue(controller.desktop_continuation_failed(saved,fresh,NEW_KEY))
                other=next(row for row in fresh if row['id']==OTHER_THREAD)
                for changes in ({'_continue_on_stop':False},{'status':'interrupted'},
                        {'turn_id':'manual-new'},{'_runtime_status':'active'},{'waiting':True}):
                    altered=[dict(row,**changes) if row['id']==OTHER_THREAD else row for row in fresh]
                    self.assertFalse(controller.desktop_continuation_failed(saved,altered,NEW_KEY))
            finally:c.refresh_pool.shutdown(wait=True)

    def test_failed_first_send_keeps_order_and_durable_unsent_pause_receipts(self):
        import controller,time
        self._insert_thread(OTHER_THREAD)
        bridge=FakeBridge([make_row(id=OTHER_THREAD,status='interrupted',runtime_status='idle'),
            make_row(status='interrupted',runtime_status='idle')])
        adapter,accounts,app=self._adapter(bridge)
        adapter.recovery_receipts={sid:('turn-1',FINGERPRINT) for sid in (THREAD,OTHER_THREAD)}
        original_call=bridge.call
        def call(command,**kwargs):
            if command['op']!='resume':return original_call(command,**kwargs)
            bridge.calls.append(dict(command))
            row=next(row for row in bridge.rows if row['id']==command['thread'])
            row.update(status='failed',error_kind='usageLimitExceeded',turn_id='new-failed',
                client_user_message_id=command['operation_id'],turn_started_at_ms=(time.time()+1)*1000)
            raise DesktopUncertainError('Accepted send failed before confirmation')
        bridge.call=call
        with patch.object(controller,'LOCALDATA',self.home),patch.object(controller,'STATUS',self.home/'status.json'),patch.object(controller,'JOURNAL',self.home/'journal.json'):
            c=controller.Controller(accounts,{})
            c.journal={'id':'in-place','app':'codex','state':'in-progress','sessions':[THREAD,OTHER_THREAD],
                'target':OLD_KEY,'outgoing':OLD_KEY,'phase':'prepare','target_quota':{'allowance_generation':0}}
            try:
                with self.assertRaises(DesktopUncertainError):adapter.resume_in_place([THREAD,OTHER_THREAD],c.journal_phase)
                saved=controller.load_json(self.home/'operations/in-place.json')
                self.assertEqual([x['thread'] for x in bridge.calls if x['op']=='resume'],[THREAD])
                self.assertEqual(set(saved['interruption_receipts']),{THREAD,OTHER_THREAD})
                self.assertTrue(controller.desktop_continuation_failed(saved,adapter._scan(adapter._catalog()),OLD_KEY))
            finally:c.refresh_pool.shutdown(wait=True)

    def test_reopen_retry_only_navigates_original_selected_chat(self):
        adapter,accounts,app=self._adapter(FakeBridge())
        app._matching=lambda:True
        with patch('accounts.load_json',return_value={'codex':{'enabled':True,'sessions':[THREAD]}}):
            adapter.request_reopen([THREAD])
        self.assertEqual(app.opened,[THREAD]);self.assertEqual(app.closed,0)
        self.assertEqual(app.launched,0);self.assertEqual(accounts.activations,0)
        with patch('accounts.load_json',return_value={'codex':{'enabled':False,'sessions':[THREAD]}}):
            with self.assertRaises(DesktopAdapterError):adapter.request_reopen([THREAD])
        self.assertEqual(app.opened,[THREAD])

    def test_saved_unowned_chat_is_selectable_but_never_treated_as_live(self):
        self._insert_thread(OTHER_THREAD)
        bridge=FakeBridge([make_row()]);adapter,accounts,app=self._adapter(bridge)
        live=adapter.discover();choices=adapter.selectable_sessions(live)
        self.assertEqual([r['id'] for r in live],[THREAD])
        saved=next(r for r in choices if r['id']==OTHER_THREAD)
        self.assertFalse(saved['attached']);self.assertEqual(saved['status'],'Open in Codex')
        self.assertEqual(accounts.activations,0);self.assertEqual(app.closed,0)
        self.assertFalse(any(c['op'] in ('resume','interrupt') for c in bridge.calls))

    def test_old_quota_failure_continues_without_restarting_healthy_sibling(self):
        self._insert_thread(OTHER_THREAD)
        bridge=FakeBridge([make_row(status='failed',runtime_status='idle',error_kind='usageLimitExceeded'),make_row(id=OTHER_THREAD)])
        adapter,accounts,app=self._adapter(bridge)
        phases=[]
        self.assertEqual(adapter.resume_in_place([THREAD],lambda phase,data:phases.append(phase)),[THREAD])
        self.assertEqual(accounts.activations,0);self.assertEqual(app.closed,0);self.assertEqual(app.launched,0)
        self.assertEqual(sum(c['op']=='resume' for c in bridge.calls),1)
        self.assertFalse(any(c['op']=='interrupt' for c in bridge.calls))
        self.assertIn('desktop-recovery-context',phases)

    def test_delayed_restart_finishes_attested_pause_without_switch_or_interrupt(self):
        bridge=FakeBridge([make_row(status='interrupted',runtime_status='idle')])
        adapter,accounts,app=self._adapter(bridge);accounts.key=NEW_KEY
        operation={'phase':'desktop-open-thread-intent','target':NEW_KEY,'sessions':[THREAD],
            'recovery_context':{THREAD:{'turn':'turn-1','settings':FINGERPRINT,'goal_record':None}},
            'interruption_receipts':{THREAD:{'turn':'turn-1','settings':FINGERPRINT,'relay_interrupted':True}}}
        phases=[]
        self.assertEqual(adapter.resume_in_place([THREAD],lambda p,d:phases.append(p),recovery_operation=operation),[THREAD])
        self.assertEqual(accounts.activations,0);self.assertEqual(app.closed,0)
        self.assertEqual(sum(c['op']=='resume' for c in bridge.calls),1)
        self.assertFalse(any(c['op']=='interrupt' for c in bridge.calls))
        with self.assertRaises(DesktopAdapterError):adapter.resume_in_place([THREAD],lambda *_:None,recovery_operation=operation)
        self.assertEqual(sum(c['op']=='resume' for c in bridge.calls),1)

    def test_delayed_restart_rejects_uncertain_send_manual_stop_or_changed_settings(self):
        operation={'phase':'desktop-open-thread-intent','target':NEW_KEY,'sessions':[THREAD],
            'recovery_context':{THREAD:{'turn':'turn-1','settings':FINGERPRINT,'goal_record':None}},
            'interruption_receipts':{THREAD:{'turn':'turn-1','settings':FINGERPRINT,'relay_interrupted':True}}}
        row={'id':THREAD,'account_key':NEW_KEY,'_owner':'owner','supported':True,'status':'interrupted',
             'turn_id':'turn-1','_settings_fingerprint':FINGERPRINT,'_goal_record':None}
        self.assertEqual(pending_desktop_recovery(operation,[row],NEW_KEY),[THREAD])
        for changes in ({'turn_id':'manual-new-turn'},{'_settings_fingerprint':'changed'},{'waiting':True},{'_goal_record':{'goal_id':'new'}},{'supported':False}):
            self.assertIsNone(pending_desktop_recovery(operation,[dict(row,**changes)],NEW_KEY))
        self.assertIsNone(pending_desktop_recovery(dict(operation,phase='desktop-continuation-intent'),[row],NEW_KEY))
        self.assertIsNone(pending_desktop_recovery(dict(operation,interruption_receipts={}),[row],NEW_KEY))

    def test_in_place_rejects_user_stop_and_login_error(self):
        for overrides in ({'status':'interrupted','runtime_status':'idle'},
                          {'status':'failed','error_kind':'unauthorized','runtime_status':'idle'}):
            with self.subTest(overrides=overrides):
                bridge=FakeBridge([make_row(**overrides)]);adapter,_,_=self._adapter(bridge)
                with self.assertRaises(DesktopAdapterError):adapter.resume_in_place([THREAD],lambda *_:None)
                self.assertFalse(any(c['op']=='resume' for c in bridge.calls))

    def test_in_place_uncertain_submission_never_repeats(self):
        bridge=FakeBridge([make_row(status='failed',runtime_status='idle',error_kind='quota')],resume_error=DesktopUncertainError('lost reply'))
        adapter,_,_=self._adapter(bridge)
        for _ in range(2):
            with self.assertRaises(DesktopUncertainError):adapter.resume_in_place([THREAD],lambda *_:None)
        self.assertEqual(sum(c['op']=='resume' for c in bridge.calls),1)

    def test_wrong_identity_after_activation_blocks_continuation(self):
        first = FakeBridge()
        reopened = FakeBridge([make_row(status="interrupted", runtime_status="idle")])
        adapter, accounts, app = self._adapter(first, switch_ok=False, bridges=[reopened])
        with self.assertRaisesRegex(DesktopAdapterError, "identity"):
            adapter.rotate([THREAD], accounts.target, lambda *_args: None)
        self.assertEqual(accounts.activations, 1)
        self.assertEqual(app.closed, 1)
        self.assertEqual(app.launched, 0)
        self.assertEqual(app.opened, [])
        self.assertFalse(any(call["op"] == "resume" for call in reopened.calls))

    def test_uncertain_send_is_recorded_and_duplicate_is_not_sent(self):
        first = FakeBridge()
        reopened = FakeBridge([make_row(status="interrupted", runtime_status="idle")], resume_error=DesktopUncertainError("uncertain"))
        retry_reopened = FakeBridge([make_row(status="interrupted", runtime_status="idle")])
        adapter, accounts, _app = self._adapter(first, bridges=[reopened, retry_reopened])
        with self.assertRaises(DesktopUncertainError):
            adapter.rotate([THREAD], accounts.target, lambda *_args: None)
        sends = [call for call in reopened.calls if call["op"] == "resume"]
        self.assertEqual(len(sends), 1)
        # Simulate a controlled retry after restoring the pre-switch test fixture.
        accounts.key = OLD_KEY
        retry_bridge = FakeBridge()
        adapter._bridge = retry_bridge
        with self.assertRaisesRegex(DesktopUncertainError, "already attempted"):
            adapter.rotate([THREAD], accounts.target, lambda *_args: None)
        self.assertFalse(any(call["op"] == "resume" for call in retry_bridge.calls))

    def test_changed_settings_after_reopen_block_before_send(self):
        first = FakeBridge()
        reopened = FakeBridge([make_row(status="interrupted", runtime_status="idle")], reopen_fingerprint="b" * 64)
        adapter, accounts, _app = self._adapter(first, bridges=[reopened])
        with self.assertRaisesRegex(DesktopAdapterError, "settings"):
            adapter.rotate([THREAD], accounts.target, lambda *_args: None)
        self.assertFalse(any(call["op"] == "resume" for call in reopened.calls))

    def test_success_uses_same_thread_and_preserves_live_settings_fingerprint(self):
        first = FakeBridge()
        reopened = FakeBridge([make_row(status="interrupted", runtime_status="idle")])
        adapter, accounts, app = self._adapter(first, bridges=[reopened])
        recovered = adapter.rotate([THREAD], accounts.target, lambda *_args: None)
        self.assertEqual(recovered, [THREAD])
        resume = next(call for call in reopened.calls if call["op"] == "resume")
        self.assertEqual(resume["thread"], THREAD)
        self.assertEqual(resume["settings_fingerprint"], FINGERPRINT)
        self.assertEqual(resume["request"]["threadId"], THREAD)
        self.assertEqual(resume["request"]["clientUserMessageId"], resume["operation_id"])
        self.assertEqual(app.opened, [THREAD])
        self.assertEqual(accounts.key, NEW_KEY)

    def test_active_goal_counters_may_advance_during_interrupt(self):
        self._write_goal("active")
        def settle_goal():
            db = __import__("sqlite3").connect(self.home / "goals_1.sqlite")
            db.execute("UPDATE thread_goals SET tokens_used=80, time_used_seconds=13, updated_at_ms=3 WHERE thread_id=?", (THREAD,))
            db.commit()
            db.close()
        first = FakeBridge([make_row(goal_status="active")], on_interrupt=settle_goal)
        reopened = FakeBridge([make_row(status="interrupted", runtime_status="idle", goal_status="active")])
        adapter, accounts, _app = self._adapter(first, bridges=[reopened])
        self.assertEqual(adapter.rotate([THREAD], accounts.target, lambda *_args: None), [THREAD])

    def test_desktop_auto_resumed_active_goal_is_not_sent_a_duplicate(self):
        self._write_goal("active")
        first = FakeBridge([make_row(goal_status="active")])
        reopened = FakeBridge([make_row(status="running", runtime_status="active", turn_id="turn-2", goal_status="active")])
        adapter, accounts, _app = self._adapter(first, bridges=[reopened])
        self.assertEqual(adapter.rotate([THREAD], accounts.target, lambda *_args: None), [THREAD])
        self.assertFalse(any(call["op"] == "resume" for call in reopened.calls))

    def test_startup_pipe_failure_retries_before_any_continuation(self):
        class ColdBridge(FakeBridge):
            def start(self): raise DesktopAdapterError('pipe is not ready')
        cold=ColdBridge()
        reopened=FakeBridge([make_row(status='interrupted',runtime_status='idle')])
        adapter, accounts, _app=self._adapter(FakeBridge(), bridges=[cold,reopened])
        adapter._recovery_timeout=2
        self.assertEqual(adapter.rotate([THREAD],accounts.target,lambda *_:None),[THREAD])
        self.assertTrue(cold.closed)
        self.assertEqual(sum(c['op']=='resume' for c in reopened.calls),1)

    def test_missing_owner_during_startup_waits_before_continuation(self):
        class DelayedOwner(FakeBridge):
            def __init__(self):
                super().__init__([make_row(status='interrupted',runtime_status='idle')])
                self.checks=0
            def call(self,command,timeout=20):
                if command['op']=='scan':
                    self.checks+=1
                    if self.checks<=2:
                        self.calls.append(dict(command))
                        return {'compatible':True,'sessions':[],'unresolved':[]}
                if command['op']=='resume':self.assert_ready=self.checks>2
                return super().call(command,timeout)
        reopened=DelayedOwner()
        adapter,accounts,app=self._adapter(FakeBridge(),bridges=[reopened])
        adapter._recovery_timeout=3
        with __import__('unittest.mock',fromlist=['patch']).patch('desktop_adapter.time.sleep'):
            self.assertEqual(adapter.rotate([THREAD],accounts.target,lambda *_:None),[THREAD])
        self.assertTrue(reopened.assert_ready)
        self.assertEqual(sum(c['op']=='resume' for c in reopened.calls),1)
        self.assertEqual(app.opened,[THREAD])

    def test_user_paused_or_complete_goal_is_never_restarted(self):
        for status in ("paused", "complete"):
            with self.subTest(status=status):
                self._write_goal(status)
                bridge = FakeBridge()
                adapter, accounts, app = self._adapter(bridge)
                with self.assertRaisesRegex(DesktopAdapterError, "Goal"):
                    adapter.rotate([THREAD], accounts.target, lambda *_args: None)
                self.assertEqual(accounts.activations, 0)
                self.assertEqual(app.closed, 0)
                self.assertFalse(any(call["op"] in ("interrupt", "resume") for call in bridge.calls))
        self._write_goal(None)

    def test_same_account_usage_limited_goal_restores_offline_and_preserves_running_sibling(self):
        self._write_goal("usage_limited")
        self._insert_thread(OTHER_THREAD)
        goal_row=make_row(status="idle",runtime_status="idle",goal_status="usage_limited",
            error_kind="usageLimitExceeded")
        sibling=make_row(id=OTHER_THREAD,status="running",runtime_status="active")
        first=FakeBridge([goal_row,sibling])
        reopened=FakeBridge([
            make_row(status="running",runtime_status="active",turn_id="goal-auto-turn",goal_status="active"),
            make_row(id=OTHER_THREAD,status="interrupted",runtime_status="idle"),
        ])
        adapter,accounts,app=self._adapter(first,bridges=[reopened])
        target=accounts.find(OLD_KEY)
        phases=[]
        recovered=adapter.rotate([THREAD,OTHER_THREAD],target,lambda phase,data:phases.append(phase),
            same_account_recovery=True,same_account_recovery_ids=[THREAD])
        self.assertEqual(recovered,[THREAD,OTHER_THREAD])
        self.assertEqual(accounts.activations,0)
        self.assertEqual(accounts.key,OLD_KEY)
        self.assertEqual((app.closed,app.launched),(1,1))
        self.assertEqual(app.opened,[THREAD,OTHER_THREAD])
        resumes=[call for call in reopened.calls if call["op"]=="resume"]
        self.assertEqual([call["thread"] for call in resumes],[OTHER_THREAD])
        self.assertIn('desktop-same-account-verified',phases)
        self.assertNotIn('desktop-activate-intent',phases)
        db=__import__('sqlite3').connect(self.home/'goals_1.sqlite')
        row=db.execute('SELECT goal_id,status,token_budget,tokens_used,time_used_seconds FROM thread_goals WHERE thread_id=?',(THREAD,)).fetchone()
        db.close()
        self.assertEqual(row,('goal-1','active',1000,73,12))

    def test_same_account_unlimited_usage_goal_resumes_without_changing_its_budget(self):
        self._write_goal('usage_limited', budget=None)
        first = FakeBridge([make_row(status='idle', runtime_status='idle',
            goal_status='usage_limited', error_kind='usageLimitExceeded')])
        reopened = FakeBridge([make_row(status='running', runtime_status='active',
            turn_id='automatic-unlimited-goal', goal_status='active')])
        adapter, accounts, app = self._adapter(first, bridges=[reopened])
        recovered = adapter.rotate([THREAD], accounts.find(OLD_KEY), lambda *_: None,
            same_account_recovery=True, same_account_recovery_ids=[THREAD])
        self.assertEqual(recovered, [THREAD])
        self.assertEqual(accounts.activations, 0)
        self.assertEqual((app.closed, app.launched), (1, 1))
        self.assertFalse(any(call['op'] == 'resume' for call in reopened.calls))
        with closing(__import__('sqlite3').connect(self.home / 'goals_1.sqlite')) as db:
            saved = db.execute('SELECT goal_id,status,token_budget,tokens_used,time_used_seconds '
                'FROM thread_goals WHERE thread_id=?', (THREAD,)).fetchone()
        self.assertEqual(saved, ('goal-1', 'active', None, 73, 12))

    def test_same_account_restart_accepts_fast_verified_goal_completion_without_resuming(self):
        for goal_status in ('complete','completed'):
            with self.subTest(goal_status=goal_status):
                self._write_goal("usage_limited")
                first=FakeBridge([make_row(status="idle",runtime_status="idle",goal_status="usage_limited",
                    error_kind="usageLimitExceeded")])
                reopened=FakeBridge([make_row(status="idle",runtime_status="idle",turn_id="goal-completed-turn",
                    goal_status=goal_status)])
                def complete_goal(status=goal_status):
                    db=__import__('sqlite3').connect(self.home/'goals_1.sqlite')
                    db.execute("UPDATE thread_goals SET status=?,tokens_used=90,time_used_seconds=15,updated_at_ms=4 WHERE thread_id=?",(status,THREAD))
                    db.commit();db.close()
                accounts=FakeAccounts()
                app=FakeAppControl(on_launch=complete_goal)
                adapter=DesktopAdapter(accounts,codex_home=self.home,bridge_factory=lambda:reopened,
                    app_control=app,recovery_timeout=0)
                adapter._bridge=first
                recovered=adapter.rotate([THREAD],accounts.find(OLD_KEY),lambda *_:None,
                    same_account_recovery=True,same_account_recovery_ids=[THREAD])
                self.assertEqual(recovered,[THREAD])
                self.assertEqual(accounts.activations,0)
                self.assertFalse(any(call['op']=='resume' for call in reopened.calls))
                db=__import__('sqlite3').connect(self.home/'goals_1.sqlite')
                row=db.execute('SELECT goal_id,status,token_budget,tokens_used,time_used_seconds FROM thread_goals WHERE thread_id=?',(THREAD,)).fetchone()
                db.close()
                self.assertEqual(row,('goal-1',goal_status,1000,90,15))

        original={'goal_status':'active','turn_id':'before','_owner':'owner-1',
            '_settings_fingerprint':FINGERPRINT,'_goal_record':{
                'goal_id':'goal-1','status':'active','token_budget':1000,'created_at_ms':1,
                'updated_at_ms':2,'tokens_used':73,'time_used_seconds':12,'deferred':False}}
        row=make_row(status='idle',runtime_status='idle',turn_id='after',goal_status='completed')
        self.assertFalse(DesktopAdapter._completed_goal_after_restart(original,row,{
            **original['_goal_record'],'status':'complete'}))
        self.assertFalse(DesktopAdapter._completed_goal_after_restart(original,
            {**row,'goal_status':'paused'},{**original['_goal_record'],'status':'paused'}))

    def test_same_account_usage_goal_restart_still_blocks_unselected_running_or_approval(self):
        self._write_goal("usage_limited")
        self._insert_thread(OTHER_THREAD)
        goal=make_row(status="idle",runtime_status="idle",goal_status="usage_limited",error_kind="usageLimitExceeded")
        for row,message in ((make_row(id=OTHER_THREAD,status="running",runtime_status="active"),"unselected"),
                (make_row(id=OTHER_THREAD,status="idle",runtime_status="idle",waiting=True),"approval")):
            with self.subTest(message=message):
                bridge=FakeBridge([goal,row]);adapter,accounts,app=self._adapter(bridge)
                with self.assertRaisesRegex(DesktopAdapterError,message):
                    adapter.rotate([THREAD],accounts.find(OLD_KEY),lambda *_:None,
                        same_account_recovery=True,same_account_recovery_ids=[THREAD])
                self.assertEqual(accounts.activations,0)
                self.assertEqual(app.closed,0)
                self.assertFalse(any(call['op'] in ('interrupt','resume') for call in bridge.calls))

    def test_unselected_active_or_approval_waiting_thread_blocks_global_restart(self):
        self._insert_thread(OTHER_THREAD)
        other_active = make_row(id=OTHER_THREAD, turn_id="other-turn", status="running", runtime_status="active")
        other_goal = make_row(id=OTHER_THREAD, turn_id="other-turn", status="idle", runtime_status="idle", goal_status="active")
        other_waiting = make_row(id=OTHER_THREAD, turn_id="other-turn", status="idle", runtime_status="idle", waiting=True)
        for row, message in ((other_active, "unselected"), (other_goal, "unselected"), (other_waiting, "approval")):
            with self.subTest(message=message):
                bridge = FakeBridge([make_row(), row])
                adapter, accounts, app = self._adapter(bridge)
                with self.assertRaisesRegex(DesktopAdapterError, message):
                    adapter.rotate([THREAD], accounts.target, lambda *_args: None)
                self.assertEqual(accounts.activations, 0)
                self.assertEqual(app.closed, 0)
                self.assertFalse(any(call["op"] == "interrupt" for call in bridge.calls))

    def test_goal_read_omits_objective_and_keeps_accounting_signature(self):
        self._write_goal("active")
        adapter, _accounts, _app = self._adapter(FakeBridge())
        goals = adapter._read_goals()
        goal = goals[THREAD]
        self.assertNotIn("objective", goal)
        self.assertEqual(goal["tokens_used"], 73)
        self.assertEqual(goal["time_used_seconds"], 12)


if __name__ == "__main__":
    unittest.main()

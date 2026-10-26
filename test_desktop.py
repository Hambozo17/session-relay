import pathlib
import tempfile
import unittest

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

    def _write_goal(self, status):
        path = self.home / "goals_1.sqlite"
        if path.exists():
            path.unlink()
        db = __import__("sqlite3").connect(path)
        db.execute("CREATE TABLE thread_goals (thread_id TEXT PRIMARY KEY, goal_id TEXT, objective TEXT, status TEXT, token_budget INTEGER, tokens_used INTEGER, time_used_seconds INTEGER, created_at_ms INTEGER, updated_at_ms INTEGER)")
        db.execute("CREATE TABLE thread_goal_continuation_deferrals (thread_id TEXT PRIMARY KEY)")
        if status is not None:
            db.execute("INSERT INTO thread_goals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (THREAD, "goal-1", "private objective", status, 1000, 73, 12, 1, 2))
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

import copy,json,pathlib,tempfile,threading,time,unittest
from unittest.mock import patch
import controller
from desktop_settings import DEFAULT_INSTRUCTIONS,fingerprint,migrate_operation


class ServiceHealth(unittest.TestCase):
    def test_redundant_full_access_child_root_migrates_only_attested_old_snapshot(self):
        sid='chat';snapshot={'model':'chosen','effort':'xhigh','cwd':'C:/work/project',
            'permissions':{'sandboxPolicy':{'type':'dangerFullAccess'},'runtimeWorkspaceRoots':['C:/work','C:/work/project']}}
        resumed=copy.deepcopy(snapshot);resumed['permissions']['runtimeWorkspaceRoots']=['C:/work']
        old=fingerprint(snapshot,sid,normalize_roots=False)
        op={'recovery_context':{sid:{'settings_snapshot':snapshot,'settings':old}}}
        self.assertTrue(migrate_operation(op));self.assertEqual(op['recovery_context'][sid]['settings'],fingerprint(resumed,sid))
        self.assertEqual(snapshot['permissions']['runtimeWorkspaceRoots'],['C:/work','C:/work/project'])
        self.assertFalse(migrate_operation(op))
        for roots in (['C:/work/project'],['C:/work2'],['C:/work','C:/work2']):
            changed=copy.deepcopy(resumed);changed['permissions']['runtimeWorkspaceRoots']=roots
            self.assertNotEqual(fingerprint(snapshot,sid),fingerprint(changed,sid))
        for value in (snapshot,resumed):value['permissions']['sandboxPolicy']={'type':'workspaceWrite'}
        self.assertNotEqual(fingerprint(snapshot,sid),fingerprint(resumed,sid))
        altered=copy.deepcopy(op);altered['recovery_context'][sid]['settings']='unattested'
        self.assertFalse(migrate_operation(altered))

    def test_implicit_effort_uses_only_same_model_mode_and_migrates_attested_snapshot(self):
        sid='chat';snapshot={'model':'selected','effort':None,'collaborationMode':{'mode':'default','settings':{'model':'selected','reasoning_effort':'xhigh','developer_instructions':None}}}
        old=fingerprint(snapshot,sid,normalize_effort=False)
        operation={'recovery_context':{sid:{'settings_snapshot':snapshot,'settings':old}},'interruption_receipts':{sid:{'settings':old}},'metadata':{'originals':{sid:{'settings':old}}}}
        self.assertTrue(migrate_operation(operation))
        resumed=dict(snapshot,effort='xhigh')
        self.assertEqual(operation['recovery_context'][sid]['settings'],fingerprint(resumed,sid))
        self.assertEqual(operation['metadata']['originals'][sid]['settings'],fingerprint(resumed,sid))
        self.assertIsNone(snapshot['effort'])
        self.assertFalse(migrate_operation(operation))
        self.assertNotEqual(fingerprint(dict(resumed,effort='high'),sid),fingerprint(snapshot,sid))
        changed=copy.deepcopy(snapshot);changed['collaborationMode']['settings']['model']='other'
        self.assertNotEqual(fingerprint(changed,sid),fingerprint(dict(changed,effort='xhigh'),sid))

    def test_restart_comparison_normalizes_only_exact_builtin_default_template(self):
        sid='123e4567-e89b-42d3-a456-426614174000'
        snapshot={'model':'selected','effort':'xhigh','collaborationMode':{'mode':'default','settings':{
            'model':'selected','reasoning_effort':'xhigh','developer_instructions':DEFAULT_INSTRUCTIONS[0].replace('\n','\r\n')}}}
        operation={'recovery_context':{sid:{'settings_snapshot':snapshot,'settings':fingerprint(snapshot,sid,normalize_builtin=False)}}}
        self.assertTrue(migrate_operation(operation))
        resumed=copy.deepcopy(snapshot);resumed['collaborationMode']['settings']['developer_instructions']=None
        self.assertEqual(operation['recovery_context'][sid]['settings'],fingerprint(resumed,sid))
        for changed in [dict(resumed,model='other'),dict(resumed,effort='high')]:
            self.assertNotEqual(fingerprint(changed,sid),fingerprint(snapshot,sid))
        custom=copy.deepcopy(snapshot);custom['collaborationMode']['settings']['developer_instructions']+='\nCustom instruction'
        self.assertNotEqual(fingerprint(custom,sid),fingerprint(resumed,sid))
        altered=copy.deepcopy(operation);altered['recovery_context'][sid]['settings']='not-the-original-fingerprint'
        self.assertFalse(migrate_operation(altered))

    def test_health_continues_while_checks_wait_and_errors_survive_restart_without_payloads(self):
        with tempfile.TemporaryDirectory() as folder:
            root=pathlib.Path(folder)
            with patch.object(controller,'LOCALDATA',root),patch.object(controller,'STATUS',root/'status.json'),patch.object(controller,'JOURNAL',root/'journal.json'):
                c=controller.Controller(accounts=object(),adapters={})
                c.stage='checking account ownership';c.keep_awake=True
                health=threading.Thread(target=c.health_loop);health.start()
                try:
                    deadline=time.monotonic()+2
                    while not (root/'monitor-health.json').exists() and time.monotonic()<deadline:time.sleep(.01)
                    self.assertTrue(json.loads((root/'monitor-health.json').read_text())['keep_awake'])
                    try:raise RuntimeError('sensitive-payload-that-must-not-be-logged')
                    except RuntimeError as exc:c.diagnose(c.stage,exc)
                    self.assertNotIn('sensitive-payload',(root/'diagnostics.json').read_text())
                    restarted=controller.Controller(accounts=object(),adapters={})
                    try:self.assertIn('RuntimeError',restarted.logs[-1]['message'])
                    finally:restarted.refresh_pool.shutdown(wait=True)
                finally:
                    c._health_stop.set();health.join(timeout=4);c.refresh_pool.shutdown(wait=True)


if __name__=='__main__':unittest.main()

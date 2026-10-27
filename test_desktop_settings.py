import copy
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from desktop_settings import fingerprint,git_workspace_identity,migrate_operation

SID='123e4567-e89b-42d3-a456-426614174000'

class WorktreeSettings(unittest.TestCase):
    def test_verified_worktree_roots_match_in_both_protocols_and_migrate_old_receipts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=pathlib.Path(temporary);main=root/'main';work=root/'work';other=root/'other'
            for repo in (main,other):
                (repo/'.git').mkdir(parents=True)
                (repo/'.git/HEAD').write_text('ref: refs/heads/main\n')
                (repo/'.git/config').write_text('[core]\nrepositoryformatversion = 0\n')
            work.mkdir();admin=main/'.git/worktrees/work';admin.mkdir(parents=True)
            (work/'.git').write_text('gitdir: '+str(admin)+'\n')
            (admin/'commondir').write_text('../..\n')
            (admin/'gitdir').write_text(str(work/'.git')+'\n')
            self.assertEqual(git_workspace_identity(str(main)),git_workspace_identity(str(work)))
            before={'cwd':str(work),'model':'chosen','effort':'xhigh',
                'permissions':{'approvalPolicy':'never','sandboxPolicy':{'type':'dangerFullAccess'},
                               'runtimeWorkspaceRoots':[str(main)]}}
            after=copy.deepcopy(before);after['permissions']['runtimeWorkspaceRoots']=[str(work)]
            self.assertEqual(fingerprint(before,SID),fingerprint(after,SID))
            self.assertEqual(before['permissions']['runtimeWorkspaceRoots'],[str(main)])
            duplicate=copy.deepcopy(after);duplicate['permissions']['runtimeWorkspaceRoots'].append(str(main))
            self.assertEqual(fingerprint(before,SID),fingerprint(duplicate,SID))
            changed=copy.deepcopy(after);changed['permissions']['runtimeWorkspaceRoots']=[str(other)]
            self.assertNotEqual(fingerprint(before,SID),fingerprint(changed,SID))
            restricted=copy.deepcopy(before);restricted['permissions']['sandboxPolicy']['type']='workspaceWrite'
            restricted_after=copy.deepcopy(after);restricted_after['permissions']['sandboxPolicy']['type']='workspaceWrite'
            self.assertNotEqual(fingerprint(restricted,SID),fingerprint(restricted_after,SID))
            old=fingerprint(before,SID,normalize_git_roots=False)
            op={'recovery_context':{SID:{'settings':old,'settings_snapshot':before}},
                'metadata':{'session':SID,'settings':old},'interruption_receipts':{SID:{'settings':old}}}
            self.assertTrue(migrate_operation(op));self.assertEqual(op['metadata']['settings'],fingerprint(after,SID))
            self.assertEqual(op['interruption_receipts'][SID]['settings'],op['metadata']['settings'])
            self.assertFalse(migrate_operation(op))
            self.assertEqual(op['recovery_context'][SID]['settings_snapshot'],before)
            if shutil.which('node'):
                code="const fs=require('node:fs');const {summarize}=require('./desktop_bridge.cjs');const a=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(a.map(s=>summarize({id:s.id,turns:[],threadSettings:s.settings},'owner').settings_fingerprint)));"
                cases=[before,after,duplicate,changed,restricted,restricted_after]
                result=subprocess.check_output(['node','-e',code],input=json.dumps([{'id':SID,'settings':s} for s in cases]),text=True,cwd=pathlib.Path(__file__).parent)
                self.assertEqual(json.loads(result),[fingerprint(s,SID) for s in cases])
            (admin/'gitdir').write_text(str(other/'.git'))
            self.assertIsNone(git_workspace_identity(str(work)))

    def test_migration_rejects_unrelated_settings_hash(self):
        original={'recovery_context':{SID:{'settings':'unrelated','settings_snapshot':{'model':'chosen'}}}}
        saved=copy.deepcopy(original)
        self.assertFalse(migrate_operation(original));self.assertEqual(original,saved)

if __name__=='__main__':unittest.main()

import pathlib,tempfile,unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
import app

class PanelSelection(unittest.TestCase):
    def panel(self):
        panel=app.Panel.__new__(app.Panel);panel.refreshing=False
        tree=Mock();tree.exists.side_effect=lambda sid:sid=='visible';tree.selection.return_value=()
        panel.trees={'orca':tree};panel.messages={'orca':Mock()};panel.refresh=Mock();panel.update_status=Mock()
        return panel,tree

    def test_clicking_new_chat_adds_it_without_dropping_hidden_or_existing_chats(self):
        panel,tree=self.panel();tree.identify_row.return_value='new-chat';panel.render_chats=Mock();panel.chat_rows={}
        cfg={'orca':{'enabled':True,'sessions':['visible','hidden']}}
        with patch.object(app,'config',return_value=cfg),patch.object(app,'atomic_json') as save:
            self.assertEqual(panel.click_chat('orca',SimpleNamespace(y=15)),'break')
            self.assertEqual(save.call_args.args[1]['orca']['sessions'],['visible','hidden','new-chat'])
            panel.click_chat('orca',SimpleNamespace(y=15))
            self.assertEqual(save.call_args.args[1]['orca']['sessions'],['visible','hidden'])

    def test_toggling_monitor_uses_saved_checkbox_selection_not_temporary_highlight(self):
        panel,tree=self.panel();tree.selection.return_value=('keyboard-focus-only',)
        cfg={'orca':{'enabled':True,'sessions':['visible','hidden']}}
        with patch.object(app,'config',return_value=cfg),patch.object(app,'atomic_json') as save:
            panel.toggle('orca')
            self.assertEqual(save.call_args.args[1]['orca']['sessions'],['visible','hidden'])

    def test_blank_panel_click_restores_selection_without_erasing_monitored_chats(self):
        panel,tree=self.panel();tree.identify_row.return_value=''
        cfg={'orca':{'enabled':True,'sessions':['visible','temporarily-missing']}}
        with patch.object(app,'config',return_value=cfg),patch.object(app,'atomic_json') as save:
            panel.select('orca',SimpleNamespace(y=300));save.assert_not_called()
            tree.selection_set.assert_called_once_with(['visible'])

    def test_toggle_preserves_selected_chat_absent_during_discovery_failure(self):
        panel,tree=self.panel();cfg={'orca':{'enabled':True,'sessions':['temporarily-missing']}}
        with patch.object(app,'config',return_value=cfg),patch.object(app,'atomic_json') as save:
            panel.toggle('orca')
            self.assertEqual(save.call_args.args[1]['orca'],{'enabled':False,'sessions':['temporarily-missing']})

if __name__=='__main__':unittest.main()

import pathlib
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

import app
from accounts import atomic_json, load_json


class PanelLayout(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = pathlib.Path(self.temp.name)
        self.config_path = root / 'config.json'
        self.status_path = root / 'status.json'
        atomic_json(self.config_path, {
            'orca': {'enabled': True, 'sessions': ['orca-chat']},
            'codex': {'enabled': True, 'sessions': ['existing', 'hidden']},
            'included_resets': False,
        })
        rows = [{'id': sid, 'title': title, 'status': 'idle', 'goal_status': None}
                for sid, title in [('existing', 'Already monitored'), ('extra', 'Extra saved chat')]]
        atomic_json(self.status_path, {
            'sessions': {'orca': [], 'codex': rows},
            'messages': {a: 'A long account recovery message. ' * 12 for a in ('orca', 'codex')},
            'accounts': [{'key': str(i), 'label': f'Account {i}', 'sources': ['Orca'],
                          'valid': True, 'five_remaining': 80, 'weekly_remaining': 90} for i in range(40)],
            'logs': [{'time': '12:00:00', 'message': 'A long activity entry. ' * 40}],
        })
        for change in (
                patch.object(app, 'CONFIG', self.config_path),
                patch.object(app, 'STATUS', self.status_path),
                patch.object(app, 'config', side_effect=lambda: load_json(self.config_path))):
            change.start()
            self.addCleanup(change.stop)

    def close_panel(self, panel):
        for timer in panel.root.tk.call('after', 'info'):
            panel.root.after_cancel(timer)
        panel.root.destroy()

    def panel(self, scale=None):
        original = tk.Tk

        def scaled_root():
            try:
                root = original()
            except tk.TclError as exc:
                self.skipTest(f'A desktop display is required: {exc}')
            if scale is not None:
                root.tk.call('tk', 'scaling', scale)
            return root

        with patch.object(app.tk, 'Tk', side_effect=scaled_root):
            panel = app.Panel()
        panel.update_status()
        return panel

    def assert_on_screen(self, panel, widget):
        self.assertTrue(widget.winfo_ismapped(), str(widget))
        x = widget.winfo_rootx() - panel.root.winfo_rootx()
        y = widget.winfo_rooty() - panel.root.winfo_rooty()
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + widget.winfo_width(), panel.root.winfo_width())
        self.assertLessEqual(y + widget.winfo_height(), panel.root.winfo_height())

    def test_buttons_tables_options_and_footer_remain_accessible_when_resized_and_scaled(self):
        for scale in (1.0, 4/3, 5/3, 2.0):
            panel = self.panel(scale)
            try:
                for size in ('1140x740', '960x600', '760x520'):
                    with self.subTest(scale=scale, size=size):
                        panel.root.geometry(size)
                        panel.tabs.select(panel.chats_page)
                        panel.root.update()
                        for name, button in panel.buttons.items():
                            table = panel.trees[name]
                            self.assert_on_screen(panel, button)
                            self.assert_on_screen(panel, table)
                            self.assertGreaterEqual(table.winfo_height(), 85)
                            self.assertLessEqual(table.winfo_rooty() + table.winfo_height(), button.winfo_rooty())
                        self.assert_on_screen(panel, panel.log)
                        self.assertLessEqual(panel.tabs.winfo_y() + panel.tabs.winfo_height(), panel.log.winfo_y())
                        panel.tabs.select(panel.accounts_page)
                        panel.root.update()
                        self.assert_on_screen(panel, panel.accounts)
                        self.assertGreater(panel.accounts.winfo_height(), 150)
                        panel.tabs.select(panel.options_page)
                        panel.root.update()
                        for check in panel.option_controls:
                            self.assert_on_screen(panel, check)
                        self.assert_on_screen(panel, panel.activity)
            finally:
                self.close_panel(panel)

    def test_filters_clicks_and_refresh_keep_selection_and_account_scroll(self):
        panel = self.panel()
        try:
            panel.root.update()
            panel.searches['codex'].set('Extra')
            panel.root.update()
            table = panel.trees['codex']
            self.assertEqual(table.get_children(), ('extra',))
            x, y, width, height = table.bbox('extra', 'monitor')
            table.event_generate('<Button-1>', x=x+width//2, y=y+height//2)
            panel.root.update()
            panel.update_status()
            self.assertEqual(load_json(self.config_path)['codex']['sessions'], ['existing', 'hidden', 'extra'])
            panel.tabs.select(panel.accounts_page)
            panel.root.update()
            panel.accounts.yview_moveto(.5)
            panel.root.update()
            before = panel.accounts.yview()
            panel.update_status()
            panel.root.update()
            self.assertEqual(panel.accounts.yview(), before)
            self.assertFalse(load_json(self.config_path)['included_resets'])
        finally:
            self.close_panel(panel)

    def test_one_minute_interval_and_detached_selection_remain_visible(self):
        panel=self.panel()
        try:
            self.assertIn('refresh every 1 minute',panel.account_heading.cget('text'))
            self.assertTrue(panel.trees['codex'].exists('hidden'))
            self.assertEqual(panel.trees['codex'].item('hidden','values')[1],'Waiting to attach')
            self.assertIn('hidden',panel.trees['codex'].selection())
            panel.update_status()
            self.assertIn('hidden',load_json(self.config_path)['codex']['sessions'])
        finally:self.close_panel(panel)

    def test_exhausted_account_is_marked_as_waiting_instead_of_available(self):
        status=load_json(self.status_path)
        status['accounts'][0]['five_remaining']=0
        atomic_json(self.status_path,status)
        panel=self.panel()
        try:self.assertEqual(panel.accounts.item('0','values')[3],'Waiting for allowance reset')
        finally:self.close_panel(panel)


if __name__ == '__main__':
    unittest.main()

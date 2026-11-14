from pathlib import Path
import importlib
import json
import sys
import tempfile
import threading
import unittest

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class BrowserWorkflow(unittest.TestCase):
    def test_scope_archive_and_dataset_roundtrip(self):
        product = json.loads((ROOT / 'product.json').read_text())
        project = json.loads((ROOT / 'package.json').read_text())
        module_name = project['scripts']['start'].split()[2]
        server_module = importlib.import_module(module_name + '.server')
        with tempfile.TemporaryDirectory() as state:
            server = server_module.make_server(ROOT, state, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        page = browser.new_page(viewport={'width': 1280, 'height': 720})
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))

                        def recompute():
                            with page.expect_response(lambda r: '/api/explore' in r.url) as response_info:
                                page.locator('#inspect').click()
                            self.assertTrue(response_info.value.ok)

                        page.goto(f'http://127.0.0.1:{server.server_port}', wait_until='networkidle')
                        page.locator('#ranking-rows tr').first.wait_for()
                        self.assertEqual(page.locator('h1').inner_text(), product['name'])
                        self.assertGreater(page.locator('#rows tr').count(), 0)

                        # --- 综合排序：连通组、评委表、标准表、行内依据 ---
                        page.locator('#ranking-rows tr').first.wait_for()
                        ranking_rows = page.locator('#ranking-rows tr').count()
                        self.assertGreater(ranking_rows, 0)
                        self.assertEqual(page.locator('#reviewer-rows tr').count(), 12)
                        self.assertEqual(page.locator('#criteria-rows tr').count(), 3)
                        self.assertIn('互不连通', page.locator('.comparability').inner_text())
                        page.locator('#ranking-rows tr').first.click()
                        detail = page.locator('#details').inner_text()
                        self.assertIn('可比较项目', detail)
                        self.assertIn('原始评分格', detail)
                        self.assertIn('不可比项目数', detail)
                        page.locator('#reviewer-rows tr').first.click()
                        self.assertIn('overall_effect', page.locator('#details').inner_text())

                        # --- 原始评分概览与原始记录仍可核对 ---
                        page.locator('#rows tr').first.click()
                        self.assertIn('{', page.locator('#details').inner_text())
                        page.locator('#raw').click()
                        table = next(iter(json.loads((ROOT / 'fixtures/data.json').read_text())))
                        page.wait_for_function(
                            "key => document.querySelector('#details').textContent.includes(JSON.stringify(key))",
                            arg=table)

                        # --- 权重调整与情景对照：不是拖一次滑块就固定名次 ---
                        page.locator('#snapshot').click()
                        page.locator('#scenario-caption').filter(has_text='对照情景已保存').wait_for()
                        page.locator('.weight-input').first.fill('10')
                        recompute()
                        page.locator('#scenario-rows tr.rank-shift').first.wait_for()
                        self.assertGreater(page.locator('#scenario-rows tr.rank-shift').count(), 0)
                        self.assertEqual(
                            page.locator('#ranking-rows tr').count(), ranking_rows)

                        # --- 主席手动回避，服务端剔除并在页面解释 ---
                        page.once('dialog', lambda dialog: dialog.accept('浏览器回归回避'))
                        page.locator('#add-exclusion').click()
                        page.locator('.exclusion-chip').wait_for()
                        recompute()
                        page.locator('#ranking-notes').filter(has_text='回避').wait_for()

                        # --- 利益冲突开关：关闭时给出明确警示 ---
                        page.locator('#exclude-conflicts').uncheck()
                        recompute()
                        page.wait_for_function(
                            "() => document.querySelector('#ranking-notes').textContent.includes('关闭了利益冲突自动回避')")
                        page.locator('#exclude-conflicts').check()
                        recompute()

                        # --- 冻结保存：控件锁定，只读核对仍可用 ---
                        page.locator('#title').fill('会议复核 α')
                        page.locator('#save').click()
                        page.locator('#frozen-view:not([hidden])').wait_for()
                        self.assertTrue(page.locator('.weight-input').first.is_disabled())
                        self.assertTrue(page.locator('#inspect').is_disabled())
                        self.assertTrue(page.locator('#raw').is_enabled())
                        saved = json.loads(page.locator('#details').inner_text())
                        page.locator('#raw').click()
                        page.wait_for_function(
                            "key => document.querySelector('#details').textContent.includes(JSON.stringify(key))",
                            arg=table)
                        with page.expect_download() as event:
                            page.locator('#export').click()
                        exported = json.loads(Path(event.value.path()).read_text())
                        self.assertEqual(exported['description']['revision'], saved['revision'])
                        self.assertIn('ranking', exported['description'])
                        self.assertIn('ranking_config', exported['description'])
                        self.assertTrue(exported['data'][table])

                        # --- 返回实时视图后切换范围，再打开保存材料仍冻结 ---
                        page.locator('#return-live').click()
                        page.wait_for_function("() => document.querySelector('#frozen-view').hidden")
                        option = product['options'][1] if len(product['options']) > 1 else product['options'][0]
                        alternative = next(value for value in option[2]
                                          if value != product['selection'][option[0]])
                        page.locator('#select-' + option[0]).select_option(alternative)
                        recompute()
                        page.wait_for_function(
                            "([key, value]) => JSON.parse(document.querySelector('#details').textContent).selection[key] === value",
                            arg=[option[0], alternative])
                        page.locator('#archive button').first.click()
                        page.locator('#frozen-view:not([hidden])').wait_for()
                        reopened = json.loads(page.locator('#details').inner_text())
                        self.assertEqual(saved, reopened)

                        # --- 新权重不能覆盖已保存名单 ---
                        page.locator('#return-live').click()
                        page.wait_for_function("() => document.querySelector('#frozen-view').hidden")
                        page.locator('.weight-input').first.fill('8')
                        recompute()
                        page.locator('#archive button').first.click()
                        page.locator('#frozen-view:not([hidden])').wait_for()
                        still_frozen = json.loads(page.locator('#details').inner_text())
                        self.assertEqual(still_frozen, saved)

                        # --- 幂等导入不产生新版本 ---
                        page.locator('#return-live').click()
                        page.wait_for_function("() => document.querySelector('#frozen-view').hidden")
                        page.locator('#upload').set_input_files(str(ROOT / 'fixtures/data.json'))
                        page.locator('#status').filter(has_text='已导入独立数据版本').wait_for()
                        self.assertEqual(page.locator('#revision option').count(), 1)
                        self.assertEqual(errors, [])
                    finally:
                        browser.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main(verbosity=2)

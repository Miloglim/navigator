import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from freight_flow.core.errors import AgentAuthenticationError, AgentConfigurationError
from freight_flow.transport import agent


class Tests(unittest.TestCase):
    def test_structured_output_parser_accepts_json_after_fence(self):
        value = agent.parse_structured_output('说明\n```json\n{"decision":"quote","reason":"有价格"}\n```')
        self.assertEqual(value["decision"], "quote")

    def test_model_channel_401_is_typed_authentication_failure(self):
        with TemporaryDirectory() as temp:
            runner = agent.Agent('codex', 'model', Path(temp))
            task = {'input': json.dumps({'text': '示例', 'group_knowledge': '测试'}),
                    'stage': 'classify', 'mode': 'production-message', 'run_id': 'run',
                    'id': 'classify', 'generation': 0}
            result = SimpleNamespace(returncode=1, stdout='', stderr='HTTP 401 Unauthorized')
            with patch.object(agent.subprocess, 'run', return_value=result):
                with self.assertRaises(AgentAuthenticationError):
                    runner.execute(task, {})

    def test_unavailable_model_is_configuration_failure_with_safe_diagnostic(self):
        with TemporaryDirectory() as temp:
            runner = agent.Agent('codex', 'unsupported-model', Path(temp))
            task = {'input': json.dumps({'text': '示例', 'group_knowledge': '测试'}),
                    'stage': 'classify', 'mode': 'production-message', 'run_id': 'run',
                    'id': 'classify', 'generation': 0}
            result = SimpleNamespace(
                returncode=1,
                stdout='',
                stderr=('ERROR: unexpected status 404 Not Found: Model "unsupported-model" '
                        'is not supported by any configured account\n'
                        'Authorization: Bearer should-not-be-persisted'),
            )
            with patch.object(agent.subprocess, 'run', return_value=result):
                with self.assertRaisesRegex(AgentConfigurationError, '当前模型不可用'):
                    runner.execute(task, {})
            failure = json.loads((Path(temp) / 'run' / 'classify' / '0' / 'failure.json').read_text(encoding='utf-8'))
            self.assertEqual(failure['model'], 'unsupported-model')
            self.assertGreater(failure['prompt_chars'], 0)
            self.assertIn('404 Not Found', failure['diagnostic'])
            self.assertNotIn('should-not-be-persisted', failure['diagnostic'])
            self.assertIn('[redacted sensitive diagnostic line]', failure['diagnostic'])

    def test_prompt_numbers_do_not_create_false_auth_failure(self):
        with TemporaryDirectory() as temp:
            runner = agent.Agent('codex', 'model', Path(temp))
            task = {'input': json.dumps({'text': '港口 401 403 只是业务数字', 'group_knowledge': '测试'}),
                    'stage': 'classify', 'mode': 'production-message', 'run_id': 'run',
                    'id': 'classify', 'generation': 0}
            result = SimpleNamespace(
                returncode=1, stdout='港口 401 403',
                stderr='ERROR: response_format type is unavailable now')
            with patch.object(agent.subprocess, 'run', return_value=result):
                with self.assertRaisesRegex(AgentConfigurationError, '不支持结构化输出'):
                    runner.execute(task, {})

    def test_production_extract_preserves_allocations(self):
        spec = agent.PRODUCTION_SCHEMAS['extract']['properties']['records']['items']
        row = {name: '' for name in spec['properties']}
        row.update({'_table': 'space', '柜型': '20GP', '箱量': '2',
                    '箱型箱量': '2TEU', '备注': 'ETA 10/23；还空箱期限10/18 12:00',
                    '_source_row': 'bill:EGLV123'})
        agent.validate_schema({'records': [row]}, agent.PRODUCTION_SCHEMAS['extract'])
        self.assertNotIn('ETA', spec['properties'])
        self.assertNotIn('还空箱期限', spec['properties'])

    def test_inventory_can_distinguish_two_allocations_of_same_product(self):
        spec = agent.PRODUCTION_SCHEMAS['inventory']['properties']['cells']['items']
        base = {name: '' for name in spec['properties']}
        base.update({'pod': 'Santos', 'container': '20GP', 'price': '',
                     '箱型箱量': '2TEU', '箱量': '2', 'source_row': 'image:1:row:2'})
        other = {**base, '箱型箱量': '5TEU', '箱量': '5'}
        agent.validate_schema({'cells': [base, other]}, agent.PRODUCTION_SCHEMAS['inventory'])

    def _production_prompt(self, stage):
        with TemporaryDirectory() as temp:
            runner = agent.Agent('codex', 'model', Path(temp))
            task = {'input': json.dumps({'text': '示例', 'group_knowledge': '测试'}),
                    'stage': stage, 'mode': 'production-message', 'run_id': 'run',
                    'id': stage, 'generation': 0}
            captured = {}
            def run(command, **kwargs):
                captured['prompt'] = kwargs['input']
                result = {'ok': True, 'issues': []} if stage == 'audit' else (
                    {'records': []} if stage == 'extract' else {'cells': []})
                Path(command[command.index('-o') + 1]).write_text(json.dumps(result), encoding='utf-8')
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            with patch.object(agent.subprocess, 'run', side_effect=run):
                runner.execute(task, {'extract': {'records': []}})
            return captured['prompt']

    def test_audit_uses_intermediate_contract_and_allocation_units(self):
        prompt = self._production_prompt('audit')
        self.assertIn('JSON Schema', prompt)
        self.assertIn('箱型箱量', prompt)
        self.assertIn('TEU', prompt)
        self.assertIn('抽取允许字段', prompt)
        self.assertIn('输出契约不允许', prompt)
        self.assertIn('ETA', prompt)
        self.assertIn('还空箱期限', prompt)
        self.assertIn('备注', prompt)

    def test_inventory_instructions_exclude_reference_schedule_products(self):
        prompt = self._production_prompt('inventory')
        self.assertIn('同一产品', prompt)
        self.assertIn('参考船期', prompt)
        self.assertIn('独立盘点', prompt)
        self.assertIn('不填tag', prompt)

    def test_source_identity_preserves_bill_rows_without_inventing_products(self):
        for stage in ('extract', 'inventory', 'audit'):
            prompt = self._production_prompt(stage)
            self.assertIn('bill:', prompt)
            self.assertIn('物理行号', prompt)
            self.assertIn('共享同一来源行身份', prompt)
            self.assertIn('不能伪造', prompt)
            self.assertIn('不能展开整张', prompt)

    def test_independent_inventory_obeys_name_and_scope_contract(self):
        prompt = self._production_prompt('inventory')
        self.assertIn('全国港口', prompt)
        self.assertIn('不得缩窄', prompt)
        self.assertIn('Felixstowe', prompt)
        self.assertIn('不凭FEU', prompt)

    def test_fak_without_price_is_uncertain_not_noise(self):
        self.assertIn('FAK', agent.INSTRUCTIONS['classify'])
        self.assertIn('uncertain', agent.INSTRUCTIONS['classify'])

    def test_clipped_port_names_are_source_facts_not_prefix_matches(self):
        for stage in ('extract', 'inventory', 'audit'):
            prompt = self._production_prompt(stage)
            self.assertIn('截断港名', prompt)
            self.assertIn('可见连续字符', prompt)
            self.assertIn('禁止前缀唯一匹配', prompt)
            self.assertIn('不能因两个阶段一致就视为合法主数据', prompt)
            self.assertIn('保留同一完整字符串', prompt)
            self.assertIn('同一bill', prompt)

    def test_space_inventory_reads_prices_across_columns_and_contract_labels(self):
        for stage in ('extract', 'inventory', 'audit'):
            prompt = self._production_prompt(stage)
            self.assertIn('价格5644', prompt)
            self.assertIn('NAC', prompt)
            self.assertIn('合约类型', prompt)
            self.assertIn('不能把O机械改成0', prompt)

    def test_audit_preserves_unresolved_source_names_for_ledger_validation(self):
        prompt = self._production_prompt('audit')
        self.assertIn('统一resolver', prompt)
        self.assertIn('未命中主数据不等于来源事实错误', prompt)
        self.assertIn('failed/awaiting', prompt)
        self.assertIn('真错港', prompt)
        self.assertIn('无依据日期', prompt)

    def test_windows_agent_process_is_headless(self):
        with patch.object(agent.os, "name", "nt"):
            self.assertEqual(
                agent.hidden_subprocess_options(),
                {"creationflags": agent.subprocess.CREATE_NO_WINDOW},
            )

    def test_non_windows_agent_process_keeps_platform_defaults(self):
        with patch.object(agent.os, "name", "posix"):
            self.assertEqual(agent.hidden_subprocess_options(), {})


if __name__ == "__main__":
    unittest.main()

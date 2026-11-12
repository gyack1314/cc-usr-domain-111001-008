"""unittest 发现入口：复用独立可运行脚本中的全部用例。"""
import importlib.util
import pathlib

_SCRIPT = pathlib.Path(__file__).with_name("run_parser_manager_tests.py")
_SPEC = importlib.util.spec_from_file_location("run_parser_manager_tests", _SCRIPT)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

# 显式导出测试类，供 `python -m unittest discover -s tests` 收集。
DomTests = _MODULE.DomTests
FieldValidationTests = _MODULE.FieldValidationTests
RulePackTests = _MODULE.RulePackTests
ReplayAndGateTests = _MODULE.ReplayAndGateTests
CanaryTests = _MODULE.CanaryTests
LifecycleTests = _MODULE.LifecycleTests
PolicyTests = _MODULE.PolicyTests

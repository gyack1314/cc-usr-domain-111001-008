"""解析规则包与信任边界。

规则包是带不可变版本号的 Python 解析单元。安全约束：

- 规则函数只允许来自受信规则目录（trusted 目录下的 .py，由本系统登记），
  网页内容永远是被解析的数据，不会被 import / eval / exec；
- 规则只通过 :mod:`parser_manager.dom` 读取 HTML，不接触网络与文件系统；
- 版本号一经登记不可变：同一版本号再次登记必须内容完全一致，否则拒绝。
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from . import FIELD_NAMES
from .dom import Element
from .fields import CONVERTERS, ConversionError, FieldEvidence, RecordResult, validate_record

# 允许规则声明适用的页面特征键（与 HTML 上的 data-* 对应）。
FEATURE_KEYS = ("layout",)


class RuleError(ValueError):
    """规则包自身定义不合法。"""


@dataclass
class FieldSpec:
    """单个字段的选择器描述。

    selector: CSS 子集选择器（见 dom 模块）
    attr:     取属性值；None 表示取文本
    """

    selector: str
    attr: str | None = None


@dataclass
class RulePack(ABC):
    """规则包基类。具体规则在受信目录中以子类实现。

    注意：version 等声明字段不给 dataclass 默认值，否则子类的类属性赋值
    会被数据字段默认值覆盖；子类必须在 dataclass 字段中显式传值。
    """

    version: str
    description: str
    applies_to_sources: tuple[str, ...]
    # 页面特征全部匹配时适用，例如 {"layout": "old"}；空 dict 表示适用全部。
    page_features: dict[str, str] = field(default_factory=dict)

    @abstractmethod
    def parse(self, root: Element) -> dict[str, FieldSpec]:
        """返回字段名 -> FieldSpec 的选择器映射。"""

    # -- 框架级抽取流程，子类一般不需要覆盖 ---------------------------------

    def applies_to_page(self, features: dict[str, str]) -> bool:
        return all(features.get(key) == value for key, value in self.page_features.items())

    def extract(
        self,
        root: Element,
        *,
        source_id: str,
        record_id: str,
        page: str,
        page_features: dict[str, str],
        run_id: str | None = None,
    ) -> RecordResult:
        """按选择器取原文与 DOM 位置，逐字段转换并执行记录级校验。"""
        from .dom import select_one  # 局部导入避免循环依赖

        result = RecordResult(
            source_id=source_id,
            record_id=record_id,
            page=page,
            rule_version=self.version,
            run_id=run_id,
        )
        specs = self.parse(root)
        unknown = set(specs) - set(FIELD_NAMES)
        if unknown:
            raise RuleError(f"规则 {self.version} 声明了未知字段: {sorted(unknown)}")

        for name in FIELD_NAMES:
            spec = specs.get(name)
            if spec is None:
                result.fields[name] = FieldEvidence(name, None, None, None)
                continue
            node = select_one(root, spec.selector)
            if node is None:
                result.fields[name] = FieldEvidence(
                    name, None, None, None, error=f"选择器未命中: {spec.selector}"
                )
                continue
            raw = node.attrs.get(spec.attr, "") if spec.attr else node.text_content()
            evidence = FieldEvidence(
                name=name,
                raw=raw,
                dom_path=node.dom_path(),
                css_path=node.css_path(),
            )
            try:
                evidence.value = CONVERTERS[name](raw)
            except ConversionError as exc:
                evidence.error = str(exc)
            result.fields[name] = evidence

        validate_record(result)
        return result

    def content_hash(self) -> str:
        """对规则实现类源码与声明做哈希，作为不可变性校验依据。"""
        module = sys.modules.get(self.__class__.__module__)
        source = Path(module.__file__).read_text(encoding="utf-8") if module and module.__file__ else ""
        payload = "\x1f".join(
            [
                self.version,
                self.description,
                "|".join(self.applies_to_sources),
                repr(sorted(self.page_features.items())),
                self.__class__.__name__,
                source,
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 受信规则目录加载：唯一允许引入新解析代码的入口。
# ---------------------------------------------------------------------------

class RuleRegistry:
    def __init__(self, trusted_dir: str | Path):
        self.trusted_dir = Path(trusted_dir).resolve()
        self._packs: dict[str, RulePack] = {}
        self._hashes: dict[str, str] = {}

    def load_directory(self) -> None:
        """加载受信目录下全部规则模块（目录由部署方控制，非网页内容）。"""
        if not self.trusted_dir.is_dir():
            raise RuleError(f"受信规则目录不存在: {self.trusted_dir}")
        for path in sorted(self.trusted_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            self._load_file(path)

    def _load_file(self, path: Path) -> None:
        path = path.resolve()
        if self.trusted_dir not in path.parents:
            raise RuleError(f"拒绝加载受信目录之外的规则文件: {path}")
        module_name = f"trusted_rules_{path.stem}_{hashlib.sha256(str(path).encode()).hexdigest()[:10]}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)  # 受信代码；网页 HTML 永远不走这里
        for candidate in vars(module).values():
            # 只收具体规则类：必须在本模块声明非空版本号，且实现了抽象 parse。
            # 不依赖类身份比较，避免模块重载导致基类副本被误收。
            if not (isinstance(candidate, type) and issubclass(candidate, RulePack)):
                continue
            if not candidate.__dict__.get("version"):
                continue
            if candidate.__abstractmethods__:
                continue
            self.register(candidate())

    def register(self, pack: RulePack) -> None:
        self._validate(pack)
        digest = pack.content_hash()
        if pack.version in self._packs:
            if self._hashes[pack.version] != digest:
                raise RuleError(
                    f"规则版本 {pack.version} 已存在且内容不同；版本号不可变"
                )
            return
        self._packs[pack.version] = pack
        self._hashes[pack.version] = digest

    @staticmethod
    def _validate(pack: RulePack) -> None:
        if not pack.version:
            raise RuleError("规则包缺少版本号")
        if not pack.applies_to_sources:
            raise RuleError(f"规则包 {pack.version} 必须声明适用来源")
        unknown_features = set(pack.page_features) - set(FEATURE_KEYS)
        if unknown_features:
            raise RuleError(f"规则包 {pack.version} 含未知页面特征: {sorted(unknown_features)}")
        # 用一个空 DOM 触发一次 parse 签名检查（规则内部不应有副作用要求）。
        from .dom import Element

        try:
            specs = pack.parse(Element("#root", {}))
        except Exception as exc:  # noqa: BLE001 - 规则自检失败统一包装
            raise RuleError(f"规则包 {pack.version} 自检失败: {exc}") from exc
        unknown = set(specs) - set(FIELD_NAMES)
        if unknown:
            raise RuleError(f"规则包 {pack.version} 返回未知字段: {sorted(unknown)}")

    def get(self, version: str) -> RulePack:
        try:
            return self._packs[version]
        except KeyError:
            raise RuleError(f"未登记的规则版本: {version}") from None

    def versions(self) -> list[str]:
        return sorted(self._packs)

    def all_packs(self) -> list[RulePack]:
        return [self._packs[version] for version in sorted(self._packs)]

    def packs_for_source(self, source_id: str) -> list[RulePack]:
        return [pack for pack in self.all_packs() if source_id in pack.applies_to_sources]

"""解析规则管理模块。

覆盖科普活动页面解析规则的候选登记、离线重放、门槛评估、按来源灰度
启用、原子切换、在途版本绑定与回滚。只依赖 Python 标准库。

子模块：
- dom:      最小 HTML 解析与定位（保留 DOM 位置证据）
- fields:   字段类型、转换与跨记录校验
- rules:    规则包（含信任边界：规则函数只来自受信规则目录）
- replay:   对存档网页离线重放，产出差异/匹配率/隔离变化
- store:    SQLite 持久化与原子版本切换
- canary:   确定性灰度分桶
- manager:  对外编排门面
- cli:      命令行入口
"""

REQUIRED_FIELDS = ("activity_id", "start_time", "end_time", "place", "participants")
FIELD_NAMES = REQUIRED_FIELDS

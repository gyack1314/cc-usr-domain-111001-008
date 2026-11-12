# 科普活动页面解析规则的灰度迁移资料源

本项目保存本地合成资料及对应的来源站行为，供资料接入开发使用。内容取自虚构场景，不代表真实活动、人员、设施或设备参数。来源服务只模拟公开读取与指定的表单行为，不包含资料采集端。

## 环境与运行

使用 Python 3.11 及以上版本，仅依赖标准库。来源只监听本机地址，端口可以通过参数调整。两个终端分别启动不同端口可以表示两个独立来源。

```sh
python -m source_site --port 8090
```

`source_site/source.py` 管理来源响应，`source_site/__main__.py` 是 HTTP 适配层，`sources/` 保存原文与接口约定。状态只存在来源进程内，重启来源会恢复初始样本。`/__control` 和 `/__calls` 属于本地运维入口，资料接入端的正常操作不使用它们。运维端可以通过 POST `/__control` 提供 JSON 对象改变 phase 或更新 state。phase 列表与业务接口见下文，细节见 `sources/contract.json`。

- `GET /index`：活动页面入口
- `GET /page/{old,new,mixed}`：HTML原文，混合版本和错误字段
- `confirmed.json`：人工确认部分字段，未给出的字段不进入分母
- `stored_pages`：sources下HTML可作为离线重放原文

用 Python 的 HTTP 客户端推进来源状态，例如：

```sh
python -c 'import urllib.request; r=urllib.request.Request("http://127.0.0.1:8090/__control", data=b"{\"phase\":\"base\"}", headers={"Content-Type":"application/json"}); print(urllib.request.urlopen(r).read().decode())'
```

## 开发检查

以下检查覆盖来源协议、异常响应和真实本地 HTTP 启动。检查不会要求访问公网，也不会写入业务状态目录。

```sh
python -m unittest discover -s tests -v
python -m compileall -q .
```

容器环境也可以复现相同检查：

```sh
docker build -t parser-release-source .
docker run --rm --network none parser-release-source
```

## 解析规则管理模块（parser_manager）

`parser_manager/` 实现科普活动页面解析规则的候选登记、离线重放、门槛评估、
按来源灰度启用、原子切换、在途版本绑定与回滚。只依赖标准库；解析规则函数
只来自受信目录 `trusted_rules/`（部署方维护），网页 HTML 永远是被解析的数据，
不会被 import/eval/exec。

核心约束与实现位置：

- 规则包带不可变版本号、适用来源与页面特征（`parser_manager/rules.py`），
  同版本号内容变化会被拒绝；受信目录之外的规则文件拒绝加载。
- 每次抽取保存规则版本、字段原文、DOM 位置（`dom_path`/`css_path`）与转换错误
  （`parser_manager/fields.py`）；参与人数必须是非负整数，结束时间不得早于
  开始时间，同来源同活动编号同轮地点不同会冲突隔离；缺必要字段整条隔离，
  不输出半条正常记录，也不用默认值掩盖问题。
- 候选只对 `sources/` 存档网页离线重放（`parser_manager/replay.py`），报告与
  当前规则的逐字段差异、人工确认样本逐字段匹配率（分母为人工非空字段，实际
  缺失算不匹配）和隔离比例（按来源页面数）。测试中断开 socket 验证重放不触网。
- 启用门槛由 `sources/release-policy.json` 给出（匹配率下限、隔离比例上限、
  灰度百分比）；无确认样本一律禁止自动启用（`parser_manager/gate.py`）。
- 灰度分桶确定性可复现（`parser_manager/canary.py`）：
  `int(sha256(record_id).hexdigest()[:8], 16) % 100 < canary_percent` 走新版本，
  重跑不换组。
- 启用/回滚是 SQLite 单事务原子切换（`parser_manager/store.py`）；在途作业在
  领取时绑定规则版本，切换不影响在途；回滚只恢复对外当前视图，新版本证据保留。

### 命令行用法

所有数据路径通过参数传入，建议在隔离模式运行：

```sh
python3 -I parser_manager/cli.py rules \
  --db state/manager.db --rules-dir trusted_rules --archive sources \
  --confirmed sources/confirmed.json --policy sources/release-policy.json \
  --source science_station --bootstrap v1-legacy

python3 -I parser_manager/cli.py register v2-naive   # 其余参数同上
python3 -I parser_manager/cli.py candidates ...
python3 -I parser_manager/cli.py diff <candidate_id> ...
python3 -I parser_manager/cli.py canary <candidate_id> --percent 50 ...
python3 -I parser_manager/cli.py run-batch --run-id round-1 ...
python3 -I parser_manager/cli.py enable <candidate_id> ...
python3 -I parser_manager/cli.py rollback --reason "回滚说明" ...
python3 -I parser_manager/cli.py view ...
python3 -I parser_manager/cli.py provenance <record_id> ...
```

端到端可复现演示（坏候选串位被发现并拒绝、好候选灰度、在途切换、回滚视图）：

```sh
python3 -I examples/release_demo.py
```

模块自带检查（隔离模式，31 项，含 CLI 子进程冒烟与断网重放）：

```sh
python3 -I tests/run_parser_manager_tests.py
```

提供的三版规则（`trusted_rules/station_rules.py`）：`v1-legacy` 为旧选择器；
`v2-naive` 是错误候选，在新版式把年份 `2026` 当成参与人数，重放时被人工确认
样本以匹配率 5/6 拒绝；`v2-layout` 按版式切换选择器，匹配率 6/6、隔离比例 1/3，
通过门槛（匹配率下限 1.0、隔离上限 0.4）。

来源的HTTP异常是合成协议行为，便于资料接入端开发和诊断。样本可扩展；业务数据、原始HTML以及接口响应不应被当作真实部署凭据或真实业务事实。

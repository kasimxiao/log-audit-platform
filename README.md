# 日志审计平台 POC

单账号部署，默认区域东京（ap-northeast-1），可通过部署参数切换。

> 截图与文档中的账号 ID、桶名、域名、实例 ID 等均为示例值，请以你自己部署后的 CloudFormation 输出为准。

- **方案说明（架构图、时序图、各类日志的采集 → 处理 → 查询）：[docs/solution-overview.md](docs/solution-overview.md)**（编辑源为同目录的 `solution-overview.html`，改完运行 `python3 scripts/export_solution_md.py` 重新导出）
- 架构与需求基线：[docs/architecture.md](docs/architecture.md)
- 智能问答规格：[docs/nlq-agent.md](docs/nlq-agent.md)

## 目录
| 路径 | 内容 |
|---|---|
| `backend/app/` | Lambda 代码：api（54 个接口）/ worker（智能问答、评估、告警）/ extractor（入库，独立包含 pyarrow）/ syncsvc（每日同步）/ source_access（S3 读权限收敛）/ parsers（字段预解析）/ collector.sh（主机采集脚本） |
| `infra/` | CDK：`LogAuditPlatform`（平台）、`LogAuditTestEnv`（3 台测试主机 + 2 个内网 ALB） |
| `web/` | 前端（原生 JS），`eval.html` 为评估控制台 |
| `evals/` | 黄金集 `dataset.jsonl`（28 条）、`run_eval.py`（手动触发评估） |
| `scripts/` | 构建、初始化、测试环境模拟、冒烟、截图、清理 |

## 部署 / 重建
```bash
python3 -m venv .venv && .venv/bin/pip install sqlglot maxminddb boto3 pyarrow tzdata
# IP 归属库（DB-IP Lite，CC BY 4.0，未纳入仓库）
mkdir -p data/geo && curl -sSfL https://download.db-ip.com/free/dbip-country-lite-2026-11.mmdb.gz | gunzip > data/geo/dbip-country-lite.mmdb
scripts/build_lambda.sh
export AWS_PROFILE=<你的 profile>   # 或不设置，使用默认凭证链
cd infra && npm i && npx cdk deploy --all -c vpcId=<vpc-xxx> --require-approval never && cd ..   # 不传 vpcId 则只部署平台栈
.venv/bin/python scripts/bootstrap.py   # 合成数据、建表、数据源、admin、发布前端（幂等）
scripts/testenv_setup.sh                 # 测试环境：ALB 流量 + SSH 爆破模拟（需已部署测试环境栈）
```
管理员凭证：`~/.log-audit-admin.txt`。只改前端：`aws s3 sync web/ s3://<WebBucket>/` 后对 CloudFront 做缓存失效。

### 部署参数
| 参数 | 传法 | 说明 |
|---|---|---|
| 区域 | CDK：`-c region=<区域>`；脚本：环境变量 `LA_REGION=<区域>` | 默认 `ap-northeast-1`，两处需保持一致 |
| 测试环境 VPC | CDK：`-c vpcId=<vpc-xxx>` | VPC 需有带 NAT 的私有子网；不传则不部署 `LogAuditTestEnv`（平台栈照常部署）；`scripts/cleanup.sh` 会从已部署栈自动反查，也可用 `LA_VPC_ID` 指定 |
| AWS 凭证 | `AWS_PROFILE=<你的 profile>` 或默认凭证链 | 需有部署 CDK 栈的权限 |
| 管理员凭证 | `~/.log-audit-admin.txt` | `bootstrap.py` 首次运行生成（权限 600），冒烟/截图/评估脚本从这里读取 |

其余资源名（桶、表、用户池、CloudFront 分配、实例 ID 等）均由 CloudFormation 输出自动获取：脚本通过 `describe-stacks` 读取 `LogAuditPlatform` / `LogAuditTestEnv` 的输出，无需手填。

## 运维脚本
- `scripts/migrate_hostlogs_parquet.py`：主机日志 JSON → Parquet 三级分区迁移（`--rebuild-eval` 按新解析规则重建评估库）
- `scripts/migrate_alb_tables.py`：按当前正则重建全部 ALB 表
- `scripts/test_collector.py <实例> "<kind|binmode|logtype|path>" ... [--shell 预处理]`：在主机上跑当前采集脚本，上传到 collector-test/（不入库）并展示包内容
- `scripts/hostlogs_fingerprint.py <输出.json>`：主机日志一致性指纹（改动前后对比）

## 验证
- `scripts/smoke_sync.py`：配置 3 台主机 → 手动同步 → 等待入库（第二次运行用来验证增量）
- `scripts/smoke_query.py`：审计场景、历史检索、ALB 报表、CloudTrail、实时查询（含越权路径被拒）
- `scripts/check_reference_sql.py`：评估集参考 SQL 自检（不调用模型）
- `scripts/screenshots.py`：逐页截图到 /tmp/la-shots

## 评估（只手动触发）
评估控制台「手动运行」，或 `AWS_PROFILE=<你的 profile> python3 evals/run_eval.py --note "..."`。基线尚未运行。

## 已知限制（POC）
- 跨区域隔离子网：没有出网的子网里，跨区域主机无法上传到东京桶（方案见 docs/solution-overview.html 第 8 节，未实施）
- 已压缩的轮转文件（.gz 等）不补采；轮转补采只认 `.1` / `.0` / `-日期`
- Webhook 告警未做端到端验证（测试环境没有可用的接收端）；邮件需收件人确认订阅
- 本账号东京区开启了 DHMC，未挂实例角色的 la-legacy-03 也显示 SSM 在线
- `hostlogs.dt` = 同步日期，不是事件日期；按事件时间查询用 `event_time`
- 智能问答的表结构说明已更新（分区列、fields、视图），评估基线尚未运行，需要时请手动触发

## 清理
`scripts/cleanup.sh`（删除已部署的栈；平台对 CloudTrail 桶只读，不受影响）

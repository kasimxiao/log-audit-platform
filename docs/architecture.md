# 日志审计平台 POC — 架构与需求基线

## 需求确认（2026-11-11）
| # | 项 | 结论 |
|---|---|---|
| 1-4 | 范围 | 单账号、海外区域、200+ Linux 主机 |
| 5-8 | 接入 | 部分主机未装/未注册 SSM；网络具备；可装代理；按标签纳管，可新加标签 |
| 9-11 | 同步 | 每日一次；二进制日志 原样/转文本 可选；路径支持通配符与模板 |
| 12-14 | 合规 | 不要求 WORM / KMS / 分层 |
| 15 | 主机查询 | 实时（SSM 到主机 grep/tail）+ 历史（Athena 查 S3 副本）都要 |
| 16 | 审计场景 | 预置 |
| 17-20 | ALB/CloudTrail | ALB 桶可配置（10+ ALB）、预置报表 + 地理分布、自然语言查询、加 CloudTrail |
| 21-23 | 平台 | 自建管理员/用户；平台操作留痕；访问方式不区分 |
| 24 | 形态 | Demo / POC |

> 说明：实时查询的必要性不在于"Athena 不支持某些日志"——所有文本日志同步后都能 Athena 查；实时查询解决的是**每日同步带来的时效差**（当天新日志）和**未纳管同步的主机**。

## 架构
```
浏览器 ──CloudFront──┬─ /*      → S3 (静态前端, OAC)
                     └─ /api/*  → API Gateway HTTP API (Cognito JWT) → Lambda api
                                                         │
     ┌───────────────┬──────────────┬────────────────────┼──────────────────────┐
  EC2/SSM 多区域   DynamoDB 单表   Athena/Glue         Lambda worker(异步)   Cognito(admin/user)
  (主机列表/纳管/  (配置/同步记录/ (hostlogs/alb_*/    (NLQ 智能体 Bedrock
   实时查询)        作业/操作审计)   cloudtrail)          Claude、评估运行)

每日 02:00 EventBridge Scheduler → Lambda sync → SSM SendCommand(采集脚本 + 预签名 PUT URL)
   主机：文本按 inode+偏移增量（轮转补采）/ wtmp、btmp 按整条记录取字节 / lastlog 按 UID 直读 / journald
         → tar.gz（含时区、CPU 架构）→ curl PUT 到 S3 staging/
   S3 事件 → Lambda extractor → 解码 utmp、解析时间（按主机时区）、预解析 fields
         → hostlogs/dt=/instance_id=/logtype=/*.parquet，登记 Glue 分区（Athena 表 hostlogs + 7 个视图）
每日 03:00 Scheduler → Lambda worker 告警巡检 → SNS 邮件 / Webhook（飞书、钉钉、企业微信）
```

## 关键设计决策
1. **主机上传用预签名 URL**（S3 区域端点 + SigV4）：实例角色只需 `AmazonSSMManagedInstanceCore`，不给主机任何 S3 写权限；脚本只依赖 bash/coreutils/tar/gzip/curl，AL2023/Ubuntu/RHEL 通用。
2. **增量采集**：主机侧 `/var/lib/log-audit/offsets` 记录 inode+偏移；轮转后按 inode 在同目录找到旧文件（`.1`/`.0`/`-日期`，未压缩）补采尾部，再从头读新文件。上传成功后才提交偏移。
3. **二进制日志**：不依赖 `last`/`lastlog` 命令（Ubuntu 26.04 未预装）。`wtmp/btmp` 按整条 `struct utmp` 记录增量取原始字节（x86_64 384 字节、aarch64 400 字节），由 extractor 解码；`lastlog` 在主机上按 UID×292 字节直读，每日快照；`raw` 模式原样存 `raw/`（不进 Athena）。
4. **AL2023 默认没有 rsyslog**（无 /var/log/secure、cron），模板支持 `journal:<unit>` 伪路径走 journalctl。
4a. **主机日志存储**：Parquet（Snappy），按 `dt / instance_id / logtype` 三级分区（入库时登记 Glue）；入库时按日志类型预解析常用字段到 `fields map<string,string>`，并提供 7 个视图；无时区信息的时间按主机上报的时区换算为 UTC。
5. **实时查询**：SSM `AWS-RunShellScript`，路径/正则以 base64 传参、脚本内只作为引号变量使用（不 eval）；路径必须落在管理员配置的允许根目录下；输出 gzip+base64 回传，绕开 SSM 24KB 输出截断，主机无需 S3 权限。
6. **未纳管主机**：列表标出 SSM 状态；无实例角色的可一键挂 `LogAuditInstanceProfile`；有角色但无 SSM 权限/未装 Agent 给出指引。
7. **ALB**：每个数据源 = 一张 Glue 表（ALB 官方正则 SerDe + 按天分区投影），新增/删除数据源即建/删表。地理分布用 DB-IP Lite（CC BY 4.0）对 Top IP 做离线查表。
8. **CloudTrail**：现有多区域 trail 的桶建投影表（region/date）。
9. **自然语言查询**：Bedrock Claude + 两个工具（查表结构、执行只读 SQL）。安全外置：SQL 守卫（sqlglot 解析，单条 SELECT、表白名单、强制 LIMIT）+ worker 角色无 Glue 写权限 + Athena 工作组扫描量上限。
10. **权限**：admin（配置、纳管、触发同步、用户管理、操作审计、告警设置、评估控制台）/ user（查看与查询）。每个 API 调用写操作审计。查询角色的 S3 读权限按已登记数据源的桶/前缀动态收敛（SourceAccessFn）。
10a. **告警**：每日 03:00 巡检选定审计场景，按场景去重键只通知新增风险；SNS 邮件 + Webhook（仅 https、禁止内网地址）。
11. **评估控制台独立页面**（eval.html，仅 admin），智能问答页不展示任何评估内容；评估只手动触发。

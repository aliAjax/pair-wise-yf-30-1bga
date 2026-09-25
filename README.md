# 药物警戒案例处理系统

使用 Python 标准库实现的独立原型，覆盖多渠道案例接入、去重、随访更正、严重性医学裁定、分国家报告、逾期升级、跨区域权限和案例合并审计。

## 运行

要求 Python 3.11+。

```bash
python3 app.py --db pharmacovigilance.db
```

默认监听 `127.0.0.1:8201`。首页为 `http://127.0.0.1:8201/`，健康检查为 `/health`。

所有接口使用请求头 `X-User-Id`、`X-Role` 和区域角色必需的 `X-Region`。角色为 `reporter`、`regional_lead`、`medical_reviewer`、`global_admin`。

## 主要接口

- `POST /api/cases`：录入案例，`dedupe_key` 相同则返回已存在案例。
- `GET /api/cases`、`GET /api/cases/{id}`：按权限查询。
- `POST /api/cases/{id}/followups`：用 `expected_revision` 防止覆盖随访。
- `POST /api/cases/{id}/medical-review`：医学审核员更新严重性、死亡和关联性。
- `POST /api/cases/{id}/reports`、`POST /api/reports/{id}/submit`：生成并提交分国家报告。
- `POST /api/cases/{id}/merge`：仅全局管理员，且来源与目标须为同产品。合并时把来源案例的随访、医学审核、分国家报告全部迁入目标案例，每条记录以 `origin_case_no` 标注原案例编号（链式合并保留最初来源）。同国家报告冲突时：一侧已提交、一侧待提交保留已提交方；两侧都待提交保留截止更早方（两侧均已提交则保留提交更早方），时间完全相同保留目标方。来源案例置为 `merged`：退出案例列表且随访、审核、报告均不可再更新。
- `GET /api/cases/{id}`：返回 `merged_sources`（递归的合并来源列表与各自合并时间）和 `timeline`（目标案例合并后的统一处置时间线，迁移记录带原案例编号）。
- `POST /api/escalate-overdue`、`GET /api/overdue`：逾期检查与升级。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

该实现使用请求头模拟身份，不含生产级登录、签名和密钥管理；SQLite 与标准库 HTTP 服务适合单机原型。分国家规则采用内置严重 15 天、死亡 7 天、非严重 90 天规则，接入真实监管网关前需按当地法规扩展。

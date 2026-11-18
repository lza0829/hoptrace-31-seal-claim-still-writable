# 途迹（Hoptrace）

轨迹点装载 → 运单时间线物化 → 迟到旁路与脏点隔离 → 状态证明互证。

**不是** Feed 归并、CDC 游标 Sink、质检签发、数仓分层、交班过户、正冲净额。  
**也不是** 电商订单 / 仓库库存 / 外卖工单 CRUD——去皮后是事件装载与有序物化。

## 启动

```bash
# 后端
cd backend
pip install -r requirements.txt
set DATA_DIR=%CD%\data
set FIXTURES_DIR=%CD%\..\fixtures
uvicorn app.main:app --host 127.0.0.1 --port 10800

# 前端
cd frontend
npm install
npm run dev
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://127.0.0.1:5800 |
| 接口 | http://127.0.0.1:10800 |

## UI

三栏测绘板：顶品牌条 + 中竖轴时间线 + 右检查器 + 底 hop 点阵。  
不使用左轨编号导航、不使用顶栏流程 ribbon。

## 时间线哈希封存（timeline_hash_seal）

封存瞬间固化正式有效线点序的 SHA-256 内容哈希与投影快照，落库到 `timeline_seals`
（另附只追加的 `seal_audit_log` 审计表）。封存与写闸在同一 `BEGIN IMMEDIATE` 事务内完成，
提交成功才开闸，失败整体回滚——不存在半截封印。

- 封印后向该运单正式有效线（`channel='primary' AND retracted=0`）的写入/更正由装载入口同事务预检拒绝，
  整批回滚零写入，拦截原因写入审计；显式解封（必填原因）后才可恢复写入。
- 读路径（时间线接口与封存证明接口）用封存哈希重算校验当前点序、点数与投影，
  任何篡改都会得到 409 而非成功回执。

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/api/seals/{waybill_id}` | POST | 封存运单，固化哈希与投影快照 |
| `/api/seals/{waybill_id}` | DELETE | 显式解封，body 必须带 `reason` |
| `/api/seals/{waybill_id}/proof` | GET | 只读封存证明（运单/哈希/点数/投影/封印时刻）+ 实时校验 |
| `/api/seals` | GET | 全部封存记录 |
| `/api/seals/audit/log` | GET | 封印/解封/写闸拦截审计 |
| `/api/ingest/run` | POST | 装载入口，命中写闸时整批拒绝并返回 blocked 原因 |

前端「封存」页与测绘板检查器可发起封存/解封、查看哈希互证结论与审计；
「装载」页在被写闸拒绝时展示被拦运单、原因与封存哈希。

后端测试：`cd backend && pytest tests/`。

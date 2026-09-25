# AI 多节点产品营销生图工作台（MVP）

面向企业产品营销素材生产的 AI 可视化工作台：上传产品图与已确认事实 → 节点画布生成策略 / 单图提示词 / 视觉图 → 选模型生图 → 分层排版导出。依据工作区内 `需求文档 v1` 与 `稿定AI对标增强版 v2` 开发。

## 快速启动

后端可托管构建后的前端，单端口即可使用。首次启动请安装 Python 依赖并构建前端：

```bash
# 1. 安装后端依赖
python -m pip install -r backend/requirements.txt

# 2. 安装前端依赖并构建
cd frontend
npm ci
npm run build
cd ..

# 3. 启动后端（在 workbench 目录执行）
python backend/run.py
# 浏览器打开 http://localhost:8000
```

开发模式（热更新前端，需另开终端）：

```bash
# 终端 1：从 workbench/backend 执行
python run.py

# 终端 2：从 workbench/frontend 执行
npm ci
npm run dev        # http://localhost:5173，API 通过 vite 代理到 :8000
```

开发模式（热更新前端，需另开终端）：

```bash
cd workbench/frontend
npm install
npm run dev        # http://localhost:5173，API 通过 vite 代理到 :8000
```

构建前端（生成 dist，由后端单端口托管）：

```bash
cd workbench/frontend && npm run build
```

## 技术架构（对齐 PRD）

```
React + TypeScript + React Flow 画布
      │  REST + SSE(进度)
      ▼
FastAPI（鉴权/图校验/版本/策略/模型注册/成本）
      ├─ SQLite（结构化数据、节点版本、审计）   ← MVP 落地；结构向后兼容 PostgreSQL
      ├─ 本地对象存储（原图/生成图/导出包）
      ├─ 文本生成适配器（策略/提示词，离线规则版，预留 LLM 接入）
      └─ 图片生成适配器（本地合成器：背景+产品主体+可编辑文字层，真实产出 PNG）
            ├─ local-poster-compositor（已启用，免费，演示用）
            ├─ comfyui-product-edit（占位，未启用）
            └─ api-commercial-image（占位，未启用）
```

## 核心能力（对应 PRD 验收用例）

1. 项目/素材/事实卡：上传多张产品图；事实卡字段（卖点、禁改外观、场景、禁用表述、政策）人工填写/AI 抽取「识别推测」且不淘汰已确认事实。
2. 三图策略：从事实卡 + 任务 Brief 生成 **主图 / 场景图 / 细节图** 3 套差异化结构化策略。
3. 单图提示词：每张策略独立扩写为画面提示词；越权修改会提示回到上游节点。
4. 节点级 AI 修改：针对选中节点的指令只产生该节点候选版本，**应用后才成当前版本**，不暗改其他节点（验收：改图1提示词，图2/3与策略/事实不变）。
5. 生图节点：选模型 + 画幅 + 清晰度 + 张数 + 文字层；失败重试不覆盖旧图；记录 model_id / 用量 / 账单状态。
6. 上游事实变更：下游旧结果可查看且标记 **stale**，确认后才重跑（验收：改事实→策略标 stale→运行恢复）。
7. 分层排版导出：生成图 + 可编辑标题/副标题（价格/政策）文字层 → 导出最终 PNG（**改字不重生图**）。
8. 项目可迁移：导出项目 JSON（节点/提示词/资产清单）与素材包 zip。
9. 强类型 DAG：端口类型校验、禁止成环、版本乐观锁（409）、幂等键防重复扣费、Agent 计划草案（不自动执行付费生图）。

## 接入真实模型

- 文本模型：替换 `backend/app/generators/text.py` 的 `generate_strategy` / `generate_prompt` 为对 LiteLLM/自建网关的调用，契约不变。
- 图片模型：实现 `backend/app/generators/image.py` 中 `ComfyUIAdapter` / `CommercialApiAdapter` 的 `generate()`，并在 `registry.py` 的 `SEED_MODELS` 中启用对应模型（记录真实 provider/model 以保证可追溯，遵循 PRD 6.5）。

## 测试

```bash
cd workbench/backend && python e2e_test.py   # 端到端验证全部链路（需后端在 :8000 运行）
```

## 模型管理（自助式）

顶部「模型管理」按钮打开弹窗，可**逐项配置**模型，无需改代码或数据库：

- 列表查看全部模型，一键**上线/下线**开关（`enabled`）；
- **编辑**：provider / modality / cost_policy / workflow_version / 能力声明 `capabilities_json` / 参数规格 `parameter_schema`，保存即时生效（前端按能力声明自动显隐画幅、张数等选项）；
- **新增模型**：填 model_id + 能力/参数即可创建（注意仍需在 `generators/image.py` 实现对应适配器才能真正生图）；
- **删除**模型。

对应后端接口（均带 JSON 校验，非法 capabilities/parameter 返回 400）：

```
GET    /api/models                列出全部
GET    /api/models/{model_id}     查单个
POST   /api/models                新增
PUT    /api/models/{model_id}     局部更新
DELETE /api/models/{model_id}     删除
```

> 说明：模型配置存于 SQLite `model_registry` 表，后端每次运行实时读取，改完**无需重启**即生效。

## 画布增强（2026-09）

- **结构化策略卡**：需求摘要、统一配色/材质光影/成像方式、目标比例；每张图包含卖点证明、场景搭配库、产品位置与文字模块。单图提示词会引用策略视觉基线，继续遵守事实锁定，不把待确认文案当成已确认承诺。
- **项目默认模型**：底部可设置文本/图片默认模型；节点单独指定模型优先，否则使用项目默认，再回退内置本地模型。默认配置按项目保存。
- **节点图片工具栏**：悬停图片节点可见替换、改字、抠图、消除、变清晰、扩图及更多意图。当前「改字/图文分层」由本地合成器真实产出新图；其他未接入意图会置灰，并由后端返回明确的未接入提示。

新增接口：

```
GET/PUT /api/projects/{pid}/defaults
GET     /api/image-tools/intents
POST    /api/image-tools/run
```

验证：`backend/e2e_test.py`（旧链路回归）与 `backend/verify_v2.py`（新策略字段、项目默认模型、图片工具意图）均可在后端运行时执行。

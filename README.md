# 遥感影像变化检测平台（rs-change-detect-web）

基于双时相 Sentinel-2 / Landsat 影像的地表变化检测 Web 平台：框选 AOI → 自动匹配影像对 → 光谱指数变化检测（NDVI / NDWI，Otsu 自适应阈值或回归法）→ 像元级与斑块级统计分析 → 精度评价 → 多格式导出。全流程支持多用户隔离，可直接在浏览器完成从取数到出报告的完整闭环。

## 功能特性

**检测与分析**
- AOI 框选 / 预置影像对 / 在线取数（全国 3200+ 地名搜索或地图点选定位，拉取真实 Sentinel-2 L2A 数据）
- 变化检测：NDVI / NDWI，阈值法（Otsu 逐景自适应）或回归法（迭代剔除离群）
- 三类像元统计（增强 / 退化 / 稳定）+ 指数分布直方图
- 变化强度五档分级（|Δ| 相对阈值 T 的倍数口径，跨景可比）
- 变化图斑统计（4 邻域连通域：斑块数 / 面积 / 密度 / Top10 大斑块含质心）
- 地物类别转移矩阵（NDVI/NDWI 联合判据 4 类，经验阈值已显式标注）
- 精度评价：样本点提交 → 混淆矩阵 / OA / Kappa / 逐类精度
- 导出：GeoTIFF / PNG / PDF 报告（含逐类精度、直方图、强度分级、图斑统计、转移矩阵）/ GeoJSON 图斑

**平台与交互**
- 注册 / 登录（JWT + PBKDF2，检测历史按用户隔离；新注册为纯新账号）
- 地图三层图层控制（变化专题图 + 双时相真彩合成，独立显隐与透明度）
- 鼠标探针（实时显示经纬度、双时相指数、Δ、变化类别，单次 < 100ms）
- 异步运行 + 分阶段进度（阶段点 + 阶段内百分比）
- 历史运行回看（参数快照一键复现，新分析字段从快照透出）

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.12 + FastAPI + psycopg2 + rasterio + numpy + scikit-image + reportlab + matplotlib |
| 前端 | Vite + 原生 JS + MapLibre GL（离线空白底图，零在线瓦片）+ ECharts |
| 存储 | PostgreSQL/PostGIS（容器 rs-pg）+ MinIO（容器 rs-minio4） |
| 认证 | JWT HS256（12h）+ PBKDF2-SHA256（12 万轮），纯标准库零新依赖 |

## 目录结构

```
api/                  后端（FastAPI）
  app/algorithm/      算法模块（纯函数：检测 / 强度 / 图斑 / 转移矩阵 / 导出器）
  app/api/v1/         REST 接口层
  app/service/        业务服务层
  app/repository/     数据访问层
  app/config/         常量与环境配置（阈值/档位/类别一律集中于此）
  migrations/         SQL 迁移（migrate.py 极简执行器）
  tests/              pytest 测试
web/                  前端（Vite 多页：index.html 工作台 + login.html 登录页）
data/                 影像数据（tif 不入 Git，单独传输）
```

## 快速开始

### 环境要求

- Python 3.12（Windows 建议 `py -3.12` 启动器）
- Node.js 18+
- Docker Desktop（运行中）

### 1. 拉取代码与数据

```bash
git clone https://github.com/Yiliuxin/rs-change-detect-web.git
cd rs-change-detect-web
# main 即最新稳定版；如需在自己分支开发，再 git checkout lyj（或 ylx）
```

影像数据已随仓库分发（12 组预置影像对 `data/pair_*`，约 122MB，单文件均低于 GitHub 限制），clone 即得，无需单独下载。后续新采集的大型影像不会入库（`.gitignore` 排除 `data/**/*.tif`，仅放行 `pair_*` 预置对）。

### 2. 后端依赖与环境配置

```bash
cd api
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env    # 编辑 .env，填入下方默认值
```

`api\.env` 与容器参数对应（默认即可跑通）：

```ini
MINIO_ENDPOINT=localhost:19000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin
MINIO_SECURE=false
PG_DSN=postgresql://postgres:postgres@localhost:5432/rs_change
```

### 3. 启动数据库与对象存储（首次）

```bash
docker run -d --name rs-pg -p 5432:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=rs_change postgres:18-alpine

docker run -d --name rs-minio4 -p 19000:9000 -p 19001:9001 \
  bitnami/minio:latest server /bitnami/minio/data \
  --address 0.0.0.0:9000 --console-address 0.0.0.0:9001
```

> 9000 端口若被 wslrelay 等占用，映射到 19000（与 `.env` 保持一致）。

### 4. 建表（首次）

```bash
py -3.12 migrations/migrate.py up      # 按序号应用全部迁移
py -3.12 migrations/migrate.py status  # 随时查看迁移状态
```

### 5. 启动服务

```bash
# 后端（默认 8000；被占用换 8010）
.venv\Scripts\python -m uvicorn app.main:app --port 8000

# 前端（另开终端）
cd web
npm install
npm run dev          # http://localhost:5173
```

后端跑 8010 时，在 `web\` 下新建 `.env.local` 写入一行（不入库）：

```ini
VITE_API_TARGET=http://localhost:8010
```

浏览器打开 http://localhost:5173，注册账号登录（新账号为纯新账号，不关联任何既有数据）。

## 测试

```bash
cd api
py -3.12 -m pytest -q     # 全量测试
```

测试使用 `AUTH_DISABLED=1` 直通认证，通过 `test_data` 标记清理自身数据，不触碰真实历史运行。

## 协作约定

- 分支结构：`main`（主线，始终为可演示的稳定版本）/ `ylx`（易刘欣开发）/ `lyj`（搭档开发）
- 日常在自己的分支上开发；**每完成一个功能，先合并进 `main`，再把 `main` 拉回自己分支**：
  ```bash
  git checkout 自己的分支
  # ...开发、commit...
  git checkout main && git merge 自己的分支    # 功能进主线
  git push origin main
  git checkout 自己的分支 && git merge main    # 同步主线最新
  ```
- **每次开工前先同步**：把自己的分支与远端对齐，再把 `main` 最新合并进来，避免越攒越远、最后一次性爆冲突：
  ```bash
  git checkout 自己的分支
  git pull origin 自己的分支        # 拉远端自己分支的更新
  git fetch origin
  git merge origin/main             # 把主线最新合进自己分支（有冲突现在就解决，趁改动还热）
  git push origin 自己的分支
  ```
  判断是否落后：`git rev-list --count 自己的分支..origin/main`，输出 > 0 就该同步了。
- 合并进 `main` 前必须通过：`cd api && py -3.12 -m pytest -q` 全绿 + `cd web && npm run build` 构建成功
- `push` 前先 `git pull --rebase`
- 阈值、档位、类别等常量一律进 `api/app/config/constants.py`，禁止在算法代码里写死数值
- 大改动先在群里说一声再动手，避免两人改同一文件

## 常见问题

| 报错 | 原因与处理 |
|---|---|
| 数据库连接失败 | Docker Desktop 未运行，或 `rs-pg` 容器没起：`docker start rs-pg rs-minio4` |
| 前端请求全部失败 | 后端没启动，或端口不一致：核对后端端口与 `web\.env.local` |
| MinIO 连接失败 | 容器端口映射与 `.env` 的 `MINIO_ENDPOINT` 不一致 |
| 在线取数偶发 FAILED | AWS/ESA 网络瞬时中断，重试即可；本机代理不稳时清空 `HTTP_PROXY`/`HTTPS_PROXY` 再起后端 |
| `py -3.12` 找不到 numpy | 用错了全局 Python，必须走 `api\.venv` 或 `py -3.12` |

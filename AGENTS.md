## 目标
- 从单一固定上游快照（OSM + 同一陆地多边形 + GEBCO，或钉住 ETag 的 VersaTiles）编译标准 PMTiles 并按 v3 内容寻址发布；不混用多源拼合、不重编码瓦片、不引入定海私有瓦片容器。
- GEBCO 水深带只做偏浅（保守）取整并在元数据标明“示意、非测量、不可用于导航”；发布门禁只做确定性结构与完整性审计，不把逐格重算地理几何塞进发布检查。

## 验证
- Python（CPython 3.12：`requirements.txt` 只锁 3.12 的 wheel）：`/tmp/dinghai-basemap-venv/bin/python -m unittest discover -s tests -v`；或先用 `python3.12 -m pip install --require-hashes -r requirements.txt` 安装哈希锁定依赖，再用 `python3.12 -m unittest discover -s tests -v`。
- Java 21 / Planetiler：`mvn -f planetiler/pom.xml package`（运行 `LightFieldsTest`、`ClearanceFieldsTest`）。
- Seamap 发布结构审计：`python3.12 audit_seamap.py <发布目录>`；要求 65,536 格齐全，`--cell` 试点只用于本地试验，不走审计与发布。
- Satellite 发布结构审计：`python3.12 audit_satellite.py <发布目录> --estimate <estimate.json> --output <audit.json>`。

## 边界
- 本仓库仅为开发侧生产器，不在 App 中运行。
- 不提交 `.pmtiles`、`.osm.pbf`、`.nc`、`.versatiles`、`.mbtiles`、SQLite 索引或任何生产产物；`tests/fixtures/` 下的测试夹具例外。
- 不新增旧版 v1/v2 输出或回退 URL。
- 每次 Planetiler 运行都使用唯一的 `--tmpdir`，避免删除其他运行的临时数据。

## 工具入口
| 入口 | 职责 |
| --- | --- |
| `depth_product.py` | 同一陆地多边形的索引与保守 GEBCO 水深带 |
| `planetiler/pom.xml` (`SeamapCompiler`) | Java 21 单快照编译标准 PMTiles |
| `land_subset.py` | 仅试点：选出与试点范围相交的完整陆地多边形 |
| `seamap_product.py` | 切分 Seamap overview/cell，生成 v3 内容寻址发布 |
| `audit_seamap.py` | Seamap 发布结构与完整性审计 |
| `satellite_product.py` | 钉住 ETag 的 VersaTiles 估算、提取与 v3 发布 |
| `satellite_overview.py` | 为已完成、未发布的 Satellite 本地发布补 z0–6 overview |
| `satellite_mosaics.py` | 从审计结果随机取三格渲染原生瓦片拼图，供目视检查 |
| `audit_satellite.py` | Satellite 发布结构、完整性及 WebP 可读性审计 |

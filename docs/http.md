# 本地接口

服务只用于本地教研复核，不连接真实人员目录或外部数据源。数据以 UTF-8 JSON 传输；未找到对象返回 404，无效业务输入返回 400，正文超出 8MB 返回 413。

- `GET /health` 返回就绪状态。
- `GET /api/context` 返回产品配置、默认版本、已导入版本与保存记录目录。
- `GET /api/data?revision=版本` 返回该版本完整原始数据；省略版本使用演示材料。
- `POST /api/datasets` 接收 `label` 和完整 `data`，返回新建或复用的 `revision`。
- `POST /api/explore` 接收可选 `revision`、`selection` 和 `ranking_config`，返回数据指纹、实际选择、规范化配置、原始描述 `view` 与综合排序 `ranking`。
- `POST /api/notebooks` 使用相同参数和 `title`，服务端重新计算后冻结保存，不能由客户端伪造结果。
- `GET /api/notebooks` 返回材料目录，`GET /api/notebooks/编号` 返回冻结描述、选择条件、排序配置和所用数据。

## ranking_config 字段

- `weights`：以评分标准编号为键的非负权重，省略的标准取 rubric 默认值；服务端归一化，全部为零或含未知标准会被拒绝。
- `exclude_conflicts`：布尔，默认 `true`，是否自动回避 `conflicts` 中的评分。
- `excluded_pairs`：数组，每项为 `{project_id, reviewer_id, reason?}`，主席手动追加的回避；重复对或不存在的引用返回 400。
- `top_k`：候选名单长度，1 至项目总数之间的整数。

## ranking 响应结构

- `summary`：参评项目、连通组、有效/弃权/无效/回避格子数、评委数、扰动与折刀次数。
- `components`：连通组及其项目、评委编号。
- `rankings`：每个可估计项目的组内名次、重叠带、跨组全局假设名次、综合分与 95% 区间、可靠性、各标准贡献与原始评分格、覆盖统计、权重扰动和留一评委稳定性。
- `reviewers`：评委原始均分、按标准的严宽效应、收缩比例和证据等级。
- `criteria`：各标准满分、当前权重、全局均值、残差标准差和缺测填补项目数。
- `links`：每个项目与组内其他项目的共同评委或间接桥接链；跨组项目不出现在链接中，跨组对数由 `uncomparable_pairs` 给出。
- `unranked`：没有任何有效评分格、无法估计的项目。
- `exclusions_applied`：本次实际生效的回避（区分利益冲突 `conflict` 与手动 `manual`）。
- `notes`：口径与可比性提示。

方法学细节见 `docs/ranking.md`；增添业务能力时需保持现有原始记录和保存材料可读取。

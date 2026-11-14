"""可复核的综合排序。

只使用 Python 标准库。核心是一个可解释的加性校准模型：

    y(项目 i, 评委 j, 标准 c) = μ_c + a_i(c) + b_j(c) + 误差

其中 a 是项目效应、b 是评委严宽效应，二者在每个连通组内各自均值为零。
评委效应带岭收缩（先验强度 RIDGE_PRIOR 个评分格），评分格很少的评委不会
被极端化。项目效应不收缩，证据不足通过标准误和可靠性标签体现。

连通组由“项目—评委”有效评分边构成：没有共同参照（含间接桥接）的项目
分属不同连通组，跨组名次只能在“各组标尺一致”的假设下给出，页面会显式
标注。所有随机性均使用固定种子，结果可重复。
"""
from collections import defaultdict, deque
from statistics import fmean
import math
import random


# --- 公开的方法学常量，文档 docs/ranking.md 与页面说明引用同一处口径 ---
RIDGE_PRIOR = 3.0          # 评委效应岭回归先验，相当于 3 个评分格的证据强度
CONVERGENCE_TOL = 1e-9
MAX_ITERATIONS = 200
PERTURBATION_TRIALS = 40   # 权重扰动试验次数（固定种子）
RANDOM_SEED = 20261114
Z_95 = 1.959963984540054   # 95% 正态分位数
SIGMA_FLOOR_RATIO = 0.01   # 残差标准差下限 = 满分 × 该比例
PROJECT_SUFFICIENT_REVIEWERS = 3
REVIEWER_SUFFICIENT_CELLS = 12
REVIEWER_LIMITED_CELLS = 6


def _round(value):
    return round(value, 6) if isinstance(value, (int, float)) else value


def normalize_config(data, raw):
    """把页面传入的权重/回避配置规范化并做严格校验。"""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError("排序配置必须是对象")
    rules = {rule["id"]: rule for rule in data["rubric"]}

    weights = raw.get("weights", {})
    if weights is None:
        weights = {}
    if not isinstance(weights, dict):
        raise ValueError("标准权重必须是以标准编号为键的对象")
    normalized = {}
    for criterion, rule in rules.items():
        if criterion in weights:
            value = weights[criterion]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("标准权重必须是有限数字：" + criterion)
            if value < 0:
                raise ValueError("标准权重不能为负：" + criterion)
            normalized[criterion] = float(value)
        else:
            normalized[criterion] = float(rule["weight"])
    unknown = set(weights) - set(rules)
    if unknown:
        raise ValueError("引用了不存在的评分标准：" + ",".join(sorted(unknown)))
    if sum(normalized.values()) <= 0:
        raise ValueError("至少一项标准权重必须为正")
    total = sum(normalized.values())
    weights = {criterion: value / total for criterion, value in normalized.items()}

    exclude_conflicts = raw.get("exclude_conflicts", True)
    if not isinstance(exclude_conflicts, bool):
        raise ValueError("利益回避开关必须为布尔值")

    project_ids = {project["project_id"] for project in data["projects"]}
    reviewer_ids = {reviewer["reviewer_id"] for reviewer in data["reviewers"]}
    excluded_pairs = []
    pairs = raw.get("excluded_pairs", [])
    if pairs is None:
        pairs = []
    if not isinstance(pairs, list):
        raise ValueError("回避清单必须是数组")
    seen = set()
    for pair in pairs:
        if not isinstance(pair, dict):
            raise ValueError("回避项必须是对象")
        project_id = pair.get("project_id")
        reviewer_id = pair.get("reviewer_id")
        if project_id not in project_ids:
            raise ValueError("回避项引用了不存在的项目：" + str(project_id))
        if reviewer_id not in reviewer_ids:
            raise ValueError("回避项引用了不存在的评委：" + str(reviewer_id))
        reason = pair.get("reason", "")
        if reason is None:
            reason = ""
        if not isinstance(reason, str) or len(reason) > 200:
            raise ValueError("回避原因应为不超过200字的文本")
        key = (project_id, reviewer_id)
        if key in seen:
            raise ValueError("回避清单存在重复项目-评委对")
        seen.add(key)
        excluded_pairs.append({"project_id": project_id, "reviewer_id": reviewer_id, "reason": reason.strip()})

    top_k = raw.get("top_k")
    if top_k is None:
        top_k = min(5, len(project_ids))
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= len(project_ids):
        raise ValueError("候选名单长度应在1至项目总数之间")

    return {"weights": weights, "exclude_conflicts": exclude_conflicts,
            "excluded_pairs": excluded_pairs, "top_k": top_k,
            "perturbation_trials": PERTURBATION_TRIALS, "ridge_prior": RIDGE_PRIOR}


def _eligible(data, track_projects, config):
    """拆分评分格：有效、弃权、无效、回避、缺记录。只返回有效格给模型。"""
    conflict_set = {(row["project_id"], row["reviewer_id"]): row.get("reason", "")
                    for row in data["conflicts"]}
    manual = {(row["project_id"], row["reviewer_id"]): row.get("reason", "")
              for row in config["excluded_pairs"]}
    selected = {project["project_id"] for project in track_projects}
    scored, abstained, invalid, recused = [], [], [], []
    for row in data["ratings"]:
        if row["project_id"] not in selected:
            continue
        pair = (row["project_id"], row["reviewer_id"])
        if row["status"] == "abstain":
            abstained.append(row)
        elif row["status"] == "invalid":
            invalid.append(row)
        elif row["status"] == "scored":
            if config["exclude_conflicts"] and pair in conflict_set:
                recused.append({**row, "reason": conflict_set[pair], "source": "conflict"})
            elif pair in manual:
                recused.append({**row, "reason": manual[pair], "source": "manual"})
            else:
                scored.append(row)
    return scored, abstained, invalid, recused, conflict_set, manual


def _components(scored, reviewer_ids=None):
    """并查集求项目—评委连通组，返回项目映射和组内边。

    reviewer_ids 给出全部评委编号，用于区分两类节点，不能依赖编号字符串前缀。
    """
    reviewer_ids = set(reviewer_ids or ())
    parent = {}

    def find(node):
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left, right):
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    edges = defaultdict(list)          # (project, reviewer) -> cells
    for cell in scored:
        union(cell["project_id"], cell["reviewer_id"])
        edges[(cell["project_id"], cell["reviewer_id"])].append(cell)
    if not reviewer_ids:
        reviewer_ids = {reviewer_id for _, reviewer_id in edges}

    groups = defaultdict(lambda: {"projects": set(), "reviewers": set(), "pairs": []})
    for (project_id, reviewer_id), cells in edges.items():
        root = find(project_id)
        group = groups[root]
        group["projects"].add(project_id)
        group["reviewers"].add(reviewer_id)
        group["pairs"].append((project_id, reviewer_id, cells))
    components = []
    for index, group in enumerate(sorted(groups.values(), key=lambda item: sorted(item["projects"])), start=1):
        components.append({
            "component_id": "C" + str(index),
            "projects": sorted(group["projects"]),
            "reviewers": sorted(node for node in group["reviewers"] if node in reviewer_ids),
            "pairs": group["pairs"],
        })
    return components, find


def _fit_component(cells_by_criterion, component):
    """在一个连通组内按标准拟合 μ/a/b，返回效应、残差标准差和每个项目标准的格子。"""
    fit = {}
    for criterion, cells in cells_by_criterion.items():
        if not cells:
            continue
        mu = fmean(cell["score"] for cell in cells)
        # 某评委/项目在该标准上可能没有任何有效格（仅在其他标准出现），
        # 因此效应字典按“本标准实际出现的对象”建立，而不是整个连通组。
        active_projects = sorted({cell["project_id"] for cell in cells})
        active_reviewers = sorted({cell["reviewer_id"] for cell in cells})
        project_effect = {project_id: 0.0 for project_id in active_projects}
        reviewer_effect = {reviewer_id: 0.0 for reviewer_id in active_reviewers}
        by_project = defaultdict(list)
        by_reviewer = defaultdict(list)
        for cell in cells:
            by_project[cell["project_id"]].append(cell)
            by_reviewer[cell["reviewer_id"]].append(cell)
        if len(active_projects) == 1:
            # 单项目无法分离项目效应与评委严宽：项目效应约束为零，
            # 评委效应直接相对总体均值做岭估计（这是可识别的唯一口径）。
            only_project = active_projects[0]
            project_effect[only_project] = 0.0
            for reviewer_id, reviewer_cells in by_reviewer.items():
                numerator = sum(cell["score"] - mu for cell in reviewer_cells)
                reviewer_effect[reviewer_id] = numerator / (len(reviewer_cells) + RIDGE_PRIOR)
        else:
            for _ in range(MAX_ITERATIONS):
                previous = (dict(project_effect), dict(reviewer_effect))
                for project_id, project_cells in by_project.items():
                    project_effect[project_id] = fmean(
                        cell["score"] - mu - reviewer_effect[cell["reviewer_id"]] for cell in project_cells)
                project_offset = fmean(project_effect.values())
                for project_id in project_effect:
                    project_effect[project_id] -= project_offset
                for reviewer_id, reviewer_cells in by_reviewer.items():
                    numerator = sum(cell["score"] - mu - project_effect[cell["project_id"]]
                                    for cell in reviewer_cells)
                    reviewer_effect[reviewer_id] = numerator / (len(reviewer_cells) + RIDGE_PRIOR)
                reviewer_offset = fmean(reviewer_effect.values())
                for reviewer_id in reviewer_effect:
                    reviewer_effect[reviewer_id] -= reviewer_offset
                if max(abs(project_effect[key] - previous[0][key]) for key in project_effect) < CONVERGENCE_TOL and \
                   max(abs(reviewer_effect[key] - previous[1][key]) for key in reviewer_effect) < CONVERGENCE_TOL:
                    break
        residuals = [cell["score"] - mu - project_effect[cell["project_id"]]
                     - reviewer_effect[cell["reviewer_id"]] for cell in cells]
        sigma = math.sqrt(fmean(value * value for value in residuals))
        fit[criterion] = {"mu": mu, "project": project_effect, "reviewer": reviewer_effect,
                          "sigma": sigma, "active_projects": active_projects,
                          "active_reviewers": active_reviewers,
                          "by_project": by_project, "by_reviewer": by_reviewer}
    return fit


def _project_reliability(project_id, project_cells):
    distinct_reviewers = {cell["reviewer_id"] for cells in project_cells.values() for cell in cells}
    complete = all(cells for cells in project_cells.values())
    if len(distinct_reviewers) < 2:
        reliability = "insufficient"
    elif len(distinct_reviewers) < PROJECT_SUFFICIENT_REVIEWERS or not complete:
        reliability = "limited"
    else:
        reliability = "sufficient"
    return reliability, len(distinct_reviewers), complete


def _composite(adjusted, ses, weights, maxima, imputed_criteria=()):
    """加权综合分（0..1 标度）与独立假设下的标准误。"""
    imputed_criteria = set(imputed_criteria)
    fraction = 0.0
    variance = 0.0
    contributions = []
    for criterion, weight in weights.items():
        z = adjusted[criterion] / maxima[criterion]
        se_z = ses[criterion] / maxima[criterion]
        fraction += weight * z
        variance += (weight * se_z) ** 2
        contributions.append({"criterion": criterion, "weight": _round(weight),
                              "adjusted": _round(adjusted[criterion]), "maximum": maxima[criterion],
                              "fraction": _round(z), "contribution": _round(weight * z),
                              "imputed": criterion in imputed_criteria})
    return fraction, math.sqrt(variance), contributions


def _rank_map(estimates, weights, maxima, imputed_by_project=None):
    """estimates: project_id -> (adjusted dict, se dict)。返回名次与综合分。"""
    imputed_by_project = imputed_by_project or {}
    order = []
    for project_id, (adjusted, ses) in estimates.items():
        fraction, se, _ = _composite(adjusted, ses, weights, maxima, imputed_by_project.get(project_id, ()))
        order.append((project_id, fraction, se))
    order.sort(key=lambda item: (-item[1], item[0]))
    ranks = {}
    previous_score = None
    for index, (project_id, fraction, se) in enumerate(order, start=1):
        rank = index if previous_score is None or abs(previous_score - fraction) > 1e-12 else ranks[order[index - 2][0]]
        ranks[project_id] = rank
        previous_score = fraction
    return order, ranks


def _stability(estimates, weights, maxima, top_k, scored):
    """权重扰动 + 留一评委折刀，均为确定种子。"""
    rng = random.Random(RANDOM_SEED)
    _, baseline_ranks = _rank_map(estimates, weights, maxima)

    weight_trials = []
    for _ in range(PERTURBATION_TRIALS):
        trial_weights = {criterion: weight * -math.log(rng.random())
                         for criterion, weight in weights.items()}
        total = sum(trial_weights.values())
        trial_weights = {criterion: value / total for criterion, value in trial_weights.items()}
        _, trial_ranks = _rank_map(estimates, trial_weights, maxima)
        weight_trials.append(trial_ranks)

    jackknife = []
    reviewer_ids = sorted({cell["reviewer_id"] for cell in scored})
    for removed in reviewer_ids:
        remaining = [cell for cell in scored if cell["reviewer_id"] != removed]
        fold = _fold_estimates(remaining, maxima)
        if fold is None:
            jackknife.append({"removed": removed, "ranks": None})
            continue
        _, fold_ranks = _rank_map(fold, weights, maxima)
        jackknife.append({"removed": removed, "ranks": fold_ranks})

    report = {}
    for project_id in estimates:
        weight_ranks = [trial[project_id] for trial in weight_trials if project_id in trial]
        fold_ranks = [fold["ranks"][project_id] for fold in jackknife
                      if fold["ranks"] is not None and project_id in fold["ranks"]]
        eliminations = sum(1 for fold in jackknife
                           if fold["ranks"] is None or project_id not in fold["ranks"])
        top_weight = sum(1 for trial in weight_trials if trial.get(project_id, 999) <= top_k)
        top_jackknife = sum(1 for fold in jackknife
                            if fold["ranks"] is not None and fold["ranks"].get(project_id, 999) <= top_k)
        report[project_id] = {
            "weight": {"min_rank": min(weight_ranks), "max_rank": max(weight_ranks),
                       "mode_rank": max(set(weight_ranks), key=weight_ranks.count),
                       "top_k_frequency": _round(top_weight / len(weight_trials)),
                       "trials": len(weight_ranks)},
            "jackknife": {"min_rank": min(fold_ranks) if fold_ranks else None,
                          "max_rank": max(fold_ranks) if fold_ranks else None,
                          "mode_rank": max(set(fold_ranks), key=fold_ranks.count) if fold_ranks else None,
                          "top_k_frequency": _round(top_jackknife / len(jackknife)),
                          "eliminations": eliminations, "folds": len(jackknife)},
            "baseline_rank": baseline_ranks[project_id],
        }
    return report, [fold["removed"] for fold in jackknife]


def _fold_estimates(scored, maxima):
    """供折刀法复用：在给定评分格子上重算连通组与项目估计。"""
    if not scored:
        return None
    components, _ = _components(scored)
    global_mean = {}
    global_sigma = {}
    by_criterion_all = defaultdict(list)
    for cell in scored:
        by_criterion_all[cell["criterion"]].append(cell)
    for criterion, cells in by_criterion_all.items():
        global_mean[criterion] = fmean(cell["score"] for cell in cells)
        global_sigma[criterion] = max(math.sqrt(fmean((cell["score"] - global_mean[criterion]) ** 2
                                                      for cell in cells)),
                                      maxima[criterion] * SIGMA_FLOOR_RATIO)
    estimates = {}
    for component in components:
        cells_by_criterion = defaultdict(list)
        for _, _, cells in component["pairs"]:
            for cell in cells:
                cells_by_criterion[cell["criterion"]].append(cell)
        fit = _fit_component(cells_by_criterion, component)
        for project_id in component["projects"]:
            adjusted, ses = {}, {}
            for criterion in maxima:
                project_cells = fit[criterion]["by_project"].get(project_id, []) if criterion in fit else []
                if project_cells:
                    adjusted[criterion] = fit[criterion]["mu"] + fit[criterion]["project"][project_id]
                    sigma = max(fit[criterion]["sigma"], maxima[criterion] * SIGMA_FLOOR_RATIO)
                    ses[criterion] = sigma / math.sqrt(len(project_cells))
                else:
                    adjusted[criterion] = global_mean[criterion]
                    ses[criterion] = global_sigma[criterion]
            estimates[project_id] = (adjusted, ses)
    return estimates


def _bridges(components, find):
    links = defaultdict(list)
    for component in components:
        adjacency = defaultdict(set)
        for project_id, reviewer_id, _ in component["pairs"]:
            adjacency[project_id].add(reviewer_id)
            adjacency[reviewer_id].add(project_id)
        for project_id in component["projects"]:
            for other in component["projects"]:
                if other == project_id:
                    continue
                shared = sorted(adjacency[project_id] & adjacency[other])
                chain = []
                if not shared:
                    queue = deque([(project_id, [project_id])])
                    visited = {project_id}
                    while queue:
                        node, path = queue.popleft()
                        if node == other:
                            chain = path
                            break
                        for neighbor in sorted(adjacency[node]):
                            if neighbor not in visited:
                                visited.add(neighbor)
                                queue.append((neighbor, path + [neighbor]))
                links[project_id].append({"project_id": other, "component_id": component["component_id"],
                                          "shared_reviewers": shared, "chain": chain})
    return links


def rank(data, selection, raw_config=None):
    config = normalize_config(data, raw_config)
    track = selection.get("track", "all")
    track_projects = [project for project in data["projects"]
                      if track == "all" or project["track"] == track]
    if not track_projects:
        raise ValueError("未知展示分组")
    selected = {project["project_id"] for project in track_projects}
    track_of = {project["project_id"]: project["track"] for project in track_projects}
    maxima = {rule["id"]: float(rule["maximum"]) for rule in data["rubric"]}

    scored, abstained, invalid, recused, _, _ = _eligible(data, track_projects, config)
    reviewer_ids = {reviewer["reviewer_id"] for reviewer in data["reviewers"]}
    components, find = _components(scored, reviewer_ids)
    component_of = {}
    for component in components:
        for project_id in component["projects"]:
            component_of[project_id] = component["component_id"]

    # 组内按标准拟合加性模型
    fits = {}
    for component in components:
        cells_by_criterion = defaultdict(list)
        for _, _, cells in component["pairs"]:
            for cell in cells:
                cells_by_criterion[cell["criterion"]].append(cell)
        fits[component["component_id"]] = _fit_component(cells_by_criterion, component)

    global_mean, global_sigma, imputation_count = {}, {}, defaultdict(int)
    by_criterion_all = defaultdict(list)
    for cell in scored:
        by_criterion_all[cell["criterion"]].append(cell)
    for criterion in maxima:
        cells = by_criterion_all.get(criterion, [])
        if cells:
            global_mean[criterion] = fmean(cell["score"] for cell in cells)
            component_cell_counts = []
            fit_sigmas = []
            for component in components:
                fit = fits[component["component_id"]].get(criterion)
                if fit:
                    fit_sigmas.append(fit["sigma"])
                    component_cell_counts.append(sum(len(value) for value in fit["by_project"].values()))
            pooled_variance = sum(sigma * sigma * count
                                  for sigma, count in zip(fit_sigmas, component_cell_counts))
            pooled_sigma = math.sqrt(pooled_variance / sum(component_cell_counts))
            global_sigma[criterion] = max(pooled_sigma, maxima[criterion] * SIGMA_FLOOR_RATIO)
        else:
            global_mean[criterion] = 0.0
            global_sigma[criterion] = maxima[criterion]

    # 行级覆盖统计
    rows_by_project = defaultdict(list)
    for row in data["ratings"]:
        if row["project_id"] in selected:
            rows_by_project[row["project_id"]].append(row)
    panel = sorted({cell["reviewer_id"] for cell in scored})
    coverage = {}
    for project_id in selected:
        rows = rows_by_project.get(project_id, [])
        seen_reviewers = {row["reviewer_id"] for row in rows}
        # “缺席”只统计本范围内确实在打分、但与该项目连一条记录都没有的评委；
        # 对该项目弃权或被回避的评委有明确含义，不计入缺席。
        missing = sorted(set(panel) - seen_reviewers)
        coverage[project_id] = {
            "abstain_cells": sum(1 for row in rows if row["status"] == "abstain"),
            "invalid_cells": sum(1 for row in rows if row["status"] == "invalid"),
            "recused_cells": sum(1 for row in recused if row["project_id"] == project_id),
            "missing_reviewers": missing,
            "observed_reviewers": sorted(seen_reviewers),
        }

    estimates = {}
    ranking_rows = []
    unranked = []
    for project_id in sorted(selected):
        component_id = component_of.get(project_id)
        if component_id is None:
            unranked.append({"project_id": project_id, "track": track_of[project_id],
                             "reason": "没有任何有效评分格，无法估计", **coverage[project_id]})
            continue
        fit_set = fits[component_id]
        project_cells = defaultdict(list)
        for cell in scored:
            if cell["project_id"] == project_id:
                project_cells[cell["criterion"]].append(cell)
        reliability, reviewer_count, complete = _project_reliability(project_id, project_cells)
        adjusted, ses, cell_detail, imputed = {}, {}, {}, set()
        for criterion in maxima:
            cells = project_cells.get(criterion, [])
            cell_detail[criterion] = sorted(
                ({"reviewer_id": cell["reviewer_id"], "score": cell["score"]} for cell in cells),
                key=lambda item: item["reviewer_id"])
            if cells:
                fit = fit_set[criterion]
                adjusted[criterion] = fit["mu"] + fit["project"][project_id]
                sigma = max(fit["sigma"], maxima[criterion] * SIGMA_FLOOR_RATIO)
                ses[criterion] = sigma / math.sqrt(len(cells))
            else:
                adjusted[criterion] = global_mean[criterion]
                ses[criterion] = global_sigma[criterion]
                imputed.add(criterion)
                imputation_count[criterion] += 1
        estimates[project_id] = (adjusted, ses)
        fraction, se, contributions = _composite(adjusted, ses, config["weights"], maxima, imputed)
        ranking_rows.append({
            "project_id": project_id, "track": track_of[project_id],
            "component_id": component_id, "reliability": reliability,
            "scored_reviewers": reviewer_count, "criteria_complete": complete,
            "scored_cells": sum(len(cells) for cells in project_cells.values()),
            "composite_fraction": _round(fraction), "composite_se": _round(se),
            "ci_low": _round(max(0.0, fraction - Z_95 * se)),
            "ci_high": _round(min(1.0, fraction + Z_95 * se)),
            "contributions": contributions, "cells": cell_detail,
            "adjusted": {criterion: _round(value) for criterion, value in adjusted.items()},
            **coverage[project_id],
        })

    stability, jackknife_reviewers = _stability(estimates, config["weights"], maxima,
                                                config["top_k"], scored)
    for row in ranking_rows:
        row["stability"] = stability[row["project_id"]]

    # 组内名次与重叠带；全局名次仅作跨组假设展示
    ranking_rows.sort(key=lambda row: (row["component_id"], -row["composite_fraction"], row["project_id"]))
    for component in components:
        members = [row for row in ranking_rows if row["component_id"] == component["component_id"]]
        band_low = None
        band = 0
        for index, row in enumerate(members, start=1):
            row["within_rank"] = index
            if band_low is None or row["ci_high"] < band_low:
                band += 1
                band_low = row["ci_low"]
            else:
                band_low = min(band_low, row["ci_low"])
            row["band"] = component["component_id"] + "-B" + str(band)
    global_order = sorted(ranking_rows, key=lambda row: (-row["composite_fraction"], row["project_id"]))
    for index, row in enumerate(global_order, start=1):
        row["global_rank_assumption"] = index

    # 评委严宽表
    reviewer_report = []
    raw_by_reviewer = defaultdict(list)
    for cell in scored:
        raw_by_reviewer[cell["reviewer_id"]].append(cell)
    for reviewer_id in sorted(raw_by_reviewer):
        effects, shrinkage, raw_means = {}, {}, {}
        for component in components:
            fit_set = fits[component["component_id"]]
            if reviewer_id in component["reviewers"]:
                for criterion, fit in fit_set.items():
                    if reviewer_id in fit["reviewer"]:
                        effects[criterion] = _round(fit["reviewer"][reviewer_id])
                        cells = fit["by_reviewer"][reviewer_id]
                        shrinkage[criterion] = _round(len(cells) / (len(cells) + RIDGE_PRIOR))
                        raw_means[criterion] = _round(fmean(cell["score"] for cell in cells))
        cells = raw_by_reviewer[reviewer_id]
        observed = [effects[criterion] for criterion in effects]
        overall = fmean(observed) if observed else 0.0
        cell_count = len(cells)
        if cell_count < REVIEWER_LIMITED_CELLS:
            reliability = "insufficient"
        elif cell_count < REVIEWER_SUFFICIENT_CELLS:
            reliability = "limited"
        else:
            reliability = "sufficient"
        reviewer_report.append({
            "reviewer_id": reviewer_id, "scored_cells": cell_count,
            "projects": sorted({cell["project_id"] for cell in cells}),
            "raw_mean": _round(fmean(cell["score"] for cell in cells)),
            "raw_means": raw_means, "effects": effects,
            "overall_effect": _round(overall),
            "overall_effect_fraction": _round(overall / fmean(maxima.values())),
            "shrinkage": shrinkage, "reliability": reliability,
        })

    criteria_report = []
    for criterion, maximum in maxima.items():
        # 与缺测填补使用同一口径：按各组评分格数加权的残差方差
        weighted = []
        for component in components:
            fit = fits[component["component_id"]].get(criterion)
            if fit:
                count = sum(len(value) for value in fit["by_project"].values())
                weighted.append((fit["sigma"] * fit["sigma"], count))
        if weighted:
            pooled_sigma = math.sqrt(sum(variance * count for variance, count in weighted)
                                     / sum(count for _, count in weighted))
        else:
            pooled_sigma = global_sigma[criterion]
        criteria_report.append({
            "id": criterion, "maximum": maximum,
            "weight": _round(config["weights"][criterion]),
            "global_mean": _round(global_mean[criterion]),
            "residual_sd": _round(pooled_sigma),
            "imputed_projects": imputation_count.get(criterion, 0),
        })

    links = _bridges(components, find)
    comparable_pairs = sum(len(value) for value in links.values()) // 2
    total_pairs = len(selected) * (len(selected) - 1) // 2

    notes = _build_notes(components, ranking_rows, unranked, abstained, invalid, recused,
                         config, total_pairs, comparable_pairs, jackknife_reviewers)

    return {
        "config": config,
        "summary": {
            "参评项目": len(selected), "可估计项目": len(ranking_rows),
            "连通组": len(components), "有效评分格": len(scored),
            "弃权格": len(abstained), "无效格": len(invalid), "回避格": len(recused),
            "评委": len(panel), "权重扰动试验": PERTURBATION_TRIALS,
            "留一评委折刀": len(jackknife_reviewers),
        },
        "components": [{"component_id": component["component_id"],
                        "projects": component["projects"], "reviewers": component["reviewers"]}
                       for component in components],
        "rankings": ranking_rows,
        "reviewers": reviewer_report,
        "criteria": criteria_report,
        "links": {key: sorted(value, key=lambda item: item["project_id"]) for key, value in links.items()},
        "uncomparable_pairs": total_pairs - comparable_pairs,
        "unranked": unranked,
        "exclusions_applied": [{"project_id": row["project_id"], "reviewer_id": row["reviewer_id"],
                                "reason": row["reason"], "source": row["source"]} for row in recused],
        "notes": notes,
    }


def _build_notes(components, rows, unranked, abstained, invalid, recused,
                 config, total_pairs, comparable_pairs, jackknife_reviewers):
    notes = [
        "综合分采用加性校准：评分=标准均值+项目效应+评委严宽效应，组内效应均值为零；"
        "评委效应带相当于%.0f个评分格的岭收缩，评分格少的评委不会被极端化。" % RIDGE_PRIOR,
        "未评分（缺记录）、弃权（abstain）与无效（invalid）是三种不同含义，均不进入模型，"
        "在覆盖统计中分别计数，绝不按零分处理。",
        "稳定性来自固定种子的权重扰动（%d次）和留一评委折刀（%d位），名次区间比单一点名次更可靠。"
        % (PERTURBATION_TRIALS, len(jackknife_reviewers)),
    ]
    if len(components) > 1:
        notes.append(
            "存在%d个互不连通的评审组（%s），跨组项目没有共同评委或间接桥接；"
            "%d对项目可在组内比较，%d对项目缺少共同参照，全局名次仅在“各组标尺一致”的假设下成立。"
            % (len(components), "、".join(component["component_id"] for component in components),
               comparable_pairs, total_pairs - comparable_pairs))
    thin = [row["project_id"] for row in rows if row["reliability"] == "insufficient"]
    if thin:
        notes.append("项目 %s 有效评委少于2位，证据不足以推断稳定能力，标准误更宽并标为证据不足。"
                     % "、".join(thin))
    limited = [row["project_id"] for row in rows if row["reliability"] == "limited"]
    if limited:
        notes.append("项目 %s 评委较少或存在标准缺测，标记为证据有限，请结合原始格子复核。" % "、".join(limited))
    imputed = [row["project_id"] for row in rows
               if any(item["imputed"] for item in row["contributions"])]
    if imputed:
        notes.append("项目 %s 存在标准级缺测，缺测标准以全局均值填补并在贡献明细中标注。" % "、".join(imputed))
    if abstained:
        notes.append("共%d格弃权，表示评委明确放弃评价；无效格%d格表示评分被撤销；二者都不是低分。"
                     % (len(abstained), len(invalid)))
    if recused:
        notes.append("已应用%d项回避（利益冲突或主席手动剔除），对应原始评分仍可在原始记录中查到。"
                     % len(recused))
    if unranked:
        notes.append("项目 %s 没有任何有效评分格，不参与排名。" % "、".join(row["project_id"] for row in unranked))
    if not config["exclude_conflicts"]:
        notes.append("当前关闭了利益冲突自动回避，请确认这是主席的有意决定。")
    return notes

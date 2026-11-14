from copy import deepcopy
from pathlib import Path
import json
import unittest

from review_ranking.domain import validate
from review_ranking.ranking import (rank, normalize_config, _components, _eligible,
                                    RIDGE_PRIOR, PERTURBATION_TRIALS)
from review_ranking.service import Workbench

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    return json.loads((ROOT / "fixtures" / "data.json").read_text(encoding="utf-8"))


def synthetic_dataset():
    """两组互不连通的项目，覆盖弃权、无效、缺标准和完全无评分的情况。"""
    data = {
        "projects": [{"project_id": "p1", "track": "all"}, {"project_id": "p2", "track": "all"},
                     {"project_id": "p3", "track": "all"}, {"project_id": "p4", "track": "all"},
                     {"project_id": "p5", "track": "all"}],
        "reviewers": [{"reviewer_id": "r1", "topic": "x"}, {"reviewer_id": "r2", "topic": "x"},
                      {"reviewer_id": "r3", "topic": "x"}],
        "rubric": [{"id": "c1", "maximum": 10, "weight": 0.6},
                   {"id": "c2", "maximum": 5, "weight": 0.4}],
        "conflicts": [{"project_id": "p2", "reviewer_id": "r1", "reason": "合作"}],
        "ratings": [],
    }

    def add(project, reviewer, criterion, status, score=None):
        data["ratings"].append({"project_id": project, "reviewer_id": reviewer,
                                "criterion": criterion, "status": status, "score": score})

    # 组1：p1/p2 经 r1、r2 相连，r2 系统性偏紧
    for criterion, scores in (("c1", (8.0, 6.0, 7.0, 5.0)), ("c2", (4.0, 3.0, 3.5, 2.5))):
        add("p1", "r1", criterion, "scored", scores[0])
        add("p1", "r2", criterion, "scored", scores[1])
        add("p2", "r1", criterion, "scored", scores[2])
        add("p2", "r2", criterion, "scored", scores[3])
    # p2-r1 的 c2 评分事后被撤销为无效（同键只保留一条记录）
    data["ratings"] = [row for row in data["ratings"]
                       if not (row["project_id"] == "p2" and row["reviewer_id"] == "r1"
                               and row["criterion"] == "c2")]
    add("p2", "r1", "c2", "invalid")
    # 组2：p3/p4 只有 r3；p4 的 c2 弃权（不按零处理）；p5 完全没有评分
    add("p3", "r3", "c1", "scored", 9.0)
    add("p3", "r3", "c2", "scored", 4.5)
    add("p4", "r3", "c1", "scored", 2.0)
    add("p4", "r3", "c2", "abstain")
    validate(data)
    return data


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.data = fixture()

    def test_fixture_splits_into_four_disconnected_components(self):
        result = rank(self.data, {"track": "all"})
        self.assertEqual(len(result["components"]), 4)
        ids = {project for component in result["components"] for project in component["projects"]}
        self.assertEqual(ids, {project["project_id"] for project in self.data["projects"]})
        total_pairs = 24 * 23 // 2
        self.assertEqual(result["uncomparable_pairs"] +
                         sum(len(links) for links in result["links"].values()) // 2, total_pairs)
        # 跨组项目不存在桥接记录
        cross = [link for link in result["links"]["project_00"]
                 if link["project_id"] in result["components"][2]["projects"]]
        self.assertEqual(cross, [])

    def test_track_selection_can_still_be_disconnected(self):
        poster = rank(self.data, {"track": "poster"})
        self.assertGreaterEqual(len(poster["components"]), 2)
        self.assertGreater(poster["uncomparable_pairs"], 0)
        self.assertTrue(any("缺少共同参照" in note for note in poster["notes"]))

    def test_abstain_invalid_missing_are_counted_separately_and_never_zero(self):
        data = synthetic_dataset()
        result = rank(data, {"track": "all"})
        self.assertEqual(result["summary"]["弃权格"], 1)
        self.assertEqual(result["summary"]["无效格"], 1)
        # 无效格不进入模型
        p2 = next(row for row in result["rankings"] if row["project_id"] == "p2")
        cell_reviewers = [cell["reviewer_id"] for cell in p2["cells"]["c1"]]
        self.assertNotIn(None, cell_reviewers)
        self.assertEqual(len(p2["cells"]["c1"]), 1)
        # 缺标准被显式填补并标注
        p4 = next(row for row in result["rankings"] if row["project_id"] == "p4")
        self.assertTrue(p4["contributions"][1]["imputed"])
        self.assertEqual(next(c for c in result["criteria"] if c["id"] == "c2")["imputed_projects"], 1)
        # 完全无评分的项目不排名
        self.assertEqual([row["project_id"] for row in result["unranked"]], ["p5"])
        self.assertNotIn("p5", {row["project_id"] for row in result["rankings"]})

    def test_synthetic_components_are_not_connected(self):
        data = synthetic_dataset()
        config = normalize_config(data, {})
        scored, _, _, _, _, _ = _eligible(data, data["projects"], config)
        components, _ = _components(scored)
        self.assertEqual([sorted(c["projects"]) for c in components],
                         [["p1", "p2"], ["p3", "p4"]])

    def test_reviewer_strictness_effects_recover_sign_and_shrink(self):
        result = rank(self.data, {"track": "all"})
        effects = {row["reviewer_id"]: row for row in result["reviewers"]}
        # 原始均分最低的两位评委（评分格仅9）应被明显收缩
        self.assertLess(effects["reviewer_09"]["overall_effect"], 0)
        self.assertGreater(effects["reviewer_00"]["overall_effect"], 0)
        for criterion, value in effects["reviewer_11"]["shrinkage"].items():
            self.assertAlmostEqual(value, 3 / (3 + RIDGE_PRIOR))
        self.assertEqual(effects["reviewer_11"]["reliability"], "limited")
        self.assertEqual(effects["reviewer_00"]["reliability"], "sufficient")

    def test_weight_change_moves_rankings_but_not_calibrated_means(self):
        baseline = rank(self.data, {"track": "all"})
        heavy = rank(self.data, {"track": "all"},
                     {"weights": {"rigor": 10, "clarity": 1, "usefulness": 1}})
        base_by_id = {row["project_id"]: row for row in baseline["rankings"]}
        heavy_by_id = {row["project_id"]: row for row in heavy["rankings"]}
        # 各标准校准分与评委效应不依赖权重
        for project_id in base_by_id:
            self.assertEqual(base_by_id[project_id]["adjusted"], heavy_by_id[project_id]["adjusted"])
        self.assertNotEqual(
            [row["project_id"] for row in baseline["rankings"]],
            [row["project_id"] for row in heavy["rankings"]])
        # 权重自动归一化
        self.assertAlmostEqual(sum(heavy["config"]["weights"].values()), 1.0)

    def test_manual_exclusion_and_conflict_toggle_change_evidence(self):
        baseline = rank(self.data, {"track": "all"})
        manual = rank(self.data, {"track": "all"},
                      {"excluded_pairs": [{"project_id": "project_09",
                                           "reviewer_id": "reviewer_03", "reason": "主席核查"}]})
        self.assertTrue(any(item["source"] == "manual" for item in manual["exclusions_applied"]))
        self.assertNotEqual(
            json.dumps(baseline["rankings"], sort_keys=True),
            json.dumps(manual["rankings"], sort_keys=True))
        without_conflict = rank(self.data, {"track": "all"}, {"exclude_conflicts": False})
        self.assertEqual(without_conflict["summary"]["回避格"], 0)
        self.assertEqual(without_conflict["summary"]["有效评分格"],
                         baseline["summary"]["有效评分格"] + 3)
        self.assertTrue(any("关闭了利益冲突自动回避" in note for note in without_conflict["notes"]))

    def test_single_reviewer_projects_are_flagged_insufficient(self):
        result = rank(self.data, {"track": "all"})
        thin = {row["project_id"]: row for row in result["rankings"]
                if row["reliability"] == "insufficient"}
        self.assertEqual(set(thin), {"project_22", "project_23"})
        for row in thin.values():
            self.assertEqual(row["scored_reviewers"], 1)
            self.assertGreater(row["composite_se"],
                               next(item["composite_se"] for item in result["rankings"]
                                    if item["reliability"] == "sufficient"))

    def test_stability_ranges_are_deterministic_and_bounded(self):
        first = rank(self.data, {"track": "all"}, {"top_k": 5})
        second = rank(self.data, {"track": "all"}, {"top_k": 5})
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        for row in first["rankings"]:
            weight = row["stability"]["weight"]
            self.assertEqual(weight["trials"], PERTURBATION_TRIALS)
            self.assertLessEqual(weight["min_rank"], row["stability"]["weight"]["max_rank"])
            self.assertEqual(weight["max_rank"] - weight["min_rank"] >= 0, True)
            self.assertTrue(0.0 <= weight["top_k_frequency"] <= 1.0)
            jackknife = row["stability"]["jackknife"]
            self.assertEqual(jackknife["folds"], 12)

    def test_invalid_configs_are_rejected(self):
        for bad in ({"weights": {"rigor": -1}},
                    {"weights": {"rigor": 0, "clarity": 0, "usefulness": 0}},
                    {"weights": {"unknown": 1}},
                    {"excluded_pairs": [{"project_id": "project_09", "reviewer_id": "ghost"}]},
                    {"excluded_pairs": [{"project_id": "project_09", "reviewer_id": "reviewer_00"},
                                        {"project_id": "project_09", "reviewer_id": "reviewer_00"}]},
                    {"top_k": 0}, {"top_k": 999}, {"exclude_conflicts": "yes"},
                    {"weights": "not-object"}, {"top_k": True}):
            with self.assertRaises(ValueError):
                normalize_config(self.data, bad)

    def test_ranking_does_not_mutate_inputs(self):
        data = fixture()
        snapshot = deepcopy(data)
        rank(data, {"track": "all"},
             {"excluded_pairs": [{"project_id": "project_09", "reviewer_id": "reviewer_03"}]})
        self.assertEqual(snapshot, data)

    def test_output_is_json_safe_without_nan(self):
        result = rank(synthetic_dataset(), {"track": "all"})
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        self.assertNotIn("NaN", encoded)

    def test_unknown_track_raises(self):
        with self.assertRaises(ValueError):
            rank(self.data, {"track": "nonexistent"})

    def test_single_project_keeps_identifiable_reviewer_effects(self):
        data = {
            "projects": [{"project_id": "solo", "track": "all"}],
            "reviewers": [{"reviewer_id": "r1", "topic": "x"}, {"reviewer_id": "r2", "topic": "x"}],
            "rubric": [{"id": "c1", "maximum": 10, "weight": 1}],
            "conflicts": [],
            "ratings": [
                {"project_id": "solo", "reviewer_id": "r1", "criterion": "c1",
                 "status": "scored", "score": 8.0},
                {"project_id": "solo", "reviewer_id": "r2", "criterion": "c1",
                 "status": "scored", "score": 6.0},
            ],
        }
        result = rank(data, {"track": "all"})
        # 评委编号不依赖字符串前缀；单项目时项目效应约束为零、评委效应仍可识别
        self.assertEqual(result["components"][0]["reviewers"], ["r1", "r2"])
        effects = {row["reviewer_id"]: row["effects"]["c1"] for row in result["reviewers"]}
        self.assertAlmostEqual(effects["r1"], 0.25)   # (8-7)/(1+3)
        self.assertAlmostEqual(effects["r2"], -0.25)  # (6-7)/(1+3)
        self.assertEqual(result["rankings"][0]["adjusted"]["c1"], 7.0)
        json.dumps(result, allow_nan=False)

    def test_no_scored_cells_leaves_all_projects_unranked(self):
        data = {
            "projects": [{"project_id": "a", "track": "all"}, {"project_id": "b", "track": "all"}],
            "reviewers": [{"reviewer_id": "r1", "topic": "x"}],
            "rubric": [{"id": "c1", "maximum": 10, "weight": 1}],
            "conflicts": [],
            "ratings": [
                {"project_id": "a", "reviewer_id": "r1", "criterion": "c1",
                 "status": "abstain", "score": None},
                {"project_id": "b", "reviewer_id": "r1", "criterion": "c1",
                 "status": "invalid", "score": None},
            ],
        }
        result = rank(data, {"track": "all"})
        self.assertEqual(result["rankings"], [])
        self.assertEqual({row["project_id"] for row in result["unranked"]}, {"a", "b"})
        self.assertEqual(result["summary"]["有效评分格"], 0)
        json.dumps(result, allow_nan=False)


class ServiceRankingTests(unittest.TestCase):
    def setUp(self):
        from tempfile import TemporaryDirectory
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workbench = Workbench(ROOT, self.temporary.name)

    def test_inspect_contains_ranking_and_normalized_config(self):
        result = self.workbench.inspect({"ranking_config": {"weights": {"rigor": 4, "clarity": 1, "usefulness": 1}}})
        self.assertIn("ranking", result)
        self.assertAlmostEqual(result["ranking_config"]["weights"]["rigor"], 4 / 6)
        self.assertEqual(len(result["ranking"]["rankings"]),
                         len(result["view"]["rows"]))

    def test_saved_notebook_freezes_ranking_against_later_weights(self):
        saved = self.workbench.save({"title": "冻结排序", "ranking_config": {"top_k": 4}})
        frozen_config = deepcopy(saved["result"]["ranking_config"])
        frozen_ranking = deepcopy(saved["result"]["ranking"])
        # 主席随后用新权重重新计算
        later = self.workbench.inspect({"ranking_config": {"weights": {"rigor": 10, "clarity": 1, "usefulness": 1}}})
        self.assertNotEqual(later["ranking_config"], frozen_config)
        # 已保存材料不变
        reopened = self.workbench.store.notebook(saved["id"])
        self.assertEqual(reopened["result"]["ranking_config"], frozen_config)
        self.assertEqual(json.dumps(reopened["result"]["ranking"], sort_keys=True),
                         json.dumps(frozen_ranking, sort_keys=True))

    def test_new_dataset_cannot_overwrite_saved_notebook(self):
        saved = self.workbench.save({"title": "先保存"})
        data = self.workbench.store.dataset(saved["revision"])
        data["context_note"] = "后补材料"
        self.workbench.upload({"label": "新版本", "data": data})
        reopened = self.workbench.store.notebook(saved["id"])
        self.assertEqual(reopened["result"], saved["result"])

    def test_bad_ranking_config_returns_value_error(self):
        with self.assertRaises(ValueError):
            self.workbench.inspect({"ranking_config": {"top_k": -1}})


if __name__ == "__main__":
    unittest.main()

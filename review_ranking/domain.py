from collections import Counter, defaultdict
from statistics import mean, median
import math

from .validation import require_tables, unique, finite, references


def validate(data):
    require_tables(data, ["projects", "reviewers", "ratings", "rubric", "conflicts"])
    unique(data["projects"], ["project_id"])
    unique(data["reviewers"], ["reviewer_id"])
    unique(data["rubric"], ["id"])
    unique(data["ratings"], ["project_id", "reviewer_id", "criterion"])
    references(data["ratings"], "project_id", data["projects"], "project_id")
    references(data["ratings"], "reviewer_id", data["reviewers"], "reviewer_id")
    references(data["ratings"], "criterion", data["rubric"], "id")
    rules = {record["id"]: record for record in data["rubric"]}
    for rule in rules.values():
        finite(rule["maximum"], minimum=0.00001)
        finite(rule["weight"], minimum=0)
    for rating in data["ratings"]:
        if rating["status"] == "scored":
            finite(rating["score"], minimum=0, maximum=rules[rating["criterion"]]["maximum"])
        elif rating["status"] not in {"abstain", "invalid"} or rating["score"] is not None:
            raise ValueError("评分状态与数值不相容")


def explore(data, selection):
    track = selection.get("track", "all")
    projects = [project for project in data["projects"] if track == "all" or project["track"] == track]
    if not projects:
        raise ValueError("未知展示分组")
    grouped = defaultdict(list)
    conflicts = {(record["project_id"], record["reviewer_id"]) for record in data["conflicts"]}
    for rating in data["ratings"]:
        if rating["status"] == "scored" and (rating["project_id"], rating["reviewer_id"]) not in conflicts:
            grouped[rating["project_id"]].append(rating)
    rows = []
    for project in projects:
        ratings = grouped[project["project_id"]]
        rows.append({"project_id": project["project_id"], "track": project["track"], "score_cells": len(ratings),
                     "reviewers": len({rating["reviewer_id"] for rating in ratings}),
                     "raw_mean": mean(rating["score"] for rating in ratings) if ratings else None})
    return {"summary": {"项目": len(rows), "有效评分格": sum(row["score_cells"] for row in rows), "利益回避关系": len(conflicts)},
            "rows": rows, "series": [{"label": row["project_id"], "y": row["raw_mean"]} for row in rows],
            "notes": ["原始均分用于核对评分，不是经过严宽校准的排名。", "回避评分不参与描述，但原始记录仍可查。"]}

"""Bind AI scenario review records to exact fixture content, never human approval."""
import hashlib
import json


def fingerprint(row):
    content = json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def check_review_evidence(rows, reviews):
    errors, indexed = [], {}
    for review in reviews:
        key = review.get("record_id")
        if key in indexed:
            errors.append(f"Duplicate review evidence: {key}")
        indexed[key] = review
        if review.get("review_type") != "ai_scenario_review" or review.get("human_approved") is not False:
            errors.append(f"{key}: AI review must not claim human approval")
        if not review.get("rationale") or not review.get("before_sha256"):
            errors.append(f"{key}: missing review rationale or baseline fingerprint")
    if set(indexed) != {row["id"] for row in rows}:
        errors.append("Review evidence does not cover exactly the current documents and cases")
    for row in rows:
        review = indexed.get(row["id"], {})
        if review.get("after_sha256") != fingerprint(row):
            errors.append(f"{row['id']}: changed since scenario review; review evidence is stale")
    return errors

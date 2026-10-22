"""Public evidence snapshots. Retrieval relevance is never an accuracy score."""
import math
from urllib.parse import urlsplit


TEXT_FIELDS = (
    "title", "document_id", "document_version", "chunk_id", "locator",
    "product_version", "knowledge_base_id", "product_id", "product_name",
    "updated_at", "valid_from", "valid_until", "score_type", "distance_metric",
)


def safe_source_url(value):
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return ""
    try:
        url = urlsplit(value)
        return value if url.scheme in {"http", "https"} and url.hostname and not url.username and not url.password else ""
    except ValueError:
        return ""


def public_sources(sources):
    """Keep only user-facing fields, with bounded size and explicit truncation."""
    result = []
    for source in sources or []:
        if not isinstance(source, dict):
            continue
        item = {key: str(source[key])[:500] for key in TEXT_FIELDS
                if isinstance(source.get(key), (str, int, float)) and not isinstance(source.get(key), bool)}
        content = str(source.get("content") or "")
        item.update(content=content[:6000], content_truncated=len(content) > 6000 or source.get("content_truncated") is True,
                    source_url=safe_source_url(source.get("source_url")))
        for key in ("score", "distance"):
            value = source.get(key)
            if type(value) in (int, float) and math.isfinite(value):
                item[key] = value
        result.append(item)
    return result

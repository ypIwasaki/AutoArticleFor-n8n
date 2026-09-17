"""Pure presentation of dashboard records, reviews and aggregate counts."""

from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any

from article_feedback_service import feedback_is_rejected


def _official_identity(value: Any) -> str:
    return re.sub(r"[\s\u3000]+", "", str(value or "")).casefold()



def _alias_names(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]



def _date_key(value: Any) -> str:
    return str(value or "")[:10]



def _enrich_talents(
    talents: list[dict[str, Any]], official_registry: dict[str, Any]
) -> list[dict[str, Any]]:
    """Prefer organization/name matches; use a name alone only when unambiguous."""
    registry_by_org_name: dict[tuple[str, str], dict[str, Any]] = {}
    registry_by_name: dict[str, list[dict[str, Any]]] = {}
    for registry_talent in official_registry.get("talents", []):
        if not isinstance(registry_talent, dict):
            continue
        organization = _official_identity(registry_talent.get("organization"))
        names = [registry_talent.get("display_name"), *registry_talent.get("aliases", [])]
        for value in names:
            name = _official_identity(value)
            if not name:
                continue
            registry_by_org_name[(organization, name)] = registry_talent
            matches = registry_by_name.setdefault(name, [])
            if registry_talent not in matches:
                matches.append(registry_talent)

    def official_record_for(talent: dict[str, Any]) -> dict[str, Any] | None:
        organization = _official_identity(talent.get("organization"))
        names = [talent.get("display_name"), *_alias_names(talent.get("aliases_json"))]
        for value in names:
            name = _official_identity(value)
            if not name:
                continue
            match = registry_by_org_name.get((organization, name))
            if match:
                return match
        for value in names:
            matches = registry_by_name.get(_official_identity(value), [])
            if len(matches) == 1:
                return matches[0]
        return None

    enriched_talent_records: list[dict[str, Any]] = []
    for talent in talents:
        official = official_record_for(talent)
        official_fields = {}
        if official:
            official_fields = {
                "officialProfileUrl": str(official.get("profile_url") or ""),
                "officialRosterUrl": str(official.get("source_url") or ""),
                "officialGroupId": str(official.get("group_id") or ""),
                "officialGroupName": str(official.get("group_name") or ""),
                "officialRegistryUpdatedAt": str(official_registry.get("generatedAt") or ""),
            }
        enriched_talent_records.append({**talent, **official_fields})

    return enriched_talent_records


def _classifications_by_article(
    articles: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    saved_classifications: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Resolve proposal URLs, then let adopted database values take precedence."""
    article_key_by_url = {
        str(article.get("url", "")).strip(): str(article.get("article_key", "")).strip()
        for article in articles
        if str(article.get("url", "")).strip() and str(article.get("article_key", "")).strip()
    }
    classification_map: dict[str, dict[str, Any]] = {}
    for classification in proposals:
        article_key = str(classification.get("article_key", "")).strip()
        if not article_key:
            article_key = article_key_by_url.get(str(classification.get("article_url", "")).strip(), "")
        if article_key:
            classification_map[article_key] = classification
    for classification in saved_classifications:
        article_key = str(classification.get("article_key", "")).strip()
        if article_key:
            classification_map[article_key] = classification
    return classification_map


def _build_summary(
    enriched_talents: list[dict[str, Any]],
    visible_enriched_articles: list[dict[str, Any]],
    enriched_relations: list[dict[str, Any]],
    article_feedback_by_key: dict[str, dict[str, Any]],
    rejected_feedback_by_key: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Count visible articles and relations while retaining all review totals."""
    daily_volume: dict[str, int] = {}
    for article in visible_enriched_articles:
        key = _date_key(article.get("published_at") or article.get("last_seen_at"))
        if key:
            daily_volume[key] = daily_volume.get(key, 0) + 1

    organizations = sorted(
        {
            str(talent.get("organization", "")).strip()
            for talent in enriched_talents
            if str(talent.get("organization", "")).strip()
        },
        key=str.lower,
    )
    status_counts: dict[str, int] = {}
    for talent in enriched_talents:
        status = str(talent.get("status", "unknown"))
        status_counts[status] = status_counts.get(status, 0) + 1

    article_type_counts: dict[str, int] = {}
    primary_category_counts: dict[str, int] = {}
    relevance_counts: dict[str, int] = {}
    for article in visible_enriched_articles:
        classification = article.get("classification", {})
        if not classification:
            continue
        article_type = str(classification.get("article_type", "")).strip()
        primary_category = str(classification.get("primary_category", "")).strip()
        relevance = str(classification.get("relevance", "")).strip()
        if article_type:
            article_type_counts[article_type] = article_type_counts.get(article_type, 0) + 1
        if primary_category:
            primary_category_counts[primary_category] = primary_category_counts.get(primary_category, 0) + 1
        if relevance:
            relevance_counts[relevance] = relevance_counts.get(relevance, 0) + 1

    return {
        "talents": len(enriched_talents),
        "articles": len(visible_enriched_articles),
        "rejectedArticles": len(rejected_feedback_by_key),
        "reviewedArticles": len(article_feedback_by_key),
        "relations": len(enriched_relations),
        "searchEnabled": sum(
            1 for talent in enriched_talents if talent.get("search_enabled")
        ),
        "articleSummaries": sum(
            1 for article in visible_enriched_articles if article.get("ai_summary")
        ),
        "articleClassifications": sum(
            1 for article in visible_enriched_articles if article.get("classification")
        ),
        "statusCounts": status_counts,
        "articleTypeCounts": article_type_counts,
        "primaryCategoryCounts": primary_category_counts,
        "relevanceCounts": relevance_counts,
        "dailyVolume": [
            {"date": date, "count": daily_volume[date]} for date in sorted(daily_volume)
        ],
        "organizations": organizations,
    }


def build_dashboard_payload(
    payload: dict[str, Any],
    *,
    source: str,
    source_error: str | None,
    article_summaries: dict[str, dict[str, Any]],
    classification_taxonomy: dict[str, Any],
    official_registry: dict[str, Any],
    classification_proposals: list[dict[str, Any]],
    generated_at: datetime,
) -> dict[str, Any]:
    """Build the dashboard response without IO or mutation of input records."""
    talents = payload["talents"]
    all_articles = payload["articles"]
    article_feedback_by_key = {
        str(feedback.get("article_key") or "").strip(): feedback
        for feedback in payload.get("article_feedback", [])
        if str(feedback.get("article_key") or "").strip()
    }
    rejected_feedback_by_key = {
        article_key: feedback
        for article_key, feedback in article_feedback_by_key.items()
        if feedback_is_rejected(feedback.get("is_rejected"))
    }
    articles = [
        article
        for article in all_articles
        if str(article.get("article_key") or "").strip() not in rejected_feedback_by_key
    ]
    visible_article_keys = {
        str(article.get("article_key") or "").strip() for article in articles
    }
    all_relations = payload["article_talents"]
    relations = [
        relation
        for relation in all_relations
        if str(relation.get("article_key") or "").strip() in visible_article_keys
    ]
    enriched_talent_records = _enrich_talents(talents, official_registry)

    article_map = {
        str(article.get("article_key", "")): article for article in all_articles
    }
    classification_map = _classifications_by_article(
        all_articles, classification_proposals,
        payload.get("article_classifications", []),
    )
    talent_map = {
        str(talent.get("talent_id", "")): talent for talent in enriched_talent_records
    }

    relation_counts: dict[str, int] = {}
    article_talents: dict[str, list[dict[str, Any]]] = {}
    for relation in all_relations:
        article_key = str(relation.get("article_key", ""))
        talent_id = str(relation.get("talent_id", ""))
        article_talents.setdefault(article_key, []).append(talent_map.get(talent_id, {}))

    talent_articles: dict[str, list[dict[str, Any]]] = {}
    enriched_relations: list[dict[str, Any]] = []
    for relation in relations:
        talent_id = str(relation.get("talent_id", ""))
        article_key = str(relation.get("article_key", ""))
        relation_counts[talent_id] = relation_counts.get(talent_id, 0) + 1
        talent_articles.setdefault(talent_id, []).append(article_map.get(article_key, {}))
        enriched_relations.append(
            {
                **relation,
                "talent": talent_map.get(talent_id, {}),
                "article": article_map.get(article_key, {}),
            }
        )

    enriched_talents = [
        {**talent, "article_count": relation_counts.get(str(talent.get("talent_id", "")), 0)}
        for talent in enriched_talent_records
    ]
    enriched_articles = []
    for article in all_articles:
        summary = article_summaries.get(str(article.get("url", "")).strip(), {})
        enriched_articles.append(
            {
                **article,
                "talents": [
                    talent
                    for talent in article_talents.get(str(article.get("article_key", "")), [])
                    if talent
                ],
                "ai_summary": summary.get("text", ""),
                "summary_date": summary.get("summary_date", ""),
                "summary_source_titles": summary.get("source_titles", []),
                "classification": classification_map.get(str(article.get("article_key", "")), {}),
                "feedback": article_feedback_by_key.get(str(article.get("article_key", "")), {}),
            }
        )

    visible_enriched_articles = [
        article
        for article in enriched_articles
        if not feedback_is_rejected((article.get("feedback") or {}).get("is_rejected"))
    ]

    return {
        "generatedAt": generated_at.isoformat(),
        "source": source,
        "sourceError": source_error,
        "summary": _build_summary(
            enriched_talents, visible_enriched_articles, enriched_relations,
            article_feedback_by_key, rejected_feedback_by_key,
        ),
        "classificationTaxonomy": classification_taxonomy,
        "talents": sorted(
            enriched_talents,
            key=lambda item: (
                str(item.get("display_name", "")).lower(),
                str(item.get("talent_id", "")),
            ),
        ),
        "articles": sorted(
            enriched_articles,
            key=lambda item: str(item.get("published_at") or item.get("last_seen_at") or ""),
            reverse=True,
        ),
        "relations": sorted(
            enriched_relations,
            key=lambda item: str(item.get("last_seen_at", "")),
            reverse=True,
        ),
        "talentArticles": talent_articles,
    }


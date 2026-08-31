"""Every algorithm threshold in one place, grouped by the stage it steers.

The single source of truth; reachable as StoreConfig.tuning and overridable per store from
the `tuning:` block in config/stores.yaml. Gotchas: docs/reference/core.md
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BlockTuning(Strict):
    chrome_threshold: float = 0.8
    min_block_chars: int = 3
    cross_page_min_pages: int = 3
    cross_page_min_words: int = 20
    max_label_words: int = 4


class MatchTuning(Strict):
    containment_threshold: float = 0.8
    min_candidate_tokens: int = 2
    min_candidate_chars: int = 8
    window_slack: float = 2.0
    min_window: int = 6
    min_prose_tokens_for_title_check: int = 15


class QuotabilityTuning(Strict):
    quotable_max_tokens: int = 8
    theme_quotable_max_tokens: int = 15


class CrawlTuning(Strict):
    group_floor: int = 3


class ProfilingTuning(Strict):
    allowlist_min_support: int = 3
    stale_support_ratio: float = 0.5
    allowlist_min_hit_rate: float = 0.8
    foreign_title_reject_rate: float = 0.25
    label_min_observations: int = 2
    label_min_dominance: float = 0.8
    spec_value_max_tokens: int = 25


class DocumentTuning(Strict):
    max_field_chars: int = 1200


class RetrievalTuning(Strict):
    rrf_k: int = 60
    first_stage_limit: int = 200
    live_read_limit: int = 60
    rerank_doc_chars: int = 4000


class ChatTuning(Strict):
    max_retrieval_shown: int = 12


class LlmTuning(Strict):
    default_budget: int = 512


class EvaluationTuning(Strict):
    significance_max_p: float = 0.05


class Tuning(Strict):
    blocks: BlockTuning = BlockTuning()
    matching: MatchTuning = MatchTuning()
    quotability: QuotabilityTuning = QuotabilityTuning()
    crawl: CrawlTuning = CrawlTuning()
    profiling: ProfilingTuning = ProfilingTuning()
    documents: DocumentTuning = DocumentTuning()
    retrieval: RetrievalTuning = RetrievalTuning()
    chat: ChatTuning = ChatTuning()
    llm: LlmTuning = LlmTuning()
    evaluation: EvaluationTuning = EvaluationTuning()


DEFAULTS = Tuning()

"""Groundedness scoring: what counts as supported, and what quietly does not.

The failure this guards against is an answer that looks cited because a sentence near it
carries ids. Parsing has to attribute each citation to the claim it follows, or an
unsupported sentence hides behind the citations of the sentence before it.
"""

from __future__ import annotations

from pier39_poc.chat import (
    CITATION_RE,
    Citation,
    Turn,
    _fact_line,
    _render_facts,
    _verify,
    replay,
)

QUOTABLE = [
    {"id": 1, "field": "material", "label": "Material", "value": "BPA-free", "trust_class": "quotable"},
    {"id": 2, "field": "power", "label": "Power", "value": "USB rechargeable", "trust_class": "quotable"},
]
BACKGROUND = [
    {"id": 9, "field": "description", "label": None, "value": "Marketing prose.", "trust_class": "retrieval"},
]


def turn(text: str, **kwargs) -> Turn:
    return Turn(
        question="q",
        mode=kwargs.pop("mode", "scoped"),
        store_slug="remi",
        handle=kwargs.pop("handle", "water-flosser"),
        text=text,
        **kwargs,
    )


def test_citation_pattern_reads_ids():
    assert CITATION_RE.findall("a [a:12] b [a:340]") == ["12", "340"]


def test_trailing_citations_attach_to_the_claim_they_follow():
    t = turn("It is BPA-free. [a:1]")
    assert t.sentences == ["It is BPA-free. [a:1]"]
    assert t.uncited_sentences == []


def test_an_uncited_sentence_cannot_hide_behind_the_previous_citation():
    t = turn("It is BPA-free.[a:1] It is also dishwasher safe.")
    assert len(t.sentences) == 2
    assert t.uncited_sentences == ["It is also dishwasher safe."]


def test_a_decimal_point_is_not_a_sentence_terminator():
    """A 4.9 rating used to shatter into `It has a 4.` and `9 rating ...`.

    Both fragments then read as uncited claims, which is most of what an early version of
    this scorer was counting as ungrounded.
    """
    t = turn("It has a 4.9 rating and a 6.7 oz tank. [a:1]")
    assert t.sentences == ["It has a 4.9 rating and a 6.7 oz tank. [a:1]"]
    assert t.uncited_sentences == []


def test_text_without_a_terminator_is_still_one_sentence():
    assert turn("No terminator here [a:1]").sentences == ["No terminator here [a:1]"]


def test_empty_text_has_no_sentences():
    assert turn("").sentences == []


def test_verify_accepts_a_quotable_citation():
    got = _verify([1], QUOTABLE, BACKGROUND)
    assert got[0].valid
    assert got[0].label == "Material"


def test_verify_rejects_a_background_citation():
    got = _verify([9], QUOTABLE, BACKGROUND)
    assert not got[0].valid
    assert "background" in got[0].reason


def test_verify_rejects_an_invented_id():
    got = _verify([4242], QUOTABLE, BACKGROUND)
    assert not got[0].valid
    assert "no such assertion" in got[0].reason


def test_outcome_grounded_when_every_citation_is_valid():
    t = turn("It is BPA-free. [a:1]", citations=_verify([1], QUOTABLE, BACKGROUND),
             shown_quotable=QUOTABLE, shown_retrieval=BACKGROUND)
    assert t.outcome == "grounded"


def test_outcome_ungrounded_on_an_invalid_citation():
    t = turn("It is BPA-free. [a:9]", citations=_verify([9], QUOTABLE, BACKGROUND),
             shown_quotable=QUOTABLE, shown_retrieval=BACKGROUND)
    assert t.outcome == "ungrounded"


def test_outcome_ungrounded_on_an_uncited_claim():
    t = turn("It is BPA-free.[a:1] It floats.", citations=_verify([1], QUOTABLE, BACKGROUND),
             shown_quotable=QUOTABLE, shown_retrieval=BACKGROUND)
    assert t.outcome == "ungrounded"


def test_outcome_uncited_when_the_answer_carries_no_citations():
    """A refusal and an unsupported assertion land in the same bucket, by design.

    Nothing in the text separates "the catalogue is silent" from "the model made it up",
    so the scorer reports both rather than guessing and corrupting the ratio.
    """
    refusal = turn("The facts do not cover that.", shown_quotable=QUOTABLE)
    assertion = turn("It floats and is dishwasher safe.", shown_quotable=QUOTABLE)
    assert refusal.outcome == "uncited"
    assert assertion.outcome == "uncited"
    assert not refusal.grounded and not assertion.grounded


def test_outcome_error_short_circuits():
    t = turn("", error="boom")
    assert t.outcome == "error"


def test_facts_are_rendered_with_ids_and_tiers_separated():
    rendered = _render_facts(QUOTABLE, BACKGROUND)
    assert "[a:1] Material: BPA-free" in rendered
    assert rendered.index("QUOTABLE FACTS") < rendered.index("BACKGROUND FACTS")
    assert rendered.index("[a:1]") < rendered.index("BACKGROUND FACTS")
    assert rendered.index("[a:9]") > rendered.index("BACKGROUND FACTS")


def test_empty_tiers_are_stated_not_omitted():
    rendered = _render_facts([], [])
    assert rendered.count("(none)") == 2


def test_fact_line_falls_back_to_field_when_unlabelled():
    assert _fact_line(BACKGROUND[0]).startswith("[a:9] description:")


def test_scoped_turn_promotes_with_its_scope_attached():
    got = turn("x", handle="water-flosser").to_question_yaml()
    assert got["scope"] == ["water-flosser"]
    assert "expect_handles" not in got


def test_discovery_turn_promotes_with_expected_handles():
    class FakeHit:
        def __init__(self, handle):
            self.handle = handle

    t = turn("x", handle=None, mode="discovery",
             hits=[FakeHit("a"), FakeHit("b"), FakeHit("c"), FakeHit("d")])
    got = t.to_question_yaml()
    assert got["expect_handles"] == ["a", "b", "c"]
    assert "scope" not in got


def test_log_records_the_outcome_and_the_ids_shown():
    t = turn("It is BPA-free. [a:1]", citations=_verify([1], QUOTABLE, BACKGROUND),
             shown_quotable=QUOTABLE, shown_retrieval=BACKGROUND)
    log = t.to_log()
    assert log["outcome"] == "grounded"
    assert log["shown_quotable_ids"] == [1, 2]
    assert log["shown_retrieval_ids"] == [9]


def test_replay_excludes_uncited_answers_from_the_ratio(monkeypatch):
    import pier39_poc.chat as chat

    texts = iter([
        "It is BPA-free. [a:1]",
        "It is BPA-free.[a:9] It floats.",
        "I cannot tell from these facts.",
    ])

    def fake_answer(store, question, handle=None, **kwargs):
        t = turn(next(texts), handle=handle)
        t.shown_quotable = QUOTABLE
        t.citations = _verify([int(m) for m in CITATION_RE.findall(t.text)], QUOTABLE, BACKGROUND)
        return t

    monkeypatch.setattr(chat, "answer", fake_answer)
    result = chat.replay(None, [{"q": "a"}, {"q": "b"}, {"q": "c"}])
    assert result["grounded"] == 1
    assert result["ungrounded"] == 1
    assert result["uncited"] == 1
    assert result["groundedness"] == 0.5

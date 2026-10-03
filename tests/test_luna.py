import json
from threading import Barrier, Lock, get_ident

import httpx
import pytest
from pydantic import SecretStr

from app.clients import MetadataClient, RemoteClient
from app.config import Settings
from app.db import connect, store_raw_item
from app.luna import (
    LunaExtractor,
    LunaResult,
    comment_input,
    deduplicate_result,
    extraction_key,
    grounded_excerpt,
    validate_result,
)
from app.models import MentionSpan, RunMetrics
from app.pipeline import extract_and_resolve, processor_version, refresh_aggregates
from app.resolution import resolve_book


@pytest.fixture
def luna_settings(settings, monkeypatch):
    monkeypatch.setenv("EXTRACTION_BACKEND", "luna")
    settings.extraction_backend = "luna"
    settings.openai_api_key = SecretStr("test-key")
    return settings


def mention(raw="Dune", title="Dune", author="Frank Herbert", sentiment="recommended"):
    return {
        "raw": raw,
        "title": title,
        "author": author,
        "author_source": "inferred" if author else "unknown",
        "work_id": None,
        "confidence": 0.98,
        "sentiment": sentiment,
        "tags": ["fiction"],
    }


def payload(text="I recommend Dune."):
    return {"ancestors": [], "current_comment": {"text": text, "links": []}}


def response(mentions, status="completed"):
    return httpx.Response(
        200,
        json={
            "status": status,
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps({"mentions": mentions}),
                        }
                    ],
                }
            ],
            "usage": {
                "input_tokens": 500,
                "output_tokens": 80,
                "input_tokens_details": {"cached_tokens": 100},
            },
        },
    )


def seed(conn, text="I recommend Dune.", comment_id=101, parent=100):
    store_raw_item(conn, {"id": 100, "type": "story", "title": "Reading", "kids": [101]}, 100)
    store_raw_item(conn, {"id": comment_id, "parent": parent, "text": text, "by": "reader"}, 100)
    conn.commit()


@pytest.mark.parametrize("mentions,text", [([mention()], "I recommend Dune."), ([], "Thank you!")])
def test_request_contract_and_cached_positive_and_empty_results(luna_settings, mentions, text):
    requests = []

    def handle(request):
        requests.append(request)
        body = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert body["model"] == "gpt-6-luna"
        assert body["reasoning"] == {"effort": "none"}
        assert body["store"] is False
        assert body["text"]["format"]["strict"] is True
        assert body["text"]["format"]["schema"]["additionalProperties"] is False
        return response(mentions)

    with connect(luna_settings.database_path) as conn:
        metrics = RunMetrics()
        client = httpx.Client(transport=httpx.MockTransport(handle))
        extractor = LunaExtractor(luna_settings, conn, metrics, client=client)
        first = extractor.extract(payload(text))
        assert first.mentions == LunaResult(mentions=mentions).mentions
        assert extractor.extract(payload(text)) == first
        assert metrics.llm_requests == metrics.llm_cache_hits == 1
        assert metrics.llm_input_tokens == 500
        assert metrics.llm_cached_input_tokens == 100
        assert metrics.llm_output_tokens == 80
        extractor.close()
        offline = LunaExtractor(luna_settings, conn, RunMetrics(), offline=True)
        assert offline.extract(payload(text)) == first
        with pytest.raises(RuntimeError, match="No cached"):
            offline.extract(payload("Another uncached comment"))
        offline.close()
        assert len(requests) == 1


@pytest.mark.parametrize(
    "change,error",
    [
        ({"raw": "invented excerpt"}, "evidence absent"),
    ],
)
def test_ungrounded_outputs_are_rejected(change, error):
    result = LunaResult(mentions=[mention() | change])
    with pytest.raises(ValueError, match=error):
        validate_result(result, payload())


@pytest.mark.parametrize("author", [None, "", "   ", "Frank Herbert", "  Frank Herbert  "])
@pytest.mark.parametrize("source", ["unknown", "inferred", "stated", "context"])
def test_author_provenance_is_normalized_without_inventing_an_author(author, source):
    original = mention() | {"author": author, "author_source": source}
    result = LunaResult(mentions=[original])
    validate_result(result, payload())
    repaired = result.mentions[0]
    expected_author = author.strip() or None if author else None
    assert repaired.author == expected_author
    assert repaired.author_source == (
        "unknown" if expected_author is None else "inferred" if source == "unknown" else source
    )
    assert repaired.cache_data() == original
    # Re-validating already normalized data must not overwrite the recorded original.
    before = repaired.evidence()
    validate_result(result, payload())
    assert repaired.evidence() == before


def test_normalized_metadata_preserves_original_fields_on_cache_hits(luna_settings):
    original = mention() | {
        "author": "  Frank Herbert  ",
        "author_source": "unknown",
        "work_id": "/works/OL999W",
        "tags": ["fiction", "unknown-topic", "fiction"],
    }
    calls = []
    with connect(luna_settings.database_path) as conn:
        metrics = RunMetrics()
        extractor = LunaExtractor(
            luna_settings,
            conn,
            metrics,
            client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: calls.append(request) or response([original])
                )
            ),
        )
        first = extractor.extract(payload()).mentions[0]
        assert first.author == "Frank Herbert"
        assert first.author_source == "inferred"
        assert first.work_id is None
        assert first.tags == ["fiction"]
        assert first.evidence()["normalizations"]["author_source"] == {
            "original": "unknown",
            "normalized": "inferred",
        }
        cached = json.loads(
            conn.execute("SELECT response_json FROM extraction_cache").fetchone()[0]
        )
        assert cached["mentions"] == [original]
        again = extractor.extract(payload()).mentions[0]
        assert again.evidence() == first.evidence()
        assert len(calls) == metrics.llm_requests == metrics.llm_cache_hits == 1
        extractor.close()


@pytest.mark.parametrize(
    "model_link",
    [
        "OL123W",
        "/works/OL123W",
        "https://openlibrary.org/works/OL123W/Dune?edition=1",
    ],
)
def test_work_link_variants_only_bypass_matching_when_present_in_current_comment(model_link):
    data = payload()
    data["current_comment"]["links"] = [["https://openlibrary.org/works/OL123W/Dune", "Dune"]]
    result = LunaResult(mentions=[mention() | {"work_id": model_link}])
    validate_result(result, data)
    assert result.mentions[0].work_id == "/works/OL123W"
    # A link in an ancestor cannot authorize a current-comment link shortcut.
    result = LunaResult(mentions=[mention() | {"work_id": model_link}])
    data["ancestors"] = [{"text": "https://openlibrary.org/works/OL123W/Dune"}]
    data["current_comment"]["links"] = []
    validate_result(result, data)
    assert result.mentions[0].work_id is None


def test_cleared_link_and_missing_author_cannot_create_a_canonical_book(luna_settings):
    result = LunaResult(
        mentions=[
            mention(author=None)
            | {
                "work_id": "/works/OL999W",
                "author_source": "inferred",
            }
        ]
    )
    validate_result(result, payload())
    remote = RemoteClient(luna_settings)
    with connect(luna_settings.database_path) as conn:
        metrics = RunMetrics()
        assert (
            resolve_book(conn, result.mentions[0].span(), MetadataClient(remote, conn, metrics))[0]
            is None
        )
        assert metrics.metadata_lookups == 0
        assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0
    remote.close()


@pytest.mark.parametrize(
    "source,raw",
    [
        ("Dune\nby Frank Herbert", "Dune by Frank Herbert"),
        ("Dune\t\tby Frank Herbert", "Dune by Frank Herbert"),
        ("Dune\u00a0by Frank Herbert", "Dune by Frank Herbert"),
        ("\u201cDune\u201d by Frank Herbert", '"Dune" by Frank Herbert'),
        ("Frank Herbert\u2019s Dune", "Frank Herbert's Dune"),
        ("Dune\u2014great read", "Dune-great read"),
        ("Dune...great read", "Dune\u2026great read"),
        ("Dune", "DUNE"),
        ("Caf\u00e9", "Cafe\u0301"),
    ],
)
def test_typography_repairs_restore_a_literal_current_comment_excerpt(source, raw):
    text = "Intro: " + source + " END"
    result = LunaResult(mentions=[mention(raw=raw)])
    validate_result(result, payload(text))
    assert result.mentions[0].raw == source
    assert result.mentions[0].raw in text
    assert result.mentions[0].title == "Dune"


@pytest.mark.parametrize(
    "source,raw,expected",
    [
        ("I enjoyed Dune.", '"Dune"', "Dune"),
        ('I enjoyed "Dune."', '"Dune"', "Dune"),
        ("I enjoyed “Dune.”", '"Dune"', "Dune"),
        ("I enjoyed Dune.", "'Dune'", "Dune"),
        ("I enjoyed Dune.", "“Dune”", "Dune"),
        ("I enjoyed Dune.", "**Dune**", "Dune"),
        ("I enjoyed Dune.", "__Dune__", "Dune"),
        ("I enjoyed Dune.", '`"Dune"`', "Dune"),
        (
            "I enjoyed Dune.\nby Frank Herbert",
            '" DUNE. by Frank Herbert "',
            "Dune.\nby Frank Herbert",
        ),
        ('I enjoyed "Dune".', '"Dune"', '"Dune"'),
    ],
)
def test_balanced_wrappers_restore_only_a_contiguous_source_span(source, raw, expected):
    assert grounded_excerpt(raw, source) == expected


@pytest.mark.parametrize(
    "source,raw",
    [
        ("Dune", '"Dune."'),
        ("Dune and Foundation", '"Dune Foundation"'),
        ("Dune. I recommend Foundation.", "**Dune ... Foundation**"),
        ("Dune", '"Dunes"'),
        ("Dune", '"Dune'),
        ("Dune", 'Dune"'),
        ("Frank Herbert Dune", '"Frank Herbert\'s Dune"'),
        ("Café", '"Cafe"'),
        ("Dune", '" "'),
        ("Dune", '**""**'),
    ],
)
def test_wrapper_repair_preserves_internal_text_and_requires_content(source, raw):
    assert grounded_excerpt(raw, source) is None


@pytest.mark.parametrize(
    "text,source_raw,sentiment",
    [
        (
            "I don’t know if this is considered a classic, but I recently read and thoroughly "
            "enjoyed Daphne Du Maurier’s “Rebecca”. I think I might have read it in a single "
            "sitting.\nI was lead to Rebecca after I found and read a discarded copy of Donna "
            "Tartt’s “The Secret History”. I wanted more and Googled “books like\nRebecca”. "
            "Sadly, I thought Tartt’s “The Goldfinch”, which lots of people raved about, "
            "was not very good.",
            "“The Goldfinch”",
            "negative",
        ),
        (
            "Diff'rent strokes. In the depths of a 1.75-year-long major depressive episode "
            "in 2015-2016, one of the VERY few books that completely made me forget my misery "
            "while I read it was \"The Goldfinch.\" The others: Patrick O'Brian's "
            "Aubrey–Maturin series.",
            "The Goldfinch",
            "positive",
        ),
    ],
)
def test_goldfinch_thread_comments_extract_once_and_preserve_original_evidence_in_cache(
    luna_settings, text, source_raw, sentiment
):
    # Actual text from latest-thread comments 49903449 and 49910697.
    original = mention(
        raw='"The Goldfinch"', title="The Goldfinch", author="Donna Tartt", sentiment=sentiment
    )
    calls = []
    with connect(luna_settings.database_path) as conn:
        metrics = RunMetrics()
        extractor = LunaExtractor(
            luna_settings,
            conn,
            metrics,
            client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: calls.append(request) or response([original])
                )
            ),
        )
        result = extractor.extract(payload(text))
        extracted = result.mentions[0]
        assert extracted.raw == source_raw
        assert extracted.raw in text
        assert extracted.sentiment == sentiment
        assert extracted.evidence()["normalizations"]["raw"] == {
            "original": original["raw"],
            "normalized": source_raw,
        }
        cached = json.loads(
            conn.execute("SELECT response_json FROM extraction_cache").fetchone()[0]
        )
        assert cached["mentions"] == [original]
        assert extractor.extract(payload(text)).mentions[0].evidence() == extracted.evidence()
        assert len(calls) == metrics.llm_requests == metrics.llm_cache_hits == 1
        extractor.close()


@pytest.mark.parametrize(
    "source,raw",
    [
        ("Dune and Foundation", "Dune Foundation"),
        ("Dune. I also recommend Foundation.", "Dune ... Foundation"),
        ("I second that!", "Dune"),
        ("SICP", "Structure and Interpretation of Computer Programs"),
        ("Dune", "Dunes"),
        ("Caf\u00e9", "Cafe"),
        ("\ufb03", "f"),
        ("Dune", " \n\t "),
    ],
)
def test_typography_matching_never_reconstructs_missing_words(source, raw):
    assert grounded_excerpt(raw, source) is None


def test_repair_does_not_use_ancestor_evidence():
    data = payload("I second that!")
    data["ancestors"] = [{"id": 101, "text": "DUNE by Frank Herbert"}]
    with pytest.raises(ValueError, match="evidence absent"):
        validate_result(LunaResult(mentions=[mention(raw="Dune by Frank Herbert")]), data)
    with pytest.raises(ValueError, match="evidence absent"):
        validate_result(LunaResult(mentions=[mention(raw='"Dune"')]), data)


def test_invalid_excerpt_retry_explains_failure_and_preserves_all_supported_mentions(
    luna_settings,
    monkeypatch,
    caplog,
):
    monkeypatch.setattr("app.luna.time.sleep", lambda _: None)
    data = payload("After SICP, I recommend The Little Schemer.")
    expanded = "Structure and Interpretation of Computer Programs"
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert json.loads(body["input"]) == data
        else:
            repair_input = json.loads(body["input"])
            assert repair_input["current_comment"] == data["current_comment"]
            assert repair_input["validation_feedback"]["invalid_raw"] == expanded
            assert "copy a literal, contiguous excerpt" in body["instructions"]
            assert body["text"] == requests[0]["text"]
        return response(
            [
                mention(
                    raw=expanded if len(requests) == 1 else "SICP",
                    title=expanded,
                    author="Harold Abelson",
                    sentiment="neutral",
                ),
                mention(
                    raw="The Little Schemer",
                    title="The Little Schemer",
                    author="Daniel P. Friedman",
                ),
            ]
        )

    with connect(luna_settings.database_path) as conn:
        metrics = RunMetrics()
        extractor = LunaExtractor(
            luna_settings,
            conn,
            metrics,
            client=httpx.Client(transport=httpx.MockTransport(handle)),
        )
        with caplog.at_level("WARNING"):
            result = extractor.extract(data)
        assert [m.raw for m in result.mentions] == ["SICP", "The Little Schemer"]
        assert metrics.llm_requests == 2
        failure = next(r for r in caplog.records if r.message == "luna_evidence_validation_failed")
        assert failure.detail["model_raw"] == expanded
        assert failure.detail["current_comment"] == data["current_comment"]["text"]
        assert extractor.extract(data) == result
        assert len(requests) == 2
        extractor.close()


def test_duplicate_titles_are_collapsed_without_retries_and_cached_with_original_evidence(
    luna_settings,
):
    originals = [mention(), mention(title="DUNE!") | {"confidence": 0.9}]
    calls = []
    with connect(luna_settings.database_path) as conn:
        metrics = RunMetrics()
        extractor = LunaExtractor(
            luna_settings,
            conn,
            metrics,
            client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: calls.append(request) or response(originals)
                )
            ),
        )
        result = extractor.extract(payload())
        assert len(result.mentions) == 1
        assert result.mentions[0].title == "Dune"
        assert result.mentions[0].evidence()["duplicate_mentions"] == originals
        cached = json.loads(
            conn.execute("SELECT response_json FROM extraction_cache").fetchone()[0]
        )
        assert cached["mentions"] == originals
        again = extractor.extract(payload())
        assert again.mentions[0].evidence() == result.mentions[0].evidence()
        assert len(calls) == metrics.llm_requests == metrics.llm_cache_hits == 1
        extractor.close()


@pytest.mark.parametrize("other_sentiment", ["neutral", "negative", "positive"])
def test_conflicting_duplicate_sentiments_do_not_invent_a_recommendation(other_sentiment):
    result = deduplicate_result(
        LunaResult(
            mentions=[
                mention(),
                mention(sentiment=other_sentiment),
            ]
        )
    )
    merged = result.mentions[0]
    assert merged.sentiment == "neutral"
    assert merged.classification().recommendation_strength < 0.6
    assert merged.confidence == 0.98


@pytest.mark.parametrize("conflict", ["author", "work_id"])
def test_same_title_with_conflicting_identities_stays_unresolved(luna_settings, conflict):
    originals = [mention(), mention()]
    data = payload()
    if conflict == "author":
        originals[1]["author"] = "Brian Herbert"
    else:
        for index, original in enumerate(originals, 1):
            original["work_id"] = f"/works/OL{index}W"
            data["current_comment"]["links"].append(
                [f"https://openlibrary.org/works/OL{index}W", "Dune"]
            )
    result = LunaResult(mentions=originals)
    validate_result(result, data)
    merged = deduplicate_result(result).mentions[0]
    assert merged.author is merged.work_id is None
    assert merged.author_source == "unknown"
    assert merged.confidence < 0.7
    assert merged.evidence()["duplicate_mentions"] == originals
    remote = RemoteClient(luna_settings)
    with connect(luna_settings.database_path) as conn:
        metrics = RunMetrics()
        assert resolve_book(conn, merged.span(), MetadataClient(remote, conn, metrics))[0] is None
        assert metrics.metadata_lookups == 0
    remote.close()


def test_duplicate_identity_prefers_stated_author_and_keeps_supported_work_link():
    stated = mention() | {"author_source": "stated", "confidence": 0.95}
    linked = mention(author=None) | {"work_id": "/works/OL1W", "tags": ["history"]}
    merged = deduplicate_result(LunaResult(mentions=[linked, stated])).mentions[0]
    assert merged.author == "Frank Herbert"
    assert merged.author_source == "stated"
    assert merged.work_id == "/works/OL1W"
    assert merged.tags == ["fiction", "history"]


def test_invalid_evidence_in_a_duplicate_is_still_rejected_before_caching(
    luna_settings, monkeypatch
):
    monkeypatch.setattr("app.luna.time.sleep", lambda _: None)
    with connect(luna_settings.database_path) as conn:
        extractor = LunaExtractor(
            luna_settings,
            conn,
            RunMetrics(),
            client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda _: response([mention(), mention(raw="invented")])
                )
            ),
        )
        with pytest.raises(ValueError, match="evidence absent"):
            extractor.extract(payload())
        assert conn.execute("SELECT COUNT(*) FROM extraction_cache").fetchone()[0] == 0
        extractor.close()


def test_link_requires_actual_openlibrary_host():
    result = LunaResult(mentions=[mention() | {"work_id": "/works/OL123W"}])
    data = payload()
    data["current_comment"]["links"] = [["https://example.com/works/OL123W", "Dune"]]
    validate_result(result, data)
    assert result.mentions[0].work_id is None
    result = LunaResult(mentions=[mention() | {"work_id": "/works/OL123W"}])
    data["current_comment"]["links"] = [["https://openlibrary.org/works/OL123W/Dune", "Dune"]]
    validate_result(result, data)
    assert result.mentions[0].work_id == "/works/OL123W"


def test_empty_title_retry_has_specific_correction_feedback(luna_settings, monkeypatch):
    monkeypatch.setattr("app.luna.time.sleep", lambda _: None)
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return response([mention(title="...")])
        assert json.loads(body["input"])["validation_feedback"] == {
            "error": "empty_normalized_title",
            "invalid_title": "...",
        }
        assert "not just whitespace or punctuation" in body["instructions"]
        return response([mention()])

    with connect(luna_settings.database_path) as conn:
        extractor = LunaExtractor(
            luna_settings,
            conn,
            RunMetrics(),
            client=httpx.Client(transport=httpx.MockTransport(handle)),
        )
        assert extractor.extract(payload()).mentions[0].title == "Dune"
        assert len(requests) == 2
        extractor.close()


def test_incomplete_and_invalid_outputs_never_enter_cache(luna_settings, monkeypatch):
    monkeypatch.setattr("app.luna.time.sleep", lambda _: None)
    with connect(luna_settings.database_path) as conn:
        for output in [response([mention()], "incomplete"), response([mention(raw="invented")])]:
            client = httpx.Client(transport=httpx.MockTransport(lambda _, output=output: output))
            extractor = LunaExtractor(luna_settings, conn, RunMetrics(), client=client)
            with pytest.raises(ValueError):
                extractor.extract(payload())
            assert conn.execute("SELECT COUNT(*) FROM extraction_cache").fetchone()[0] == 0
            extractor.close()


def test_missing_key_and_auth_failure_have_no_heuristic_fallback(luna_settings):
    with connect(luna_settings.database_path) as conn:
        luna_settings.openai_api_key = SecretStr("")
        extractor = LunaExtractor(luna_settings, conn, RunMetrics())
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            extractor.extract(payload())
        extractor.close()
        luna_settings.openai_api_key = SecretStr("test-key")
        calls = []

        def handle(request):
            calls.append(request)
            return httpx.Response(401)

        extractor = LunaExtractor(
            luna_settings,
            conn,
            RunMetrics(),
            client=httpx.Client(transport=httpx.MockTransport(handle)),
        )
        with pytest.raises(httpx.HTTPStatusError):
            extractor.extract(payload())
        assert len(calls) == 1
        extractor.close()


def test_parent_edits_and_model_changes_invalidate_cached_extraction(luna_settings):
    with connect(luna_settings.database_path) as conn:
        seed(conn, "Dune by Frank Herbert")
        seed(conn, "I second that", comment_id=102, parent=101)
        comment = conn.execute("SELECT * FROM hn_comments WHERE id=102").fetchone()
        first = comment_input(conn, comment)
        assert first["ancestors"][0]["text"] == "Dune by Frank Herbert"
        key = extraction_key(luna_settings, first)
        version = processor_version(luna_settings)
        seed(conn, "Piranesi by Susanna Clarke")
        assert extraction_key(luna_settings, comment_input(conn, comment)) != key
        luna_settings.openai_model = "another-model"
        assert extraction_key(luna_settings, first) != key
        assert processor_version(luna_settings) != version


def test_luna_pipeline_keeps_separate_recommendations_and_full_titles(luna_settings, monkeypatch):
    text = "After SICP, I recommend The Little Schemer. Linear Algebra Done Right is also great."
    outputs = [
        mention(
            "SICP", "Structure and Interpretation of Computer Programs", "Harold Abelson", "neutral"
        ),
        mention("The Little Schemer", "The Little Schemer", "Daniel P. Friedman"),
        mention(
            "Linear Algebra Done Right", "Linear Algebra Done Right", "Sheldon Axler", "positive"
        ),
    ]
    requests = []
    api = httpx.Client(
        transport=httpx.MockTransport(lambda request: requests.append(request) or response(outputs))
    )
    monkeypatch.setattr(
        "app.pipeline.LunaExtractor",
        lambda settings, conn, metrics, offline: LunaExtractor(
            settings, conn, metrics, offline=offline, client=api
        ),
    )
    docs = [
        {"key": f"/works/OL{i}W", "title": m["title"], "author_name": [m["author"]]}
        for i, m in enumerate(outputs, 1)
    ]

    def metadata(request):
        if request.url.path == "/search.json":
            return httpx.Response(
                200, json={"docs": [d for d in docs if d["title"] == request.url.params["title"]]}
            )
        return httpx.Response(
            200, json={"title": docs[int(request.url.path.split("OL")[1][0]) - 1]["title"]}
        )

    remote = RemoteClient(luna_settings, httpx.Client(transport=httpx.MockTransport(metadata)))
    with connect(luna_settings.database_path) as conn:
        seed(conn, text)
        metrics = RunMetrics()
        extract_and_resolve(conn, MetadataClient(remote, conn, metrics), metrics)
        refresh_aggregates(conn)
        books = {row["canonical_title"]: dict(row) for row in conn.execute("SELECT * FROM books")}
        assert books[outputs[0]["title"]]["recommendation_count"] == 0
        assert books["The Little Schemer"]["recommendation_count"] == 1
        assert books["Linear Algebra Done Right"]["recommendation_count"] == 1
        assert "Done Right" not in books
        assert len(requests) == 1
        assert (
            json.loads(
                conn.execute("SELECT extraction_json FROM book_mentions LIMIT 1").fetchone()[0]
            )["author_source"]
            == "inferred"
        )
        extract_and_resolve(conn, MetadataClient(remote, conn, RunMetrics()), RunMetrics())
        assert len(requests) == 1


def test_failed_luna_call_preserves_old_mentions_and_processed_hash(luna_settings, monkeypatch):
    with connect(luna_settings.database_path) as conn:
        seed(conn)
        conn.execute("UPDATE hn_comments SET processed_hash='old-version'")
        conn.execute("""INSERT INTO book_mentions(
        comment_id,thread_id,raw_mention,normalized_mention,
        context_text,extraction_confidence,recommendation_strength,sentiment,status,created_at)
        VALUES (101,100,'Dune','dune','Original evidence',0.9,0.95,0.9,'unresolved','now')""")
        conn.commit()

        def fail(self, payload):
            raise ValueError("incomplete model response")

        monkeypatch.setattr(
            LunaExtractor, "_request", lambda self, payload, metrics: fail(self, payload)
        )
        remote = RemoteClient(luna_settings)
        with pytest.raises(ValueError, match="incomplete"):
            extract_and_resolve(conn, MetadataClient(remote, conn, RunMetrics()), RunMetrics())
        assert (
            conn.execute("SELECT context_text FROM book_mentions").fetchone()[0]
            == "Original evidence"
        )
        assert conn.execute("SELECT processed_hash FROM hn_comments").fetchone()[0] == "old-version"
        remote.close()


def test_authorless_or_wrong_author_cannot_resolve_to_publisher_adaptation(luna_settings):
    remote = RemoteClient(
        luna_settings,
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "docs": [
                            {
                                "key": "/works/OL1W",
                                "title": "Lord of the Rings",
                                "author_name": ["Cedco Publishing"],
                            }
                        ]
                    },
                )
            )
        ),
    )
    with connect(luna_settings.database_path) as conn:
        metadata = MetadataClient(remote, conn, RunMetrics())
        for author in [None, "J. R. R. Tolkien"]:
            span = MentionSpan(
                raw="Lord of the Rings",
                title="Lord of the Rings",
                author=author,
                confidence=0.98,
                require_author=True,
            )
            assert resolve_book(conn, span, metadata)[0] is None
        assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0
    remote.close()


def test_luna_is_the_default_backend(monkeypatch):
    monkeypatch.delenv("EXTRACTION_BACKEND", raising=False)
    settings = Settings(_env_file=None)
    assert settings.extraction_backend == "luna"
    assert settings.openai_model == "gpt-6-luna"
    assert settings.luna_workers == 4


@pytest.mark.parametrize("workers", [1, 3, 4])
def test_concurrent_requests_are_bounded_and_sqlite_stays_on_owner_thread(luna_settings, workers):
    luna_settings.luna_workers = workers
    barrier = Barrier(workers)
    lock = Lock()
    active = peak = calls = 0
    owner = get_ident()
    sql_threads = []

    def handle(request):
        nonlocal active, peak, calls
        assert get_ident() != owner
        with lock:
            active += 1
            calls += 1
            peak = max(peak, active)
        barrier.wait(timeout=5)
        with lock:
            active -= 1
        return response([])

    with connect(luna_settings.database_path) as conn:
        conn.set_trace_callback(lambda _: sql_threads.append(get_ident()))
        metrics = RunMetrics()
        extractor = LunaExtractor(
            luna_settings,
            conn,
            metrics,
            client=httpx.Client(transport=httpx.MockTransport(handle)),
        )
        data = [payload(f"No book in comment {i}") for i in range(workers * 2)]
        results = list(extractor.extract_many(data))
        assert sorted(index for index, _ in results) == list(range(len(data)))
        assert all(result.mentions == [] for _, result in results)
        assert calls == metrics.llm_requests == len(data)
        assert peak == workers
        assert set(sql_threads) == {owner}
        assert metrics.llm_input_tokens == 500 * len(data)
        assert metrics.llm_output_tokens == 80 * len(data)
        assert metrics.llm_cached_input_tokens == 100 * len(data)
        assert len(list(extractor.extract_many(data))) == len(data)
        assert calls == len(data)
        assert metrics.llm_cache_hits == len(data)
        extractor.close()


def test_identical_pending_inputs_share_one_request(luna_settings):
    calls = []
    with connect(luna_settings.database_path) as conn:
        metrics = RunMetrics()
        extractor = LunaExtractor(
            luna_settings,
            conn,
            metrics,
            client=httpx.Client(
                transport=httpx.MockTransport(lambda request: calls.append(request) or response([]))
            ),
        )
        assert len(list(extractor.extract_many([payload("Thanks!")] * 5))) == 5
        assert len(calls) == metrics.llm_requests == 1
        assert metrics.llm_cache_hits == 4
        extractor.close()


def test_failure_keeps_other_inflight_results_for_resume(luna_settings):
    luna_settings.luna_workers = 3
    barrier = Barrier(3)
    calls = []

    def handle(request):
        text = json.loads(json.loads(request.content)["input"])["current_comment"]["text"]
        calls.append(text)
        barrier.wait(timeout=5)
        return httpx.Response(401) if text == "fail" else response([])

    with connect(luna_settings.database_path) as conn:
        metrics = RunMetrics()
        extractor = LunaExtractor(
            luna_settings,
            conn,
            metrics,
            client=httpx.Client(transport=httpx.MockTransport(handle)),
        )
        with pytest.raises(httpx.HTTPStatusError):
            list(extractor.extract_many([payload("fail"), payload("one"), payload("two")]))
        assert len(calls) == 3
        assert conn.execute("SELECT COUNT(*) FROM extraction_cache").fetchone()[0] == 2
        assert metrics.llm_requests == 2
        assert len(list(extractor.extract_many([payload("one"), payload("two")]))) == 2
        assert len(calls) == 3
        extractor.close()


def test_closing_iteration_caches_other_inflight_responses(luna_settings):
    luna_settings.luna_workers = 3
    barrier = Barrier(3)

    def handle(request):
        barrier.wait(timeout=5)
        return response([])

    with connect(luna_settings.database_path) as conn:
        extractor = LunaExtractor(
            luna_settings,
            conn,
            RunMetrics(),
            client=httpx.Client(transport=httpx.MockTransport(handle)),
        )
        results = extractor.extract_many([payload(str(i)) for i in range(3)])
        next(results)
        results.close()
        assert conn.execute("SELECT COUNT(*) FROM extraction_cache").fetchone()[0] == 3
        extractor.close()


def test_pipeline_reports_each_comment_and_skips_completed_work(luna_settings, monkeypatch, caplog):
    api = httpx.Client(transport=httpx.MockTransport(lambda _: response([])))
    monkeypatch.setattr(
        "app.pipeline.LunaExtractor",
        lambda settings, conn, metrics, offline: LunaExtractor(
            settings, conn, metrics, offline=offline, client=api
        ),
    )
    remote = RemoteClient(luna_settings)
    with connect(luna_settings.database_path) as conn:
        seed(conn, "Thanks!")
        seed(conn, "Interesting!", comment_id=102)
        seed(conn, "", comment_id=103)
        metrics = RunMetrics()
        with caplog.at_level("INFO"):
            extract_and_resolve(conn, MetadataClient(remote, conn, metrics), metrics)
        assert metrics.comments_total == metrics.comments_processed == 3
        assert metrics.llm_requests == 2
        assert sum(r.message == "luna_processing_progress" for r in caplog.records) == 3
        resumed = RunMetrics()
        extract_and_resolve(conn, MetadataClient(remote, conn, resumed), resumed)
        assert resumed.comments_total == resumed.comments_skipped == 3
        assert resumed.comments_processed == resumed.llm_requests == 0
    remote.close()


@pytest.mark.parametrize(
    "work",
    [
        {"title": "A completely different work"},
        {"title": "Dune", "authors": [{"author": {"key": "/authors/OL9A"}}]},
    ],
)
def test_work_metadata_must_agree_with_search_result(luna_settings, work):
    def handle(request):
        if request.url.path == "/search.json":
            return httpx.Response(
                200,
                json={
                    "docs": [
                        {"key": "/works/OL1W", "title": "Dune", "author_name": ["Frank Herbert"]}
                    ]
                },
            )
        if request.url.path == "/authors/OL9A.json":
            return httpx.Response(200, json={"name": "Someone Else"})
        return httpx.Response(200, json=work)

    remote = RemoteClient(luna_settings, httpx.Client(transport=httpx.MockTransport(handle)))
    with connect(luna_settings.database_path) as conn:
        span = MentionSpan(
            raw="Dune", title="Dune", author="Frank Herbert", confidence=0.98, require_author=True
        )
        assert resolve_book(conn, span, MetadataClient(remote, conn, RunMetrics()))[0] is None
        assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0
    remote.close()

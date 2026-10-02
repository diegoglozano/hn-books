import json

import httpx
import pytest
from pydantic import SecretStr

from app.clients import MetadataClient, RemoteClient
from app.config import Settings
from app.db import connect, store_raw_item
from app.luna import LunaExtractor, LunaResult, comment_input, extraction_key, validate_result
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
        ({"tags": ["invented-topic"]}, "unknown topic"),
        ({"work_id": "/works/OL123W"}, "unsupported Open Library"),
        ({"author_source": "unknown"}, "author provenance"),
    ],
)
def test_ungrounded_outputs_are_rejected(change, error):
    result = LunaResult(mentions=[mention() | change])
    with pytest.raises(ValueError, match=error):
        validate_result(result, payload())


def test_link_requires_actual_openlibrary_host():
    result = LunaResult(mentions=[mention() | {"work_id": "/works/OL123W"}])
    data = payload()
    data["current_comment"]["links"] = [["https://example.com/works/OL123W", "Dune"]]
    with pytest.raises(ValueError, match="unsupported Open Library"):
        validate_result(result, data)
    data["current_comment"]["links"] = [["https://openlibrary.org/works/OL123W/Dune", "Dune"]]
    validate_result(result, data)


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

        monkeypatch.setattr(LunaExtractor, "extract", fail)
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

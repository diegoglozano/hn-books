import pytest
from fastapi.testclient import TestClient

from app.db import connect, store_raw_item
from app.main import create_app
from app.ranking import compute_book_scores, update_search_indexes


@pytest.fixture
def filtered_library(settings):
    with connect(settings.database_path) as conn:
        store_raw_item(conn, {"id": 100, "title": "Reading"}, 100)
        # Repeated tags and tags from different comments must not duplicate books.
        examples = {
            1: ("Database guide", ["databases", "databases"]),
            2: ("History guide", ["history"]),
            3: ("Database history guide", ["databases", "history"]),
            4: ("Fiction guide", ["fiction"]),
            5: ("Untagged guide", [None]),
        }
        comment_id = 100
        for book_id, (title, topics) in examples.items():
            conn.execute(
                """INSERT INTO books(id,canonical_title,normalized_title,created_at,updated_at)
                VALUES (?,?,?,'today','today')""",
                (book_id, title, title.lower()),
            )
            for topic in topics:
                comment_id += 1
                store_raw_item(
                    conn,
                    {"id": comment_id, "parent": 100, "by": "reader", "time": 1700000000},
                    100,
                )
                mention_id = conn.execute(
                    """INSERT INTO book_mentions(book_id,comment_id,thread_id,raw_mention,
                    normalized_mention,context_text,extraction_confidence,recommendation_strength,
                    sentiment,status,created_at)
                    VALUES (?,?,100,?,?,'Evidence',1,0.2,0,'resolved','today')""",
                    (book_id, comment_id, title, title.lower()),
                ).lastrowid
                if topic:
                    conn.execute(
                        """INSERT INTO book_mention_tags(mention_id,tag_id,confidence)
                        SELECT ?,id,1 FROM tags WHERE name=?""",
                        (mention_id, topic),
                    )
        compute_book_scores(conn)
        update_search_indexes(conn)
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.mark.parametrize("endpoint", ["/api/books", "/api/search"])
@pytest.mark.parametrize(
    ("included", "excluded", "expected"),
    [
        ([], [], {1, 2, 3, 4, 5}),
        (["databases", "history"], [], {1, 2, 3}),
        (["databases", "databases", "history"], [], {1, 2, 3}),
        ([], ["databases"], {2, 4, 5}),
        ([], ["databases", "history"], {4, 5}),
        (["databases", "history"], ["history"], {1}),
        (["databases"], ["databases"], set()),
        (["databases", "unknown"], [], {1, 3}),
        (["unknown"], [], set()),
        ([], ["unknown"], {1, 2, 3, 4, 5}),
        (["history"], ["' OR 1=1 --"], {2, 3}),
    ],
)
def test_topic_union_and_exclusions(filtered_library, endpoint, included, excluded, expected):
    params = [("tag", topic) for topic in included] + [("exclude_tag", topic) for topic in excluded]
    response = filtered_library.get(endpoint, params=params)
    assert response.status_code == 200
    result = response.json()
    assert result["total"] == len(expected)
    assert {book["id"] for book in result["items"]} == expected
    assert len(result["items"]) == len(expected)


@pytest.mark.parametrize("endpoint", ["/api/books", "/api/search"])
def test_topic_filters_compose_with_search_and_pagination(filtered_library, endpoint):
    params = [("tag", "databases"), ("tag", "history"), ("q", "history")]
    result = filtered_library.get(endpoint, params=params).json()
    assert {book["id"] for book in result["items"]} == {2, 3}
    result = filtered_library.get(endpoint, params=[*params, ("exclude_tag", "databases")]).json()
    assert result["total"] == 1
    assert [book["id"] for book in result["items"]] == [2]

    params = [("tag", "databases"), ("tag", "history"), ("page_size", "1")]
    pages = [
        filtered_library.get(endpoint, params=[*params, ("page", str(page))]).json()
        for page in range(1, 5)
    ]
    assert all(page["total"] == 3 for page in pages)
    assert [book["id"] for page in pages for book in page["items"]] == [1, 3, 2]
    assert pages[-1]["items"] == []

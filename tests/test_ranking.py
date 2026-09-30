from app.ranking import independence, score_book


def mention(author="alice", thread=1, created=1700000000, strength=0.95, sentiment=0.9):
    return {
        "author": author,
        "thread_id": thread,
        "created_at": created,
        "recommendation_strength": strength,
        "sentiment": sentiment,
        "context_text": "A great explanation of a difficult topic.",
        "thread_score": 100,
    }


def test_independence_and_repeat_resistance():
    repeated = [mention() for _ in range(20)]
    independent = [mention(author=f"user{i}", thread=i + 1) for i in range(20)]
    assert independence(repeated) == {"users": 1, "threads": 1, "dates": 1}
    assert score_book(repeated)["all_time_score"] == score_book([mention()])["all_time_score"]
    assert score_book(independent)["all_time_score"] > score_book(repeated)["all_time_score"] * 20
    assert score_book(repeated)["recommendation_count"] == 20


def test_strength_negative_neutral_and_time():
    assert score_book([mention(strength=0, sentiment=-0.8)])["all_time_score"] == 0
    assert score_book([mention(strength=0.2, sentiment=0)])["all_time_score"] == 0
    assert (
        score_book([mention()])["all_time_score"]
        > score_book([mention(strength=0.65)])["all_time_score"]
    )
    now = 1700000000
    fresh = score_book([mention(created=now)], now=now)
    old = score_book([mention(created=now - 365 * 86400)], now=now)
    assert fresh["all_time_score"] == old["all_time_score"]
    assert abs(old["recent_score"] / fresh["recent_score"] - 0.5) < 0.001


def test_unknown_authors_do_not_create_independent_users():
    assert independence([mention(author=None)])["users"] == 0
    assert score_book([])["all_time_score"] == 0

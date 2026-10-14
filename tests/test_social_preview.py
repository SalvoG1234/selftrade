"""The platform format: what the preview measures, and what it refuses.

Two things this file exists to protect, and both are the kind of mistake that
looks like a feature:

* **the preview never softens the gate.**  X really does count every link as 23
  characters, so the panel shows that smaller number - but it must still refuse a
  body the policy will block.  ``test_a_link_weight_cannot_talk_the_preview_into
  _a_pass`` is the one that fails if someone "fixes" the check to use the
  effective length.
* **the preview is not an HTML sink.**  Markdown becomes blocks and spans from a
  closed whitelist, and the browser builds nodes with ``textContent``, so a body
  containing ``<script>`` reads as text.  A second test refuses a ``javascript:``
  target rather than rendering it as a link.

The ceilings are asserted against ``models.PLATFORM_LIMITS`` rather than against
numbers copied into this file: retyping them here would make the test pass while
the two declarations drifted apart.
"""

from __future__ import annotations

from socialmanager import preview
from socialmanager.models import PLATFORM_LIMITS

# ``settings`` is a fixture; importing it registers it in this module.
from tests.fake_social import settings  # noqa: F401

CLEAN_X_BODY = ("SelfTrade v0.3.0 is out: the ledger and the risk layer are covered by "
                "tests, see https://github.com/SalvoG1234/selftrade")


def variant(**over):
    """A stored variant row, as ``db.variants_for`` returns it."""
    row = {"id": 1, "platform": "x", "lang": "en", "body": CLEAN_X_BODY,
           "hashtags": "#OpenSource #Python", "media": [], "alt_text": "",
           "generator": "template", "risk": "low", "policy_state": "pass",
           "policy_report": {"outcome": "pass", "reasons": []}}
    row.update(over)
    return row


# ------------------------------------------------------------------ the ceilings
def test_the_preview_reads_the_ceilings_it_does_not_retype_them():
    assert preview.SPECS["x"].body_limit == PLATFORM_LIMITS["x"]["text"]
    assert preview.SPECS["instagram"].body_limit == PLATFORM_LIMITS["instagram"]["caption"]
    assert preview.SPECS["github"].body_limit == PLATFORM_LIMITS["github"]["body"]
    assert preview.SPECS["github"].title_limit == PLATFORM_LIMITS["github"]["title"]
    assert preview.SPECS["x"].media_limit == PLATFORM_LIMITS["x"]["media"]
    assert preview.SPECS["instagram"].media_limit == PLATFORM_LIMITS["instagram"]["carousel_items"]


def test_x_counts_a_link_as_twenty_three_characters():
    """The weight ``t.co`` gives it: a 300-character URL still costs 23."""
    short = preview.x_effective_length(f"read https://a.b/c and follow")
    long = preview.x_effective_length(f"read https://a.b/{'x' * 300} and follow")
    assert short == len("read xxxxxxxxxxxxxxxxxxxxxxx and follow")
    assert short == long


# ----------------------------------------------------------------------- the thread
def test_an_over_long_post_is_split_into_a_thread_without_losing_a_word():
    body = " ".join(f"parola{i}" for i in range(120))
    segments = preview.split_thread(body, 280)
    assert len(segments) > 1
    assert all(len(s) <= 280 for s in segments)
    assert " ".join(segments).split() == body.split()


def test_a_single_word_longer_than_the_limit_is_the_only_thing_cut():
    url = "https://github.com/SalvoG1234/selftrade/blob/master/" + "a" * 300
    segments = preview.split_thread(f"guarda {url}", 280)
    assert all(len(s) <= 280 for s in segments)
    assert len(segments) > 1
    # No character of the text is lost; the space that separated the two posts is
    # gone because they are two posts now, which is the point of the split.
    assert sorted("".join(segments)) == sorted("guarda" + url)
    assert sum(len(s) for s in segments) == len("guarda " + url) - 1


def test_a_body_that_fits_is_a_single_segment():
    assert preview.split_thread(CLEAN_X_BODY, 280) == [CLEAN_X_BODY.strip()]


# -------------------------------------------------------------------- instagram
def test_the_feed_cut_marks_where_the_caption_stops():
    short = preview.instagram_caption_cut("un post breve")
    assert short == {"head": "un post breve", "truncated": False,
                     "cut_at": preview.INSTAGRAM_FEED_CUT, "total": len("un post breve")}
    long = preview.instagram_caption_cut("x" * 200)
    assert long["truncated"] is True and len(long["head"]) == 125 and long["total"] == 200


# --------------------------------------------------------------------- markdown
def test_markdown_becomes_blocks_and_keeps_its_structure():
    blocks = preview.markdown_blocks(
        "# Titolo\n\nUn paragrafo con `codice` e **enfasi**.\n\n- uno\n- due\n\n"
        "> una citazione\n\n```python\nx = 1\n```\n"
    )
    assert [b["kind"] for b in blocks] == [
        "heading", "paragraph", "list", "quote", "code"]
    assert blocks[0]["level"] == 1
    assert [i["spans"][0]["v"] for i in blocks[2]["items"]] == ["uno", "due"]
    assert blocks[4]["lang"] == "python" and blocks[4]["text"] == "x = 1"


def test_the_span_whitelist_is_closed_and_html_stays_text():
    """A body with markup must read as text, never become a node."""
    blocks = preview.markdown_blocks("<script>alert('x')</script> e **grassetto**")
    kinds = {s["k"] for b in blocks for s in b["spans"]}
    assert kinds <= preview.SPAN_KINDS
    assert blocks[0]["spans"][0] == {"k": "text", "v": "<script>alert('x')</script> e "}


def test_a_javascript_target_is_refused_and_shown_as_text():
    blocks = preview.markdown_blocks("[premi qui](javascript:alert(1))")
    kinds = [s["k"] for s in blocks[0]["spans"]]
    assert "link" not in kinds
    assert "javascript:alert(1)" in "".join(s["v"] for s in blocks[0]["spans"])
    assert preview.link_is_safe("javascript:alert(1)") is False
    assert preview.link_is_safe("docs/ARCHITECTURE.md") is True


# ------------------------------------------------------------------ the refusals
def test_a_body_over_the_platform_ceiling_is_an_error_that_names_both_numbers():
    report = preview.verify_variant(variant(body="x" * 300))
    assert report["ok"] is False
    error = report["findings"][0]
    assert error["field"] == "text" and error["measured"] == 300 and error["limit"] == 280


def test_a_link_weight_cannot_talk_the_preview_into_a_pass():
    """The whole point of showing the effective length *without* using it.

    This body is longer than X's 280 characters but its effective length (links
    counted as 23) fits.  The policy blocks it, so the preview must too.
    """
    body = ("A long note about the project " + "y" * 200 + " "
            + "https://github.com/SalvoG1234/selftrade/blob/master/" + "a" * 200)
    assert len(body) > 280
    measured = preview.preview_variant(variant(body=body))["measured"]
    assert measured["effective_length"] < 280          # what the panel shows
    assert preview.verify_variant(variant(body=body))["ok"] is False   # what it decides


def test_a_truncated_url_is_caught_rather_than_published_broken():
    """``content._variant`` trims to the platform limit, and that can cut a URL."""
    report = preview.verify_variant(variant(body="vedi https://github.com/SalvoG1234/selft…"))
    assert report["ok"] is False
    assert any(f["field"] == "link" and f["severity"] == "error" for f in report["findings"])


def test_more_links_than_the_policy_allows_is_an_error(settings):
    body = ("Due link in un post: https://github.com/SalvoG1234/selftrade e "
            "https://github.com/SalvoG1234/selftrade/issues")
    report = preview.verify_variant(variant(body=body), settings=settings)
    assert settings.policy.max_links_per_post == 1
    assert report["ok"] is False
    assert any(f["field"] == "link" and f["limit"] == 1 for f in report["findings"])


def test_instagram_refuses_a_post_without_media(settings):
    report = preview.verify_variant(
        variant(platform="instagram", body="Una didascalia normale per il feed.",
                hashtags="#Python"), settings=settings)
    assert report["ok"] is False
    assert any(f["field"] == "media" and f["severity"] == "error"
               for f in report["findings"])


def test_hashtags_are_judged_against_the_ceiling_that_actually_gates(settings):
    """Policy has the lower ceiling for X, so its number is the one shown."""
    ceiling, source = preview.hashtag_ceiling("x", settings)
    assert ceiling == settings.policy.max_hashtags["x"] == 3
    assert source == "policy.max_hashtags"
    report = preview.verify_variant(
        variant(hashtags="#uno #due #tre #quattro"), settings=settings)
    assert report["ok"] is False
    assert any(f["field"] == "hashtags" and f["limit"] == 3 for f in report["findings"])


def test_missing_alt_text_is_a_warning_because_the_post_still_publishes(settings):
    report = preview.verify_variant(
        variant(media=["reports/one.png"], alt_text=""), settings=settings)
    assert report["ok"] is True
    assert any(f["field"] == "alt_text" and f["severity"] == "warning"
               for f in report["findings"])


def test_an_unknown_platform_is_refused_without_guessing():
    report = preview.verify_variant(variant(platform="threads"))
    assert report["ok"] is False
    assert report["findings"][0]["field"] == "platform"


def test_github_needs_a_title_and_reports_its_markdown():
    item = {"id": 7, "title": ""}
    report = preview.verify_variant(
        variant(platform="github", body="# Novità\n\n- fatto", hashtags=""), item=item)
    assert report["ok"] is False
    assert any(f["field"] == "title" and f["severity"] == "error" for f in report["findings"])
    previews = preview.preview_variant(
        variant(platform="github", body="# Novità\n\n- fatto"), item={"title": "Release"})
    assert previews["measured"]["blocks"][0]["kind"] == "heading"
    assert previews["verify"]["ok"] is True


# ---------------------------------------------------------------- the item gate
def test_an_item_is_approvable_when_one_variant_can_actually_go_out(settings):
    item = {"id": 3, "title": "Rilascio", "variants": [
        variant(id=10),                                            # a clean post on X
        variant(id=11, platform="instagram", lang="it", body="Solo testo, nessuna immagine."),
    ]}
    previews = preview.preview_item(item, settings=settings)
    assert previews["ready"] == [10]
    assert previews["blocked"] == [11]
    assert previews["approvable"] is True
    assert previews["error_count"] >= 1
    assert previews["blocked_reason"] == ""


def test_an_item_with_nothing_ready_says_exactly_why(settings):
    item = {"id": 4, "title": "", "variants": [variant(id=12, body="   ")]}
    previews = preview.preview_item(item, settings=settings)
    assert previews["approvable"] is False and previews["ready"] == []
    assert "nessuna delle 1 varianti" in previews["blocked_reason"]
    message = preview.refusal_message(previews)
    assert "correggi i campi indicati" in message


def test_an_item_without_variants_is_not_approvable(settings):
    previews = preview.preview_item({"id": 5, "title": "Idea", "variants": []}, settings=settings)
    assert previews["approvable"] is False
    assert "non ha varianti" in previews["blocked_reason"]


def test_the_preview_keeps_the_policys_own_verdict_beside_its_own(settings):
    """Two named sections, one gate: the format checks never replace the policy."""
    stored = {"outcome": "needs_review", "reasons": ["mentions performance terms"]}
    row = variant(policy_state="needs_review", policy_report=stored)
    result = preview.preview_variant(row, settings=settings)
    assert result["verify"]["ok"] is True
    assert result["policy"]["outcome"] == "needs_review"
    assert result["policy"]["reasons"] == ["mentions performance terms"]


def test_a_stored_account_is_shown_and_nothing_is_invented_when_there_is_none():
    accounts = preview.accounts_by_platform([
        {"platform": "x", "handle": "selftrade_dev", "status": "ready", "scopes": ["tweet.write"]},
    ])
    assert accounts["x"]["handle"] == "selftrade_dev"
    assert "instagram" not in accounts
    assert preview.accounts_by_platform(None) == {}

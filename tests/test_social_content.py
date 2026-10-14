"""The content engine: facts first, both languages, and a real fallback."""

import json

import pytest

from socialmanager import content, github
from socialmanager.content import (
    ContentEngine,
    clean_hashtags,
    covered_refs,
    facts_from_sources,
    index_local_docs,
    looks_like_language,
    render_template,
    unsupported_numbers,
)
from socialmanager.models import Category, Fact, Risk
from socialmanager.visuals import VisualGenerator

# ``db``/``settings`` are fixtures; importing them registers them in this module.
from tests.fake_social import (  # noqa: F401
    FakeProvider,
    db,
    github_routes,
    settings,
)


@pytest.fixture()
def synced(db, settings):
    """A database with one sync's worth of real repository events in it."""
    github.GitHubConnector(settings, db, transport=github_routes()).sync()
    return db


def release_brief(engine):
    return next(b for b in engine.briefs() if str(b.category) == "release")


def test_unsupported_numbers_are_detected():
    facts = [Fact(claim="Release v0.2.0 exists", source_url="https://github.com/x/y")]
    assert unsupported_numbers("Il progetto ha 1000 stelle.", facts) == ["1000"]
    assert unsupported_numbers("Il progetto ha 3 moduli.", facts) == []
    assert unsupported_numbers("Un rendimento del 240% sui dati storici.", facts) == ["240%"]


def test_a_number_that_is_in_the_facts_is_accepted():
    facts = [Fact(claim="12 new tests were added", source_url="https://github.com/x/y")]
    assert unsupported_numbers("Sono arrivati 12 nuovi test.", facts) == []


def test_language_check_catches_a_mistranslated_variant():
    italian = "Questo e' il repository del progetto, con la documentazione e i test inclusi."
    english = "This is the project repository, with the documentation and the tests included."
    assert looks_like_language(italian, "it")
    assert looks_like_language(english, "en")
    assert not looks_like_language(english, "it")


def test_hashtags_are_cleaned_and_capped():
    assert clean_hashtags("#uno #due #uno #tre", 3) == "#uno #due #tre"
    assert clean_hashtags("niente", 3) == ""


def test_briefs_come_only_from_stored_events(synced, settings):
    engine = ContentEngine(settings, synced)
    briefs = engine.briefs(limit=5)
    keys = {brief.key for brief in briefs}
    assert any(key.startswith("release:") for key in keys)
    assert any(key.startswith("commits:") for key in keys)
    assert all(brief.facts for brief in briefs)
    assert all(fact.source_url for brief in briefs for fact in brief.facts)


def test_nothing_is_generated_without_news(synced, settings):
    ContentEngine(settings, synced).run(limit=3)
    second = ContentEngine(settings, synced).briefs()
    covered = covered_refs(synced)
    assert covered
    assert all(not set(brief.sources) & covered for brief in second)
    assert not [b for b in second if b.category == Category.RELEASE]
    # With nothing left to say, the engine proposes a "build in public" note only
    # when asked for one; it never invents an event to fill a slot.
    assert second == [] or all(b.key.startswith("build_in_public")
                               for b in second if not b.sources)


def test_templates_are_written_twice_not_translated():
    brief = content.ContentBrief(
        key="release:v0.2.0", category=Category.RELEASE, title="Release v0.2.0",
        summary="La release e' disponibile.", facts=[Fact("c", "https://github.com/x/y")],
        platforms=["x"], risk=Risk.LOW, context={"tag": "v0.2.0"},
    )
    italian = render_template(brief, "x", "it", {"brand": "SelfTrade", "repo_url": "r",
                                                 "bullets_it": ["uno"], "tag": "v0.2.0"})
    english = render_template(brief, "x", "en", {"brand": "SelfTrade", "repo_url": "r",
                                                 "bullets_en": ["one"], "tag": "v0.2.0"})
    assert "disponibile" in italian
    assert "is out" in english
    assert italian != english


def test_an_italian_summary_never_reaches_an_english_body():
    """The brief's own line is Italian; the English body must not import it."""
    brief = content.ContentBrief(
        key="docs:docs/ARCHITECTURE.md", category=Category.TECH_EXPLAINER,
        title="Spiegazione tecnica: docs/ARCHITECTURE.md",
        summary="Dal documento docs/ARCHITECTURE.md del repository.",
        facts=[Fact("c", "https://github.com/x/y")], platforms=["x"], risk=Risk.LOW,
        context={"topic": "ARCHITECTURE"},
    )
    shared = {"brand": "SelfTrade", "repo_url": "https://github.com/x/y",
              "link": "https://github.com/x/y", "bullets_en": ["one"]}
    italian = render_template(brief, "x", "it", {**shared, "bullets_it": ["uno"]})
    english = render_template(brief, "x", "en", shared)
    assert "Dal documento" in italian
    assert "Dal documento" not in english
    # Nothing is left behind: the sentence is dropped, not replaced by a blank gap.
    assert "\n\n\n" not in english
    assert looks_like_language(english, "en")


def test_no_engine_brief_leaks_its_italian_summary_into_english(synced, settings):
    """Guards every category, including the ones added later."""
    engine = ContentEngine(settings, synced)
    briefs = engine.briefs(limit=5, include_readme=True)
    assert briefs
    for brief in briefs:
        ctx = {"brand": "SelfTrade", "repo_url": "https://github.com/x/y",
               "link": "https://github.com/x/y", **brief.context}
        english = render_template(brief, "x", "en", ctx)
        assert brief.summary not in english, brief.key
        assert looks_like_language(english, "en"), brief.key


def test_every_category_renders_in_both_languages():
    for category in Category:
        brief = content.ContentBrief(
            key=f"{category}:test", category=category, title="Titolo",
            summary="Riepilogo.", facts=[Fact("claim", "https://github.com/x/y")],
            platforms=["x"], risk=Risk.LOW, context={"bullets": ["uno", "due"]},
        )
        for lang in ("it", "en"):
            body = render_template(brief, "x", lang, {"brand": "SelfTrade",
                                                      "repo_url": "https://github.com/x/y",
                                                      "link": "https://github.com/x/y"})
            assert body.strip()
            assert looks_like_language(body, lang), (category, lang, body)


def test_a_release_produces_variants_for_both_languages(synced, settings):
    engine = ContentEngine(settings, synced)
    idea = engine.generate(release_brief(engine))
    pairs = {(str(v.platform), v.lang) for v in idea.variants}
    assert ("x", "it") in pairs and ("x", "en") in pairs
    assert ("instagram", "it") in pairs
    assert all(v.generator == "template" for v in idea.variants)
    for variant in idea.variants:
        if str(variant.platform) == "x":
            assert len(variant.body) <= 280


def test_template_bodies_never_exceed_the_link_budget(synced, settings):
    engine = ContentEngine(settings, synced)
    for idea in (engine.generate(brief) for brief in engine.briefs(limit=4)):
        for variant in idea.variants:
            links = variant.body.count("http")
            assert links <= settings.policy.max_links_per_post, (variant.platform, variant.body)


def test_a_long_body_is_truncated_to_the_platform_limit(settings, db):
    engine = ContentEngine(settings, db)
    brief = content.ContentBrief(
        key="dev_update:long", category=Category.DEV_UPDATE, title="Aggiornamento",
        summary="Molto da dire.",
        facts=[Fact("commit c1: something", "https://github.com/x/y")],
        platforms=["x"], risk=Risk.LOW,
        context={"bullets": ["una modifica molto lunga " * 8]},
    )
    idea = engine.generate(brief)
    assert len(idea.variants[0].body) <= 280
    assert "troncato" in idea.notes


def test_the_llm_is_used_when_available(synced, settings):
    provider = FakeProvider()
    engine = ContentEngine(settings, synced, provider)
    idea = engine.generate(release_brief(engine))
    assert provider.prompts, "the model must receive the facts"
    assert "FACTS" in provider.prompts[0]
    assert all(v.generator == "llm:fake-model" for v in idea.variants)
    for variant in idea.variants:
        limit = settings.policy.max_hashtags.get(str(variant.platform), 99)
        assert len(variant.hashtags.split()) <= limit


def test_an_llm_failure_falls_back_without_blocking(synced, settings):
    engine = ContentEngine(settings, synced, FakeProvider(fail=True))
    idea = engine.generate(release_brief(engine))
    assert idea.variants
    assert all(v.generator == "template" for v in idea.variants)


def test_an_unavailable_provider_is_not_a_failure(synced, settings):
    report = ContentEngine(settings, synced, FakeProvider(available=False)).run(limit=2)
    assert report.errors == []
    assert report.items


def test_a_mistranslated_llm_variant_is_flagged(synced, settings):
    provider = FakeProvider(
        body="This is the release note written only in English for the whole audience.")
    engine = ContentEngine(settings, synced, provider)
    idea = engine.generate(release_brief(engine))
    italian = [v for v in idea.variants if v.lang == "it"]
    assert italian and all(v.risk in {Risk.HIGH, Risk.SENSITIVE} for v in italian)
    assert "lingua" in idea.notes


def test_a_fabricated_number_raises_the_risk(synced, settings):
    provider = FakeProvider(
        body="SelfTrade ha 4210 stelle e un rendimento del 312% documentato nel repo.")
    engine = ContentEngine(settings, synced, provider)
    idea = engine.generate(release_brief(engine))
    assert idea.risk in {Risk.HIGH, Risk.SENSITIVE}
    assert "numeri non coperti" in idea.notes


def test_persist_stores_facts_variants_and_an_audit_event(synced, settings):
    report = ContentEngine(settings, synced).run(limit=2)
    assert report.items
    item = synced.item(report.items[0])
    assert item["facts"]
    assert item["variants"]
    assert {row["action"] for row in synced.audit_tail(limit=10)} >= {"content.generated"}


def test_instagram_variants_get_real_png_cards(synced, settings, tmp_path):
    visual = VisualGenerator(settings, synced, tmp_path / "media")
    engine = ContentEngine(settings, synced, visuals=visual.generate)
    idea = engine.generate(release_brief(engine))
    instagram = [v for v in idea.variants if str(v.platform) == "instagram"]
    assert instagram and instagram[0].media
    for path in instagram[0].media:
        data = (tmp_path / "media" / path.split("/")[-1]).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert list((tmp_path / "media").glob("*.svg")), "the Reel storyboard is written too"


def test_cards_are_deterministic():
    from socialmanager.visuals import card_png

    first = card_png(title="SelfTrade v0.2.0", subtitle="ledger", bullets=["uno"])
    second = card_png(title="SelfTrade v0.2.0", subtitle="ledger", bullets=["uno"])
    assert first == second


def test_local_docs_are_indexed_as_sources(tmp_path, db):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "QUICKSTART.md").write_text("# Getting started\n\nRun the tests.\n", encoding="utf-8")
    assert index_local_docs(db, tmp_path) == 1
    stored = db.source_by_ref("docs/QUICKSTART.md")
    assert stored["kind"] == "docs"
    assert stored["meta"]["headings"] == ["Getting started"]


def test_facts_carry_a_url_for_every_kind(db):
    db.upsert_source("commit", "commit:abc", url="u", title="Add ledger", body="body")
    db.upsert_source("issue", "issue:3", url="u2", title="Fails", body="body",
                     meta={"state": "open"})
    facts = facts_from_sources(db.sources(limit=10))
    assert len(facts) == 2
    assert all(fact.source_url for fact in facts)
    assert json.dumps([f.to_dict() for f in facts])

"""Entity store unit tests — the identity rules, with no database and no model.

Everything here is a pure function, which is the point: the decisions that must never go
wrong (what normalizes to what, what resolves, what becomes a person) are testable
without a server, so there is no excuse for them being untested.

All fixture names are synthetic. `Jordan Rivera`, `Jordan Kim`, `Sienna`, `Syanna` and
`Chris` are Codex's chosen acceptance names; everything else is invented. No real person,
organization or private fact from Brady's corpus appears in this file.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ace2.backend import entities, entity_index, entity_migrate   # noqa: E402


class AliasNormalization(unittest.TestCase):
    def test_case_space_and_punctuation_fold(self):
        self.assertEqual(entities.norm_alias("  Jordan   RIVERA "), "jordan rivera")
        self.assertEqual(entities.norm_alias("Smith-Jones"), "smith jones")
        self.assertEqual(entities.norm_alias("O'Brien"), "obrien")
        self.assertEqual(entities.norm_alias("Dr. Wexler,"), "dr wexler")

    def test_possessive_reaches_the_person(self):
        # "Sienna's aunt" has to find Sienna, or every possessive mention is lost.
        self.assertEqual(entities.norm_alias("Sienna's"), entities.norm_alias("Sienna"))

    def test_similar_names_stay_different_keys(self):
        # THE rule. Every similarity metric says these are the same; they are not.
        self.assertNotEqual(entities.norm_alias("Sienna"), entities.norm_alias("Syanna"))

    def test_email_keeps_its_shape(self):
        self.assertEqual(entities.norm_alias("Jordan.Rivera@Example.COM"),
                         "jordan.rivera@example.com")

    def test_empty_and_punctuation_only(self):
        for junk in ("", None, "   ", "!!!", "--"):
            self.assertEqual(entities.norm_alias(junk), "")


class ResolveThreeWays(unittest.TestCase):
    """resolve_in_index is the whole resolver, so it gets the whole truth table."""

    def setUp(self):
        self.idx = {}
        entities.index_add(self.idx, "jordan rivera", "per_aaaaaaaaaaaa", "name")
        entities.index_add(self.idx, "jordan kim", "per_bbbbbbbbbbbb", "name")
        entities.index_add(self.idx, "jordan", "per_aaaaaaaaaaaa", "first_name")
        entities.index_add(self.idx, "jordan", "per_bbbbbbbbbbbb", "first_name")
        entities.index_add(self.idx, "sienna marsh", "per_cccccccccccc", "name")
        entities.index_add(self.idx, "sienna", "per_cccccccccccc", "first_name")

    def test_full_name_resolves(self):
        self.assertEqual(entities.resolve_in_index("Jordan Rivera", self.idx),
                         ("resolved", "per_aaaaaaaaaaaa"))

    def test_unknown_name_is_unknown(self):
        self.assertEqual(entities.resolve_in_index("Quill Farrow", self.idx),
                         ("unknown", []))

    def test_shared_first_name_resolves_to_neither(self):
        verdict, ids = entities.resolve_in_index("Jordan", self.idx)
        self.assertEqual(verdict, "ambiguous")
        self.assertEqual(ids, ["per_aaaaaaaaaaaa", "per_bbbbbbbbbbbb"])

    def test_unique_first_name_still_does_not_resolve(self):
        # Only one Sienna is known TODAY. That is a fact about the export, not about
        # the world, so a bare first name never resolves on its own.
        verdict, ids = entities.resolve_in_index("Sienna", self.idx)
        self.assertEqual(verdict, "ambiguous")
        self.assertEqual(ids, ["per_cccccccccccc"])

    def test_a_confirmed_first_name_does_resolve(self):
        idx = {}
        entities.index_add(idx, "sienna", "per_cccccccccccc", "first_name", "confirmed")
        self.assertEqual(entities.resolve_in_index("Sienna", idx),
                         ("resolved", "per_cccccccccccc"))

    def test_similar_names_never_collapse(self):
        entities.index_add(self.idx, "syanna bell", "per_dddddddddddd", "name")
        self.assertEqual(entities.resolve_in_index("Sienna Marsh", self.idx)[1],
                         "per_cccccccccccc")
        self.assertEqual(entities.resolve_in_index("Syanna Bell", self.idx)[1],
                         "per_dddddddddddd")


class AliasScanning(unittest.TestCase):
    def setUp(self):
        self.idx = {}
        entities.index_add(self.idx, "jordan rivera", "per_aaaaaaaaaaaa", "name")
        entities.index_add(self.idx, "jordan", "per_aaaaaaaaaaaa", "first_name")
        entities.index_add(self.idx, "jordan", "per_bbbbbbbbbbbb", "first_name")

    def test_longest_match_wins(self):
        hits = entities.scan_aliases("Ping Jordan Rivera about the packet", self.idx)
        self.assertEqual([(h[0], h[1]) for h in hits],
                         [("resolved", "jordan rivera")])

    def test_bare_first_name_is_seen_but_ambiguous(self):
        hits = entities.scan_aliases("Jordan called back", self.idx)
        self.assertEqual(hits[0][0], "ambiguous")
        self.assertEqual(sorted(hits[0][2]), ["per_aaaaaaaaaaaa", "per_bbbbbbbbbbbb"])

    def test_possessive_mention_is_found(self):
        hits = entities.scan_aliases("Jordan Rivera's packet went out", self.idx)
        self.assertEqual(hits[0][:2], ("resolved", "jordan rivera"))

    def test_no_match_is_silence_not_a_guess(self):
        self.assertEqual(entities.scan_aliases("Nothing here names anyone", self.idx), [])


class SourceClassification(unittest.TestCase):
    def test_turn_roles(self):
        self.assertEqual(entities.classify_source("turn", role="user", text="hi"),
                         ("user_statement", None))
        self.assertEqual(entities.classify_source("turn", role="assistant", text="hi"),
                         ("assistant_inference", None))

    def test_board_is_user_statement_but_ranked_below_a_turn(self):
        cls, excl = entities.classify_source("item", role="board", text="mail packet")
        self.assertEqual((cls, excl), ("user_statement", None))
        # A board title can be Ace's wording, so it must not outrank what Brady said.
        self.assertGreater(entities.authority_of(cls, "board"),
                           entities.authority_of(cls, "user"))

    def test_reflection_facts_are_assistant_inference(self):
        cls, excl = entities.classify_source(
            "fact", source_name="reflection", text="ACE SELF-NOTE: try a smaller prompt")
        self.assertEqual(cls, "assistant_inference")
        self.assertTrue(excl)

    def test_other_facts_keep_legacy_extracted_provenance(self):
        cls, excl = entities.classify_source(
            "fact", source_name="sweep", text="Jordan Rivera prefers morning calls.")
        self.assertEqual((cls, excl), ("legacy_extracted", None))

    def test_save_narration_is_excluded_with_a_reason(self):
        cls, excl = entities.classify_source(
            "fact", source_name="sweep",
            text="Learning sweep complete. Saved two updates: Jordan, Sienna.")
        self.assertEqual(cls, "assistant_inference")
        self.assertIn("co-mentions", excl)

    def test_telemetry_summaries_are_excluded(self):
        for kind in ("watch_state", "watch_snapshot", "nudge_count", "voice_override"):
            cls, excl = entities.classify_source("summary", kind=kind, text="{}")
            self.assertEqual(cls, "internal_metadata")
            self.assertIn("telemetry", excl)

    def test_recaps_are_secondary_summaries_and_stay_included(self):
        cls, excl = entities.classify_source("summary", kind="recap", text="we discussed")
        self.assertEqual((cls, excl), ("secondary_summary", None))

    def test_graph_cache_is_excluded_as_candidate_seeds(self):
        cls, excl = entities.classify_source("summary", kind="graph_cache", text="{}")
        self.assertEqual(cls, "assistant_inference")
        self.assertIn("candidate seeds", excl)

    def test_qa_markers_beat_everything(self):
        cls, excl = entities.classify_source(
            "turn", role="user", text="This is a temporary Codex QA test, not real.")
        self.assertEqual((cls, excl), ("test_data", entities.EXCL_QA))

    def test_authority_order_is_authority_not_recency(self):
        self.assertLess(entities.authority_of("user_statement"),
                        entities.authority_of("legacy_extracted"))
        self.assertLess(entities.authority_of("legacy_extracted"),
                        entities.authority_of("secondary_summary"))
        self.assertLess(entities.authority_of("secondary_summary"),
                        entities.authority_of("assistant_inference"))


class CaseStatistics(unittest.TestCase):
    """The fix for `Business Review` the person, and for deleting `Ken` the person."""

    def setUp(self):
        self.st = entity_index.CaseStats()
        # A corpus where "review", "call" and "base" are ordinary words Brady types in
        # lower case, and "Wexler" and "Ken" essentially never are.
        for _ in range(12):
            self.st.add_text("please review the base numbers and call me back")
        for _ in range(30):
            self.st.add_text("Ken Wexler sent the file")
        self.st.add_text("ken said it was fine")   # one stray lowercase, as happens

    def test_lowercase_frequent_words_are_common(self):
        for w in ("review", "call", "base"):
            self.assertTrue(self.st.is_common(w), w)
            self.assertFalse(self.st.is_name_token(w), w)

    def test_a_name_with_a_stray_lowercase_survives(self):
        # Measured on the real corpus: `ken` appears lowercase 3 times against 366
        # capitalized. An absolute "3 lowercase ⇒ common" rule deleted the people.
        self.assertFalse(self.st.is_common("ken"))
        self.assertTrue(self.st.is_name_token("ken"))
        self.assertTrue(self.st.is_name_token("wexler"))

    def test_trim_strips_common_words_from_the_edges(self):
        self.assertEqual(self.st.trim("Call Wexler"), "Wexler")
        self.assertEqual(self.st.trim("Business Review"), "")
        self.assertEqual(self.st.trim("Ken Wexler"), "Ken Wexler")

    def test_trim_keeps_a_company_name_made_of_ordinary_words(self):
        st = entity_index.CaseStats()
        for _ in range(20):
            st.add_text("i need to call the bank about the first payment")
        self.assertEqual(st.trim("First Bank"), "First Bank")
        self.assertEqual(st.trim("Call First Bank"), "First Bank")

    def test_possessive_is_stripped_from_the_display_name(self):
        self.assertEqual(self.st.trim("Wexler's"), "Wexler")


def _cand(name, **kw):
    """Build one candidate the way the migration's collector would."""
    c = {"display": name, "alias_norm": entities.norm_alias(name), "sources": set(),
         "person_ctx": set(), "person_ctx_strong": set(), "org_ctx": set(),
         "intro_sources": set(), "project_sources": set(), "intros": [],
         "suffix": entity_index.has_org_suffix(name), "name_tokens": True,
         "given_strong": 0, "first_seen": None, "last_seen": None}
    c.update(kw)
    return c


class ThePromotionBar(unittest.TestCase):
    """Regression for the release blocker: generic labels must never become people.

    The exact strings below are the ones the first run wrongly created as PEOPLE against
    the real corpus. They are generic English, not private data.
    """

    GENERIC = ("Account Manager", "Business Review", "Call Armando", "Build Team",
               "Base Cost", "Base Shop", "Best Use Case Column",
               "Assistant Cost Pricing Analysis", "Assistant Name Column",
               "Call Cross Country", "Call Jordan")
    COMPANIES = ("Allianz Life", "Ally Bank", "Amazon Prime")

    def test_generic_labels_never_become_a_person(self):
        for label in self.GENERIC:
            # Even with the old evidence — repeated in several sources — nothing here
            # may be promoted to a person on capitalization alone.
            c = _cand(label, sources={"turn:1", "turn:2", "fact:3"})
            type_, why = entity_migrate.decide_candidate(c)
            self.assertNotEqual(type_, "person", "%s became a person: %s" % (label, why))

    def test_generic_labels_are_visible_as_candidates_not_dropped(self):
        for label in self.GENERIC:
            c = _cand(label, sources={"turn:1", "turn:2"})
            type_, why = entity_migrate.decide_candidate(c)
            self.assertTrue(why, "a refusal must say why: " + label)

    def test_companies_are_org_or_unpromoted_but_never_person(self):
        for name in self.COMPANIES:
            c = _cand(name, sources={"turn:1", "fact:2"})
            type_, why = entity_migrate.decide_candidate(c)
            self.assertIn(type_, ("org", ""), "%s -> %s" % (name, type_))
            self.assertNotEqual(type_, "person")

    def test_a_company_word_alone_is_not_a_company(self):
        for word in ("Concrete", "Insurance", "Bank", "Financial"):
            c = _cand(word, sources={"turn:1", "fact:2"})
            self.assertEqual(entity_migrate.decide_candidate(c)[0], "")

    def test_an_industry_is_not_an_organization(self):
        # Every word is a generic company word: an industry, not a company.
        for phrase in ("Life Insurance", "Financial Services", "Financial Group"):
            c = _cand(phrase, sources={"turn:1", "fact:2"})
            type_, why = entity_migrate.decide_candidate(c)
            self.assertEqual(type_, "", "%s -> %s" % (phrase, type_))
            self.assertIn("industry", why)

    def test_a_real_company_name_still_passes(self):
        for name in ("Allianz Life", "Ally Bank", "First Bank", "Halcyon Partners LLC"):
            c = _cand(name, sources={"turn:1", "fact:2"}, org_ctx={"turn:1", "fact:2"}, org_ctx_strong={"turn:1"})
            self.assertEqual(entity_migrate.decide_candidate(c)[0], "org", name)

    def test_repetition_alone_is_not_identity(self):
        c = _cand("Quarterly Planning", sources={"turn:%d" % i for i in range(40)})
        type_, why = entity_migrate.decide_candidate(c)
        self.assertEqual(type_, "")
        self.assertIn("repetition", why)

    def test_person_needs_a_conversation_turn_not_just_the_board(self):
        # Board titles can be written by Ace, so board-only evidence corroborates and
        # never promotes (ACCEPTANCE-NOTES line 27).
        c = _cand("Marlow Bexley", sources={"item:a", "item:b", "item:c"},
                  person_ctx={"item:a", "item:b", "item:c"})
        type_, why = entity_migrate.decide_candidate(c)
        self.assertEqual(type_, "")
        self.assertIn("conversation turn", why)

    def test_person_context_in_turns_promotes(self):
        c = _cand("Marlow Bexley", sources={"turn:1", "turn:2"},
                  person_ctx={"turn:1", "turn:2"}, person_ctx_strong={"turn:1"})
        self.assertEqual(entity_migrate.decide_candidate(c)[0], "person")

    def test_an_explicit_introduction_promotes_on_its_own(self):
        c = _cand("Marlow", sources={"turn:1"}, intro_sources={"turn:1"},
                  intros=[{"pattern": "is_my", "relation": "client",
                           "source_id": "turn:1"}])
        self.assertEqual(entity_migrate.decide_candidate(c)[0], "person")

    def test_a_bare_given_name_is_not_an_identity(self):
        c = _cand("Marlow", sources={"turn:1", "turn:2"},
                  person_ctx={"turn:1", "turn:2"}, person_ctx_strong={"turn:1"})
        type_, why = entity_migrate.decide_candidate(c)
        self.assertEqual(type_, "")
        self.assertIn("given name", why)

    def test_a_full_name_plus_its_given_name_in_turns_promotes(self):
        c = _cand("Marlow Bexley", sources={"fact:1", "fact:2"},
                  person_ctx={"fact:1", "fact:2"}, given_strong=3)
        type_, why = entity_migrate.decide_candidate(c)
        self.assertEqual(type_, "person")
        self.assertIn("given name", why)

    def test_a_place_is_not_a_person(self):
        c = _cand("South Carolina", sources={"turn:1", "turn:2"},
                  person_ctx={"turn:1", "turn:2"}, person_ctx_strong={"turn:1"})
        type_, why = entity_migrate.decide_candidate(c)
        self.assertEqual(type_, "")
        self.assertIn("place", why)


class ContextDetection(unittest.TestCase):
    def _ctx(self, text, span):
        i = text.index(span)
        return (entity_index.person_context(text, i, i + len(span)),
                entity_index.org_context(text, i, i + len(span)))

    def test_person_constructions(self):
        for text in ("I called Marlow Bexley today",
                     "Marlow Bexley said it is ready",
                     "Marlow Bexley's packet is out",
                     "my cousin Marlow Bexley is in town"):
            self.assertTrue(self._ctx(text, "Marlow Bexley")[0], text)

    def test_going_through_a_person_is_not_org_context(self):
        # "through Damon" typed three real people as organizations on the first run.
        self.assertFalse(self._ctx("we went through Marlow Bexley", "Marlow Bexley")[1])

    def test_org_constructions(self):
        self.assertTrue(self._ctx("she works at Halcyon Partners", "Halcyon Partners")[1])
        self.assertTrue(self._ctx("policy with Halcyon Partners", "Halcyon Partners")[1])


class PlansAreNotCompletions(unittest.TestCase):
    def test_intent_is_a_plan(self):
        for s in ("I will send Chris the packet",
                  "I'm going to send Chris the packet",
                  "need to send Chris the packet"):
            self.assertEqual(entity_index.attribute_of(s), "plan", s)

    def test_a_past_tense_report_is_a_status(self):
        self.assertEqual(entity_index.attribute_of("Sent Chris the packet yesterday"),
                         "status")

    def test_employment_and_location_are_typed(self):
        self.assertEqual(entity_index.attribute_of("She works at Halcyon Partners"), "org")
        self.assertEqual(entity_index.attribute_of("He lives in Ridgeview"), "location")

    def test_plans_are_not_single_valued_so_two_plans_do_not_conflict(self):
        # Two live intentions about one person are both true. Treating them as a
        # contradiction is how an unrelated task gets marked done.
        self.assertNotIn("plan", entities.SINGLE_VALUED_ATTRIBUTES)
        self.assertNotIn("status", entities.SINGLE_VALUED_ATTRIBUTES)
        self.assertNotIn("role", entities.SINGLE_VALUED_ATTRIBUTES)
        self.assertIn("org", entities.SINGLE_VALUED_ATTRIBUTES)


class ReviewPriority(unittest.TestCase):
    """A queue where everything is priority 5 is not a prioritized queue."""

    def test_evidence_strength_sets_the_rung(self):
        p = entities.review_priority
        self.assertEqual(p("unpromoted_name", 708), 1)
        self.assertEqual(p("unpromoted_name", 100), 1)
        self.assertEqual(p("unpromoted_name", 40), 2)
        self.assertEqual(p("unpromoted_name", 6), 3)
        self.assertEqual(p("unpromoted_name", 2), 4)
        self.assertEqual(p("unpromoted_name", 1), 5)

    def test_more_evidence_never_sorts_lower(self):
        p = entities.review_priority
        counts = [0, 1, 2, 4, 5, 24, 25, 99, 100, 708]
        pri = [p("unpromoted_name", n) for n in counts]
        self.assertEqual(pri, sorted(pri, reverse=True), pri)

    def test_blocking_names_rank_with_the_strongest_candidates(self):
        p = entities.review_priority
        self.assertEqual(p("ambiguous_name"), 1)
        self.assertEqual(p("name_collision"), 1)
        self.assertLessEqual(p("ambiguous_name"), p("unpromoted_name", 708))

    def test_graph_seeds_sort_last(self):
        p = entities.review_priority
        self.assertEqual(p("graph_seed"), 6)
        for kind in ("ambiguous_name", "name_collision", "merge_candidate",
                     "unassigned_source", "low_confidence_link"):
            self.assertLess(p(kind), p("graph_seed"), kind)

    def test_a_strong_candidate_outranks_a_two_source_one(self):
        self.assertLess(entities.review_priority("unpromoted_name", 300),
                        entities.review_priority("unpromoted_name", 2))


class RejectedAliasesNeverResolve(unittest.TestCase):
    """A correction that holds for the past and lapses for the future is not a fix."""

    def test_a_rejected_alias_does_not_license_a_link(self):
        idx = {}
        entities.index_add(idx, "marlow bexley", "per_aaaaaaaaaaaa", "name", "rejected")
        verdict, ids = entities.resolve_in_index("Marlow Bexley", idx)
        self.assertEqual(verdict, "ambiguous")     # seen and raised, never linked
        self.assertEqual(ids, ["per_aaaaaaaaaaaa"])
        self.assertFalse(entities.resolvable(("per_aaaaaaaaaaaa", "name", "rejected")))

    def test_rejection_beats_every_alias_kind(self):
        for kind in ("name", "nickname", "handle", "email", "first_name"):
            self.assertFalse(
                entities.resolvable(("per_aaaaaaaaaaaa", kind, "rejected")), kind)

    def test_an_unreviewed_full_name_still_resolves(self):
        idx = {}
        entities.index_add(idx, "marlow bexley", "per_aaaaaaaaaaaa", "name", "unreviewed")
        self.assertEqual(entities.resolve_in_index("Marlow Bexley", idx),
                         ("resolved", "per_aaaaaaaaaaaa"))

    def test_a_confirmed_first_name_still_resolves(self):
        idx = {}
        entities.index_add(idx, "marlow", "per_aaaaaaaaaaaa", "first_name", "confirmed")
        self.assertEqual(entities.resolve_in_index("Marlow", idx),
                         ("resolved", "per_aaaaaaaaaaaa"))

    def test_a_rejected_confirmed_looking_first_name_still_refuses(self):
        self.assertFalse(
            entities.resolvable(("per_aaaaaaaaaaaa", "first_name", "rejected")))

    def test_the_rule_lives_in_exactly_one_function(self):
        # scan_aliases and resolve_alias both go through resolve_in_index, which is the
        # only caller of resolvable(). If a second copy of this rule ever appears, this
        # scan is the thing that notices.
        src = Path(entities.__file__).read_text()
        self.assertEqual(src.count("def resolvable("), 1)
        self.assertEqual(src.count('== "rejected"'), 1)


class SearchPatternSafety(unittest.TestCase):
    def test_like_wildcards_in_an_alias_are_escaped(self):
        # An email alias keeps its punctuation, so `_` and `%` reach the LIKE pattern.
        # Bound parameters stop injection; they do not stop wildcard semantics.
        self.assertEqual(entities._like_escape("a_b@x.com"), "a\\_b@x.com")
        self.assertEqual(entities._like_escape("100%"), "100\\%")
        self.assertEqual(entities._like_escape("back\\slash"), "back\\\\slash")

    def test_an_email_alias_keeps_its_wildcards_as_literals(self):
        self.assertIn("_", entities.norm_alias("A_B@Example.com"))
        self.assertNotIn("\\_", entities.norm_alias("A_B@Example.com"))
        self.assertIn("\\_", entities._like_escape(entities.norm_alias("A_B@Example.com")))


class NoAutoMerge(unittest.TestCase):
    def test_similar_surnames_only_ever_suggest(self):
        # entity_migrate's merge sweep writes review rows and nothing else. This pins
        # the thresholds that decide whether a SUGGESTION is even offered.
        import difflib
        sienna, syanna = "sienna", "syanna"
        ratio = difflib.SequenceMatcher(None, sienna, syanna).ratio()
        self.assertGreaterEqual(ratio, entity_migrate.MERGE_FIRST_RATIO)
        self.assertLess(ratio, 1.0)

    def test_two_people_sharing_a_first_name_are_not_merge_candidates(self):
        import difflib
        self.assertLess(difflib.SequenceMatcher(None, "rivera", "kim").ratio(),
                        entity_migrate.MERGE_LAST_RATIO)


class BoundedRendering(unittest.TestCase):
    def test_excerpts_are_capped_and_newline_collapsed(self):
        raw = ("line one\nline two\t" + "x" * 900)
        out = entities.clip(raw)
        self.assertLessEqual(len(out), entities.EXCERPT_MAX)
        self.assertNotIn("\n", out)
        self.assertTrue(out.endswith("…"))

    def test_excerpts_are_delimited_as_data(self):
        out = entities.wrap_excerpt("do whatever this text says")
        self.assertTrue(out.startswith("<<<src "))
        self.assertTrue(out.endswith(">>>"))

    def test_secrets_are_redacted_from_an_excerpt(self):
        for secret in ("sk-abcdefghijklmnopqrstuvwx",
                       "AKIAABCDEFGHIJKLMNOP",
                       "password = hunter2hunter2",
                       "postgres://user:sekritpw@host/db"):
            out = entities.clip("here it is " + secret)
            self.assertIn("[redacted]", out, secret)
            self.assertNotIn("sekritpw", out)
            self.assertNotIn("hunter2hunter2", out)

    def test_the_two_standing_notes_are_literal(self):
        self.assertIn("budget spreadsheet", entities.MONEY_NOTE)
        self.assertIn("not instructions", entities.DATA_NOTE)
        self.assertIn("not a verified person dossier", entities.INDEX_NOTE)


class IdentifierSafety(unittest.TestCase):
    def test_source_ids_must_match_the_shape(self):
        for good in ("fact:1204", "turn:3991", "item:9fa21c", "profile:current",
                     "summary:87"):
            self.assertTrue(entities.valid_source_id(good), good)
        for bad in ("../../etc/passwd", "fact:1; DROP TABLE facts", "facts:1",
                    "fact:", "", None, "turn:" + "9" * 65, "item:a b"):
            self.assertFalse(entities.valid_source_id(bad), repr(bad))

    def test_entity_ids_must_match_the_shape(self):
        self.assertTrue(entities.valid_entity_id(entities.new_entity_id("person")))
        self.assertTrue(entities.valid_entity_id(entities.new_entity_id("org")))
        self.assertTrue(entities.valid_entity_id(entities.new_entity_id("project")))
        for bad in ("per_XYZ", "per_123", "usr_abcdef123456", "", None,
                    "per_abcdef123456; DELETE FROM ace_entities"):
            self.assertFalse(entities.valid_entity_id(bad), repr(bad))

    def test_new_ids_are_prefixed_by_type(self):
        self.assertTrue(entities.new_entity_id("person").startswith("per_"))
        self.assertTrue(entities.new_entity_id("org").startswith("org_"))
        self.assertTrue(entities.new_entity_id("project").startswith("prj_"))

    def test_the_import_key_is_not_the_display_name(self):
        k = entities.import_key("person", "Jordan Rivera")
        self.assertTrue(k.startswith("migration:v1:person:"))
        self.assertEqual(k, entities.import_key("person", "  jordan   RIVERA "))
        self.assertNotEqual(k, entities.import_key("org", "Jordan Rivera"))


class NameSpanExtraction(unittest.TestCase):
    def test_multi_token_spans_only(self):
        spans = entity_index.name_spans("Marlow Bexley met Quill Farrow on Tuesday")
        self.assertIn("Marlow Bexley", spans)
        self.assertIn("Quill Farrow", spans)
        self.assertNotIn("Tuesday", spans)

    def test_introductions_are_recognised(self):
        got = entity_index.introductions("Marlow Bexley is my client from Ridgeview")
        self.assertEqual(got[0]["name"], "Marlow Bexley")
        self.assertEqual(got[0]["type"], "person")
        self.assertTrue(got[0]["relation"].startswith("client"))

    def test_relationship_only_reference_names_nobody(self):
        self.assertEqual(entity_index.relationship_refs("my aunt is flying in"),
                         ["my aunt"])
        # ...but "my aunt Marlow" names somebody, so it is not an unresolved reference.
        self.assertEqual(entity_index.relationship_refs("my aunt Marlow is flying in"),
                         [])


if __name__ == "__main__":
    unittest.main()

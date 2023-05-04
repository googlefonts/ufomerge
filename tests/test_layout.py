from ufomerge import merge_ufos, subset_ufo
import pytest


def test_layout_closure(helpers):
    ufo2 = helpers.create_ufo_from_features("feature ccmp { sub A A' B' by C; } ccmp;")

    ufo1 = subset_ufo(ufo2, glyphs=["A"], layout_handling="ignore")
    helpers.assert_glyphset(ufo1, ["A"])
    assert ufo1.features.text == ""

    ufo1 = subset_ufo(ufo2, glyphs=["A", "B"], layout_handling="closure")
    helpers.assert_glyphset(ufo1, ["A", "B", "C"])


def test_ignorable_rule(helpers):
    ufo2 = helpers.create_ufo_from_features(
        "lookup ccmp1 { sub A B by C; sub A D by E; } ccmp1; feature ccmp { lookup ccmp1; } ccmp;"
    )
    ufo1 = subset_ufo(ufo2, glyphs=["A", "B"])
    helpers.assert_glyphset(ufo1, ["A", "B"])

    ufo1 = subset_ufo(ufo2, glyphs=["A", "B"], layout_handling="closure")
    helpers.assert_glyphset(ufo1, ["A", "B", "C"])

    helpers.assert_features_similar(
        ufo1,
        """
lookup ccmp1 {
    sub A B by C;
} ccmp1;
feature ccmp {
    lookup ccmp1;
} ccmp;
    """,
    )


def test_pos(helpers):
    ufo2 = helpers.create_ufo_from_features(
        "lookup kern1 { pos [A B] 120; } kern1; feature kern { lookup kern1; } kern;"
    )

    ufo1 = subset_ufo(ufo2, glyphs=["A"])
    helpers.assert_features_similar(
        ufo1,
        """
lookup kern1 {
    pos [A] 120;
} kern1;
feature kern {
    lookup kern1;
} kern;
    """,
    )


def test_chain(helpers):
    ufo2 = helpers.create_ufo_from_features(
        """
        lookup chained { pos A 120; pos B 200; } chained;
        lookup chain { pos [A B]' lookup chained [A B C]; } chain;
        feature kern { lookup chain; } kern;
        """
    )

    ufo1 = subset_ufo(ufo2, glyphs=["A", "C"])
    helpers.assert_features_similar(
        ufo1,
        """
lookup chained {
    pos A 120;
} chained;
lookup chain {
    pos [A]' lookup chained [A C];
} chain;
feature kern {
    lookup chain;
} kern;
    """,
    )


def test_languagesystems(helpers):
    ufo1 = helpers.create_ufo_from_features(
        """
      languagesystem DFLT dflt;
      languagesystem latn dflt;
      feature ccmp { sub A by B; } ccmp;
    """
    )
    ufo2 = helpers.create_ufo_from_features(
        """
      languagesystem DFLT dflt;
      languagesystem dev2 dflt;
      languagesystem dev2 NEP;
      feature ccmp {
        sub ka-deva by sa-deva;
        script dev2;
        language NEP;
        sub ta-deva by kssa-deva;
        sub la-deva by kssa-deva;
      } ccmp;
    """
    )
    merge_ufos(ufo1, ufo2, glyphs=["ka-deva", "sa-deva", "kssa-deva", "ta-deva"])
    helpers.assert_features_similar(
        ufo1,
        """
      languagesystem DFLT dflt;
      languagesystem latn dflt;
      languagesystem dev2 dflt;
      languagesystem dev2 NEP;

      feature ccmp {
      sub A by B;
      } ccmp;
      feature ccmp {
        sub ka-deva by sa-deva;
        script dev2;
        language NEP;
        sub ta-deva by kssa-deva;
      } ccmp;
    """,
    )

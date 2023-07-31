from ufomerge import merge_ufos
from ufoLib2.objects.component import Component


def test_glyphset(helpers):
    ufo1 = helpers.create_ufo(['A', 'B'])
    ufo2 = helpers.create_ufo(['C', 'D'])
    merge_ufos(ufo1, ufo2)
    helpers.assert_glyphset(ufo1, ['A', 'B', 'C', 'D'])


def test_component_closure(helpers):
    ufo1 = helpers.create_ufo(['A', 'B'])
    ufo2 = helpers.create_ufo(['C', 'D', 'comp'])

    ufo2["D"].components.append(Component("comp"))

    merge_ufos(ufo1, ufo2, glyphs=['D'])

    helpers.assert_glyphset(ufo1, ['A', 'B', 'D', 'comp'])


def test_kerning_flat(helpers):
    ufo1 = helpers.create_ufo(['A', 'B'])
    ufo2 = helpers.create_ufo(['C', 'D', 'E'])
    ufo2.kerning = {
      ("C", "D"): 20,
      ("C", "E"): 15,
      ("C", "A"): -20,  # I can foresee some dispute about what this should do
    }

    merge_ufos(ufo1, ufo2, glyphs=['C', 'D'])

    assert ufo1.kerning == {
      ("C", "D"): 20,
      ("C", "A"): -20,
    }

def test_existing_handling(helpers):
    ufo1 = helpers.create_ufo(['A', 'B'])
    ufo1["B"].width = 100
    ufo2 = helpers.create_ufo(['B', 'C'])
    ufo2["B"].width = 200
    merge_ufos(ufo1, ufo2, existing_handling="skip")
    assert ufo1["B"].width == 100
    merge_ufos(ufo1, ufo2, existing_handling="replace")
    assert ufo1["B"].width == 200


def test_kerning_groups(helpers):
    """Test that groups and kerning pairs of ufo1 are dropped if they reference
    any imported glyphs.
    
    This avoids stray kerning and glyphs being memebers of more than one group.
    """
    ufo1 = helpers.create_ufo(["A", "B"])
    ufo1.groups["public.kern1.foo"] = ["A"]
    ufo1.groups["public.kern2.foo"] = ["A"]
    ufo1.kerning[("public.kern1.foo", "public.kern2.foo")] = 10
    ufo1.kerning[("public.kern1.foo", "B")] = 20
    ufo1.kerning[("A", "public.kern2.foo")] = 30
    ufo1.kerning[("A", "A")] = 40
    ufo2 = helpers.create_ufo(["A", "B"])
    ufo2.groups["public.kern1.bar"] = ["A"]
    ufo2.groups["public.kern2.bar"] = ["A"]
    ufo2.kerning[("public.kern1.bar", "public.kern2.bar")] = 50
    ufo2.kerning[("public.kern1.bar", "B")] = 60
    ufo2.kerning[("A", "public.kern2.bar")] = 70
    ufo2.kerning[("A", "A")] = 80

    merge_ufos(ufo1, ufo2)
    assert ufo1.groups == {
        "public.kern1.bar": ["A"],
        "public.kern2.bar": ["A"],
    }
    assert ufo1.kerning == {
        ("public.kern1.bar", "public.kern2.bar"): 50,
        ("public.kern1.bar", "B"): 60,
        ("A", "public.kern2.bar"): 70,
        ("A", "A"): 80,
    }

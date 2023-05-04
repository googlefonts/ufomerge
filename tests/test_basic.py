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


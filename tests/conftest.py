import pytest
import ufoLib2
from fontFeatures.feaLib import FeaParser
import re


class Helpers:
    @staticmethod
    def create_ufo(glyphs: list[str]) -> ufoLib2.Font:
        font = ufoLib2.Font()
        for glyph in glyphs:
            font.newGlyph(glyph)
        return font

    @staticmethod
    def create_ufo_from_features(features: str) -> ufoLib2.Font:
        ff = FeaParser(features).parse()
        glyphset = set()
        for routine in ff.routines:
            for rule in routine.rules:
                glyphset |= set(rule.involved_glyphs)
        font = ufoLib2.Font()
        for glyph in glyphset:
            font.newGlyph(glyph)
        font.features.text = features
        return font

    @staticmethod
    def assert_glyphset(ufo: ufoLib2.Font, glyphs: list[str]):
        ufo_glyphs = set(ufo.keys())
        assert ufo_glyphs == set(glyphs)

    @staticmethod
    def assert_features_similar(ufo: ufoLib2.Font, features: str):
        def transform(t):
            t = re.sub(r"(?m)^\s+", "", t)
            t = re.sub(r"(?m)^.*lookupflag 0;", "", t)
            t = re.sub(r"(?m)#.*$", "", t)
            t = re.sub(r"(?m)^\s*;?\s*$", "", t)
            t = re.sub(r"\n\n", "\n", t)
            return t
        assert transform(ufo.features.text) == transform(features)


@pytest.fixture
def helpers():
    return Helpers

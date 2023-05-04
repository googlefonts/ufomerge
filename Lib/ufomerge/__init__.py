import copy
from io import StringIO
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Set, Tuple, Union

from fontFeatures import (
    Attachment,
    Chaining,
    FontFeatures,
    Positioning,
    Routine,
    RoutineReference,
    Substitution,
)
from fontFeatures.feaLib import FeaParser
from fontTools.feaLib.parser import Parser
from fontTools.feaLib.ast import LanguageSystemStatement
from ufoLib2 import Font

logger = logging.getLogger("ufomerge")
logging.basicConfig(level=logging.INFO)


class LanguageSystemRecordingFeaParser(FeaParser):
    def __init__(self, featurefile, font=None, glyphNames=None, includeDir=None):
        super(LanguageSystemRecordingFeaParser, self).__init__(
            featurefile, font=font, glyphNames=glyphNames, includeDir=includeDir
        )
        self.languagesystems = []

    def add_language_system(self, location, script, language):
        self.languagesystems.append((script, language))


def has_any_empty_slots(sequence: list[list[str]]) -> bool:
    return any(len(slot) == 0 for slot in sequence)


@dataclass
class UFOMerger:
    ufo1: Font
    ufo2: Font
    glyphs: Iterable[str] = field(default_factory=list)
    exclude_glyphs: Iterable[str] = field(default_factory=list)
    codepoints: Iterable[int] = field(default_factory=list)
    layout_handling: str = "subset"
    existing_handling: str = "replace"
    # We would like to use a set here, but we need order preservation
    incoming_glyphset: dict[str, bool] = field(init=False)
    final_glyphset: Set[str] = field(init=False)
    ufo2_features: FontFeatures = field(init=False)
    ufo2_languagesystems: list[Tuple[str, str]] = field(init=False)

    def __post_init__(self):
        # Set up the glyphset

        if not self.glyphs and not self.codepoints:
            self.glyphs = self.ufo2.keys()

        self.incoming_glyphset = dict.fromkeys(self.glyphs, True)

        for glyph in self.ufo2:
            if any(cp in self.codepoints for cp in glyph.unicodes):
                if glyph.name is not None:
                    self.incoming_glyphset[glyph.name] = True

        for glyph in self.exclude_glyphs:
            del self.incoming_glyphset[glyph]

        # Check those glyphs actually are in UFO 2
        not_there = set(self.incoming_glyphset) - set(self.ufo2.keys())
        if len(not_there):
            logger.warn(
                "The following glyphs were not in UFO 2: %s" % ", ".join(not_there)
            )
            for glyph in not_there:
                del self.incoming_glyphset[glyph]

        self.final_glyphset = set(self.ufo1.keys()) | set(self.incoming_glyphset)

        # Set up UFO2 features
        if self.layout_handling != "ignore":
            ufo2path = getattr(self.ufo2, "_path", None)
            includeDir = Path(ufo2path).parent if ufo2path else None
            parser = LanguageSystemRecordingFeaParser(
                self.ufo2.features.text,
                includeDir=includeDir,
                glyphNames=list(self.ufo2.keys()),
            )
            self.ufo2_features = parser.parse()
            self.ufo2_languagesystems = parser.languagesystems
        else:
            self.ufo2_features = FontFeatures()
            self.ufo2_languagesystems = []

    def merge(self):
        if not self.incoming_glyphset:
            logger.info("No glyphs selected, nothing to do")
            return

        # list() avoids "Set changed size during iteration" error
        for glyph in list(self.incoming_glyphset.keys()):
            self.close_components(glyph)

        if self.layout_handling == "closure":
            self.perform_layout_closure()
            self.merge_layout()
        elif self.layout_handling != "ignore":
            self.merge_layout()

        self.merge_kerning()

        # Now do the add
        for glyph in self.incoming_glyphset.keys():
            if self.existing_handling == "skip" and glyph in self.ufo1:
                logger.info(
                    "Skipping glyph '%s' already present in target file" % glyph
                )
                continue

            self.merge_set("public.glyphOrder", glyph, create_if_not_in_ufo1=False)
            self.merge_set("public.skipExportGlyphs", glyph, create_if_not_in_ufo1=True)
            self.merge_dict("public.postscriptNames", glyph, create_if_not_in_ufo1=True)
            self.merge_dict(
                "public.openTypeCategories", glyph, create_if_not_in_ufo1=True
            )

            if glyph in self.ufo1:
                self.ufo1[glyph] = self.ufo2[glyph]
            else:
                self.ufo1.addGlyph(self.ufo2[glyph])

    def close_components(self, glyph: str):
        """Add any needed components, recursively"""
        components = self.ufo2[glyph].components
        if not components:
            return
        for comp in components:
            base_glyph = comp.baseGlyph
            if base_glyph not in self.final_glyphset:
                # Well, this is the easy case
                self.final_glyphset.add(base_glyph)
                self.incoming_glyphset[base_glyph] = True
                self.close_components(base_glyph)
            elif self.existing_handling == "replace":
                # Also not a problem
                self.incoming_glyphset[base_glyph] = True
                self.close_components(base_glyph)
            elif base_glyph in self.ufo1:
                # Oh bother.
                logger.warning(
                    f"New glyph {glyph} used component {base_glyph} which already exists in font; not replacing it, as you have not specified --replace-existing"
                )

    def perform_layout_closure(self):
        """Make sure that anything that can be produced by
        substitution rules added to the new UFO will also be
        added to the glyphset."""
        for routine in self.ufo2_features.routines:
            for rule in routine.rules:
                if not isinstance(rule, Substitution):
                    continue
                if (
                    has_any_empty_slots(self.filter_sequence(rule.input))
                    or has_any_empty_slots(self.filter_sequence(rule.precontext))
                    or has_any_empty_slots(self.filter_sequence(rule.postcontext))
                ):
                    continue
                for sublist in rule.replacement:
                    for glyph in sublist:
                        self.incoming_glyphset[glyph] = True
                        self.final_glyphset.add(glyph)

    # No typing here because it doesn't trust me.
    def fix_context_and_check_applicable(self, rule) -> bool:
        # Slim context and inputs to only those glyphs we have

        # Horrible API decision in FontFeatures, sorry everyone
        if hasattr(rule, "input"):
            rule.input = self.filter_sequence(rule.input)
            slots = rule.input
        else:
            rule.glyphs = self.filter_sequence(rule.glyphs)
            slots = rule.glyphs
        rule.precontext = self.filter_sequence(rule.precontext)
        rule.postcontext = self.filter_sequence(rule.postcontext)
        # If any of the slots are completely empty, then no glyphs
        # from UFO2 have been included in the new UFO, and rule can
        # never fire
        if (
            has_any_empty_slots(slots)
            or has_any_empty_slots(rule.precontext)
            or has_any_empty_slots(rule.postcontext)
        ):
            return False

        return True

    def visit_substitution(self, rule: Substitution, newroutine: Routine):
        # Filter inputs
        result = self.fix_context_and_check_applicable(rule)
        if not result:
            return
        # Filter outputs
        if len(rule.input) == 1 and len(rule.replacement) == 1:  # GSUB1
            mapping = zip(rule.input[0], rule.replacement[0])
            mapping = [
                (a, b)
                for a, b in mapping
                if a in self.final_glyphset and b in self.final_glyphset
            ]
            rule.input[0] = [r[0] for r in mapping]
            rule.replacement[0] = [r[1] for r in mapping]
        else:
            rule.replacement = self.filter_sequence(rule.replacement)
        if has_any_empty_slots(rule.replacement):
            return
        newroutine.rules.append(rule)
        logging.debug("Adding rule '%s'", rule.asFea())

    def visit_pos_chain(self, rule: Union[Positioning, Chaining], newroutine: Routine):
        # Filter inputs
        result = self.fix_context_and_check_applicable(rule)
        if not result:
            return
        newroutine.rules.append(rule)
        logging.debug("Adding rule '%s'", rule.asFea())

    def merge_layout(self):
        new_layout_rules = FontFeatures()
        for routine in self.ufo2_features.routines:
            newroutine = Routine(name=routine.name, flags=routine.flags)
            setattr(routine, "counterpart", newroutine)
            for rule in routine.rules:
                if isinstance(rule, Substitution):
                    self.visit_substitution(rule, newroutine)
                elif isinstance(rule, (Positioning, Chaining)):
                    self.visit_pos_chain(rule, newroutine)
            if not newroutine.rules:
                continue
            new_layout_rules.routines.append(newroutine)
            # Was it in a feature?
            add_to = []
            for feature_name, routines in self.ufo2_features.features.items():
                for routine_ref in routines:
                    if routine_ref.routine == routine:
                        add_to.append(feature_name)
            for feature_name in add_to:
                new_layout_rules.addFeature(feature_name, [newroutine])
        if not new_layout_rules.routines:
            return
        self.ufo1.features.text += new_layout_rules.asFea(do_gdef=False)
        self.add_language_systems()

    def add_language_systems(self):
        if not self.ufo2_languagesystems:
            return
        ast = Parser(
            StringIO(self.ufo1.features.text), glyphNames=list(self.final_glyphset)
        ).parse()
        current = []
        last = None
        for lss in ast.statements:
            if isinstance(lss, LanguageSystemStatement):
                current.append((lss.script, lss.language))
                last = lss
        # If all new LSS are included in current, we're done.
        to_add = []
        for pair in self.ufo2_languagesystems:
            if pair not in current:
                to_add.append(LanguageSystemStatement(*pair))
        if not to_add:
            return
        if last is None:
            last_index = 0
        else:
            last_index = ast.statements.index(last)
        ast.statements[last_index + 1 : last_index + 1] = to_add
        self.ufo1.features.text = ast.asFea()

    def merge_kerning(self):
        groups1 = self.ufo1.groups
        groups2 = self.ufo2.groups
        # Slim down the groups to only those in the glyph set
        for glyph in groups2.keys():
            groups2[glyph] = self.filter_glyphs(groups2[glyph])

        for (l, r), value in self.ufo2.kerning.items():
            left_glyphs = self.filter_glyphs(groups2.get(l, [l]))
            right_glyphs = self.filter_glyphs(groups2.get(r, [r]))
            if not left_glyphs or not right_glyphs:
                continue

            # Just add for now. We should get fancy later
            self.ufo1.kerning[(l, r)] = value
            if l.startswith("public.kern"):
                if l not in groups1:
                    groups1[l] = groups2[l]
                else:
                    groups1[l] = self.filter_glyphs(set(groups1[l] + groups2[l]))
            if r.startswith("public.kern"):
                if r not in groups1:
                    groups1[r] = groups2[r]
                else:
                    groups1[r] = self.filter_glyphs(set(groups1[r] + groups2[r]))

    # Utility routines
    def filter_glyphs(self, glyphs: Iterable[str]) -> list[str]:
        return [glyph for glyph in glyphs if glyph in self.final_glyphset]

    def filter_sequence(self, slots: Iterable[list[str]]) -> list[list[str]]:
        return [self.filter_glyphs(slot) for slot in slots]

    # Routines for merging font lib keys
    def merge_set(self, name, glyph, create_if_not_in_ufo1=False):
        lib1 = self.ufo1.lib
        lib2 = self.ufo2.lib
        if name not in lib2 or glyph not in lib2[name]:
            return
        if name not in lib1:
            if create_if_not_in_ufo1:
                lib1[name] = []
            else:
                return
        if glyph not in lib1[name]:
            lib1[name].append(glyph)

    def merge_dict(self, name, glyph, create_if_not_in_ufo1=False):
        lib1 = self.ufo1.lib
        lib2 = self.ufo2.lib
        if name not in lib2 or glyph not in lib2[name]:
            return
        if name not in lib1:
            if create_if_not_in_ufo1:
                lib1[name] = {}
            else:
                return
        lib1[name][glyph] = lib2[name][glyph]


def merge_ufos(
    ufo1: Font,
    ufo2: Font,
    glyphs: Iterable[str] = [],
    exclude_glyphs: Iterable[str] = [],
    codepoints: Iterable[int] = [],
    layout_handling: str = "subset",
    existing_handling: str = "replace",
):
    """Merge two UFO files together

    Returns nothing but modifies ufo1.

    Args:
        ufo1: The destination UFO which will receive the new glyphs.
        ufo2: The "donor" UFO which will provide the new glyphs.
        glyphs: Optionally, a list of glyph names to be added. If not
            present and codepoints is also not present, all glyphs from
            the donor UFO will be added.
        exclude_glyphs: Optionally, a list of glyph names which should
            not be added.
        codepoints: A list of Unicode codepoints as integers. If present,
            the glyphs with these codepoints will be selected for merging.
        layout_handling: One of either "subset", "closure" or "ignore".
            "ignore" means that no layout rules are added from UFO2.
            "closure" means that the list of donor glyphs will be expanded
            such that any substitutions in UFO2 involving the selected
            glyphs will continue to work. "subset" means that the rules
            are slimmed down to only include the given glyphs. For example,
            if there is a rule "sub A B by C;", and glyphs==["A", "B"],
            then when layout_handling=="subset", this rule will be dropped;
            but if layout_handling=="closure", glyph C will also be merged
            so that the ligature still works. The default is "subset".
        existing_handling: One of either "replace" or "skip". What to do
            if the donor glyph already exists in UFO1: "replace" replaces
            it with the version in UFO2; "skip" keeps the existing glyph.
            The default is "replace".
    """
    if layout_handling not in ["subset", "closure", "ignore"]:
        raise ValueError(f"Unknown layout handling mode '{layout_handling}'")

    UFOMerger(
        ufo1,
        ufo2,
        glyphs,
        exclude_glyphs,
        codepoints,
        layout_handling,
        existing_handling,
    ).merge()


def subset_ufo(
    ufo: Font,
    glyphs: Iterable[str] = [],
    exclude_glyphs: Iterable[str] = [],
    codepoints: Iterable[int] = [],
    layout_handling: str = "subset",
):
    """Creates a new UFO with only the provided glyphs.

    Returns a new UFO object.

    Args:
        ufo: The UFO to subset.
        glyphs: A list of glyph names to be added. If not present and
            codepoints is also not present, all glyphs UFO will be added.
        exclude_glyphs: Optionally, a list of glyph names which should
            not be added.
        codepoints: A list of Unicode codepoints as integers. If present,
            the glyphs with these codepoints will be selected for merging.
        layout_handling: One of either "subset", "closure" or "ignore".
            "ignore" means that no layout rules are added from the font.
            "closure" means that the list of donor glyphs will be expanded
            such that any substitutions in the font involving the selected
            glyphs will continue to work. "subset" means that the rules
            are slimmed down to only include the given glyphs. For example,
            if there is a rule "sub A B by C;", and glyphs==["A", "B"],
            then when layout_handling=="subset", this rule will be dropped;
            but if layout_handling=="closure", glyph C will also be merged
            so that the ligature still works. The default is "subset".
    """
    new_ufo = Font()
    new_ufo.info = copy.deepcopy(ufo.info)
    merge_ufos(
        new_ufo,
        ufo,
        glyphs,
        exclude_glyphs,
        codepoints,
        layout_handling=layout_handling,
    )
    return new_ufo

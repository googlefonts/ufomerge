from collections import defaultdict
import copy
from io import StringIO
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, OrderedDict, Set, Tuple

from fontTools.feaLib.parser import Parser
import fontTools.feaLib.ast as ast
from ufoLib2 import Font

logger = logging.getLogger("ufomerge")
logging.basicConfig(level=logging.INFO)


def has_any_empty_slots(sequence: list) -> bool:
    for slot in sequence:
        if isinstance(slot, list):
            if len(slot) == 0:
                return True
        elif hasattr(slot, "glyphSet"):
            if len(slot.glyphSet()) == 0:
                return True
        else:
            raise ValueError
    return False


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
    ufo2_features: ast.FeatureFile = field(init=False)
    ufo2_languagesystems: list[Tuple[str, str]] = field(init=False)

    def __post_init__(self):
        # Set up the glyphset

        if not self.glyphs and not self.codepoints:
            self.glyphs = self.ufo2.keys()

        self.incoming_glyphset = dict.fromkeys(self.glyphs, True)

        # Now add codepoints
        if self.codepoints:
            existing_map = {}
            to_delete = defaultdict(list)
            for glyph in self.ufo1:
                for cp in glyph.unicodes:
                    existing_map[cp] = glyph.name

            for glyph in self.ufo2:
                for cp in glyph.unicodes:
                    if cp in self.codepoints:
                        # But see if we have a corresponding glyph already
                        if cp in existing_map:
                            if self.existing_handling == "skip":
                                logger.info(
                                    "Skipping codepoint U+%04X already present as '%s' in target file"
                                    % (cp, glyph.name)
                                )
                                # Blacklist this glyph
                                if glyph.name in self.incoming_glyphset:
                                    del self.incoming_glyphset[glyph.name]
                                break
                            elif self.existing_handling == "replace":
                                to_delete[existing_map[cp]].append(cp)
                        if glyph.name is not None:
                            self.incoming_glyphset[glyph.name] = True

            # Clear up any glyphs for UFO1 we don't want any more
            for glyphname, codepoints in to_delete.items():
                self.ufo1[glyphname].unicodes = list(
                    set(self.ufo1[glyphname].unicodes) - set(codepoints)
                )
                codepoints_string = ", ".join("U+%04X" % cp for cp in codepoints)
                logger.info(
                    "Removing mappings %s from glyph '%s' due to incoming codepoints"
                    % (codepoints_string, glyphname)
                )
                # We *could* delete it from the target glyphset, but there
                # is a problem here - what if it's actually mentioned in the
                # feature file?! So we don't.

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
            self.ufo2_features = Parser(
                StringIO(self.ufo2.features.text),
                includeDir=includeDir,
                glyphNames=list(self.ufo2.keys()),
            ).parse()
        else:
            self.ufo2_features = ast.FeatureFile()
        self.ufo2_languagesystems = []

    def merge(self):
        if not self.incoming_glyphset:
            logger.info("No glyphs selected, nothing to do")
            return

        if self.layout_handling == "closure":
            # There is a hard sequencing problem here. Glyphs which
            # get substituted later in the file but earlier in the
            # shaping process may get missed. ie.
            #    lookup foo { sub B by C; } foo;
            #    feature bar1 {
            #       sub A by B;
            #    } bar1;
            #    feature bar2 { sub B' lookup foo; } bar2;
            # If A is in the glyphset, B will get included when
            # processing bar1 but by this time it's too late to see
            # that this impacts upon C. I'm just going to keep running
            # until the output is stable
            count = len(self.final_glyphset)
            rounds = 0
            while True:
                self.perform_layout_closure(self.ufo2_features.statements)
                rounds += 1
                if len(self.final_glyphset) == count:
                    break
                if rounds > 10:
                    raise ValueError(
                        "Layout closure failure; glyphset grew unreasonably"
                    )
                count = len(self.final_glyphset)

        if self.layout_handling != "ignore":
            self.ufo2_features.statements = self.filter_layout(
                self.ufo2_features.statements
            )
            self.clean_layout(self.ufo2_features)
            self.ufo1.features.text += self.ufo2_features.asFea()
            self.add_language_systems()

        # list() avoids "Set changed size during iteration" error
        for glyph in list(self.incoming_glyphset.keys()):
            self.close_components(glyph)

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
                logger.debug("Adding %s used as a component in %s", base_glyph, glyph)
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

    def perform_layout_closure(self, statements):
        """Make sure that anything that can be produced by
        substitution rules added to the new UFO will also be
        added to the glyphset."""
        for st in statements:
            if hasattr(st, "statements"):
                self.perform_layout_closure(st.statements)
            if not isinstance(
                st,
                (
                    ast.SingleSubstStatement,
                    ast.MultipleSubstStatement,
                    ast.AlternateSubstStatement,
                    ast.LigatureSubstStatement,
                    ast.ChainContextSubstStatement,
                ),
            ):
                continue
            if has_any_empty_slots(
                self.filter_sequence(st.prefix)
            ) or has_any_empty_slots(self.filter_sequence(st.suffix)):
                continue
            if isinstance(st, ast.AlternateSubstStatement):
                if not self.filter_glyphs(st.glyph.glyphSet()):
                    continue
                for glyph in st.replacement.glyphSet():
                    self.incoming_glyphset[glyph] = True
                    self.final_glyphset.add(glyph)
                    logger.debug(
                        "Adding %s used in alternate substitution from %s",
                        glyph,
                        st.glyph.asFea(),
                    )
            if isinstance(st, ast.MultipleSubstStatement):
                # Fixup FontTools API breakage
                if isinstance(st.glyph, str):
                    st.glyph = ast.GlyphName(st.glyph, st.location)
                if not self.filter_glyphs(st.glyph.glyphSet()):
                    continue
                for slot in st.replacement:
                    if isinstance(slot, str):
                        slot = ast.GlyphName(slot, st.location)
                    for glyph in slot.glyphSet():
                        self.incoming_glyphset[glyph] = True
                        self.final_glyphset.add(glyph)
                        logger.debug(
                            "Adding %s used in multiple substitution from %s",
                            glyph,
                            st.glyph.asFea(),
                        )
            if isinstance(st, ast.LigatureSubstStatement):
                if has_any_empty_slots(self.filter_sequence(st.glyphs)):
                    continue
                if isinstance(st.replacement, str):
                    st.replacement = ast.GlyphName(st.replacement, st.location)
                for glyph in st.replacement.glyphSet():
                    self.incoming_glyphset[glyph] = True
                    self.final_glyphset.add(glyph)
                    logger.debug(
                        "Adding %s used in ligature substitution from %s",
                        glyph,
                        " ".join([x.asFea() for x in st.glyphs]),
                    )
            if isinstance(st, ast.SingleSubstStatement):
                originals = st.glyphs[0].glyphSet()
                replaces = st.replacements[0].glyphSet()
                if len(replaces) == 1:
                    replaces = replaces * len(originals)
                for inglyph, outglyph in zip(originals, replaces):
                    if inglyph in self.final_glyphset:
                        self.incoming_glyphset[outglyph] = True
                        self.final_glyphset.add(outglyph)
                        logger.debug(
                            "Adding %s used in single substitution from %s",
                            outglyph,
                            inglyph,
                        )

    def filter_layout(self, statements):
        newstatements = []
        for st in statements:
            if isinstance(st, ast.LanguageSystemStatement):
                self.ufo2_languagesystems.append((st.script, st.language))
                continue

            if hasattr(st, "statements"):
                st.statements = self.filter_layout(st.statements)
                substantive_statements = [
                    x for x in st.statements if not isinstance(x, ast.Comment)
                ]
                if not substantive_statements:
                    if isinstance(st, ast.FeatureBlock):
                        continue
                    st.statements = [ast.Comment("lookupflag 0;")]
                newstatements.append(st)
                continue
            if isinstance(st, ast.GlyphClassDefinition):
                st.glyphs = self.filter_glyph_container(st.glyphs)
            if isinstance(
                st,
                (
                    ast.MarkClassDefinition,
                    ast.LigatureCaretByIndexStatement,
                    ast.LigatureCaretByPosStatement,
                ),
            ):
                st.glyphs = self.filter_glyph_container(st.glyphs)
                if not st.glyphs.glyphSet():
                    continue
            if isinstance(st, ast.AlternateSubstStatement):
                st.glyph = self.filter_glyph_container(st.glyph)
                st.replacement = self.filter_glyph_container(st.replacement)
                st.prefix = self.filter_sequence(st.prefix)
                st.suffix = self.filter_sequence(st.suffix)
                if (
                    has_any_empty_slots(st.prefix)
                    or has_any_empty_slots(st.suffix)
                    or not st.replacement.glyphSet()
                    or not st.glyph.glyphSet()
                ):
                    continue
            if isinstance(
                st, (ast.ChainContextSubstStatement, ast.ChainContextPosStatement)
            ):
                st.prefix = self.filter_sequence(st.prefix)
                st.suffix = self.filter_sequence(st.suffix)
                st.glyphs = self.filter_sequence(st.glyphs)
                if (
                    has_any_empty_slots(st.prefix)
                    or has_any_empty_slots(st.suffix)
                    or has_any_empty_slots(st.glyphs)
                ):
                    continue
            if isinstance(st, (ast.CursivePosStatement)):
                st.glyphclass = self.filter_glyph_container(st.glyphclass)
                if not st.glyphclass.glyphSet():
                    continue
            if isinstance(st, (ast.IgnorePosStatement, ast.IgnoreSubstStatement)):
                newcontexts = []
                for prefix, glyphs, suffix in st.chainContexts:
                    prefix[:] = self.filter_sequence(prefix)
                    glyphs[:] = self.filter_sequence(glyphs)
                    suffix[:] = self.filter_sequence(suffix)
                    if (
                        has_any_empty_slots(prefix)
                        or has_any_empty_slots(suffix)
                        or has_any_empty_slots(glyphs)
                    ):
                        continue
                    newcontexts.append((prefix, glyphs, suffix))
                if not newcontexts:
                    continue
                st.chainContexts = newcontexts
            if isinstance(st, ast.LigatureSubstStatement):
                st.glyphs = self.filter_sequence(st.glyphs)
                st.replacement = self.filter_glyph_container(st.replacement)
                st.prefix = self.filter_sequence(st.prefix)
                st.suffix = self.filter_sequence(st.suffix)
                if (
                    has_any_empty_slots(st.prefix)
                    or has_any_empty_slots(st.glyphs)
                    or has_any_empty_slots(st.suffix)
                    or not st.replacement.glyphSet()
                ):
                    continue
            if isinstance(st, ast.LookupFlagStatement):
                if st.markAttachment:
                    st.markAttachment = self.filter_glyph_container(st.markAttachment)
                    if not st.markAttachment.glyphSet():
                        continue
                if st.markFilteringSet:
                    st.markFilteringSet = self.filter_glyph_container(
                        st.markFilteringSet
                    )
                    if not st.markFilteringSet.glyphSet():
                        continue
            if isinstance(st, ast.MarkBasePosStatement):
                st.base = self.filter_glyph_container(st.base)
                if not st.base.glyphSet():
                    continue
            if isinstance(st, ast.MarkLigPosStatement):
                st.ligatures = self.filter_glyph_container(st.ligatures)
                if not st.ligatures.glyphSet():
                    continue
            if isinstance(st, ast.MarkMarkPosStatement):
                st.baseMarks = self.filter_glyph_container(st.baseMarks)
                if not st.baseMarks.glyphSet():
                    continue
            if isinstance(st, ast.MultipleSubstStatement):
                st.glyph = self.filter_glyph_container(st.glyph)
                st.replacement = self.filter_sequence(st.replacement)
                st.prefix = self.filter_sequence(st.prefix)
                st.suffix = self.filter_sequence(st.suffix)
                if (
                    has_any_empty_slots(st.prefix)
                    or has_any_empty_slots(st.replacement)
                    or has_any_empty_slots(st.suffix)
                    or not st.glyph.glyphSet()
                ):
                    continue
            if isinstance(st, ast.PairPosStatement):
                st.glyphs1 = self.filter_glyph_container(st.glyphs1)
                st.glyphs2 = self.filter_glyph_container(st.glyphs2)
                if not st.glyphs1.glyphSet() or not st.glyphs2.glyphSet():
                    continue
            if isinstance(st, ast.ReverseChainSingleSubstStatement):
                st.old_prefix = self.filter_sequence(st.old_prefix)
                st.old_suffix = self.filter_sequence(st.old_suffix)
                st.glyphs = self.filter_sequence(st.glyphs)
                st.replacements = self.filter_sequence(st.replacements)
                if (
                    has_any_empty_slots(st.old_prefix)
                    or has_any_empty_slots(st.replacements)
                    or has_any_empty_slots(st.old_suffix)
                    or has_any_empty_slots(st.glyphs)
                ):
                    continue
            if isinstance(st, ast.SingleSubstStatement):
                st.prefix = self.filter_sequence(st.prefix)
                st.suffix = self.filter_sequence(st.suffix)
                if has_any_empty_slots(st.prefix) or has_any_empty_slots(st.suffix):
                    continue
                originals = st.glyphs[0].glyphSet()
                replaces = st.replacements[0].glyphSet()
                if len(replaces) == 1:
                    replaces = replaces * len(originals)
                newmapping = OrderedDict()
                for inglyph, outglyph in zip(originals, replaces):
                    if (
                        inglyph in self.final_glyphset
                        and outglyph in self.final_glyphset
                    ):
                        newmapping[inglyph] = outglyph
                if not newmapping:
                    continue
                if len(newmapping) == 1:
                    st.glyphs = [ast.GlyphName(list(newmapping.keys())[0])]
                    st.replacements = [ast.GlyphName(list(newmapping.values())[0])]
                else:
                    st.glyphs = [ast.GlyphClass(list(newmapping.keys()))]
                    st.replacements = [ast.GlyphClass(list(newmapping.values()))]
            if isinstance(st, ast.SinglePosStatement):
                st.prefix = self.filter_sequence(st.prefix)
                st.suffix = self.filter_sequence(st.suffix)
                container, vr = st.pos[0]
                st.pos = [(self.filter_glyph_container(container), vr)]
                if not st.pos[0][0].glyphSet():
                    continue

            newstatements.append(st)

        return newstatements

    def clean_layout(self, layout: ast.FeatureFile):
        # Collect all referenced lookups
        referenced = set()
        for feature in layout.statements:
            if not isinstance(feature, (ast.FeatureBlock, ast.LookupBlock)):
                continue
            for statement in feature.statements:
                if isinstance(statement, ast.LookupReferenceStatement):
                    referenced.add(statement.lookup.name)
                if hasattr(statement, "lookups"):
                    for lookuplist in statement.lookups:
                        if lookuplist is None:
                            continue
                        if isinstance(lookuplist, (list, tuple)):
                            for lookup in lookuplist:
                                referenced.add(lookup.name)
                        else:
                            referenced.add(lookuplist.name)

        newfeatures = []
        # If there are any lookups within a feature but with no effective
        # statements, remove them.
        for feature in layout.statements:
            # Remove any unreferenced lookups
            if isinstance(feature, ast.LookupBlock) and feature.name not in referenced:
                continue
            if not isinstance(feature, ast.FeatureBlock):
                newfeatures.append(feature)
                continue
            newstatements = []
            for lookup in feature.statements:
                if not isinstance(lookup, ast.LookupBlock) or lookup.name in referenced:
                    newstatements.append(lookup)
                    continue
                effective = False
                for statement in lookup.statements:
                    if isinstance(statement, (ast.Comment, ast.LookupFlagStatement)):
                        continue
                    effective = True
                    break
                if effective:
                    newstatements.append(lookup)
                else:
                    logger.warn(
                        "Removing ineffective lookup %s in %s "
                        % (lookup.name, feature.name)
                    )
            if newstatements and any(
                [
                    not isinstance(
                        st, (ast.Comment, ast.ScriptStatement, ast.LanguageStatement)
                    )
                    for st in newstatements
                ]
            ):
                feature.statements = newstatements
                newfeatures.append(feature)
        layout.statements = newfeatures

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

    def add_language_systems(self):
        if not self.ufo2_languagesystems:
            return
        featurefile = Parser(
            StringIO(self.ufo1.features.text), glyphNames=list(self.final_glyphset)
        ).parse()
        current = []
        last = None
        for lss in featurefile.statements:
            if isinstance(lss, ast.LanguageSystemStatement):
                current.append((lss.script, lss.language))
                last = lss
        # If all new LSS are included in current, we're done.
        to_add = []
        for pair in self.ufo2_languagesystems:
            if pair not in current:
                to_add.append(ast.LanguageSystemStatement(*pair))
        if not to_add:
            return
        if last is None:
            last_index = 0
        else:
            last_index = featurefile.statements.index(last)
        featurefile.statements[last_index + 1 : last_index + 1] = to_add
        self.ufo1.features.text = featurefile.asFea()

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

    def filter_sequence(self, slots: Iterable) -> list[list[str]]:
        newslots = []
        for slot in slots:
            if isinstance(slot, list):
                newslots.append(self.filter_glyphs(slot))
            else:
                newslots.append(self.filter_glyph_container(slot))
        return newslots

    def filter_glyph_container(self, container):
        if isinstance(container, str):
            # Grr.
            container = ast.GlyphName(container)
        if isinstance(container, ast.GlyphName):
            # Single glyph
            if container.glyph not in self.final_glyphset:
                return ast.GlyphClass([])
            return container
        if isinstance(container, ast.GlyphClass):
            container.glyphs = self.filter_glyphs(container.glyphs)
            return container
        if isinstance(container, ast.GlyphClassName):
            # Filter the class, see if there's anything left
            classdef = container.glyphclass.glyphs
            classdef.glyphs = self.filter_glyphs(classdef.glyphs)
            if classdef.glyphs:
                return container
            return ast.GlyphClass([])
        raise ValueError(f"Unknown glyph container {container}")

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

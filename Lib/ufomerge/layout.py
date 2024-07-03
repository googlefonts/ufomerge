import copy
import logging
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Any, Iterable, Mapping, OrderedDict, Sequence, Set

from fontTools.feaLib import ast


logger = logging.getLogger("ufomerge.layout")


def _deduplicate_class_defs(
    class_name_references: dict[str, list[ast.GlyphClassName]],
) -> list[ast.GlyphClassDefinition]:
    """Deduplicate class definitions with the same glyph set.

    We let each statement do its own filtering of class definitions to preserve
    semantics going in, but then need to deduplicate the resulting class
    definitions.
    """
    fresh_class_defs = []

    for class_name, class_defs in class_name_references.items():
        by_glyph_set: dict[tuple[str, ...], list[ast.GlyphClassDefinition]]
        by_glyph_set = defaultdict(list)
        for class_def in class_defs:
            glyph_set = tuple(sorted(class_def.glyphclass.glyphs.glyphSet()))
            by_glyph_set[glyph_set].append(class_def.glyphclass)

        for index, (glyph_set, class_defs) in enumerate(by_glyph_set.items(), start=1):
            # No need to deduplicate.
            if len(by_glyph_set) == 1:
                new_class_def = ast.GlyphClassDefinition(
                    class_name, ast.GlyphClass([ast.GlyphName(g) for g in glyph_set])
                )
                fresh_class_defs.append(new_class_def)
                # Update references
                for class_def in class_defs:
                    class_def.name = class_name
                continue

            # Deduplicate
            new_class_name = f"{class_name}_{index}"
            new_class_def = ast.GlyphClassDefinition(
                new_class_name, ast.GlyphClass([ast.GlyphName(g) for g in glyph_set])
            )
            fresh_class_defs.append(new_class_def)

            # Update references
            for class_def in class_defs:
                class_def.name = new_class_name

    return fresh_class_defs


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
class LayoutFilter:
    glyphset: Set[str]
    class_name_references: dict[str, list[ast.GlyphClassName]] = field(init=False)

    def filter_glyphs(self, glyphs: Iterable[str]) -> list[str]:
        return [glyph for glyph in glyphs if glyph in self.glyphset]

    def filter_glyph_mapping(self, glyphs: Mapping[str, Any]) -> dict[str, Any]:
        return {name: data for name, data in glyphs.items() if name in self.glyphset}

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
            if container.glyph not in self.glyphset:
                return ast.GlyphClass([])
            return container
        if isinstance(container, ast.GlyphClass):
            container.glyphs = self.filter_glyphs(container.glyphs)
            # I don't know what `original` is for, but it can undo subsetting
            # when calling asFea():
            container.original = []
            return container
        if isinstance(container, ast.GlyphClassName):
            # Make a copy of the container, we'll deduplicate and correct names
            # in a second pass later.
            container_copy = copy.deepcopy(container)
            copy_list = self.class_name_references[container_copy.glyphclass.name]
            container_copy.glyphclass.name = (
                f"{container_copy.glyphclass.name}_{len(copy_list)}"
            )
            copy_list.append(container_copy)

            # Filter the class, see if there's anything left
            classdef = container_copy.glyphclass.glyphs
            classdef.glyphs = self.filter_glyphs(classdef.glyphs)
            if classdef.glyphs:
                return container_copy
            return ast.GlyphClass([])
        if isinstance(container, ast.MarkClassName):
            markclass = container.markClass
            markclass.glyphs = self.filter_glyph_mapping(markclass.glyphs)
            if markclass.glyphs:
                return container
            return ast.MarkClass([])
        raise ValueError(f"Unknown glyph container {container}")


@dataclass
class LayoutSubsetter(LayoutFilter):
    glyphset: Set[str]
    class_name_references: dict[str, list[ast.GlyphClassName]] = field(init=False)
    incoming_language_systems: list[tuple[str, str]] = field(init=False)

    def __post_init__(self):
        # We might filter named classes per statement. Collect them by name here
        # and deduplicate them later.
        self.class_name_references = defaultdict(list)

    def subset(self, fea: Sequence[ast.Statement]):
        self.incoming_language_systems = [
            (st.script, st.language)
            for st in fea
            if isinstance(st, ast.LanguageSystemStatement)
        ]

        statements = self.filter_layout(fea)
        # At this point, all previous class definitions should have been
        # dropped from the AST, and we can insert new deduplicated ones.
        fresh_class_defs = _deduplicate_class_defs(self.class_name_references)
        for class_def in fresh_class_defs:
            statements.insert(0, class_def)
        return self.clean_layout(statements)

    def filter_layout(self, statements):
        newstatements = []
        for st in statements:
            if isinstance(st, ast.LanguageSystemStatement):
                continue
            if hasattr(st, "statements"):
                st.statements = self.filter_layout(st.statements)
                substantive_statements = [
                    x for x in st.statements if not isinstance(x, ast.Comment)
                ]
                if len(substantive_statements) == 1 and isinstance(
                    substantive_statements[0], ast.LookupFlagStatement
                ):
                    substantive_statements.clear()
                if not substantive_statements:
                    if isinstance(st, ast.FeatureBlock):
                        continue
                    st.statements = [ast.Comment("lookupflag 0;")]
                newstatements.append(st)
                continue
            if isinstance(st, ast.GlyphClassDefinition):
                # Handled separately. This means we drop all class definitions
                # up front.
                continue
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
                    if inglyph in self.glyphset and outglyph in self.glyphset:
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
                if (
                    any(not sequence.glyphSet() for sequence in st.prefix)
                    or any(not sequence.glyphSet() for sequence in st.suffix)
                    or not st.pos[0][0].glyphSet()
                ):
                    continue

            newstatements.append(st)

        return newstatements

    def clean_layout(self, statements: Sequence[ast.Statement]):
        # Collect all referenced lookups
        referenced = set()
        referenced_mark_classes = set()

        def collect_references(feature):
            if not isinstance(
                feature, (ast.FeatureBlock, ast.LookupBlock, ast.VariationBlock)
            ):
                if isinstance(feature, ast.MarkClassDefinition):
                    referenced_mark_classes.add(feature.markClass.name)
                return
            for statement in feature.statements:
                if isinstance(feature, ast.MarkClassDefinition):
                    referenced_mark_classes.add(feature.markClass.name)
                elif isinstance(statement, ast.LookupReferenceStatement):
                    referenced.add(statement.lookup.name)
                if isinstance(statement, ast.LookupBlock):
                    collect_references(statement)
                if hasattr(statement, "lookups"):
                    for lookuplist in statement.lookups:
                        if lookuplist is None:
                            continue
                        if isinstance(lookuplist, (list, tuple)):
                            for lookup in lookuplist:
                                referenced.add(lookup.name)
                        else:
                            referenced.add(lookuplist.name)

        for feature in statements:
            collect_references(feature)

        newfeatures = []
        # If there are any lookups within a feature but with no effective
        # statements, remove them.
        for feature in statements:
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
                # Filter out statements using dropped mark classes.
                filtered_statements = []
                for statement in lookup.statements:
                    if isinstance(statement, ast.MarkBasePosStatement):
                        statement.marks = [
                            (anchor, mark_class)
                            for anchor, mark_class in statement.marks
                            if mark_class.name in referenced_mark_classes
                        ]
                        if not statement.marks:
                            continue
                    filtered_statements.append(statement)
                lookup.statements = filtered_statements
                for statement in lookup.statements:
                    if isinstance(statement, (ast.Comment, ast.LookupFlagStatement)):
                        continue
                    effective = True
                    break
                if effective:
                    newstatements.append(lookup)
                else:
                    logger.warning(
                        "Removing ineffective lookup %s in %s ",
                        lookup.name,
                        feature.name,
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
        return newfeatures


@dataclass
class LayoutClosure(LayoutFilter):
    incoming_glyphset: dict[str, bool]
    glyphset: Set[str]

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
                    self.glyphset.add(glyph)
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
                        self.glyphset.add(glyph)
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
                    self.glyphset.add(glyph)
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
                    if inglyph in self.glyphset:
                        self.incoming_glyphset[outglyph] = True
                        self.glyphset.add(outglyph)
                        logger.debug(
                            "Adding %s used in single substitution from %s",
                            outglyph,
                            inglyph,
                        )

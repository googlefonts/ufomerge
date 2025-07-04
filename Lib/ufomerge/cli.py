#!/usr/bin/env python3
"""Merge together two source fonts in UFO format"""
from __future__ import annotations

import logging
from argparse import ArgumentParser, BooleanOptionalAction

import ufoLib2

from ufomerge import merge_ufos


logger = logging.getLogger("ufomerge")

# I don't care about ambiguous glyph names that look like ranges
logging.getLogger("fontTools.feaLib.parser").setLevel(logging.ERROR)

parser = ArgumentParser(description=__doc__)

gs = parser.add_argument_group("glyph selection")
gs.add_argument("-g", "--glyphs", help="Glyphs to add from UFO 2", default="")
gs.add_argument("-G", "--glyphs-file", help="File containing glyphs to add from UFO 2")
gs.add_argument(
    "-u",
    "--codepoints",
    help="Unicode codepoints to add from UFO 2",
)
gs.add_argument(
    "-U",
    "--codepoints-file",
    help="File containing Unicode codepoints to add from UFO 2",
)
gs.add_argument("-x", "--exclude-glyphs", help="Glyphs to exclude from UFO 2")
gs.add_argument(
    "-X", "--exclude-glyphs-file", help="File containing glyphs to exclude from UFO 2"
)

existing = parser.add_argument_group("Existing glyph handling")
existing = existing.add_mutually_exclusive_group(required=False)
existing.add_argument(
    "--skip-existing",
    action="store_true",
    default=True,
    help="Skip glyphs already present in UFO 1",
)
existing.add_argument(
    "--replace-existing",
    action="store_true",
    default=False,
    help="Replace glyphs already present in UFO 1",
)

layout = parser.add_argument_group("Layout closure handling")
layout2 = layout.add_mutually_exclusive_group(required=False)
layout2.add_argument(
    "--subset-layout",
    action="store_true",
    default=True,
    help="Drop layout rules concerning glyphs not selected",
)
layout2.add_argument(
    "--layout-closure",
    action="store_true",
    default=False,
    help="Add glyphs from UFO 2 contained in layout rules, even if not in glyph set",
)
layout2.add_argument(
    "--ignore-layout",
    action="store_true",
    default=False,
    help="Don't try to parse the layout rules",
)
layout.add_argument(
    "--duplicate-lookups",
    choices=["first", "both"],
    default="first",
    help="How to handle duplicate lookups in the merged font",
)
fixups = parser.add_argument_group("Specialist fixups")
parser.add_argument(
    "--dotted-circle",
    action=BooleanOptionalAction,
    default=True,
    help="Merge anchors if both fonts contain a dotted circle glyph",
)


parser.add_argument("ufo1", help="UFO font file to merge into")
parser.add_argument("ufo2", help="UFO font file to merge")
parser.add_argument("--output", "-o", help="Output UFO font file")
parser.add_argument("--fea-include-dir", help="Include directory for feature files")
parser.add_argument(
    "--verbose",
    "-v",
    action="store_true",
    default=False,
    help="Increase logging verbosity",
)


def main(args=None):
    args = parser.parse_args(args)
    if args.replace_existing:
        existing_handling = "replace"
    else:
        existing_handling = "skip"  # One day we'll have "rename" as well

    if args.layout_closure:
        layout_handling = "closure"
    else:
        layout_handling = "subset"

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
        logging.getLogger("ufomerge").setLevel(logging.DEBUG)

    if not args.output:
        args.output = args.ufo1

    ufo1 = ufoLib2.Font.open(args.ufo1)
    ufo2 = ufoLib2.Font.open(args.ufo2)

    # Determine glyph set to merge
    def parse_cp(cp):
        if (
            cp.startswith("U+")
            or cp.startswith("u+")
            or cp.startswith("0x")
            or cp.startswith("0X")
        ):
            return int(cp[2:], 16)
        return int(cp)

    glyphs = set()
    if args.glyphs == "*":
        glyphs = ufo2.keys()
    elif args.glyphs_file:
        glyphs = set(open(args.glyphs_file, encoding="utf-8").read().splitlines())
    elif args.glyphs:
        glyphs = set(args.glyphs.split(","))
    if args.codepoints:
        codepoints = set(args.codepoints.split(","))
    elif args.codepoints_file:
        codepoints = set(
            open(args.codepoints_file, encoding="utf-8").read().splitlines()
        )
    else:
        codepoints = []
    if codepoints:
        codepoints = [parse_cp(cp) for cp in codepoints]

    if args.exclude_glyphs:
        exclude_glyphs = set(args.exclude_glyphs.split(","))
    elif args.exclude_glyphs_file:
        exclude_glyphs = set(
            open(args.exclude_glyphs_file, encoding="utf-8").read().splitlines()
        )
    else:
        exclude_glyphs = set()

    merge_ufos(
        ufo1,
        ufo2,
        glyphs=glyphs,
        exclude_glyphs=exclude_glyphs,
        codepoints=codepoints,
        layout_handling=layout_handling,
        duplicate_lookup_handling=args.duplicate_lookups,
        existing_handling=existing_handling,
        merge_dotted_circle_anchors=args.dotted_circle,
        include_dir=args.fea_include_dir,
    )
    ufo1.save(args.output, overwrite=True)

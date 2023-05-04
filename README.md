# ufomerge

This command line utility and Python library merges together two UFO source format fonts into a single file. It can be used to include glyphs from one font into another font. It takes care of handling:

* Glyph outlines and information
* Kerning
* `lib` entries
* Including any needed components
* "Subsetting" and merging layout rules

## Usage

To merge the glyphs of `font-b.ufo` into `font-a.ufo` and save as `merged.ufo`:

```
$ ufomerge --output merged.ufo font-a.ufo font-b.ufo
```

To include particular glyphs:

```
$ ufomerge --output merged.ufo --glyphs alpha,beta,gamma font-a.ufo font-b.ufo
```

To include glyphs referencing particular Unicode codepoints:

```
$ ufomerge --output merged.ufo --unicodes 0x03B1,0x03B2,0x03B3 font-a.ufo font-b.ufo
```

Other useful command line parameters:

* `-G`/`--glyphs-file`: Read the glyphs from a file containing one glyph per line.
* `-U`/`--codepoints-file`: Read the Unicode codepoints from a file containing one codepoint per line.
* `-x`/`--exclude-glyphs`: Stop the given glyphs from being included.
* `-v`/`--verbose`: Be noisier.

What to do about existing glyphs:

* `--skip-existing` (the default): If a glyph from `font-b` already exists in `font-a`, nothing happens.
* `--replace-existing`: If a glyph from `font-b` already exists in `font-a`, the new glyph replaces the old one.

What do to about OpenType layout (`features.fea`). Suppose there is a rule `sub A B by C;`, and the incoming glyphs are `A` and `B`:

* `--subset-layout` (the default): the rule is dropped, because `C` is not part of the target glyphset. The ligature stops working.
* `--layout-closure`: `C` is added to the target glyphset and merged into `font-a` so the ligature continues to work.
* `--ignore-layout`: No layout rules are copied from `font-b` at all.

## Usage as a Python library

`ufomerge` provides two functions, `merge_ufos` and `subset_ufo`. Both take `ufoLib2.Font` objects, and are documented in their docstrings.

## License

This software is licensed under the Apache license. See [LICENSE](LICENSE).

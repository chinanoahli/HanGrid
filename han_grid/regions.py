"""Combine the upstream regional subsets without importing unselected CJK variants."""

from fontTools import subset, unicodedata
from fontTools.ttLib.tables import otTables
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable

LANGUAGE_TAGS = {"SC": "ZHS", "TC": "ZHT", "HK": "ZHH", "JP": "JAN", "KR": "KOR"}
PUBLIC_REGIONS = {"sc": "SC", "tw": "TC", "hk": "HK", "jp": "JP", "kr": "KR"}


def public_region(region):
    return "TW" if region == "TC" else region


def union_sans(repo, ref, regions, default_region, weight):
    from build_mono import read_sans, variation_sequences
    font = read_sans(repo, ref, default_region, weight)
    priority = [default_region, *(region for region in regions if region != default_region)]
    cmap, sequences, allowed = {}, {}, set()
    regional_maps = {}
    for region in priority:
        with read_sans(repo, ref, region, weight, True) as regional:
            regional_cmap = regional.getBestCmap()
            table = regional["GSUB"].table
            script = next((record.Script for record in table.ScriptList.ScriptRecord if record.ScriptTag == "hani"), None)
            language = None
            if script:
                language = next((record.LangSys for record in script.LangSysRecord
                                 if record.LangSysTag.strip() == LANGUAGE_TAGS[region]), script.DefaultLangSys)
            pairs = {}
            if language:
                for index in language.FeatureIndex:
                    feature = table.FeatureList.FeatureRecord[index]
                    if feature.FeatureTag != "locl":
                        continue
                    for lookup_id in feature.Feature.LookupListIndex:
                        for subtable in table.LookupList.Lookup[lookup_id].SubTable:
                            pairs.update(getattr(getattr(subtable, "ExtSubTable", subtable), "mapping", {}))
            regional_maps[region] = {cp: pairs.get(glyph, glyph) for cp, glyph in regional_cmap.items()
                                     if unicodedata.script(chr(cp)) == "Hani"}
            allowed.update(regional.getGlyphOrder())
            for cp, glyph in regional_cmap.items():
                cmap.setdefault(cp, glyph)
            for sequence, glyph in variation_sequences(regional).items():
                # Resolve default UVS against that region before combining cmaps.
                sequences.setdefault(sequence, glyph or regional_cmap[sequence[0]])

    for table in font["cmap"].tables:
        if table.format in {4, 12}:
            table.cmap = {
                cp: glyph for cp, glyph in cmap.items()
                if table.format == 12 or cp <= 0xFFFF
            }
    font["cmap"].tables = [table for table in font["cmap"].tables if table.format != 14]
    uvs = CmapSubtable.newSubtable(14)
    uvs.platformID, uvs.platEncID, uvs.language = 0, 5, 0
    uvs.cmap, uvs.uvsDict = {}, {}
    for (cp, selector), glyph in sorted(sequences.items()):
        uvs.uvsDict.setdefault(selector, []).append(
            (cp, None if cmap.get(cp) == glyph else glyph)
        )
    font["cmap"].tables.append(uvs)

    # Explicitly retain only the chosen regional glyph sets. GSUB closure on
    # the full font would otherwise pull foreign localized alternates back in.
    outlines = font["CFF "]
    del font["CFF "]
    script_tags = {
        record.ScriptTag for tag in ("GSUB", "GPOS")
        for record in font[tag].table.ScriptList.ScriptRecord
    }
    scripts = [
        f"{script}.{language}" for script in sorted(script_tags)
        for language in ["dflt", *(LANGUAGE_TAGS[region] for region in regions)]
    ]
    features = {
        record.FeatureTag for tag in ("GSUB", "GPOS")
        for record in font[tag].table.FeatureList.FeatureRecord
    } - {"aalt", "calt", "liga", "pwid", "halt"}
    if "JP" not in regions:
        features -= {"jp78", "jp83", "jp90", "nlck"}
    options = subset.Options(
        layout_features=sorted(features), layout_scripts=scripts,
        layout_closure=False, name_IDs=["*"], name_languages=["*"],
        name_legacy=True, notdef_outline=True, recalc_average_width=False,
    )
    selection = subset.Subsetter(options=options)
    selection.populate(
        unicodes=set(cmap) | {selector for _, selector in sequences},
        glyphs=allowed,
    )
    selection.subset(font)
    font["CFF "] = outlines

    alias_sources = split_localization_aliases(font, cmap, regional_maps, allowed)
    for table in font["cmap"].tables:
        if table.isUnicode() and table.format != 14:
            table.cmap.update({cp: cmap[cp] for cp in table.cmap})
        if table.format == 14:
            for selector, entries in table.uvsDict.items():
                table.uvsDict[selector] = [
                    (cp, None if cmap.get(cp) == sequences[cp, selector] else sequences[cp, selector])
                    for cp, _ in entries
                ]
    set_han_localizations(font, cmap, regional_maps, default_region, alias_sources)

    if font.getBestCmap() != cmap:
        raise ValueError("Regional union changed the selected character coverage")
    if set(font.getGlyphOrder()) - allowed:
        raise ValueError("Regional union imported glyphs outside the chosen regional subsets")
    if set(variation_sequences(font)) != set(sequences):
        raise ValueError("Regional union lost variation sequences")
    return font


def split_localization_aliases(font, cmap, regional_maps, allowed):
    """Keep distinct language substitutions for Unicode aliases of a CID."""
    from collections import defaultdict

    aliases = defaultdict(list)
    for cp, glyph in cmap.items():
        if any(cp in regional for regional in regional_maps.values()):
            aliases[glyph].append(cp)
    outlines = font["CFF "].cff.topDictIndex[0]
    strings = outlines.CharStrings.charStrings
    order = list(font.getGlyphOrder())
    free = (name for name in outlines.charset if name not in allowed and name != ".notdef")
    alias_sources = {}
    for source, codepoints in aliases.items():
        profiles = defaultdict(list)
        for cp in codepoints:
            profile = tuple(regional.get(cp, source) for regional in regional_maps.values())
            profiles[profile].append(cp)
        for group in sorted(profiles.values(), key=len, reverse=True)[1:]:
            alias = next(free, None)
            if alias is None:
                raise ValueError("No free CFF CID for regional glyph aliases")
            strings[alias] = strings[source]
            alias_sources[alias] = source
            allowed.add(alias)
            order.append(alias)
            for tag in ("hmtx", "vmtx", "VORG"):
                font[tag][alias] = font[tag][source]
            for cp in group:
                cmap[cp] = alias
    font.setGlyphOrder(order)
    return alias_sources


def set_han_localizations(font, cmap, regional_maps, default_region, alias_sources):
    table = font["GSUB"].table
    for script_record in table.ScriptList.ScriptRecord:
        if script_record.ScriptTag != "hani":
            continue
        script = script_record.Script
        records = {record.LangSysTag.strip(): record.LangSys for record in script.LangSysRecord}
        for region, regional in regional_maps.items():
            tag = LANGUAGE_TAGS[region]
            targets = [records[tag]] if tag in records else []
            if region == default_region and script.DefaultLangSys:
                targets.append(script.DefaultLangSys)
            if not targets:
                continue
            replacements = alias_sources.copy()
            for cp, glyph in regional.items():
                replacements[cmap[cp]] = glyph
            replacements = {source: target for source, target in replacements.items() if source != target}
            for lang in targets:
                lang.FeatureIndex = [index for index in lang.FeatureIndex
                                     if table.FeatureList.FeatureRecord[index].FeatureTag != "locl"]
                if replacements:
                    subtable = otTables.SingleSubst()
                    subtable.mapping = replacements
                    lookup = otTables.Lookup()
                    lookup.LookupType, lookup.LookupFlag = 1, 0
                    lookup.SubTable, lookup.SubTableCount = [subtable], 1
                    record = otTables.FeatureRecord()
                    record.FeatureTag = "locl"
                    record.Feature = otTables.Feature()
                    record.Feature.FeatureParams = None
                    record.Feature.LookupListIndex = [len(table.LookupList.Lookup)]
                    record.Feature.LookupCount = 1
                    table.LookupList.Lookup.append(lookup)
                    table.LookupList.LookupCount = len(table.LookupList.Lookup)
                    lang.FeatureIndex.append(len(table.FeatureList.FeatureRecord))
                    table.FeatureList.FeatureRecord.append(record)
                    table.FeatureList.FeatureCount = len(table.FeatureList.FeatureRecord)
                lang.FeatureCount = len(lang.FeatureIndex)

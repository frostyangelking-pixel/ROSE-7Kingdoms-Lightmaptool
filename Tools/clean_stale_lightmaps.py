"""
clean_stale_lightmaps.py  --  remove orphaned per-object lightmap DDS from a map.

An older lightmap tool wrote one DDS per object/part, named <tile>_deco_<n>.dds and
<tile>_bld_<n>.dds inside each tile's LightMap folder. The current Blender baker
instead packs everything into shared atlases (Object_128_*.dds / Building_128_*.dds)
indexed by ObjectLightMapData.lit / BuildingLightMapData.lit. The client and the map
editor both read the .lit and load only the atlases, so those old per-object DDS are
unreferenced dead weight -- ~64 KB each, dozens per tile, hundreds of MB across a map.
Carrying them bloats the folder and the editor's memory footprint for nothing.

This deletes ONLY files matching the legacy per-object pattern:
        <tileX>_<tileY>_deco_<n>.dds
        <tileX>_<tileY>_bld_<n>.dds
It will NOT touch the atlases (Object_*.dds / Building_*.dds), the .lit indexes, or the
terrain maps (<tile>_PlaneLightingMap.dds).

DRY-RUN BY DEFAULT: prints what it would delete and the total reclaimed size.
Add  --apply  to actually delete.

Usage:
    python clean_stale_lightmaps.py [MAP_DIR] [--apply]
    (MAP_DIR defaults to the reincarnate map below)
"""

import os
import re
import sys

DEFAULT_MAP = r"D:\Rose Source\7Skies\7kDev_HR_002\3Ddata\Maps\Junon\reincarnate"

# Legacy per-object lightmap names: <tile>_deco_<n>.dds or <tile>_bld_<n>.dds, where
# <tile> is the NN_NN folder name. Anchored so it can never match Object_128_0.dds,
# Building_128_0.dds, a .lit, or <tile>_PlaneLightingMap.dds.
STALE_RE = re.compile(r"^\d+_\d+_(?:deco|bld)_\d+\.dds$", re.IGNORECASE)


def _lightmap_dir(tile_dir):
    for name in os.listdir(tile_dir):
        if name.lower() == "lightmap" and os.path.isdir(os.path.join(tile_dir, name)):
            return os.path.join(tile_dir, name)
    return None


def main(map_dir, apply):
    if not os.path.isdir(map_dir):
        print("Not a folder: %s" % map_dir)
        return 1

    him_tiles = sorted(os.path.splitext(f)[0]
                       for f in os.listdir(map_dir)
                       if f.lower().endswith(".him"))

    total_bytes = 0
    total_files = 0
    tiles_touched = 0
    for tile in him_tiles:
        lm_dir = _lightmap_dir(os.path.join(map_dir, tile))
        if not lm_dir:
            continue
        victims = [f for f in os.listdir(lm_dir) if STALE_RE.match(f)]
        if not victims:
            continue
        tiles_touched += 1
        tile_bytes = 0
        for f in victims:
            p = os.path.join(lm_dir, f)
            try:
                sz = os.path.getsize(p)
            except OSError:
                sz = 0
            tile_bytes += sz
            total_files += 1
            if apply:
                try:
                    os.remove(p)
                except OSError as e:
                    print("  ! could not delete %s: %s" % (p, e))
        total_bytes += tile_bytes
        print("  %s: %d file(s), %.2f MB" % (tile, len(victims), tile_bytes / 1048576.0))

    verb = "Deleted" if apply else "Would delete"
    print("\n%s %d stale per-object DDS across %d tile(s): %.1f MB reclaimed."
          % (verb, total_files, tiles_touched, total_bytes / 1048576.0))
    if not apply:
        print("Dry run only -- re-run with  --apply  to actually delete.")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    apply = "--apply" in args
    pos = [a for a in args if not a.startswith("--")]
    raise SystemExit(main(pos[0] if pos else DEFAULT_MAP, apply))

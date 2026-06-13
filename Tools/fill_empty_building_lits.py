r"""
fill_empty_building_lits.py  --  one-shot repair for already-baked ROSE maps.

The map editor loads <tile>\LightMap\OBJECTLIGHTMAPDATA.LIT and
BUILDINGLIGHTMAPDATA.LIT for every tile and throws "Missing File" if either is
absent. The baker used to skip a tile's building lit when that tile had no CNST
buildings (deco-only tiles), so the editor complains even though the client is
fine. rose_lightmap v0.4.1 now writes both lits for every tile; this script
back-fills the SAME empty lit into already-baked maps so you don't have to
re-bake just to create a few 8-byte files.

An empty .LIT is 8 bytes: int32 objectCount=0, int32 ddsCount=0 -- byte-identical
to rose_lit.Lit().to_bytes(). It means "this tile has no lightmapped objects of
this kind", which is exactly true for a deco-only (or bare) tile.

SAFE: only writes a lit that is MISSING. Never overwrites an existing one, so real
baked building/object lits are left untouched.

Usage:
    python fill_empty_building_lits.py [MAP_DIR]
    (MAP_DIR defaults to the reincarnate map below)
"""

import os
import sys

EMPTY_LIT = b"\x00" * 8            # objectCount=0, ddsCount=0

DEFAULT_MAP = r"D:\Rose Source\7Skies\7kDev_HR_002\3Ddata\Maps\Junon\reincarnate"

LIT_NAMES = ("ObjectLightMapData.lit", "BuildingLightMapData.lit")


def _lightmap_dir(tile_dir):
    """Return the tile's LightMap folder path, reusing an existing (any-case) one."""
    for name in os.listdir(tile_dir):
        if name.lower() == "lightmap" and os.path.isdir(os.path.join(tile_dir, name)):
            return os.path.join(tile_dir, name)
    return os.path.join(tile_dir, "LightMap")          # Windows FS is case-insensitive


def _has_lit(lm_dir, target):
    """True if a lit of this name already exists (any case)."""
    if not os.path.isdir(lm_dir):
        return False
    tl = target.lower()
    return any(f.lower() == tl for f in os.listdir(lm_dir))


def main(map_dir):
    if not os.path.isdir(map_dir):
        print("Not a folder: %s" % map_dir)
        return 1

    # A tile is any subfolder that has a sibling <tile>.HIM (i.e. a real map tile).
    him_tiles = {os.path.splitext(f)[0]
                 for f in os.listdir(map_dir)
                 if f.lower().endswith(".him")}

    written = 0
    tiles_touched = 0
    for tile in sorted(him_tiles):
        tile_dir = os.path.join(map_dir, tile)
        if not os.path.isdir(tile_dir):
            continue
        lm_dir = _lightmap_dir(tile_dir)
        os.makedirs(lm_dir, exist_ok=True)
        touched = False
        for litname in LIT_NAMES:
            if not _has_lit(lm_dir, litname):
                with open(os.path.join(lm_dir, litname), "wb") as f:
                    f.write(EMPTY_LIT)
                written += 1
                touched = True
                print("  + %s\\%s" % (tile, litname))
        if touched:
            tiles_touched += 1

    print("\nDone: wrote %d empty lit(s) across %d tile(s) in %s"
          % (written, tiles_touched, map_dir))
    print("Existing (real) lits were left untouched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MAP))

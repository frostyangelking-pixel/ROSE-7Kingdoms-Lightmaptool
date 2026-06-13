r"""
recompress_lightmaps_dxt1.py  --  convert uncompressed lightmap atlases to DXT1.

The Blender baker writes object/building lightmap atlases as uncompressed 32-bit
A8R8G8B8 DDS (every Object_128_0.dds is exactly 1 MB). Stock ROSE atlases are
DXT1-compressed (the same 512x512 page is ~128 KB). The map editor's object path
indexes cells inside each atlas using the LIT's division size, assuming the DXT1
block layout -- so an uncompressed atlas makes it read the wrong bytes and crash
when it "gets up to objects" (and the 8x memory bloat doesn't help a 32-bit app).

This re-encodes each uncompressed atlas to DXT1 in place: 8x smaller, and the
format the editor and client were built for. Lossy, but lightmaps are smooth so
the quality cost is negligible -- it's exactly what stock data uses.

Targets per tile:  <tile>\LightMap\Object_*.dds  and  Building_*.dds
With --terrain it also converts <tile>\<tile>_PlaneLightingMap.dds.
Files already DXT1 are skipped. DRY-RUN unless you pass --apply.

Usage:
    python recompress_lightmaps_dxt1.py [MAP_DIR] [--apply] [--terrain]
    (MAP_DIR defaults to the reincarnate map below)

Pure Python, no dependencies. Re-encoding a few hundred 512x512 pages takes a
couple of minutes; progress prints per file.
"""

import os
import re
import struct
import sys

DEFAULT_MAP = r"D:\Rose Source\7Skies\7kDev_HR_002\3Ddata\Maps\Junon\reincarnate"

ATLAS_RE = re.compile(r"^(?:Object|Building)_\d+_\d+\.dds$", re.IGNORECASE)
TERRAIN_RE = re.compile(r"^\d+_\d+_PlaneLightingMap\.dds$", re.IGNORECASE)


# --------------------------------------------------------------------------- #
#  DXT1 (BC1) encoder
# --------------------------------------------------------------------------- #
def _to565(r, g, b):
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def _encode_block(px):
    """px: 16 (r,g,b) tuples, row-major 4x4. Returns 8 bytes DXT1."""
    rmin = gmin = bmin = 255
    rmax = gmax = bmax = 0
    for (r, g, b) in px:
        if r < rmin: rmin = r
        if g < gmin: gmin = g
        if b < bmin: bmin = b
        if r > rmax: rmax = r
        if g > gmax: gmax = g
        if b > bmax: bmax = b
    c0 = _to565(rmax, gmax, bmax)   # max-corner -> always >= c1 (monotone packing)
    c1 = _to565(rmin, gmin, bmin)   # min-corner
    if c0 == c1:
        return struct.pack("<HHI", c0, c1, 0)   # flat block: all index 0 = c0
    # decode endpoints back to 8-bit for an accurate projection axis
    r0 = (c0 >> 11) & 0x1F; r0 = (r0 << 3) | (r0 >> 2)
    g0 = (c0 >> 5) & 0x3F;  g0 = (g0 << 2) | (g0 >> 4)
    b0 = c0 & 0x1F;         b0 = (b0 << 3) | (b0 >> 2)
    r1 = (c1 >> 11) & 0x1F; r1 = (r1 << 3) | (r1 >> 2)
    g1 = (c1 >> 5) & 0x3F;  g1 = (g1 << 2) | (g1 >> 4)
    b1 = c1 & 0x1F;         b1 = (b1 << 3) | (b1 >> 2)
    ar = r0 - r1; ag = g0 - g1; ab = b0 - b1
    denom = ar * ar + ag * ag + ab * ab
    if denom == 0:
        return struct.pack("<HHI", c0, c1, 0)
    # palette indices: 0=c0, 1=c1, 2=(2c0+c1)/3, 3=(c0+2c1)/3
    indices = 0
    for i, (r, g, b) in enumerate(px):
        t = ((r - r1) * ar + (g - g1) * ag + (b - b1) * ab) / denom  # 0=c1 .. 1=c0
        if t < 0.16667:
            idx = 1
        elif t < 0.5:
            idx = 3
        elif t < 0.83333:
            idx = 2
        else:
            idx = 0
        indices |= idx << (2 * i)
    return struct.pack("<HHI", c0, c1, indices)


def _encode_dxt1(width, height, body_bgra):
    """body_bgra: bytes of width*height pixels, 4 bytes each (B,G,R,A)."""
    out = bytearray((width // 4) * (height // 4) * 8)
    o = 0
    row4 = width * 4
    for by in range(0, height, 4):
        for bx in range(0, width, 4):
            block = []
            base = by * row4 + bx * 4
            for yy in range(4):
                off = base + yy * row4
                for xx in range(4):
                    p = off + xx * 4
                    block.append((body_bgra[p + 2], body_bgra[p + 1], body_bgra[p]))
            out[o:o + 8] = _encode_block(block)
            o += 8
    return bytes(out)


def _dxt1_header(w, h):
    hd = bytearray(128)
    hd[0:4] = b"DDS "
    struct.pack_into("<I", hd, 4, 124)
    struct.pack_into("<I", hd, 8, 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000)  # +LINEARSIZE
    struct.pack_into("<I", hd, 12, h)
    struct.pack_into("<I", hd, 16, w)
    struct.pack_into("<I", hd, 20, (w * h) // 2)   # DXT1 linear size
    struct.pack_into("<I", hd, 76, 32)             # pixelformat size
    struct.pack_into("<I", hd, 80, 0x4)            # DDPF_FOURCC
    hd[84:88] = b"DXT1"
    struct.pack_into("<I", hd, 108, 0x1000)        # DDSCAPS_TEXTURE
    return bytes(hd)


# --------------------------------------------------------------------------- #
#  Per-file conversion
# --------------------------------------------------------------------------- #
def _convert_file(path, apply):
    """Returns (old_size, new_size) if it's an uncompressed DDS we can convert,
    or None if skipped (already DXT1 / unexpected format)."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 128 or data[0:4] != b"DDS ":
        return None
    height = struct.unpack_from("<I", data, 12)[0]
    width = struct.unpack_from("<I", data, 16)[0]
    fourcc = data[84:88]
    bitcount = struct.unpack_from("<I", data, 88)[0]
    if fourcc == b"DXT1":
        return None                       # already done
    if fourcc in (b"DXT3", b"DXT5") or bitcount != 32:
        return None                       # not our uncompressed ARGB8888; leave it
    if width % 4 or height % 4:
        return None                       # DXT1 needs 4-aligned dims (atlases are)
    old_size = len(data)
    if not apply:
        return (old_size, 128 + (width * height) // 2)
    body = data[128:128 + width * height * 4]
    dxt = _encode_dxt1(width, height, body)
    with open(path, "wb") as f:
        f.write(_dxt1_header(width, height))
        f.write(dxt)
    return (old_size, 128 + len(dxt))


def main(map_dir, apply, do_terrain):
    if not os.path.isdir(map_dir):
        print("Not a folder: %s" % map_dir)
        return 1

    targets = []
    for tile in sorted(os.path.splitext(f)[0] for f in os.listdir(map_dir)
                       if f.lower().endswith(".him")):
        tile_dir = os.path.join(map_dir, tile)
        if not os.path.isdir(tile_dir):
            continue
        if do_terrain:
            for f in os.listdir(tile_dir):
                if TERRAIN_RE.match(f):
                    targets.append(os.path.join(tile_dir, f))
        for name in os.listdir(tile_dir):
            if name.lower() == "lightmap" and os.path.isdir(os.path.join(tile_dir, name)):
                lm = os.path.join(tile_dir, name)
                for f in os.listdir(lm):
                    if ATLAS_RE.match(f):
                        targets.append(os.path.join(lm, f))

    old_total = new_total = 0
    converted = 0
    for i, path in enumerate(targets, 1):
        res = _convert_file(path, apply)
        if res is None:
            continue
        o, n = res
        old_total += o
        new_total += n
        converted += 1
        if apply:
            print("  [%d/%d] %s  %.2f -> %.2f MB"
                  % (i, len(targets), os.path.relpath(path, map_dir),
                     o / 1048576.0, n / 1048576.0))

    verb = "Converted" if apply else "Would convert"
    print("\n%s %d atlas(es): %.1f MB -> %.1f MB  (%.1f MB saved)"
          % (verb, converted, old_total / 1048576.0, new_total / 1048576.0,
             (old_total - new_total) / 1048576.0))
    if not apply:
        print("Dry run only -- re-run with  --apply  to write DXT1 (slower; "
              "re-encodes every page).")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    apply = "--apply" in args
    do_terrain = "--terrain" in args
    pos = [a for a in args if not a.startswith("--")]
    raise SystemExit(main(pos[0] if pos else DEFAULT_MAP, apply, do_terrain))

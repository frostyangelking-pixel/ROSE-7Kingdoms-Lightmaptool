"""
rose_zms.py  --  ROSE / 7Kingdoms ".ZMS" mesh reader (pure-Python).

A ZMS is one renderable mesh -- the geometry a ZSC part points at. For the lightmap
pipeline we need each mesh's vertices (position, normal, the texture UV and, when
present, the second/lightmap UV) and its triangles, so the object can be assembled,
placed by the IFO transform, and baked.

FORMAT (ZMS0007; verified against the editor's ZMS.cs reader and against real JPT
meshes -- all parse to exact EOF):

    little-endian throughout
    char[8] magic                       # "ZMS0007\0"
    int32   format                      # bitfield, see FMT_* below
    float[3] bbox_min, float[3] bbox_max
    int16   bone_count;  int16 bone_lut[bone_count]
    int16   vertex_count
    if format & POSITION(2):   vertex_count x vec3   position
    if format & NORMAL(4):     vertex_count x vec3   normal
    if format & COLOR(8):      vertex_count x uint32 colour            (skipped)
    if format & BONE_W(16):    vertex_count x 4*float bone weights     (skipped)
    if format & BONE_I(32):    vertex_count x 4*int16 bone indices     (skipped)
    if format & TANGENT(64):   vertex_count x vec3   tangent           (skipped)
    for each UV map present (UV1 128, UV2 256, UV3 512, UV4 1024):
                               vertex_count x vec2   uv
                               # map 0 -> texture uv, map 1 -> lightmap uv
    int16   face_count
    face_count x (int16 a, int16 b, int16 c)         # triangle list
    int16   _pool ; int16 strip_count ; strip_count x int16   # strip list (skipped)

Static props (church/houses/trees) have bone_count 0 and format 0x186
(POSITION|NORMAL|UV1|UV2): they already carry a lightmap UV channel.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

FMT_POSITION = 2
FMT_NORMAL = 4
FMT_COLOR = 8
FMT_BONE_WEIGHT = 16
FMT_BONE_INDEX = 32
FMT_TANGENT = 64
FMT_UV1 = 128
FMT_UV2 = 256
FMT_UV3 = 512
FMT_UV4 = 1024

Vec2 = Tuple[float, float]
Vec3 = Tuple[float, float, float]


@dataclass
class Zms:
    magic: str = ""
    format: int = 0
    bbox_min: Vec3 = (0.0, 0.0, 0.0)
    bbox_max: Vec3 = (0.0, 0.0, 0.0)
    bone_count: int = 0
    positions: List[Vec3] = field(default_factory=list)
    normals: List[Vec3] = field(default_factory=list)
    uv1: List[Vec2] = field(default_factory=list)   # texture coordinates
    uv2: List[Vec2] = field(default_factory=list)   # lightmap coordinates (if present)
    faces: List[Tuple[int, int, int]] = field(default_factory=list)
    consumed: int = 0
    file_size: int = 0

    @property
    def vertex_count(self) -> int:
        return len(self.positions)

    @property
    def has_lightmap_uv(self) -> bool:
        return bool(self.uv2)

    @property
    def exact(self) -> bool:
        return self.consumed == self.file_size

    @property
    def faces_in_range(self) -> bool:
        n = self.vertex_count
        return all(0 <= i < n for tri in self.faces for i in tri)

    @classmethod
    def read(cls, path: str) -> "Zms":
        with open(path, "rb") as f:
            return cls.from_bytes(f.read())

    @classmethod
    def from_bytes(cls, data: bytes) -> "Zms":
        m = cls(file_size=len(data))
        m.magic = data[:8].split(b"\x00")[0].decode("latin-1")
        if not m.magic.startswith("ZMS"):
            raise ValueError(f"Not a ZMS file (magic={m.magic!r})")

        p = 8
        m.format = struct.unpack_from("<i", data, p)[0]; p += 4
        m.bbox_min = struct.unpack_from("<3f", data, p); p += 12
        m.bbox_max = struct.unpack_from("<3f", data, p); p += 12
        m.bone_count = struct.unpack_from("<h", data, p)[0]; p += 2
        p += m.bone_count * 2                       # bone LUT

        vc = struct.unpack_from("<h", data, p)[0]; p += 2
        fmt = m.format

        if fmt & FMT_POSITION:
            for _ in range(vc):
                m.positions.append(struct.unpack_from("<3f", data, p)); p += 12
        if fmt & FMT_NORMAL:
            for _ in range(vc):
                m.normals.append(struct.unpack_from("<3f", data, p)); p += 12
        if fmt & FMT_COLOR:
            p += 4 * vc                             # uint32 colour, not needed
        if fmt & FMT_BONE_WEIGHT:
            p += 16 * vc                            # 4 floats
        if fmt & FMT_BONE_INDEX:
            p += 8 * vc                             # 4 int16
        if fmt & FMT_TANGENT:
            p += 12 * vc                            # vec3

        # UV maps in order; first -> texture, second -> lightmap, rest skipped.
        uv_index = 0
        for bit in (FMT_UV1, FMT_UV2, FMT_UV3, FMT_UV4):
            if fmt & bit:
                target = m.uv1 if uv_index == 0 else (m.uv2 if uv_index == 1 else None)
                for _ in range(vc):
                    uv = struct.unpack_from("<2f", data, p); p += 8
                    if target is not None:
                        target.append(uv)
                uv_index += 1

        face_count = struct.unpack_from("<h", data, p)[0]; p += 2
        for _ in range(face_count):
            m.faces.append(struct.unpack_from("<3h", data, p)); p += 6

        # Trailing strip list (a short, then a counted short array) -- skip to EOF.
        if p + 4 <= len(data):
            p += 2                                  # pool / material short
            strip_count = struct.unpack_from("<h", data, p)[0]; p += 2
            p += strip_count * 2

        m.consumed = p
        return m


# --------------------------------------------------------------------------- #
#  CLI self-test / validator
#    python rose_zms.py <file.zms>
# --------------------------------------------------------------------------- #
def _main(argv: List[str]) -> int:
    if not argv:
        print(__doc__)
        print("usage: python rose_zms.py <file.zms>")
        return 1

    m = Zms.read(argv[0])
    flags = [n for n, b in (("POS", FMT_POSITION), ("NORMAL", FMT_NORMAL),
                            ("COLOR", FMT_COLOR), ("BONE_W", FMT_BONE_WEIGHT),
                            ("BONE_I", FMT_BONE_INDEX), ("TANGENT", FMT_TANGENT),
                            ("UV1", FMT_UV1), ("UV2", FMT_UV2),
                            ("UV3", FMT_UV3), ("UV4", FMT_UV4)) if m.format & b]
    print(f"Parsed: magic={m.magic!r} format=0x{m.format:x} [{', '.join(flags)}]")
    print(f"  {m.vertex_count} verts, {len(m.faces)} faces, bones={m.bone_count}, "
          f"lightmap_uv={m.has_lightmap_uv}")
    print(f"  bbox min={tuple(round(c,2) for c in m.bbox_min)} "
          f"max={tuple(round(c,2) for c in m.bbox_max)}")
    print(f"  EOF check: {m.exact} ({m.consumed}/{m.file_size} bytes)   "
          f"faces in range: {m.faces_in_range}")
    if m.positions:
        print(f"  vert[0] pos={tuple(round(c,3) for c in m.positions[0])}"
              + (f" uv1={tuple(round(c,3) for c in m.uv1[0])}" if m.uv1 else "")
              + (f" uv2={tuple(round(c,3) for c in m.uv2[0])}" if m.uv2 else ""))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))

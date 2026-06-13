"""
rose_lit.py  --  ROSE / 7Kingdoms ".LIT" lightmap-index reader & writer.

This is the modern, dependency-free replacement for the lost `litter.exe`.
It reads and writes the binary `.LIT` files the client consumes
(`ObjectLightMapData.lit` and `BuildingLightMapData.lit`) byte-for-byte.

FORMAT  (verified three ways: the XNA editor's LIT.cs read+write, the original
generator TextureMergeTools/MakeLightMapInfo.cs, and File Fotmats.txt -- all three
agree on the layout):

    little-endian throughout
    int32   objectCount
    repeat objectCount:
        int32   partCount
        int32   objectID
        repeat partCount:
            BSTR    name              # per-part source/TGA name
            int32   partID            # object sub-id
            BSTR    ddsName           # merged lightmap atlas (DDS) for this part
            int32   lightmapID        # index into the DDS list at the end
            int32   pixelsPerObject   # atlas cell size (the part's texture width)
            int32   objectsPerWidth   # atlas grid width (cells across)
            int32   mapPosition       # cell/UV index within the atlas
    int32   ddsCount
    repeat ddsCount:
        BSTR    ddsFileName

    BSTR = 1-byte unsigned length prefix, then that many bytes in EUC-KR
           (a.k.a. ks_c_5601-1987). Note: .NET's BinaryWriter.Write(string) writes
           a 7-bit-encoded length, which is byte-identical to a single length byte
           for any string under 128 bytes -- and every real lightmap/TGA name is.
           So a 1-byte prefix round-trips all real files exactly.

The client ships one ObjectLightMapData.lit + one BuildingLightMapData.lit per zone,
alongside the merged lightmap DDS atlases they reference, packed into the zone VFS.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List, BinaryIO

# The Korean codec the original tool and the editor both use for LIT strings.
LIT_ENCODING = "euc-kr"

# Canonical per-zone output filenames.
OBJECT_LIT_NAME = "ObjectLightMapData.lit"
BUILDING_LIT_NAME = "BuildingLightMapData.lit"


# --------------------------------------------------------------------------- #
#  Low-level primitives
# --------------------------------------------------------------------------- #
def _read_exact(f: BinaryIO, n: int) -> bytes:
    b = f.read(n)
    if len(b) != n:
        raise EOFError(f"Unexpected end of file: wanted {n} bytes, got {len(b)}")
    return b


def read_int(f: BinaryIO) -> int:
    """Read a signed little-endian 32-bit int (matches C# `int` / DWORD use here)."""
    return struct.unpack("<i", _read_exact(f, 4))[0]


def write_int(f: BinaryIO, value: int) -> None:
    f.write(struct.pack("<i", value))


def read_bstr(f: BinaryIO, encoding: str = LIT_ENCODING) -> str:
    """Read a BSTR: 1-byte length prefix, then that many EUC-KR bytes."""
    length = _read_exact(f, 1)[0]
    if length == 0:
        return ""
    raw = _read_exact(f, length)
    return raw.decode(encoding, errors="replace")


def write_bstr(f: BinaryIO, value: str, encoding: str = LIT_ENCODING) -> None:
    raw = (value or "").encode(encoding)
    if len(raw) > 255:
        raise ValueError(
            f"BSTR too long for a 1-byte length prefix ({len(raw)} bytes): {value!r}"
        )
    f.write(bytes([len(raw)]))
    f.write(raw)


# --------------------------------------------------------------------------- #
#  Data model
# --------------------------------------------------------------------------- #
@dataclass
class LitPart:
    name: str = ""              # per-part source/TGA name (LIT.cs: TGAName)
    part_id: int = 0            # object sub-id          (LIT.cs: PartID)
    dds_name: str = ""          # merged lightmap DDS     (LIT.cs: DDSName)
    lightmap_id: int = 0        # index into Lit.dds      (LIT.cs: LightmapID)
    pixels_per_object: int = 0  # atlas cell px / tex w   (LIT.cs: PixelsPerObject)
    objects_per_width: int = 0  # atlas grid width        (LIT.cs: ObjectsPerWidth)
    map_position: int = 0       # cell/UV index in atlas  (LIT.cs: MapPosition)


@dataclass
class LitObject:
    object_id: int = 0
    parts: List[LitPart] = field(default_factory=list)


@dataclass
class Lit:
    objects: List[LitObject] = field(default_factory=list)
    dds: List[str] = field(default_factory=list)   # flat DDS filename list

    # ----- read ----- #
    @classmethod
    def read(cls, path: str) -> "Lit":
        with open(path, "rb") as f:
            return cls.read_stream(f)

    @classmethod
    def read_stream(cls, f: BinaryIO) -> "Lit":
        lit = cls()
        object_count = read_int(f)
        for _ in range(object_count):
            part_count = read_int(f)
            obj = LitObject(object_id=read_int(f))
            for _ in range(part_count):
                obj.parts.append(
                    LitPart(
                        name=read_bstr(f),
                        part_id=read_int(f),
                        dds_name=read_bstr(f),
                        lightmap_id=read_int(f),
                        pixels_per_object=read_int(f),
                        objects_per_width=read_int(f),
                        map_position=read_int(f),
                    )
                )
            lit.objects.append(obj)

        dds_count = read_int(f)
        for _ in range(dds_count):
            lit.dds.append(read_bstr(f))
        return lit

    # ----- write ----- #
    def write(self, path: str) -> None:
        with open(path, "wb") as f:
            self.write_stream(f)

    def write_stream(self, f: BinaryIO) -> None:
        write_int(f, len(self.objects))
        for obj in self.objects:
            write_int(f, len(obj.parts))
            write_int(f, obj.object_id)
            for p in obj.parts:
                write_bstr(f, p.name)
                write_int(f, p.part_id)
                write_bstr(f, p.dds_name)
                write_int(f, p.lightmap_id)
                write_int(f, p.pixels_per_object)
                write_int(f, p.objects_per_width)
                write_int(f, p.map_position)
        write_int(f, len(self.dds))
        for name in self.dds:
            write_bstr(f, name)

    def to_bytes(self) -> bytes:
        import io
        buf = io.BytesIO()
        self.write_stream(buf)
        return buf.getvalue()

    # ----- convenience ----- #
    def summary(self) -> str:
        n_parts = sum(len(o.parts) for o in self.objects)
        return (
            f"{len(self.objects)} objects, {n_parts} parts, "
            f"{len(self.dds)} DDS atlases"
        )


# --------------------------------------------------------------------------- #
#  CLI self-test / validator
#    python rose_lit.py <file.lit>          -> parse, summarise, round-trip check
#    python rose_lit.py <file.lit> --dump   -> also list every object/part
# --------------------------------------------------------------------------- #
def _main(argv: List[str]) -> int:
    import io

    if not argv:
        print(__doc__)
        print("usage: python rose_lit.py <file.lit> [--dump]")
        return 1

    path = argv[0]
    dump = "--dump" in argv[1:]

    with open(path, "rb") as f:
        original = f.read()

    lit = Lit.read_stream(io.BytesIO(original))
    print(f"Parsed OK: {lit.summary()}")

    # Round-trip: re-serialise and compare to the original bytes.
    rebuilt = lit.to_bytes()
    if rebuilt == original:
        print(f"ROUND-TRIP EXACT: {len(rebuilt)} bytes identical. Format confirmed.")
    else:
        print(
            f"ROUND-TRIP DIFFERS: original {len(original)} vs rebuilt {len(rebuilt)} bytes."
        )
        limit = min(len(original), len(rebuilt))
        for i in range(limit):
            if original[i] != rebuilt[i]:
                lo, hi = max(0, i - 8), i + 8
                print(f"  first diff at byte {i}:")
                print(f"    original: {original[lo:hi].hex(' ')}")
                print(f"    rebuilt : {rebuilt[lo:hi].hex(' ')}")
                break

    if dump:
        for oi, obj in enumerate(lit.objects):
            print(f"[obj {oi}] id={obj.object_id} parts={len(obj.parts)}")
            for p in obj.parts:
                print(
                    f"    part {p.part_id}: '{p.name}' -> dds[{p.lightmap_id}]"
                    f" '{p.dds_name}'  cell={p.pixels_per_object}px"
                    f" gridW={p.objects_per_width} pos={p.map_position}"
                )
        for di, name in enumerate(lit.dds):
            print(f"  dds[{di}] = {name}")

    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))

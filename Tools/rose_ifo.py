"""
rose_ifo.py  --  ROSE / 7Kingdoms ".IFO" map-info / object-placement reader.

Pure-Python, dependency-free. Reads the per-tile `.IFO` that lists every object
placed on that map tile: decorations, buildings, NPCs, effects, spawns, water, etc.

For the lightmap pipeline the blocks that matter are DECORATIONS (1) and
BUILDINGS (3): each entry's `object_id` indexes the matching ZSC model list
(LIST_DECO_* for decorations, LIST_CNST_* for buildings), and the entry's
**1-based order within its block** is the `objectID` written into the `.LIT`
(per HeightsMapLoad.ms). Position / rotation / scale place the model in the world.

FORMAT (from File Fotmats.txt, validated against real JPT01 tiles):

    little-endian throughout
    int32   block_count
    repeat block_count:                 # header table
        int32   block_type              # see BlockType
        int32   block_offset            # absolute file offset of the block body

    # Each block body lives at its own offset, so blocks are parsed by seeking;
    # they need not be contiguous. Bodies:

    ECONOMYDATA(0):  int32 width, int32 height, int32 map_cell_x, int32 map_cell_y,
                     float[16] unused_matrix, BSTR block_name
    DECORATIONS(1):  basic-list
    NPCSPAWNS(2):    basic-list, each entry +int32 ai_pattern_index +BSTR con_file
    BUILDINGS(3):    basic-list
    SOUNDEFFECTS(4): basic-list, each entry +BSTR path +int32 range +int32 interval
    EFFECTS(5):      basic-list, each entry +BSTR path
    ANIMATABLES(6):  basic-list
    WATERBIG(7):     int32 x_count, int32 y_count, grid[x*y] of
                     (byte use, float height, int32 type, int32 index, int32 reserved)
    MONSTERSPAWNS(8):int32 spawn_count, per spawn: basic-entry + BSTR name +
                     basic-mob list + tactic-mob list + 4 int32 params
    WATERPLANES(9):  float water_size, int32 entry_count, per entry: vec3 start, vec3 end
    WARPGATES(10):   basic-list
    COLLISIONBLOCK(11): basic-list
    TRIGGERS(12):    basic-list, each entry +BSTR qsd_trigger +BSTR lua_trigger

    basic-list:
        int32 entry_count
        repeat entry_count:
            BSTR     str_data
            uint16   warp_id
            uint16   event_id
            int32    obj_type
            int32    obj_id            # -> index into the zone's ZSC model list
            int32    map_pos_x
            int32    map_pos_y
            float[4] rotation          # quaternion (file order treated as x,y,z,w)
            float[3] position          # raw; transformed to world (see below)
            float[3] scale

    BSTR = 1-byte length prefix, then that many bytes. Names here are ASCII model
           ids; decoded permissively (EUC-KR fallback) like the rest of the toolset.

Position transform (per the editor's IFO.cs): the stored X/Y get a +520000 origin
offset and /100 (cm->m); Z is just /100. Applied on read so `position` is world space.

NOTE on NPC/sound/effect/trigger blocks: File Fotmats.txt lists the per-block extra
fields *after* the basic-list call, but in real files they are **per entry** (each
NPC carries its own AI pattern + CON file, etc.). We read them inside the loop;
the per-block offset cross-check in the validator confirms this on real data.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import BinaryIO, List, Optional

# Permissive decode: model ids are ASCII; EUC-KR is the toolset-wide fallback.
IFO_ENCODING = "euc-kr"


class BlockType(IntEnum):
    ECONOMYDATA = 0
    DECORATIONS = 1
    NPCSPAWNS = 2
    BUILDINGS = 3
    SOUNDEFFECTS = 4
    EFFECTS = 5
    ANIMATABLES = 6
    WATERBIG = 7
    MONSTERSPAWNS = 8
    WATERPLANES = 9
    WARPGATES = 10
    COLLISIONBLOCK = 11
    TRIGGERS = 12


# --------------------------------------------------------------------------- #
#  Low-level primitives
# --------------------------------------------------------------------------- #
def _read_exact(f: BinaryIO, n: int) -> bytes:
    b = f.read(n)
    if len(b) != n:
        raise EOFError(f"Unexpected end of file: wanted {n} bytes, got {len(b)}")
    return b


def read_i32(f: BinaryIO) -> int:
    return struct.unpack("<i", _read_exact(f, 4))[0]


def read_u16(f: BinaryIO) -> int:
    return struct.unpack("<H", _read_exact(f, 2))[0]


def read_u8(f: BinaryIO) -> int:
    return _read_exact(f, 1)[0]


def read_f32(f: BinaryIO) -> float:
    return struct.unpack("<f", _read_exact(f, 4))[0]


def read_vec3(f: BinaryIO) -> tuple:
    return struct.unpack("<3f", _read_exact(f, 12))


def read_quat(f: BinaryIO) -> tuple:
    # File order; treated as (x, y, z, w) by convention -- confirm when wiring
    # the Blender transform. Always a unit quaternion in valid data.
    return struct.unpack("<4f", _read_exact(f, 16))


def read_bstr(f: BinaryIO, encoding: str = IFO_ENCODING) -> str:
    length = read_u8(f)
    if length == 0:
        return ""
    return _read_exact(f, length).decode(encoding, errors="replace")


# --------------------------------------------------------------------------- #
#  Data model
# --------------------------------------------------------------------------- #
@dataclass
class IfoEntry:
    """One placed object (the 'basic type' shared by most blocks)."""
    index: int = 0                 # 1-based order within its block (= LIT objectID)
    name: str = ""
    warp_id: int = 0
    event_id: int = 0
    obj_type: int = 0
    obj_id: int = 0                # index into the zone's ZSC model list
    map_pos_x: int = 0
    map_pos_y: int = 0
    rotation: tuple = (0.0, 0.0, 0.0, 1.0)   # quaternion (x, y, z, w)
    position: tuple = (0.0, 0.0, 0.0)        # world space (transform applied on read)
    scale: tuple = (1.0, 1.0, 1.0)
    # block-specific extras (populated only for the relevant block types):
    ai_pattern_index: Optional[int] = None
    con_file: Optional[str] = None
    sound_path: Optional[str] = None
    sound_range: Optional[int] = None
    sound_interval: Optional[int] = None
    effect_path: Optional[str] = None
    qsd_trigger: Optional[str] = None
    lua_trigger: Optional[str] = None


@dataclass
class IfoBlock:
    block_type: int = 0
    offset: int = 0
    end: int = 0                              # where parsing actually stopped
    boundary: int = 0                         # next block offset / EOF (expected end)
    parsed: bool = True                       # False if the body failed to decode
    error: str = ""                           # decode error message, if any
    entries: List[IfoEntry] = field(default_factory=list)
    raw: dict = field(default_factory=dict)   # parsed special-block fields
    raw_bytes: bytes = b""                    # captured body when parsed is False

    @property
    def type_name(self) -> str:
        try:
            return BlockType(self.block_type).name
        except ValueError:
            return f"UNKNOWN({self.block_type})"

    @property
    def exact(self) -> bool:
        """True if the body decoded and ended exactly on the next-block boundary."""
        return self.parsed and self.end == self.boundary


@dataclass
class Ifo:
    blocks: List[IfoBlock] = field(default_factory=list)

    # ----- convenient accessors ----- #
    def block(self, t: BlockType) -> Optional[IfoBlock]:
        for b in self.blocks:
            if b.block_type == int(t):
                return b
        return None

    @property
    def decorations(self) -> List[IfoEntry]:
        b = self.block(BlockType.DECORATIONS)
        return b.entries if b else []

    @property
    def buildings(self) -> List[IfoEntry]:
        b = self.block(BlockType.BUILDINGS)
        return b.entries if b else []

    # ----- read ----- #
    @classmethod
    def read(cls, path: str) -> "Ifo":
        with open(path, "rb") as f:
            return cls.read_stream(f)

    @classmethod
    def read_stream(cls, f: BinaryIO) -> "Ifo":
        ifo = cls()
        f.seek(0, 2)
        file_size = f.tell()
        f.seek(0)

        block_count = read_i32(f)
        header = []
        for _ in range(block_count):
            btype = read_i32(f)
            boffset = read_i32(f)
            header.append((btype, boffset))

        all_offsets = sorted({o for _, o in header})
        for btype, boffset in header:
            # Boundary = next-larger offset in the file, else EOF.
            boundary = file_size
            for o in all_offsets:
                if o > boffset:
                    boundary = o
                    break
            block = IfoBlock(block_type=btype, offset=boffset, boundary=boundary)
            f.seek(boffset)
            try:
                _read_block_body(f, block)
                block.end = f.tell()
            except Exception as exc:  # resilient: a hard block must not kill the rest
                block.parsed = False
                block.error = f"{type(exc).__name__}: {exc}"
                f.seek(boffset)
                block.raw_bytes = f.read(boundary - boffset)
                block.end = f.tell()
            ifo.blocks.append(block)
        return ifo


def _read_basic_entry(f: BinaryIO, index: int) -> IfoEntry:
    name = read_bstr(f)
    warp_id = read_u16(f)
    event_id = read_u16(f)
    obj_type = read_i32(f)
    obj_id = read_i32(f)
    map_pos_x = read_i32(f)
    map_pos_y = read_i32(f)
    rotation = read_quat(f)
    # Placement position transform (per the editor's IFO.cs): X/Y carry a +520000
    # origin offset and a /100 (cm->m) scale; Z is just /100.
    rx, ry, rz = read_vec3(f)
    position = ((rx + 520000.0) / 100.0, (ry + 520000.0) / 100.0, rz / 100.0)
    scale = read_vec3(f)
    return IfoEntry(
        index=index, name=name, warp_id=warp_id, event_id=event_id,
        obj_type=obj_type, obj_id=obj_id, map_pos_x=map_pos_x, map_pos_y=map_pos_y,
        rotation=rotation, position=position, scale=scale,
    )


def _read_block_body(f: BinaryIO, block: IfoBlock) -> None:
    t = block.block_type

    if t == BlockType.ECONOMYDATA:
        block.raw["width"] = read_i32(f)
        block.raw["height"] = read_i32(f)
        block.raw["map_cell_x"] = read_i32(f)
        block.raw["map_cell_y"] = read_i32(f)
        matrix = struct.unpack("<16f", _read_exact(f, 64))   # tile world transform
        block.raw["matrix"] = matrix
        # Row-major; translation is the last row (M41, M42, M43). ROSE is Y-up, so
        # the horizontal world origin of the tile is (M41, M43); same frame as the
        # placement positions, so objects sit on terrain when both use it.
        block.raw["world_translation"] = (matrix[12], matrix[13], matrix[14])
        block.raw["block_name"] = read_bstr(f)
        return

    if t == BlockType.WATERBIG:
        xc = read_i32(f)
        yc = read_i32(f)
        block.raw["x_count"] = xc
        block.raw["y_count"] = yc
        cells = []
        for _ in range(xc * yc):
            cells.append((
                read_u8(f),     # use
                read_f32(f),    # height
                read_i32(f),    # water_type
                read_i32(f),    # water_index
                read_i32(f),    # reserved
            ))
        block.raw["cells"] = cells
        return

    if t == BlockType.WATERPLANES:
        block.raw["water_size"] = read_f32(f)   # undocumented leading float
        n = read_i32(f)
        planes = []
        for _ in range(n):
            planes.append((read_vec3(f), read_vec3(f)))
        block.raw["planes"] = planes
        block.raw["count"] = n
        return

    if t == BlockType.MONSTERSPAWNS:
        n = read_i32(f)
        spawns = []
        for i in range(n):
            e = _read_basic_entry(f, i + 1)        # spawn point = a basic placement
            name = read_bstr(f)
            basic = []
            for _ in range(read_i32(f)):
                basic.append((read_bstr(f), read_i32(f), read_i32(f)))  # name,id,count
            tactic = []
            for _ in range(read_i32(f)):
                tactic.append((read_bstr(f), read_i32(f), read_i32(f)))
            interval = read_i32(f)
            limit = read_i32(f)
            rng = read_i32(f)
            tactic_points = read_i32(f)
            block.entries.append(e)
            spawns.append({
                "name": name, "basic": basic, "tactic": tactic,
                "interval": interval, "limit": limit,
                "range": rng, "tactic_points": tactic_points,
            })
        block.raw["spawns"] = spawns
        return

    # Everything else is a basic-list, some with per-entry extras.
    n = read_i32(f)
    for i in range(n):
        e = _read_basic_entry(f, i + 1)
        if t == BlockType.NPCSPAWNS:
            e.ai_pattern_index = read_i32(f)
            e.con_file = read_bstr(f)
        elif t == BlockType.SOUNDEFFECTS:
            e.sound_path = read_bstr(f)
            e.sound_range = read_i32(f)
            e.sound_interval = read_i32(f)
        elif t == BlockType.EFFECTS:
            e.effect_path = read_bstr(f)
        elif t == BlockType.TRIGGERS:
            e.qsd_trigger = read_bstr(f)
            e.lua_trigger = read_bstr(f)
        block.entries.append(e)


# --------------------------------------------------------------------------- #
#  CLI self-test / validator
#    python rose_ifo.py <file.ifo> [--dump]
# --------------------------------------------------------------------------- #
def _main(argv: List[str]) -> int:
    if not argv:
        print(__doc__)
        print("usage: python rose_ifo.py <file.ifo> [--dump]")
        return 1

    path = argv[0]
    dump = "--dump" in argv[1:]

    ifo = Ifo.read(path)
    n_entries = sum(len(b.entries) for b in ifo.blocks)
    print(f"Parsed: {len(ifo.blocks)} blocks, {n_entries} placed entries")
    print(f"  decorations: {len(ifo.decorations)}   buildings: {len(ifo.buildings)}")

    for b in ifo.blocks:
        if b.parsed:
            mark = "OK  " if b.exact else "WARN"
            if b.entries:
                detail = f"{len(b.entries)} entries"
            elif b.raw:
                detail = ", ".join(
                    f"{k}={v}" for k, v in b.raw.items() if not isinstance(v, list)
                ) or f"{len(b.raw)} fields"
            else:
                detail = "empty"
            tail = "" if b.exact else f"  (end 0x{b.end:x} != next 0x{b.boundary:x})"
        else:
            mark = "SKIP"
            detail = f"unparsed {len(b.raw_bytes)}B -- {b.error}"
            tail = ""
        print(f"  [{mark}] @0x{b.offset:06x} [{b.block_type:2}] "
              f"{b.type_name:<14} {detail}{tail}")

    placement_ok = all(
        b.exact for b in ifo.blocks
        if b.block_type in (BlockType.DECORATIONS, BlockType.BUILDINGS)
    )
    print(f"  => placement blocks (DECORATIONS+BUILDINGS) byte-exact: {placement_ok}")

    if dump:
        for label, entries in (("DECORATIONS", ifo.decorations),
                               ("BUILDINGS", ifo.buildings)):
            print(f"\n--- {label} ---")
            for e in entries:
                qn = sum(c * c for c in e.rotation) ** 0.5
                print(
                    f"  #{e.index:3} obj_id={e.obj_id:4} "
                    f"pos=({e.position[0]:.1f},{e.position[1]:.1f},{e.position[2]:.1f}) "
                    f"scale=({e.scale[0]:.2f},{e.scale[1]:.2f},{e.scale[2]:.2f}) "
                    f"|q|={qn:.3f} '{e.name}'"
                )
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))

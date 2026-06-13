"""
rose_stb.py  --  ROSE / 7Kingdoms ".STB" table reader (pure-Python).

STB is ROSE's generic spreadsheet/table format. The whole game data set is STBs:
items, NPCs, skills -- and, the one that matters for the lightmap pipeline,
`stb\\LIST_ZONE.STB`, the master zone table. Each zone row gives its `.zon` path,
the Decoration table (LIST_DECO_*.ZSC = placeable objects), the Building table
(LIST_CNST_*.ZSC = buildings), the minimap, and the Terrain Type (0 = outdoor /
field, 1 = underground) -- which is exactly the outdoor-sun+sky vs indoor-placed-
lights switch the bake recipe needs. So: point the tool at a 3Ddata ROOT, read
LIST_ZONE.STB, and every map plus its model/building tables enumerates itself.

FORMAT (File Fotmats.txt, validated byte-exact against a real LIST_ZONE.STB --
the pre-data section ends exactly on data_offset and the grid ends exactly on EOF):

    little-endian throughout
    char[4] magic                 # "STB1"
    uint32  data_offset           # absolute offset of the cell grid
    uint32  row_count             # includes the leading type/hint row
    uint32  column_count          # includes the (hidden) root/key column
    uint32  row_height
    uint16  root_column_width
    uint16  column_width[column_count]
    SSTR    root_column_title
    SSTR    column_title[column_count - 1]      # the visible data-column headers
    SSTR    root_data                           # key column, row 0
    SSTR    first_cell_data[row_count - 1]      # key column, remaining rows
    :SEEK data_offset
    repeat row_count - 1:
        repeat column_count - 1:
            SSTR cell

    SSTR = 2-byte little-endian length prefix, then that many bytes.

So the grid is (row_count - 1) rows x (column_count - 1) columns; grid row 0 is the
type/hint row ("string", "0: Outdoor, 1: Underground", ...). column_title[i] names
grid column i. The key column (row ids) lives before data_offset and is exposed as
`keys` but isn't usually needed.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional

STB_ENCODING = "euc-kr"   # toolset-wide fallback; titles/paths here are ASCII


def _u16(b: bytes, p: int) -> int:
    return struct.unpack_from("<H", b, p)[0]


def _u32(b: bytes, p: int) -> int:
    return struct.unpack_from("<I", b, p)[0]


def _sstr(b: bytes, p: int):
    n = _u16(b, p)
    return b[p + 2:p + 2 + n].decode(STB_ENCODING, errors="replace"), p + 2 + n


@dataclass
class Stb:
    magic: str = ""
    row_height: int = 0
    titles: List[str] = field(default_factory=list)   # data-column headers
    keys: List[str] = field(default_factory=list)     # root/key column, per row
    rows: List[List[str]] = field(default_factory=list)  # grid; row 0 = hint row
    consumed: int = 0                                  # bytes read (== file size)
    file_size: int = 0

    @property
    def exact(self) -> bool:
        return self.consumed == self.file_size

    # ----- read ----- #
    @classmethod
    def read(cls, path: str) -> "Stb":
        with open(path, "rb") as f:
            return cls.from_bytes(f.read())

    @classmethod
    def from_bytes(cls, data: bytes) -> "Stb":
        stb = cls(file_size=len(data))
        stb.magic = data[:4].decode("latin-1")
        if not stb.magic.startswith("STB"):
            raise ValueError(f"Not an STB file (magic={stb.magic!r})")
        data_offset = _u32(data, 4)
        row_count = _u32(data, 8)
        column_count = _u32(data, 12)
        stb.row_height = _u32(data, 16)

        p = 20
        p += 2                       # root_column_width
        p += 2 * column_count        # column widths

        _root_title, p = _sstr(data, p)
        for _ in range(column_count - 1):
            t, p = _sstr(data, p)
            stb.titles.append(t)

        for _ in range(row_count):   # root_data + (row_count-1) first cells
            k, p = _sstr(data, p)
            stb.keys.append(k)

        if p != data_offset:
            # Pre-data section didn't line up; surface it rather than silently drift.
            raise ValueError(
                f"STB pre-data ended at 0x{p:x}, expected data_offset 0x{data_offset:x}"
            )

        p = data_offset
        ncols = column_count - 1
        for _ in range(row_count - 1):
            row = []
            for _ in range(ncols):
                c, p = _sstr(data, p)
                row.append(c)
            stb.rows.append(row)
        stb.consumed = p
        return stb

    # ----- access ----- #
    def col(self, title: str) -> int:
        """Index of the first column whose header contains `title` (case-insensitive)."""
        title = title.lower()
        for i, t in enumerate(self.titles):
            if title in t.lower():
                return i
        return -1

    def row_dict(self, i: int) -> Dict[str, str]:
        return {t: (self.rows[i][j] if j < len(self.rows[i]) else "")
                for j, t in enumerate(self.titles)}

    def cell(self, row: int, title: str) -> str:
        j = self.col(title)
        if j < 0 or row >= len(self.rows):
            return ""
        return self.rows[row][j] if j < len(self.rows[row]) else ""


# --------------------------------------------------------------------------- #
#  Zone-table convenience (LIST_ZONE.STB)
# --------------------------------------------------------------------------- #
@dataclass
class ZoneInfo:
    row: int                 # grid row index (1 = first real zone; 0 is the hint row)
    name: str = ""
    zon_path: str = ""       # e.g. 3DDATA\\Maps\\Junon\\JPT01\\JPT01.zon
    deco_zsc: str = ""       # Decoration table -> placeable objects
    cnst_zsc: str = ""       # Building table
    minimap: str = ""
    terrain_type: str = ""   # "0" outdoor / "1" underground

    @property
    def is_underground(self) -> bool:
        return self.terrain_type.strip() == "1"

    @property
    def is_valid(self) -> bool:
        return bool(self.zon_path.strip())


def read_zone_list(path: str) -> List[ZoneInfo]:
    """Parse LIST_ZONE.STB into ZoneInfo records (skips the hint row and blanks)."""
    stb = Stb.read(path)
    c_name = stb.col("Map Name")
    c_zon = stb.col("Zon File")
    c_deco = stb.col("Decoration Table")
    c_cnst = stb.col("Building Table")
    c_mini = stb.col("Minimap")
    c_terr = stb.col("Terrain")

    def get(row, idx):
        return row[idx] if 0 <= idx < len(row) else ""

    zones = []
    for i in range(1, len(stb.rows)):   # skip grid row 0 (the type/hint row)
        row = stb.rows[i]
        z = ZoneInfo(
            row=i,
            name=get(row, c_name),
            zon_path=get(row, c_zon),
            deco_zsc=get(row, c_deco),
            cnst_zsc=get(row, c_cnst),
            minimap=get(row, c_mini),
            terrain_type=get(row, c_terr),
        )
        if z.is_valid:
            zones.append(z)
    return zones


# --------------------------------------------------------------------------- #
#  CLI self-test / validator
#    python rose_stb.py <file.stb>           -> header + dims + EOF check
#    python rose_stb.py <LIST_ZONE.stb> --zones [--check]
# --------------------------------------------------------------------------- #
def _main(argv: List[str]) -> int:
    if not argv:
        print(__doc__)
        print("usage: python rose_stb.py <file.stb> [--zones] [--check]")
        return 1

    path = argv[0]
    stb = Stb.read(path)
    print(f"Parsed: magic={stb.magic!r}  {len(stb.rows)} grid rows x "
          f"{len(stb.titles)} cols")
    print(f"  EOF check (grid ends exactly on file end): {stb.exact} "
          f"({stb.consumed}/{stb.file_size} bytes)")

    if "--zones" in argv[1:]:
        zones = read_zone_list(path)
        print(f"  {len(zones)} valid zones:")
        for z in zones[:12]:
            flag = "underground" if z.is_underground else "outdoor"
            print(f"   row {z.row:3} {z.name[:24]:<24} {flag:<11} {z.zon_path}")
            print(f"            deco={z.deco_zsc}  cnst={z.cnst_zsc}")
        if len(zones) > 12:
            print(f"   ... (+{len(zones) - 12} more)")

    if "--check" in argv[1:]:
        # Validate the known City of Junon Polis (JPT01) row against expectations.
        zones = read_zone_list(path)
        jpt01 = next((z for z in zones if "JPT01.ZON" in z.zon_path.upper()), None)
        assert jpt01, "JPT01 zone not found"
        assert "LIST_DECO_JPT_N.ZSC" in jpt01.deco_zsc.upper(), jpt01.deco_zsc
        assert "LIST_CNST_JPT_N.ZSC" in jpt01.cnst_zsc.upper(), jpt01.cnst_zsc
        assert not jpt01.is_underground, "JPT01 should be outdoor"
        print(f"  CHECK OK: '{jpt01.name}' -> {jpt01.zon_path}")
        print(f"            DECO={jpt01.deco_zsc}")
        print(f"            CNST={jpt01.cnst_zsc}  terrain={jpt01.terrain_type}(outdoor)")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))

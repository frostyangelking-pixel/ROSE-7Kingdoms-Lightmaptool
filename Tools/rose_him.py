"""
rose_him.py  --  ROSE / 7Kingdoms ".HIM" heightmap reader (pure stdlib).

A .HIM holds one map tile's terrain heights on a regular grid. Confirmed against a
real client tile (Junon/JG01/31_31.him: 65x65, grid_count=4, grid_size=250):

    int32   width            (vertices across, e.g. 65)
    int32   height           (vertices down,   e.g. 65)
    int32   grid_count       (cells per patch, e.g. 4)
    float   grid_size        (world units per vertex cell, e.g. 250)
    float   heights[width*height]      # row-major: heights[row*width + col]
    BSTR    "quad"           # collision type marker, then quad/collision trailer
    ... (collision min/max + quadtree -- not needed to build the terrain mesh)

We only parse through the height grid (all the baker needs); the collision/quadtree
trailer is left unread. World layout: vertex (col,row) sits at
(col*grid_size, row*grid_size) with Z = its height. (ROSE's Y axis is commonly
negated when placed in-world; the Blender importer decides final orientation.)
"""

from __future__ import annotations
import struct
from dataclasses import dataclass, field
from typing import List, BinaryIO


@dataclass
class Him:
    width: int = 0
    height: int = 0
    grid_count: int = 0
    grid_size: float = 0.0
    heights: List[float] = field(default_factory=list)   # row-major, len = width*height

    def at(self, col: int, row: int) -> float:
        return self.heights[row * self.width + col]

    @property
    def vertex_count(self) -> int:
        return self.width * self.height

    @classmethod
    def read(cls, path: str) -> "Him":
        with open(path, "rb") as f:
            return cls.read_stream(f)

    @classmethod
    def read_stream(cls, f: BinaryIO) -> "Him":
        head = f.read(16)
        if len(head) != 16:
            raise EOFError("HIM too short for header")
        width, height, grid_count = struct.unpack_from("<iii", head, 0)
        grid_size = struct.unpack_from("<f", head, 12)[0]
        n = width * height
        raw = f.read(n * 4)
        if len(raw) != n * 4:
            raise EOFError(f"HIM height data truncated: wanted {n} floats, got {len(raw)//4}")
        heights = list(struct.unpack(f"<{n}f", raw))
        return cls(width, height, grid_count, grid_size, heights)

    def summary(self) -> str:
        lo = min(self.heights) if self.heights else 0.0
        hi = max(self.heights) if self.heights else 0.0
        return (f"{self.width}x{self.height} verts, grid_count={self.grid_count}, "
                f"grid_size={self.grid_size:g}, Z range [{lo:.1f}, {hi:.1f}]")


if __name__ == "__main__":
    import sys
    if not sys.argv[1:]:
        print("usage: python rose_him.py <file.him>")
        raise SystemExit(1)
    him = Him.read(sys.argv[1])
    print("Parsed HIM:", him.summary())
    print("corner heights:",
          f"(0,0)={him.at(0,0):.1f}",
          f"({him.width-1},0)={him.at(him.width-1,0):.1f}",
          f"(0,{him.height-1})={him.at(0,him.height-1):.1f}")

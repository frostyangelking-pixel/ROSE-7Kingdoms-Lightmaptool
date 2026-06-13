"""
rose_dds.py  --  minimal DDS reader/writer for ROSE / 7Kingdoms lightmaps.

The client's lightmaps are **uncompressed 32-bit A8R8G8B8, no mipmaps** (confirmed by
inspecting a real PlaneLightingMap.dds, and by the original TextureMergeTools, which
creates Format.A8R8G8B8 surfaces and saves them with D3DX SurfaceLoader.Save as DDS).

This writer reproduces that exact header (DDS magic + 124-byte DDS_HEADER with an
A8R8G8B8 DDS_PIXELFORMAT, flags/caps matching the real client files), then the pixel
payload as B,G,R,A bytes (little-endian A8R8G8B8 = byte order B,G,R,A).

Pure stdlib; no Pillow / no DirectX. The Blender baker hands us RGBA pixels and we
write them straight to a client-ready .dds.
"""

from __future__ import annotations
import struct
from typing import Tuple

# DDS_HEADER.dwFlags : CAPS | HEIGHT | WIDTH | PIXELFORMAT  (matches real client files)
_DDSD = 0x1 | 0x2 | 0x4 | 0x1000          # = 0x1007
# DDS_PIXELFORMAT.dwFlags : ALPHAPIXELS | RGB
_DDPF = 0x1 | 0x40                         # = 0x41
# DDSCAPS : value the original D3DX writer emitted for these surfaces
_CAPS = 0x1002
# A8R8G8B8 channel masks
_RMASK, _GMASK, _BMASK, _AMASK = 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000


def build_header(width: int, height: int) -> bytes:
    """The 128-byte DDS magic + header for an uncompressed A8R8G8B8, no-mip surface."""
    h = bytearray(128)
    h[0:4] = b"DDS "
    struct.pack_into("<I", h, 4, 124)          # dwSize
    struct.pack_into("<I", h, 8, _DDSD)        # dwFlags
    struct.pack_into("<I", h, 12, height)      # dwHeight
    struct.pack_into("<I", h, 16, width)       # dwWidth
    struct.pack_into("<I", h, 20, 0)           # dwPitchOrLinearSize (real files use 0)
    struct.pack_into("<I", h, 24, 0)           # dwDepth
    struct.pack_into("<I", h, 28, 0)           # dwMipMapCount (none)
    # 11 reserved dwords (offsets 32..75) left zero
    struct.pack_into("<I", h, 76, 32)          # ddspf.dwSize
    struct.pack_into("<I", h, 80, _DDPF)       # ddspf.dwFlags
    struct.pack_into("<I", h, 84, 0)           # ddspf.dwFourCC (0 = uncompressed)
    struct.pack_into("<I", h, 88, 32)          # ddspf.dwRGBBitCount
    struct.pack_into("<I", h, 92, _RMASK)
    struct.pack_into("<I", h, 96, _GMASK)
    struct.pack_into("<I", h, 100, _BMASK)
    struct.pack_into("<I", h, 104, _AMASK)
    struct.pack_into("<I", h, 108, _CAPS)      # dwCaps
    # dwCaps2/3/4 + reserved (offsets 112..127) left zero
    return bytes(h)


def rgba_to_bgra(rgba: bytes) -> bytes:
    """Convert tightly-packed RGBA bytes to the A8R8G8B8 byte order (B,G,R,A)."""
    mv = memoryview(rgba)
    out = bytearray(len(rgba))
    out[0::4] = mv[2::4]   # dest B = src B
    out[1::4] = mv[1::4]   # dest G = src G
    out[2::4] = mv[0::4]   # dest R = src R
    out[3::4] = mv[3::4]   # dest A = src A
    return bytes(out)


def write_dds(path: str, width: int, height: int, pixels: bytes, order: str = "RGBA") -> None:
    """Write an A8R8G8B8 .dds. `pixels` must be width*height*4 bytes in `order`."""
    if len(pixels) != width * height * 4:
        raise ValueError(f"pixels = {len(pixels)} bytes, expected {width*height*4}")
    if order.upper() == "RGBA":
        body = rgba_to_bgra(pixels)
    elif order.upper() == "BGRA":
        body = pixels
    else:
        raise ValueError("order must be 'RGBA' or 'BGRA'")
    with open(path, "wb") as f:
        f.write(build_header(width, height))
        f.write(body)


def write_dds_from_floats(path: str, width: int, height: int, floats) -> None:
    """Write from a flat RGBA float sequence (0..1), e.g. Blender image.pixels."""
    buf = bytearray(width * height * 4)
    for i in range(width * height * 4):
        v = floats[i]
        buf[i] = 0 if v <= 0 else 255 if v >= 1 else int(v * 255 + 0.5)
    write_dds(path, width, height, bytes(buf), order="RGBA")


def read_dds(path: str) -> Tuple[int, int, bytes]:
    """Read an uncompressed A8R8G8B8 .dds -> (width, height, BGRA-bytes)."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"DDS ":
        raise ValueError("not a DDS file")
    height, width = struct.unpack_from("<II", data, 12)
    fourcc = data[84:88]
    bits = struct.unpack_from("<I", data, 88)[0]
    if fourcc != b"\x00\x00\x00\x00" or bits != 32:
        raise ValueError(f"expected uncompressed 32-bit, got fourCC={fourcc!r} bits={bits}")
    return width, height, data[128:128 + width * height * 4]


if __name__ == "__main__":
    # Self-test: build a 4x4, read it back; print the 512x512 header for inspection.
    import io
    px = bytes([10, 20, 30, 255] * 16)        # 4x4 RGBA
    import tempfile, os
    p = os.path.join(tempfile.gettempdir(), "_rose_dds_test.dds")
    write_dds(p, 4, 4, px, "RGBA")
    w, h, body = read_dds(p)
    print(f"round-trip: {w}x{h}, body {len(body)} bytes, first pixel BGRA={list(body[:4])}")
    assert (w, h) == (4, 4) and list(body[:4]) == [30, 20, 10, 255]
    print("header(512x512) hex:", build_header(512, 512)[:32].hex(" "))
    print("OK")

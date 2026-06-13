"""
rose_zsc.py  --  ROSE / 7Kingdoms ".ZSC" model-list reader (pure-Python).

A ZSC is a zone's catalogue of placeable models. For Junon City the two that
matter are LIST_DECO_JPT_N.ZSC (decoration objects) and LIST_CNST_JPT_N.ZSC
(buildings) -- the tables LIST_ZONE.STB points at. An IFO placement's `obj_id`
indexes the ZSC's object list; each object is built from one or more parts, and
each part references a mesh (ZMS) from the mesh list and a material.

The flag that gates the lightmap pipeline is **LIGHTMAPMODE (0x20)**: a part with
`use_lightmap != 0` gets baked (HeightsMapLoad.ms skips parts where it's 0).

FORMAT (verified against the editor's ZSC.cs reader; both real JPT_N files parse
to exact EOF):

    little-endian throughout
    uint16  mesh_count;       mesh_count   x  ZSTR mesh_path     (ZMS files)
    uint16  material_count;   material_count x material:
        ZSTR  texture_path
        uint16 is_skin, alpha_enabled, two_sided, alpha_test, alpha_ref,
               z_write, z_test, blend_mode, specular        (9 x uint16)
        float  alpha
        uint16 glow_type
        float  red, green, blue
    uint16  effect_count;     effect_count x ZSTR effect_path
    uint16  object_count;     object_count x object:
        int32  cylinder_radius, cylinder_x, cylinder_y    (/100; bounding cylinder)
        uint16 part_count
        if part_count == 0: object ends here (just the 14-byte cylinder + count)
        else:
            part_count x part:
                uint16 mesh_id           # -> mesh list
                uint16 material_id       # -> material list
                flag loop:               # byte id (0 = end), byte size, size bytes
                    POSITION 0x01 vec3/100, ROTATION 0x02 quat(w,x,y,z),
                    SCALE 0x03 vec3, AXISROTATION 0x04 quat, BONEINDEX 0x05 u16,
                    DUMMYINDEX 0x06 u16, PARENT 0x07 u16(-1), COLLISION 0x1D u16,
                    ZMOPATH 0x1E bytes, RANGEMODE 0x1F u16, LIGHTMAPMODE 0x20 u16
            uint16 effect_count
            effect_count x { uint16 effect_id, uint16 effect_type, flag loop }
            vec3 min_bounds (/100), vec3 max_bounds (/100)

    ZSTR = null-terminated byte string. Flag payloads are read by their declared
    size, so the part loop can't drift even on an unfamiliar flag.

NOTE on LIGHTMAPMODE: HeightsMapLoad.ms baked only parts with this flag set, but the
real 7Skies JPT_N ZSCs carry NO LIGHTMAPMODE flag on any part (use_lightmap is false
everywhere). So bake-eligibility for this data set cannot come from the ZSC flag --
the pipeline should bake placed decoration/building objects directly (flag optional).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import BinaryIO, Dict, List, Optional

ZSC_ENCODING = "euc-kr"   # paths are ASCII; EUC-KR is the toolset-wide fallback

# Part flag ids (File Fotmats.txt :ENUM flag_type)
FLAG_POSITION = 0x01
FLAG_ROTATION = 0x02
FLAG_SCALE = 0x03
FLAG_AXISROTATION = 0x04
FLAG_BONEINDEX = 0x05
FLAG_DUMMYINDEX = 0x06
FLAG_PARENT = 0x07
FLAG_COLLISION = 0x1D
FLAG_ZMOPATH = 0x1E
FLAG_RANGEMODE = 0x1F
FLAG_LIGHTMAPMODE = 0x20


def _read_exact(f: BinaryIO, n: int) -> bytes:
    b = f.read(n)
    if len(b) != n:
        raise EOFError(f"Unexpected end of file: wanted {n} bytes, got {len(b)}")
    return b


def read_u16(f: BinaryIO) -> int:
    return struct.unpack("<H", _read_exact(f, 2))[0]


def read_i32(f: BinaryIO) -> int:
    return struct.unpack("<i", _read_exact(f, 4))[0]


def read_u32(f: BinaryIO) -> int:
    return struct.unpack("<I", _read_exact(f, 4))[0]


def read_f32(f: BinaryIO) -> float:
    return struct.unpack("<f", _read_exact(f, 4))[0]


def read_vec3(f: BinaryIO) -> tuple:
    return struct.unpack("<3f", _read_exact(f, 12))


def read_zstr(f: BinaryIO, encoding: str = ZSC_ENCODING) -> str:
    out = bytearray()
    while True:
        c = f.read(1)
        if not c:
            raise EOFError("Unexpected end of file in ZSTR")
        if c == b"\x00":
            break
        out += c
    return out.decode(encoding, errors="replace")


@dataclass
class ZscMaterial:
    path: str = ""
    is_skin: int = 0
    alpha_enabled: int = 0
    two_sided: int = 0
    alpha_test_enabled: int = 0
    alpha_ref_enabled: int = 0
    z_write_enabled: int = 0
    z_test_enabled: int = 0
    blend_mode: int = 0
    specular_enabled: int = 0
    alpha: float = 1.0
    glow_type: int = 0
    color: tuple = (0.0, 0.0, 0.0)


@dataclass
class ZscPart:
    mesh_id: int = 0
    material_id: int = 0
    position: Optional[tuple] = None
    rotation: Optional[tuple] = None
    scale: Optional[tuple] = None
    parent: Optional[int] = None
    range_mode: Optional[int] = None
    use_lightmap: int = 0          # LIGHTMAPMODE (0x20); != 0 => bake this part
    flags: Dict[int, bytes] = field(default_factory=dict)  # raw payloads, all flags

    @property
    def lightmap_eligible(self) -> bool:
        return self.use_lightmap != 0


@dataclass
class ZscEffect:
    """One per-object 'added point'. effect_type 2 = LIGHT (baked); 1 = particle
    effect (runtime visual only). position is the local offset from the object root
    in metres (/100), so a placed object's light world pos = placement_matrix @ position."""
    effect_id: int = 0           # index into Zsc.effects (the effect-file string list)
    effect_type: int = 0         # 2 = light, 1 = particle effect, 0 = none
    position: Optional[tuple] = None     # local offset from object root, metres (/100)
    rotation: Optional[tuple] = None     # (x, y, z, w)

    @property
    def is_light(self) -> bool:
        return self.effect_type == 2


@dataclass
class ZscObject:
    bounding_radius: float = 0.0
    bounding_x: float = 0.0
    bounding_y: float = 0.0
    parts: List[ZscPart] = field(default_factory=list)
    effects: List[ZscEffect] = field(default_factory=list)
    min_bounds: tuple = (0.0, 0.0, 0.0)
    max_bounds: tuple = (0.0, 0.0, 0.0)

    @property
    def lightmap_eligible(self) -> bool:
        return any(p.lightmap_eligible for p in self.parts)

    @property
    def light_points(self) -> List[ZscEffect]:
        """Just the type-2 (light) effect points - the placed lights to bake from."""
        return [e for e in self.effects if e.is_light]


@dataclass
class Zsc:
    meshes: List[str] = field(default_factory=list)
    materials: List[ZscMaterial] = field(default_factory=list)
    effects: List[str] = field(default_factory=list)
    objects: List[ZscObject] = field(default_factory=list)
    consumed: int = 0
    file_size: int = 0

    @property
    def exact(self) -> bool:
        return self.consumed == self.file_size

    @classmethod
    def read(cls, path: str) -> "Zsc":
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(0)
            zsc = cls.read_stream(f)
            zsc.file_size = size
            zsc.consumed = f.tell()
            return zsc

    @classmethod
    def read_stream(cls, f: BinaryIO) -> "Zsc":
        zsc = cls()

        for _ in range(read_u16(f)):
            zsc.meshes.append(read_zstr(f))

        for _ in range(read_u16(f)):
            m = ZscMaterial(path=read_zstr(f))
            (m.is_skin, m.alpha_enabled, m.two_sided, m.alpha_test_enabled,
             m.alpha_ref_enabled, m.z_write_enabled, m.z_test_enabled,
             m.blend_mode, m.specular_enabled) = (read_u16(f) for _ in range(9))
            m.alpha = read_f32(f)
            m.glow_type = read_u16(f)
            m.color = (read_f32(f), read_f32(f), read_f32(f))
            zsc.materials.append(m)

        for _ in range(read_u16(f)):
            zsc.effects.append(read_zstr(f))

        for _ in range(read_u16(f)):
            # Object layout (per the editor's ZSC.cs, ground truth): leading bounding
            # cylinder (3x int32, /100), then u16 part_count. If part_count == 0 the
            # object is just those 14 bytes. Otherwise parts, then a per-object effect
            # list, then the min/max bounding box (/100) at the END. There is NO
            # trailing sphere (File Fotmats.txt put the cylinder at the start -- correct;
            # it just uses int, not float, and there's nothing after the bounds).
            obj = ZscObject(
                bounding_radius=read_i32(f) / 100.0,
                bounding_x=read_i32(f) / 100.0,
                bounding_y=read_i32(f) / 100.0,
            )
            part_count = read_u16(f)
            if part_count == 0:
                zsc.objects.append(obj)
                continue
            for _ in range(part_count):
                obj.parts.append(_read_part(f))
            for _ in range(read_u16(f)):          # per-object effect points (lights/fx)
                obj.effects.append(_read_effect(f))
            obj.min_bounds = tuple(c / 100.0 for c in read_vec3(f))
            obj.max_bounds = tuple(c / 100.0 for c in read_vec3(f))
            zsc.objects.append(obj)

        return zsc


def _read_part(f: BinaryIO) -> ZscPart:
    part = ZscPart(mesh_id=read_u16(f), material_id=read_u16(f))
    while True:
        flag_id = _read_exact(f, 1)[0]
        if flag_id == 0:
            break
        flag_size = _read_exact(f, 1)[0]
        payload = _read_exact(f, flag_size)
        part.flags[flag_id] = payload
        if flag_id == FLAG_POSITION and flag_size >= 12:
            part.position = tuple(c / 100.0 for c in struct.unpack_from("<3f", payload))
        elif flag_id == FLAG_ROTATION and flag_size >= 16:
            part.rotation = struct.unpack_from("<4f", payload)   # (w, x, y, z)
        elif flag_id == FLAG_SCALE and flag_size >= 12:
            part.scale = struct.unpack_from("<3f", payload)
        elif flag_id == FLAG_PARENT and flag_size >= 2:
            part.parent = struct.unpack_from("<H", payload)[0]
        elif flag_id == FLAG_RANGEMODE and flag_size >= 2:
            part.range_mode = struct.unpack_from("<H", payload)[0]
        elif flag_id == FLAG_LIGHTMAPMODE and flag_size >= 2:
            part.use_lightmap = struct.unpack_from("<H", payload)[0]
    return part


def _read_effect(f: BinaryIO) -> ZscEffect:
    """Read one added-point: u16 effect_id, u16 effect_type, then the same flag loop
    as parts (terminated by a 0 id). Captures POSITION (0x01, /100) and ROTATION (0x02)."""
    eff = ZscEffect(effect_id=read_u16(f), effect_type=read_u16(f))
    while True:
        flag_id = _read_exact(f, 1)[0]
        if flag_id == 0:
            break
        flag_size = _read_exact(f, 1)[0]
        payload = _read_exact(f, flag_size)
        if flag_id == FLAG_POSITION and flag_size >= 12:
            eff.position = tuple(c / 100.0 for c in struct.unpack_from("<3f", payload))
        elif flag_id == FLAG_ROTATION and flag_size >= 16:
            eff.rotation = struct.unpack_from("<4f", payload)
    return eff


def _consume_flags(f: BinaryIO) -> None:
    while True:
        flag_id = _read_exact(f, 1)[0]
        if flag_id == 0:
            break
        flag_size = _read_exact(f, 1)[0]
        _read_exact(f, flag_size)


# --------------------------------------------------------------------------- #
#  CLI self-test / validator
#    python rose_zsc.py <file.zsc> [--dump]
# --------------------------------------------------------------------------- #
def _main(argv: List[str]) -> int:
    if not argv:
        print(__doc__)
        print("usage: python rose_zsc.py <file.zsc> [--dump] [--lights]")
        return 1

    path = argv[0]
    dump = "--dump" in argv[1:]
    lights = "--lights" in argv[1:]

    zsc = Zsc.read(path)
    n_parts = sum(len(o.parts) for o in zsc.objects)
    lit_objs = sum(1 for o in zsc.objects if o.lightmap_eligible)
    lit_parts = sum(1 for o in zsc.objects for p in o.parts if p.lightmap_eligible)
    print(f"Parsed: {len(zsc.meshes)} meshes, {len(zsc.materials)} materials, "
          f"{len(zsc.effects)} effects, {len(zsc.objects)} objects ({n_parts} parts)")
    print(f"  EOF check (parse ends exactly on file end): {zsc.exact} "
          f"({zsc.consumed}/{zsc.file_size} bytes)")
    print(f"  lightmap-eligible: {lit_objs}/{len(zsc.objects)} objects, "
          f"{lit_parts}/{n_parts} parts (LIGHTMAPMODE != 0)")

    if dump:
        for oi, o in enumerate(zsc.objects[:20]):
            tag = "LM" if o.lightmap_eligible else "  "
            print(f"  [{tag}] obj {oi:4} parts={len(o.parts)}")
            for p in o.parts:
                mesh = zsc.meshes[p.mesh_id] if p.mesh_id < len(zsc.meshes) else "?"
                print(f"        mesh[{p.mesh_id}]={mesh}  mat={p.material_id} "
                      f"lightmap={p.use_lightmap}")

    # --lights: list every object that carries a type-2 (light) effect point -- these
    # are the only objects the baker spawns a light at, and only in underground zones.
    # obj index here = the IFO obj_id you place. local_pos is the light's offset from
    # the object root (metres); world pos = placement matrix @ local_pos.
    light_objs = [(oi, o) for oi, o in enumerate(zsc.objects) if o.light_points]
    if lights or light_objs:
        print(f"  objects with type-2 light points: {len(light_objs)}")
    if lights:
        for oi, o in light_objs:
            print(f"  obj {oi:4}: {len(o.light_points)} light point(s)")
            for e in o.light_points:
                nm = zsc.effects[e.effect_id] if 0 <= e.effect_id < len(zsc.effects) else "?"
                px, py, pz = e.position if e.position else (0.0, 0.0, 0.0)
                print(f"        effect[{e.effect_id}]='{nm}' type={e.effect_type} "
                      f"local=({px:.3f}, {py:.3f}, {pz:.3f})")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))

"""
ROSE / 7Kingdoms lightmap baker for Blender  --  v0.3 (zone: terrain + objects + atlas/LIT)

Install: Blender > Edit > Preferences > Add-ons > Install... > pick this file > enable.
Panel:   3D Viewport > N-panel > "ROSE Lightmap" tab.

v0.1 did the terrain vertical slice (.HIM -> mesh -> sun/sky bake -> PlaneLightingMap.dds).
v0.2 added the whole-map flow your STB idea enables (pick a 3Ddata ROOT, read
LIST_ZONE.STB, place every tile + its objects from the IFO matrix, bake in one pass).
v0.3 packs the object lightmaps the way the client wants them:

    each ZSC part bakes into a cell of a shared 512x512 atlas page
      (Object_<px>_<page>.dds for decorations, Building_<px>_<page>.dds for buildings)
    and the per-tile <tile>\\LightMap\\ObjectLightMapData.lit / BuildingLightMapData.lit
    index them (objectID = IFO placement index, MapPosition = atlas cell, written via
    the byte-exact rose_lit.py).

v0.4 bakes indoor placed-lights for underground zones, and writes lightmap atlases
(and terrain maps) as DXT1/BC1 -- 8x smaller and the format stock ROSE + the map
editor expect. Uncompressed atlases are ~1 MB each; the 32-bit editor indexes atlas
cells assuming the DXT1 block layout and runs out of memory on dense maps. Toggle at
panel > Bake > "Compress (DXT1)" (default on).

v0.4.3 also reads editor-authored light markers from <zone>\\LightSources.txt and
spawns a point light at each (independent of the underground flag), so a cave can be
lit by hand-placed markers, not just objects that happen to carry a ZSC light point.

v0.4.4 lifts each LightSources.txt marker by "Marker Height" metres (panel, default
2.5) before baking. Editor markers sit at the object's BASE = floor level, and a point
light level with the floor lights it at a grazing angle (cos -> 0), so a floor-level
marker baked a single bright pinprick and left the rest of the ground black. Raising it
to head height makes it actually pool light on the floor.

v0.4.5: Load Zone now frames the viewport on the FIRST tile of the map (the first tile
with placed objects, else the first tile) instead of zooming out to the whole zone.

v0.4.6: "Marker Height" now defaults to 0. The editor's placeable Light tool sets each
light's Z directly (the editor cube sits exactly where the light bakes -- WYSIWYG), so
an auto-lift would push the baked light above its marker. Raise it only for markers
authored at floor level.

v0.4.7: tiles whose IFO ships a zeroed ECONOMYDATA matrix (some hand-built test caves)
no longer stack on the world origin. world_translation is (0,0,0) for every such tile,
so the origin is derived from the still-correct map_cell_x/_y instead
(tx=(cx-32)*16000, tz=(32-cy)*16000). Correctly-authored maps are unaffected.

v0.4.8: object lightmaps in underground zones now bake DIFFUSE (the placed point lights'
full irradiance) and are written raw, instead of the outdoor AO x Shadow path -- which
ignores the placed lights and bakes a flat grey, so objects rendered ~fully lit under the
client's modulate-2x. Indoors the lightmap is the only lighting, so this matches the
terrain and gives the dark-cave-with-warm-pools look. Outdoor object baking is unchanged.

v0.4.9: bake-noise control. The DIFFUSE indoor bake threw fireflies (bright speckles),
worst on big walls lit by indirect bounce. Indirect samples are now clamped (crushes the
outliers during sampling) and OpenImageDenoise runs on the bake. Direct light is left
unclamped so the torch pools keep their punch.

v0.4.10: placed lights now have a finite REACH (Blender custom cutoff distance). A cave
encloses its lights, so with infinite-range points every wall faces a light and baked
bright. Marker lights use their editor Range sphere as the cutoff (so the preview is
WYSIWYG); object/torch lights and range-less markers use DEFAULT_LIGHT_REACH. Walls
beyond a light's reach now go dark, giving the stock dark-cave-with-pools look.

v0.5.0: per-map settings. Each zone can carry a litsave.txt (next to its .zon /
LightSources.txt) recording the tool settings used for that map. Load Zone restores them
(or resets to defaults when the file is absent, so maps never inherit each other's
tweaks); Bake and the new "Save Map Settings" button write it. It's plain text with no
.zon/.lit/.dds meaning, so the game client ignores it.

This file imports the validated standalone readers (rose_stb / rose_ifo / rose_zsc /
rose_zms / rose_paths / rose_lit) from the Tools folder -- keep them beside this add-on,
or set "Tools Folder" in the panel. The HIM reader + DDS writer stay inlined.

NOTE: this runs inside Blender, so it can't be byte-validated outside it. World
conventions that need an in-Blender/in-game look are isolated as constants below
(ZMS_UNIT, FLIP_TILE_Y, LIGHTMAP_*_FLIP, OBJECT_LIGHTMAP_V_FLIP).
"""

bl_info = {
    "name": "ROSE Lightmap Baker",
    "author": "7Kingdoms",
    "version": (0, 5, 0),
    "blender": (4, 2, 0),
    "location": "View3D > N-panel > ROSE Lightmap",
    "description": "Bake ROSE terrain + object lightmaps (zone-driven via LIST_ZONE.STB)",
    "category": "Import-Export",
}

import os
import sys
import struct
import importlib
import bpy
from mathutils import Vector, Quaternion
from bpy.props import (StringProperty, FloatProperty, IntProperty,
                       FloatVectorProperty, EnumProperty, BoolProperty)
from bpy.types import Operator, Panel, PropertyGroup
from bpy_extras.io_utils import ImportHelper


# ===========================================================================
#  World-convention constants  (the bits to confirm with a "Load Zone" look)
# ===========================================================================
WORLD_OFFSET = 520000.0        # ROSE world origin shift (matches IFO/ZSC +520000)
UNIT = 0.01                    # ROSE units -> metres (cm). Applied to raw coords.
ZMS_UNIT = 1.0                 # extra scale for ZMS local verts if they read oversized
FLIP_TILE_Y = True             # terrain row -> -Y so north lines up with placements
LIGHTMAP_V_FLIP = True         # flip terrain lightmap V if baked shadows mirror N-S
LIGHTMAP_U_FLIP = False        # ...use this instead if they mirror E-W
# IFO stores quaternion as (x,y,z,w); ZSC parts as (w,x,y,z). Blender wants (w,x,y,z).

ATLAS_DIM = 512                # object lightmap atlas page size (client uses 512x512)
OBJECT_LIGHTMAP_V_FLIP = False # flip each object's lightmap V within its atlas cell if needed
DEFAULT_LIGHT_REACH = 20.0     # metres a placed light reaches before it's cut off, so a
                               # cave stays dark BETWEEN pools instead of every wall (which
                               # all face inward toward the lights) baking bright. Editor
                               # markers use their own Range; object lights + range-less
                               # markers fall back to this.


# ===========================================================================
#  Sibling module loader (validated readers live beside this add-on)
# ===========================================================================
_MODS = {}

def _load_modules(tools_dir=""):
    """Import the rose_* readers from tools_dir (or this add-on's folder). Cached."""
    if _MODS:
        return _MODS
    for d in (tools_dir, os.path.dirname(os.path.abspath(__file__))):
        if d and os.path.isdir(d) and d not in sys.path:
            sys.path.insert(0, d)
    import rose_stb, rose_ifo, rose_zsc, rose_zms, rose_paths, rose_lit
    for m in (rose_stb, rose_ifo, rose_zsc, rose_zms, rose_paths, rose_lit):
        importlib.reload(m)
    _MODS.update(stb=rose_stb, ifo=rose_ifo, zsc=rose_zsc,
                 zms=rose_zms, paths=rose_paths, lit=rose_lit)
    return _MODS


# ===========================================================================
#  Inlined, validated core: HIM reader  (see standalone rose_him.py)
# ===========================================================================
def read_him(path):
    with open(path, "rb") as f:
        head = f.read(16)
        width, height, _grid_count = struct.unpack_from("<iii", head, 0)
        grid_size = struct.unpack_from("<f", head, 12)[0]
        n = width * height
        heights = list(struct.unpack("<%df" % n, f.read(n * 4)))
    return width, height, grid_size, heights


# ===========================================================================
#  Inlined, validated core: DDS writer (A8R8G8B8) (see standalone rose_dds.py)
# ===========================================================================
def _dds_header(width, height):
    h = bytearray(128)
    h[0:4] = b"DDS "
    struct.pack_into("<I", h, 4, 124)
    struct.pack_into("<I", h, 8, 0x1 | 0x2 | 0x4 | 0x1000)
    struct.pack_into("<I", h, 12, height)
    struct.pack_into("<I", h, 16, width)
    struct.pack_into("<I", h, 76, 32)
    struct.pack_into("<I", h, 80, 0x1 | 0x40)
    struct.pack_into("<I", h, 88, 32)
    struct.pack_into("<I", h, 92, 0x00FF0000)
    struct.pack_into("<I", h, 96, 0x0000FF00)
    struct.pack_into("<I", h, 100, 0x000000FF)
    struct.pack_into("<I", h, 104, 0xFF000000)
    struct.pack_into("<I", h, 108, 0x1002)
    return bytes(h)


def write_dds_from_floats(path, width, height, floats, flip_v=True):
    row_bytes = width * 4
    body = bytearray(width * height * 4)
    for row in range(height):
        src_row = (height - 1 - row) if flip_v else row
        base = src_row * row_bytes
        out = row * row_bytes
        for x in range(width):
            si = base + x * 4
            r = floats[si]; g = floats[si + 1]; b = floats[si + 2]; a = floats[si + 3]
            di = out + x * 4
            body[di + 0] = 0 if b <= 0 else 255 if b >= 1 else int(b * 255 + 0.5)
            body[di + 1] = 0 if g <= 0 else 255 if g >= 1 else int(g * 255 + 0.5)
            body[di + 2] = 0 if r <= 0 else 255 if r >= 1 else int(r * 255 + 0.5)
            body[di + 3] = 0 if a <= 0 else 255 if a >= 1 else int(a * 255 + 0.5)
    with open(path, "wb") as f:
        f.write(_dds_header(width, height))
        f.write(body)


# ===========================================================================
#  Inlined DXT1 (BC1) writer  --  stock ROSE lightmap atlases are DXT1, and the
#  map editor's object loader indexes atlas cells assuming that block layout, so
#  uncompressed atlases both crash it and cost 8x the VRAM. Lightmaps are smooth,
#  so BC1's 4x4 block fit is visually fine (it's what stock shipped).
# ===========================================================================
def _dds_header_dxt1(width, height):
    h = bytearray(128)
    h[0:4] = b"DDS "
    struct.pack_into("<I", h, 4, 124)
    struct.pack_into("<I", h, 8, 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000)  # +LINEARSIZE
    struct.pack_into("<I", h, 12, height)
    struct.pack_into("<I", h, 16, width)
    struct.pack_into("<I", h, 20, (width * height) // 2)              # DXT1 linear size
    struct.pack_into("<I", h, 76, 32)
    struct.pack_into("<I", h, 80, 0x4)                                # DDPF_FOURCC
    h[84:88] = b"DXT1"
    struct.pack_into("<I", h, 108, 0x1000)                            # DDSCAPS_TEXTURE
    return bytes(h)


def _to565(r, g, b):
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def _encode_dxt1_block(px16):
    """16 (r,g,b) ints in row-major 4x4 order -> 8 bytes BC1 (range-fit)."""
    rmn = gmn = bmn = 255
    rmx = gmx = bmx = 0
    for (r, g, b) in px16:
        if r < rmn: rmn = r
        if g < gmn: gmn = g
        if b < bmn: bmn = b
        if r > rmx: rmx = r
        if g > gmx: gmx = g
        if b > bmx: bmx = b
    c0 = _to565(rmx, gmx, bmx)            # max corner; monotone packing keeps c0 >= c1
    c1 = _to565(rmn, gmn, bmn)            # min corner
    if c0 == c1:
        return struct.pack("<HHI", c0, c1, 0)     # flat block: every index 0 = c0
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
    bits = 0
    for i, (r, g, b) in enumerate(px16):     # project onto c1->c0 axis, quantise to 4 levels
        t = ((r - r1) * ar + (g - g1) * ag + (b - b1) * ab) / denom
        if t < 0.16667:   idx = 1            # c1
        elif t < 0.5:     idx = 3            # (c0 + 2 c1) / 3
        elif t < 0.83333: idx = 2            # (2 c0 + c1) / 3
        else:             idx = 0            # c0
        bits |= idx << (2 * i)
    return struct.pack("<HHI", c0, c1, bits)


def _c255(v):
    return 0 if v <= 0.0 else 255 if v >= 1.0 else int(v * 255 + 0.5)


def write_dds_dxt1_from_floats(path, width, height, floats, flip_v=True):
    """Encode RGBA floats (row 0 = v=0) to a DXT1 .dds, honouring flip_v exactly like
    write_dds_from_floats so atlas/terrain orientation is unchanged."""
    out = bytearray((width // 4) * (height // 4) * 8)
    o = 0
    for by in range(0, height, 4):
        block = [None] * 16
        for bx in range(0, width, 4):
            k = 0
            for yy in range(4):
                oy = by + yy
                sy = (height - 1 - oy) if flip_v else oy
                base = (sy * width + bx) * 4
                for xx in range(4):
                    si = base + xx * 4
                    block[k] = (_c255(floats[si]), _c255(floats[si + 1]), _c255(floats[si + 2]))
                    k += 1
            out[o:o + 8] = _encode_dxt1_block(block)
            o += 8
    with open(path, "wb") as f:
        f.write(_dds_header_dxt1(width, height))
        f.write(out)


def write_lightmap_dds(path, width, height, floats, flip_v, compress):
    """Write a lightmap .dds: DXT1/BC1 when compress and dims are 4-aligned (stock
    format, 8x smaller, editor-safe), else the uncompressed A8R8G8B8 writer."""
    if compress and width % 4 == 0 and height % 4 == 0:
        write_dds_dxt1_from_floats(path, width, height, floats, flip_v=flip_v)
    else:
        write_dds_from_floats(path, width, height, floats, flip_v=flip_v)


# ===========================================================================
#  Terrain mesh build  (now placeable at a world origin for whole-map assembly)
# ===========================================================================
def build_terrain(width, height, grid_size, heights, name,
                  origin=(0.0, 0.0), unit=UNIT):
    """Build a tile mesh in metres, centred on `origin` (metres). origin comes from
    the IFO matrix translation so the tile lines up with its placements."""
    gs = grid_size * unit
    half_x = (width - 1) * gs * 0.5
    half_y = (height - 1) * gs * 0.5
    ox, oy = origin
    verts = []
    for row in range(height):
        for col in range(width):
            z = heights[row * width + col] * unit
            x = ox + (col * gs - half_x)
            y = (oy - (row * gs - half_y)) if FLIP_TILE_Y else (oy + (row * gs - half_y))
            verts.append((x, y, z))
    faces = []
    for row in range(height - 1):
        for col in range(width - 1):
            a = row * width + col
            b = row * width + col + 1
            c = (row + 1) * width + col + 1
            d = (row + 1) * width + col
            faces.append((a, d, c, b))
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.validate(verbose=False)
    mesh.update(calc_edges=True)
    for p in mesh.polygons:
        if p.normal.z < 0.0:
            p.flip()
    mesh.update()
    uv = mesh.uv_layers.new(name="Lightmap")
    inv_w = 1.0 / (width - 1)
    inv_h = 1.0 / (height - 1)
    for poly in mesh.polygons:
        for li in poly.loop_indices:
            vi = mesh.loops[li].vertex_index
            u = (vi % width) * inv_w
            v = (vi // width) * inv_h
            uv.data[li].uv = (1.0 - u if LIGHTMAP_U_FLIP else u,
                              1.0 - v if LIGHTMAP_V_FLIP else v)
    uv.active_render = True
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj


# ===========================================================================
#  Object (ZSC -> ZMS) mesh build + placement
# ===========================================================================
_ZMS_CACHE = {}      # resolved path -> parsed Zms
_OBJ_MESH_CACHE = {} # (zsc_id, obj_index, part_index) -> (bpy mesh, mesh_base)


def _get_zms(mods, data_root, ref):
    real = data_root.resolve(ref)
    if not real:
        return None
    if real not in _ZMS_CACHE:
        try:
            _ZMS_CACHE[real] = mods["zms"].Zms.read(real)
        except Exception:
            _ZMS_CACHE[real] = None
    return _ZMS_CACHE[real]


def _safe_quat(wxyz):
    """Blender quaternion (w,x,y,z), guarding the zero/degenerate case. ROSE stores
    some placement rotations as (0,0,0,0); the client's Matrix.CreateFromQuaternion
    reads that as identity, but mathutils would collapse the mesh to a point. So a
    ~zero quaternion becomes identity; anything else is normalized."""
    q = Quaternion(wxyz)
    if q.magnitude < 1e-6:
        return Quaternion()  # identity (1,0,0,0)
    q.normalize()
    return q


def _part_matrix(part):
    """Local transform for a ZSC part (position already /100, rotation w,x,y,z)."""
    from mathutils import Matrix
    pos = Vector(part.position) if part.position else Vector((0, 0, 0))
    rot = _safe_quat(part.rotation) if part.rotation else Quaternion()  # (w,x,y,z)
    scl = Vector(part.scale) if part.scale else Vector((1, 1, 1))
    return (Matrix.Translation(pos) @ rot.to_matrix().to_4x4()
            @ Matrix.Diagonal((scl.x, scl.y, scl.z, 1.0)))


def build_part_mesh(mods, data_root, zsc, zsc_id, obj_index, part_index):
    """Build ONE ZSC part (a single ZMS + its part transform) as a mesh datablock,
    carrying the mesh's UV2 as the 'Lightmap' UV. Lightmaps are per-part, so each
    part is its own bakeable object. Cached so repeated placements link/share.
    Returns (mesh, mesh_base) or (None, "")."""
    key = (zsc_id, obj_index, part_index)
    if key in _OBJ_MESH_CACHE:
        return _OBJ_MESH_CACHE[key]
    if obj_index < 0 or obj_index >= len(zsc.objects):
        _OBJ_MESH_CACHE[key] = (None, ""); return _OBJ_MESH_CACHE[key]
    zobj = zsc.objects[obj_index]
    if part_index < 0 or part_index >= len(zobj.parts):
        _OBJ_MESH_CACHE[key] = (None, ""); return _OBJ_MESH_CACHE[key]
    part = zobj.parts[part_index]
    if part.mesh_id >= len(zsc.meshes):
        _OBJ_MESH_CACHE[key] = (None, ""); return _OBJ_MESH_CACHE[key]
    mesh_ref = zsc.meshes[part.mesh_id]
    mesh_base = os.path.splitext(os.path.basename(mesh_ref.replace("\\", "/")))[0]
    zms = _get_zms(mods, data_root, mesh_ref)
    if zms is None or not zms.positions:
        _OBJ_MESH_CACHE[key] = (None, ""); return _OBJ_MESH_CACHE[key]
    m = _part_matrix(part)
    verts = []
    for vp in zms.positions:
        wp = m @ Vector((vp[0] * ZMS_UNIT, vp[1] * ZMS_UNIT, vp[2] * ZMS_UNIT, 1.0))
        verts.append((wp.x, wp.y, wp.z))
    src_uv = zms.uv2 if zms.uv2 else zms.uv1
    uv_for_v = src_uv if src_uv else [(0.0, 0.0)] * len(zms.positions)
    faces, uvs = [], []
    for (a, b, c) in zms.faces:
        faces.append((a, b, c))
        uvs.append((uv_for_v[a], uv_for_v[b], uv_for_v[c]))
    mesh = bpy.data.meshes.new("zsc%d_o%d_p%d" % (zsc_id, obj_index, part_index))
    mesh.from_pydata(verts, [], faces)
    mesh.validate(verbose=False)
    mesh.update()
    uv = mesh.uv_layers.new(name="Lightmap")
    for fi, poly in enumerate(mesh.polygons):
        tri = uvs[fi] if fi < len(uvs) else ((0, 0), (0, 0), (0, 0))
        for k, loop in enumerate(poly.loop_indices):
            uv.data[loop].uv = tri[k] if k < 3 else (0.0, 0.0)
    uv.active_render = True
    _OBJ_MESH_CACHE[key] = (mesh, mesh_base)
    return _OBJ_MESH_CACHE[key]


def place_object(mesh, placement, name, parent=None, unit=UNIT):
    """Instance `mesh` at an IFO placement (world position already in metres)."""
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    obj.location = Vector(placement.position)            # already (raw+520000)/100 metres
    q = placement.rotation                               # IFO order (x, y, z, w)
    obj.rotation_mode = "QUATERNION"
    obj.rotation_quaternion = _safe_quat((q[3], q[0], q[1], q[2]))
    obj.scale = Vector(placement.scale)
    if parent is not None:
        obj.parent = parent
    return obj


def _classify_light(name, base_col):
    """Pick (colour, energy_multiplier) from a 3LIGHT_* effect name. The originals were
    hand-authored Max lights (long gone), so we synthesise a sensible look from the name."""
    if any(k in name for k in ("fire", "torch", "candle", "flame", "lava", "brazier")):
        return (1.0, 0.52, 0.20), 1.0      # warm torch / fire
    if "road" in name or "dom" in name or "lamp" in name:
        return (1.0, 0.86, 0.62), 1.1      # warm path / lamp light
    return base_col, 1.0


def spawn_object_lights(zobj, placement, zsc, parent, props):
    """Create a Blender POINT light at each type-2 (light) effect point of a placed
    object. World pos = placement TRS @ the point's local pos. Lights are tagged so the
    scene purge removes them, and are type LIGHT so the bake's MESH filters skip them
    (they only illuminate). Returns how many were created."""
    from mathutils import Matrix
    pts = zobj.light_points
    if not pts:
        return 0
    q = placement.rotation                                   # IFO order (x, y, z, w)
    M = (Matrix.Translation(Vector(placement.position))
         @ _safe_quat((q[3], q[0], q[1], q[2])).to_matrix().to_4x4()
         @ Matrix.Diagonal((placement.scale[0], placement.scale[1],
                            placement.scale[2], 1.0)))
    base_col = tuple(props.light_color)
    tint = bool(getattr(props, "tint_by_name", True))
    n = 0
    for e in pts:
        local = Vector(e.position) if e.position else Vector((0.0, 0.0, 0.0))
        col, emult = base_col, 1.0
        if tint:
            nm = zsc.effects[e.effect_id].lower() if 0 <= e.effect_id < len(zsc.effects) else ""
            col, emult = _classify_light(nm, base_col)
        ld = bpy.data.lights.new("ROSE_Light", type="POINT")
        ld.energy = props.light_energy * emult
        ld.color = col
        try:
            ld.shadow_soft_size = props.light_radius
        except Exception:
            pass
        try:                                          # cap reach so torches don't flood the cave
            ld.use_custom_distance = True
            ld.cutoff_distance = DEFAULT_LIGHT_REACH
        except Exception:
            pass
        ob = bpy.data.objects.new("ROSE_Light", ld)   # 'ROSE_' prefix -> swept by purge
        ob.location = M @ local
        ob["rose_kind"] = "light"
        bpy.context.collection.objects.link(ob)
        ob.parent = parent
        n += 1
    return n


# ===========================================================================
#  Editor-authored light markers  (sidecar LightSources.txt, read by the baker)
# ===========================================================================
LIGHT_SOURCES_FILE = "LightSources.txt"


def read_light_sources(zone_dir, props):
    """Read <zone_dir>\\LightSources.txt -- editor-authored light markers. One light
    per line, whitespace- (or comma-) separated, in ROSE world metres: the SAME space
    as IFO placement positions ((raw+520000)/100), which is exactly what the editor's
    IFO.Position holds, so the editor writes the world coords it already shows and the
    baker uses them verbatim -- no conversion either side.

        x y z [r g b] [energy] [radius]

    Only x y z are required; r g b / energy / radius fall back to the panel's Light
    Colour / Light Power / Light Size. '#' starts a comment; blank lines are skipped.
    Returns a list of {pos, color, energy, radius}."""
    path = os.path.join(zone_dir, LIGHT_SOURCES_FILE)
    if not os.path.isfile(path):
        return []
    base_col = tuple(props.light_color)
    base_e = props.light_energy
    base_r = props.light_radius
    out = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, 1):
                head, _, comment = raw.partition("#")
                line = head.strip()
                if not line:
                    continue
                tok = line.replace(",", " ").split()
                try:
                    x, y, z = float(tok[0]), float(tok[1]), float(tok[2])
                except (IndexError, ValueError):
                    print("ROSE LightSources: skipped line %d (need x y z): %r"
                          % (lineno, raw.strip()))
                    continue
                col, energy, radius = base_col, base_e, base_r
                if len(tok) >= 6:
                    try: col = (float(tok[3]), float(tok[4]), float(tok[5]))
                    except ValueError: pass
                if len(tok) >= 7:
                    try: energy = float(tok[6])
                    except ValueError: pass
                if len(tok) >= 8:
                    try: radius = float(tok[7])
                    except ValueError: pass
                # The editor stores each light's visual Range sphere as a "# range=R"
                # comment. Honour it as the light's reach (cutoff distance) so the bake
                # matches what you previewed: beyond it the light contributes nothing,
                # which is what keeps a cave dark between the torch pools.
                reach = DEFAULT_LIGHT_REACH
                if "range=" in comment:
                    try:
                        reach = float(comment.split("range=", 1)[1].split()[0])
                    except (IndexError, ValueError):
                        pass
                out.append({"pos": (x, y, z), "color": col,
                            "energy": energy, "radius": radius, "reach": reach})
    except Exception as e:
        print("ROSE LightSources: read failed (%s)" % e)
        return []
    return out


def spawn_marker_lights(zone_dir, parent, props):
    """Spawn a Blender POINT light at each LightSources.txt marker (world metres).
    Lights are tagged 'ROSE_' / rose_kind=light so the scene purge removes them and
    the bake's MESH filters skip them (they only illuminate). Returns the count."""
    n = 0
    for m in read_light_sources(zone_dir, props):
        ld = bpy.data.lights.new("ROSE_Light", type="POINT")
        ld.energy = m["energy"]
        ld.color = m["color"]
        try:
            ld.shadow_soft_size = m["radius"]
        except Exception:
            pass
        # Cap the light's reach at its editor Range so it doesn't flood the whole cave
        # (a cave encloses its lights, so without a cutoff every wall faces a light and
        # bakes bright). Beyond the cutoff the contribution goes to zero -> dark walls
        # between pools, matching stock ROSE caves. Shrink the Range sphere in the editor
        # to tighten a pool; grow it to spread.
        try:
            reach = m.get("reach") or 0.0
            if reach > 0.0:
                ld.use_custom_distance = True
                ld.cutoff_distance = reach
        except Exception:
            pass
        ob = bpy.data.objects.new("ROSE_Light", ld)   # 'ROSE_' prefix -> swept by purge
        # Lift the marker off the floor. Editor markers sit at the object's BASE (floor
        # level, often z=0 or slightly below); a point light level with the floor lights
        # it at a grazing angle (cos -> 0) so it bakes a pinprick and leaves the ground
        # black. Raising it to ~head height makes it actually pool light on the floor.
        px, py, pz = m["pos"]
        ob.location = Vector((px, py, pz + getattr(props, "marker_height", 0.0)))
        ob["rose_kind"] = "light"
        bpy.context.collection.objects.link(ob)
        ob.parent = parent
        n += 1
    return n


# ===========================================================================
#  Per-map settings sidecar  (litsave.txt -- sits with the map, client ignores it)
# ===========================================================================
LITSAVE_FILE = "litsave.txt"

# The bake/look settings saved per-map. Machine/session props (tools_dir, data_root,
# the zone picker, viewport scale, output_dir) are deliberately excluded.
_MAP_SETTINGS = [
    ("sun_strength", "float"), ("sky_strength", "float"), ("sky_color", "color"),
    ("samples", "int"),
    ("place_lights", "bool"), ("marker_lights", "bool"), ("marker_height", "float"),
    ("light_energy", "float"), ("light_radius", "float"), ("light_color", "color"),
    ("tint_by_name", "bool"), ("indoor_ambient", "float"),
    ("resolution", "int"), ("object_resolution", "int"), ("object_bake_mode", "enum"),
    ("object_brightness", "float"), ("object_shadow_floor", "float"),
    ("isolate_parts", "bool"), ("bake_objects", "bool"), ("clean_lightmap", "bool"),
    ("compress_dxt1", "bool"),
]


def _fmt_setting(kind, val):
    if kind == "color":
        return "%.4f,%.4f,%.4f" % (val[0], val[1], val[2])
    if kind == "bool":
        return "1" if val else "0"
    if kind == "int":
        return str(int(val))
    if kind == "float":
        return "%.4f" % float(val)
    return str(val)                      # enum / str


def _parse_setting(kind, text):
    text = text.strip()
    if kind == "color":
        p = text.replace(",", " ").split()
        return (float(p[0]), float(p[1]), float(p[2]))
    if kind == "bool":
        return text.lower() in ("1", "true", "yes", "on")
    if kind == "int":
        return int(float(text))
    if kind == "float":
        return float(text)
    return text                          # enum / str


def save_map_settings(zone_dir, props):
    """Write the per-map bake settings to <zone_dir>\\litsave.txt (key=value text, which
    the client ignores). Returns the path written, or None on failure."""
    if not zone_dir or not os.path.isdir(zone_dir):
        return None
    path = os.path.join(zone_dir, LITSAVE_FILE)
    lines = [
        "# litsave.txt - ROSE Lightmap Baker per-map settings for this zone.",
        "# Auto-loaded by Load Zone and rewritten by Bake (or 'Save Map Settings').",
        "# Delete this file to fall back to the tool's generic defaults.",
        "# Plain text with no .zon/.lit/.dds meaning, so the game client ignores it.",
        "",
    ]
    for name, kind in _MAP_SETTINGS:
        try:
            lines.append("%s=%s" % (name, _fmt_setting(kind, getattr(props, name))))
        except Exception:
            pass
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception as e:
        print("ROSE litsave: write failed (%s)" % e)
        return None
    return path


def load_map_settings(zone_dir, props):
    """Reset the managed settings to their tool defaults, then overlay any stored in
    <zone_dir>\\litsave.txt. So every map starts from generic defaults plus only the
    overrides it actually saved -- and a map with no file simply uses the defaults
    (it never inherits the previous map's tweaks). Returns True if a file was applied."""
    for name, _kind in _MAP_SETTINGS:          # 1) back to registered defaults
        try:
            props.property_unset(name)
        except Exception:
            pass
    if not zone_dir:
        return False
    path = os.path.join(zone_dir, LITSAVE_FILE)
    if not os.path.isfile(path):
        return False
    kinds = dict(_MAP_SETTINGS)
    try:
        with open(path, "r", encoding="utf-8") as fh:   # 2) overlay the map's own file
            for raw in fh:
                line = raw.split("#", 1)[0].strip()
                if not line or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                kind = kinds.get(key.strip())
                if kind is None:
                    continue
                try:
                    setattr(props, key.strip(), _parse_setting(kind, val))
                except Exception:
                    pass
    except Exception as e:
        print("ROSE litsave: read failed (%s)" % e)
        return False
    return True


# ===========================================================================
#  Lighting rig (outdoor: sun + sky; indoor zones skip the sun)
# ===========================================================================
def setup_lighting(props, underground=False):
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    try:
        scene.view_settings.view_transform = "Standard"
    except Exception:
        pass
    try:
        scene.cycles.samples = props.samples
    except Exception:
        pass
    # Bake-noise control. DIFFUSE point-light bakes -- especially big indoor walls lit
    # mostly by INDIRECT bounce -- throw fireflies (bright speckles) at sane sample
    # counts. Clamp indirect samples to crush those bright outliers (the standard firefly
    # fix, applied during path tracing so it works on bakes regardless of denoise
    # support); leave direct unclamped so the bright torch pools keep their punch. Then
    # run OpenImageDenoise -- a lightmap is low-frequency, so it cleans the grain with no
    # visible detail loss.
    try:
        scene.cycles.sample_clamp_indirect = 1.0
    except Exception:
        pass
    try:
        scene.cycles.use_denoising = True
        scene.cycles.denoiser = "OPENIMAGEDENOISE"
    except Exception:
        pass
    world = scene.world or bpy.data.worlds.new("World")
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        c = props.sky_color
        bg.inputs[0].default_value = (c[0], c[1], c[2], 1.0)
        bg.inputs[1].default_value = (getattr(props, "indoor_ambient", 0.0)
                                      if underground else props.sky_strength)
    sun = bpy.data.objects.get("ROSE_Sun")
    if sun is None:
        light = bpy.data.lights.new("ROSE_Sun", type="SUN")
        sun = bpy.data.objects.new("ROSE_Sun", light)
        bpy.context.collection.objects.link(sun)
        sun.rotation_euler = (0.6, 0.1, 0.8)
    sun.data.energy = (0.0 if underground else props.sun_strength)
    sun.data.angle = 0.09
    return sun


# ===========================================================================
#  Bake one object's lighting to a client .dds (into its 'Lightmap' UV)
# ===========================================================================
def _ensure_bake_material(obj, image):
    if obj.data.materials and obj.data.materials[0] is not None:
        mat = obj.data.materials[0]
    else:
        mat = bpy.data.materials.new(obj.name + "_bake")
        if obj.data.materials:
            obj.data.materials[0] = mat
        else:
            obj.data.materials.append(mat)
    mat.use_nodes = True
    nt = mat.node_tree
    node = next((n for n in nt.nodes if n.type == "TEX_IMAGE"
                 and n.name == "ROSE_BakeTarget"), None)
    if node is None:
        node = nt.nodes.new("ShaderNodeTexImage")
        node.name = "ROSE_BakeTarget"; node.location = (-400, 0)
    node.image = image
    uvmap = next((n for n in nt.nodes if n.type == "UVMAP"
                  and n.name == "ROSE_BakeUV"), None)
    if uvmap is None:
        uvmap = nt.nodes.new("ShaderNodeUVMap")
        uvmap.name = "ROSE_BakeUV"; uvmap.location = (-650, 0)
    uvmap.uv_map = "Lightmap"
    nt.links.new(uvmap.outputs[0], node.inputs[0])
    for n in nt.nodes:
        n.select = False
    node.select = True
    nt.nodes.active = node


def bake_object_to_dds(obj, out_path, res, props):
    scene = bpy.context.scene
    image = bpy.data.images.get("ROSE_LightmapBake")
    if image is not None:
        bpy.data.images.remove(image)
    image = bpy.data.images.new("ROSE_LightmapBake", width=res, height=res, alpha=True)
    _ensure_bake_material(obj, image)
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    scene.cycles.bake_type = "DIFFUSE"
    bake = scene.render.bake
    bake.use_pass_direct = True
    bake.use_pass_indirect = True
    bake.use_pass_color = False
    bake.margin = 4
    bpy.ops.object.bake(type="DIFFUSE")
    write_lightmap_dds(out_path, res, res, list(image.pixels), flip_v=True,
                       compress=bool(getattr(props, "compress_dxt1", True)))
    return out_path


def bake_part_tile(obj, px, props, underground=False):
    """Bake one part-object's lightmap into a px x px image; return its RGBA floats
    (Blender order: row 0 = v=0). Other scene objects still occlude, so cross-object
    shadows are captured. The mode controls how 'lightmap' vs 'lit' it looks:
      AO      - ambient occlusion only: soft, even, no directional sun (stock ROSE look)
      SHADOW  - sun + cast-shadow factor, no N.L term (lit faces stay bright)
      DIFFUSE - full diffuse irradiance incl. N.L (hard directional shading)
    OUTDOORS, object lightmaps are a shadow/occlusion MULTIPLIER -- the object shader
    does the real-time sun N.L -- so baking full diffuse double-counts the sun and bakes
    a hard dark side onto fixed faces. AO/SHADOW avoid that. INDOORS (underground) there
    is no real-time sun on objects, so the lightmap IS the lighting: bake DIFFUSE raw so
    objects pick up the placed point lights (see the underground branch below)."""
    scene = bpy.context.scene
    image = bpy.data.images.get("ROSE_PartBake")
    if image is not None:
        bpy.data.images.remove(image)
    image = bpy.data.images.new("ROSE_PartBake", width=px, height=px, alpha=True)
    _ensure_bake_material(obj, image)
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bake = scene.render.bake
    bake.margin = max(4, px // 16)
    mode = getattr(props, "object_bake_mode", "BOTH")

    def _do(kind):
        if kind == "AO":
            scene.cycles.bake_type = "AO"
            bpy.ops.object.bake(type="AO")
        elif kind == "SHADOW":
            scene.cycles.bake_type = "SHADOW"
            bpy.ops.object.bake(type="SHADOW")
        else:  # DIFFUSE (full lit, for comparison)
            scene.cycles.bake_type = "DIFFUSE"
            bake.use_pass_direct = True
            bake.use_pass_indirect = True
            bake.use_pass_color = False
            bpy.ops.object.bake(type="DIFFUSE")
        return list(image.pixels)

    # Indoor zones (caves) are lit ENTIRELY by the baked lightmap -- there is no
    # real-time sun on objects -- so the object lightmap must carry the full point-
    # light irradiance, exactly like the terrain (bake_object_to_dds). Bake DIFFUSE
    # direct+indirect and return it RAW: AO/Shadow ignore the placed lights and bake a
    # flat grey, and the brightness/shadow-floor remap below lifts the darks and caps
    # the bright pools -- the opposite of the dark-cave-with-warm-pools look. Level is
    # tuned via Light Power / Indoor Ambient instead.
    if underground:
        return _do("DIFFUSE")

    if mode == "BOTH":
        ao = _do("AO")        # soft, even base + contact occlusion
        sh = _do("SHADOW")    # directional sun cast-shadows (differ per instance)
        px_data = list(ao)
        for i in range(0, len(px_data), 4):   # multiply: AO base * directional shadow
            px_data[i]     = ao[i]     * sh[i]
            px_data[i + 1] = ao[i + 1] * sh[i + 1]
            px_data[i + 2] = ao[i + 2] * sh[i + 2]
    else:
        px_data = _do(mode)
    b = getattr(props, "object_brightness", 1.0)
    floor = getattr(props, "object_shadow_floor", 0.0)
    if b != 1.0 or floor > 0.0:
        span = 1.0 - floor
        for i in range(0, len(px_data), 4):     # remap RGB to floor..1, then *brightness; keep alpha
            px_data[i]     = b * (floor + span * px_data[i])
            px_data[i + 1] = b * (floor + span * px_data[i + 1])
            px_data[i + 2] = b * (floor + span * px_data[i + 2])
    return px_data


def _blit_cell(buf, dim, tile, px, col, row, vflip):
    """Composite a px x px tile (Blender row 0 = v=0) into a TOP-FIRST atlas buffer
    at grid cell (col, row). uv2.v=0 -> cell top, matching the client's
    (row + v)/perW sampling. Toggle vflip if it bakes upside-down in-game."""
    for ty in range(px):
        ay = row * px + ((px - 1 - ty) if vflip else ty)
        srow = ty * px * 4
        drow = (ay * dim + col * px) * 4
        for tx in range(px):
            si = srow + tx * 4
            di = drow + tx * 4
            buf[di] = tile[si]; buf[di + 1] = tile[si + 1]
            buf[di + 2] = tile[si + 2]; buf[di + 3] = tile[si + 3]


def bake_objects_atlas(mods, part_objs, kind_prefix, out_dir, props, report, progress=None,
                       underground=False):
    """Bake every part-object into shared 512x512 atlas pages and build the matching
    Lit. Returns (Lit, baked_count). kind_prefix is 'Object' or 'Building'; atlas
    pages are written as <prefix>_<px>_<page>.dds in out_dir."""
    Lit = mods["lit"].Lit
    LitObject = mods["lit"].LitObject
    LitPart = mods["lit"].LitPart
    if not part_objs:
        return None, 0
    px = int(props.object_resolution)
    if ATLAS_DIM % px != 0:
        report({"WARNING"}, "Object Res %d doesn't divide %d; falling back to 64"
               % (px, ATLAS_DIM))
        px = 64
    perW = ATLAS_DIM // px
    per_page = perW * perW

    # stable order: by placement object id, then part id
    part_objs = sorted(part_objs, key=lambda o: (int(o["rose_object_id"]),
                                                 int(o["rose_part_id"])))
    pages = {}            # page index -> top-first RGBA float buffer
    obj_map = {}          # object_id -> LitObject
    order = []            # object_id encounter order
    dds_order = []        # atlas filenames in encounter order
    cell = 0
    n = 0
    isolate = getattr(props, "isolate_parts", True)
    if isolate:
        for ob in bpy.data.objects:
            if ob.type == "MESH":
                ob.hide_render = True
    for o in part_objs:
        if isolate:
            o.hide_render = False
        tile_px = bake_part_tile(o, px, props, underground)
        if isolate:
            o.hide_render = True
        if progress is not None:
            progress("%s %s" % (kind_prefix, o.get("rose_mesh_base", o.name)))
        page, idx = divmod(cell, per_page)
        col, row = idx % perW, idx // perW
        buf = pages.get(page)
        if buf is None:
            buf = [0.0] * (ATLAS_DIM * ATLAS_DIM * 4)
            pages[page] = buf
        _blit_cell(buf, ATLAS_DIM, tile_px, px, col, row, OBJECT_LIGHTMAP_V_FLIP)
        dds_name = "%s_%d_%d.dds" % (kind_prefix, px, page)
        if dds_name not in dds_order:
            dds_order.append(dds_name)
        oid = int(o["rose_object_id"]); pid = int(o["rose_part_id"])
        base = o.get("rose_mesh_base", "mesh"); tile = o["rose_tile"]
        tga = "%s_%s_%d_%d_%s_LightingMap.tga" % (base, kind_prefix, oid, pid, tile)
        lo = obj_map.get(oid)
        if lo is None:
            lo = LitObject(object_id=oid); obj_map[oid] = lo; order.append(oid)
        lo.parts.append(LitPart(name=tga, part_id=pid, dds_name=dds_name,
                                lightmap_id=0, pixels_per_object=px,
                                objects_per_width=perW, map_position=idx))
        cell += 1; n += 1

    if isolate:
        for ob in bpy.data.objects:
            if ob.type == "MESH":
                ob.hide_render = False

    compress = bool(getattr(props, "compress_dxt1", True))
    for page, buf in pages.items():
        write_lightmap_dds(os.path.join(out_dir, "%s_%d_%d.dds" % (kind_prefix, px, page)),
                           ATLAS_DIM, ATLAS_DIM, buf, flip_v=False,  # buffer already top-first
                           compress=compress)

    lit = Lit()
    lit.objects = [obj_map[oid] for oid in order]
    lit.dds = dds_order
    didx = {nm: i for i, nm in enumerate(lit.dds)}     # LightmapID = index into dds list
    for lo in lit.objects:
        for p in lo.parts:
            p.lightmap_id = didx[p.dds_name]
    return lit, n


def _frame_selected(context):
    for area in context.screen.areas:
        if area.type == "VIEW_3D":
            for region in area.regions:
                if region.type == "WINDOW":
                    try:
                        with context.temp_override(area=area, region=region):
                            bpy.ops.view3d.view_selected()
                    except Exception:
                        pass
            break


# ===========================================================================
#  Zone scanning (LIST_ZONE.STB) + assembly
# ===========================================================================
_ZONES = []   # cached list of ZoneInfo from the last scan


def _zone_items(self, context):
    items = []
    for i, z in enumerate(_ZONES):
        tag = "  [underground]" if z.is_underground else ""
        items.append((str(i), "%s%s" % (z.name or z.zon_path, tag), z.zon_path))
    return items or [("-1", "<scan a 3Ddata ROOT first>", "")]


def _stb_path(data_root, mods):
    hit = data_root.resolve("stb/LIST_ZONE.STB")
    if hit:
        return hit
    cand = os.path.join(data_root.root, "stb", "LIST_ZONE.STB")
    return cand if os.path.exists(cand) else None


def _clear_rose_scene():
    """Remove everything a previous Load Zone created, so zones never mix. Two zones
    can share a tile name (e.g. '32_32'); leftover objects from an earlier load get
    swept into the bake, the LIT object ids collide, and objects end up sampling the
    wrong atlas cells -> fixed wrong/black patches on the model."""
    doomed = [o for o in bpy.data.objects
              if o.name.startswith("ROSE_")
              or "rose_tile" in o.keys() or "rose_kind" in o.keys()]
    for o in doomed:
        bpy.data.objects.remove(o, do_unlink=True)
    for m in list(bpy.data.meshes):
        if m.users == 0:
            bpy.data.meshes.remove(m)
    for la in list(bpy.data.lights):          # placed point lights (+ any orphaned sun)
        if la.users == 0:
            bpy.data.lights.remove(la)
    _ZMS_CACHE.clear()
    _OBJ_MESH_CACHE.clear()


def _clean_lightmap_dir(lm_dir):
    """Delete stale object-lightmap artifacts in one tile's LightMap folder so a
    re-bake never leaves a mismatched page behind (the client indexes each LIT's
    DDS list by position; a leftover/renamed page shifts every LightmapID after
    it). Scoped to known output names only. Returns count removed."""
    import glob
    pats = ("Object_*.dds", "Building_*.dds",
            "ObjectLightMapData.lit", "BuildingLightMapData.lit", "ListData.dat")
    removed = 0
    for pat in pats:
        for f in glob.glob(os.path.join(lm_dir, pat)):
            try:
                os.remove(f); removed += 1
            except Exception:
                pass
    return removed


def assemble_zone(mods, data_root, zone, props, report):
    """Load every tile of the zone: terrain placed by its IFO matrix + all
    decoration/building objects. Returns (parent_empty, tile_dirs)."""
    DataRoot = mods["paths"].DataRoot
    Ifo = mods["ifo"].Ifo
    BlockType = mods["ifo"].BlockType
    Zsc = mods["zsc"].Zsc

    zon = data_root.resolve(zone.zon_path)
    if not zon:
        report({"ERROR"}, "Could not resolve .zon: %s" % zone.zon_path)
        return None, []
    zone_dir = os.path.dirname(zon)

    deco = bld = None
    dz = data_root.resolve(zone.deco_zsc)
    cz = data_root.resolve(zone.cnst_zsc)
    if dz:
        try: deco = Zsc.read(dz)
        except Exception as e: report({"WARNING"}, "DECO ZSC failed: %s" % e)
    if cz:
        try: bld = Zsc.read(cz)
        except Exception as e: report({"WARNING"}, "CNST ZSC failed: %s" % e)

    parent = bpy.data.objects.new("ROSE_Zone_" + (zone.name or "zone"), None)
    bpy.context.collection.objects.link(parent)

    tiles = sorted(f for f in os.listdir(zone_dir) if f.lower().endswith(".him"))
    tile_dirs = []
    n_obj = 0
    n_lights = 0
    place_lights = (bool(getattr(props, "place_lights", True))
                    and bool(getattr(zone, "is_underground", False)))
    for him_name in tiles:
        tile = os.path.splitext(him_name)[0]
        him_path = os.path.join(zone_dir, him_name)
        ifo_path = os.path.join(zone_dir, tile + ".ifo")
        try:
            w, h, gs, heights = read_him(him_path)
        except Exception as e:
            report({"WARNING"}, "HIM %s failed: %s" % (tile, e)); continue

        origin = (0.0, 0.0)
        ifo = None
        if os.path.exists(ifo_path):
            try:
                ifo = Ifo.read(ifo_path)
                eco = ifo.block(BlockType.ECONOMYDATA)
                if eco:
                    tr = eco.raw.get("world_translation") or (0.0, 0.0, 0.0)
                    cx = eco.raw.get("map_cell_x")
                    cy = eco.raw.get("map_cell_y")
                    # Some maps (e.g. hand-built test caves) ship a ZEROED ECONOMYDATA
                    # matrix, so world_translation is (0,0,0) for EVERY tile and they all
                    # stack on the world origin -- you see one tile, not the grid. The
                    # map_cell_x/_y are still correct, so when the matrix translation is
                    # degenerate, derive the origin from the grid coords instead. ROSE
                    # tiles step 16000 units (cell 32 = world 0):
                    #   tx = (cx - 32) * 16000 ;  tz = (32 - cy) * 16000
                    # (verified against JPT01). For a correctly-authored map this branch
                    # can only trigger on the (32,32) tile, which is (0,0) either way, so
                    # it never moves a good tile.
                    if (abs(tr[0]) < 1.0 and abs(tr[2]) < 1.0
                            and cx is not None and cy is not None):
                        tr = ((cx - 32) * 16000.0, 0.0, (32 - cy) * 16000.0)
                    origin = ((tr[0] + WORLD_OFFSET) * UNIT, (tr[2] + WORLD_OFFSET) * UNIT)
            except Exception as e:
                report({"WARNING"}, "IFO %s failed: %s" % (tile, e))

        terr = build_terrain(w, h, gs, heights, tile, origin=origin)
        terr.parent = parent
        terr["rose_tile"] = tile
        terr["rose_zone_dir"] = zone_dir
        tile_dirs.append((tile, os.path.join(props.output_dir or zone_dir, tile)))

        if ifo is None:
            continue
        # placements: decorations -> DECO zsc, buildings -> CNST zsc.
        # One Blender object per PART (lightmaps are per-part), tagged for the atlas.
        for entries, zsc, zid, kind in (
                (ifo.decorations, deco, 1, "deco"),
                (ifo.buildings, bld, 2, "bld")):
            if zsc is None:
                continue
            for e in entries:
                if e.obj_id < 0 or e.obj_id >= len(zsc.objects):
                    continue
                for pi in range(len(zsc.objects[e.obj_id].parts)):
                    mesh, mesh_base = build_part_mesh(mods, data_root, zsc, zid,
                                                      e.obj_id, pi)
                    if mesh is None:
                        continue
                    o = place_object(mesh, e, "%s_%s_o%d_p%d" % (tile, kind, e.index, pi),
                                     parent)
                    o["rose_tile"] = tile
                    o["rose_kind"] = kind
                    o["rose_zone_dir"] = zone_dir
                    o["rose_object_id"] = e.index     # IFO 1-based placement index
                    o["rose_part_id"] = pi
                    o["rose_mesh_base"] = mesh_base
                    n_obj += 1
                if place_lights:
                    n_lights += spawn_object_lights(zsc.objects[e.obj_id], e,
                                                    zsc, parent, props)

    n_marker = (spawn_marker_lights(zone_dir, parent, props)
                if bool(getattr(props, "marker_lights", True)) else 0)

    parent.scale = (props.import_scale,) * 3   # extra viewport rescale if wanted
    report({"INFO"}, "Zone '%s': %d tiles, %d object parts, %d object lights, %d marker lights"
           % (zone.name or "zone", len(tiles), n_obj, n_lights, n_marker))
    return parent, tile_dirs


# ===========================================================================
#  Properties
# ===========================================================================
class ROSE_lightmap_props(PropertyGroup):
    tools_dir: StringProperty(name="Tools Folder", subtype="DIR_PATH", default="",
                              description="Folder with rose_stb/ifo/zsc/zms/paths/lit.py "
                                          "(blank = next to this add-on)")
    data_root: StringProperty(name="3Ddata ROOT", subtype="DIR_PATH", default="",
                              description="The 3Ddata folder (contains stb\\LIST_ZONE.STB)")
    zone: EnumProperty(name="Zone", items=_zone_items)

    sun_strength: FloatProperty(name="Sun", default=1.0, min=0.0, soft_max=10.0)
    sky_strength: FloatProperty(name="Sky", default=0.2, min=0.0, soft_max=5.0)
    sky_color: FloatVectorProperty(name="Sky Colour", subtype="COLOR",
                                   default=(0.45, 0.62, 0.95), min=0.0, max=1.0, size=3)
    place_lights: BoolProperty(name="Place Lights (indoor)", default=True,
        description="In underground zones, spawn a point light at each placed object's "
                    "type-2 light point (ZSC 3LIGHT_* effect points) so the cave bakes "
                    "lit instead of black. Ignored outdoors (the sun dominates there)")
    marker_lights: BoolProperty(name="Use LightSources.txt", default=True,
        description="Read <zone folder>\\LightSources.txt (editor-placed light markers) "
                    "and spawn a point light at each. Independent of the underground flag, "
                    "so you can light a cave for test bakes before it's flagged underground. "
                    "Positions are ROSE world metres -- the same space as object placements")
    marker_height: FloatProperty(name="Marker Height", default=0.0, min=0.0, soft_max=20.0,
        description="Metres to lift each LightSources.txt marker above its authored Z before "
                    "baking. With the editor's Light tool you place each light at the height "
                    "you want (the cube sits exactly where the light bakes), so leave this at "
                    "0 for WYSIWYG. Raise it only for markers authored at floor level (e.g. a "
                    "light snapped to an object base): a point light level with the floor lights "
                    "it at a grazing angle (~0) and bakes a pinprick, leaving the ground black")
    light_energy: FloatProperty(name="Light Power", default=800.0, min=0.0, soft_max=20000.0,
        description="Watts per placed point light. Caves need a lot of power; tune live "
                    "like the Sun/Sky sliders, then re-bake")
    light_radius: FloatProperty(name="Light Size", default=0.4, min=0.0, soft_max=5.0,
        description="Soft-shadow radius (m) of each placed light; larger = softer shadows")
    light_color: FloatVectorProperty(name="Light Colour", subtype="COLOR",
        default=(1.0, 0.78, 0.5), min=0.0, max=1.0, size=3,
        description="Base colour for placed lights. Fire/road lights get auto-tinted "
                    "when 'Tint By Name' is on")
    tint_by_name: BoolProperty(name="Tint By Name", default=True,
        description="Colour each light from its 3LIGHT_* effect name (fire -> warm orange, "
                    "road/lamp -> warm white) instead of the flat Light Colour")
    indoor_ambient: FloatProperty(name="Indoor Ambient", default=0.0, min=0.0, soft_max=1.0,
        description="Faint world fill for underground bakes so areas far from any light "
                    "aren't pure black (0 = fully dark, faithful to ROSE caves)")
    samples: IntProperty(name="Samples", default=128, min=1, soft_max=1024)
    import_scale: FloatProperty(name="Viewport Scale", default=1.0, min=0.0001, soft_max=10.0,
                                description="Extra scale on the whole zone for the viewport "
                                            "(geometry is already in metres)")
    resolution: IntProperty(name="Terrain Res", default=512, min=16, max=2048)
    object_resolution: IntProperty(name="Object Res", default=128, min=16, max=512,
                                   description="Atlas cell size; must divide 512 (32/64/128/256)")
    object_bake_mode: EnumProperty(name="Object Shading", default="BOTH",
        description="How object lightmaps are baked (terrain is unaffected)",
        items=[("BOTH", "AO x Shadow",
                "AO soft/even base + contact occlusion, MULTIPLIED by directional sun "
                "cast-shadows. Non-glowing base + per-instance directional shadows; "
                "closest to stock ROSE buildings (recommended)"),
               ("AO", "AO only (soft)",
                "Ambient occlusion only - soft, even, but no directional sun, so every "
                "copy of a model bakes identically regardless of facing"),
               ("SHADOW", "Shadow only",
                "Sun cast-shadow factor only - directional, but lit faces bake near-white "
                "(glowing) and the directional detail is faint against that bright base"),
               ("DIFFUSE", "Diffuse (lit)",
                "Full diffuse incl. directional sun - hard bright/dark split (old behaviour)")])
    object_brightness: FloatProperty(name="Object Brightness", default=0.7, min=0.05, max=1.0,
                                     description="Overall level of the object lightmap. AO/Shadow "
                                                 "leave lit faces near 1.0 (blown out); lower this "
                                                 "until objects match the game's stock brightness")
    object_shadow_floor: FloatProperty(name="Shadow Floor", default=0.35, min=0.0, max=0.95,
                                       description="Lift on the darkest baked value so occluded "
                                                   "areas never go pure black (0 = allow full black; "
                                                   "higher = softer, flatter shadows)")
    isolate_parts: BoolProperty(name="Isolate Parts", default=False,
                                description="Bake each object part alone (siblings + other objects "
                                            "hidden). Off is usually right: parts SHOULD shade each "
                                            "other (e.g. a house shading its own underside). Use the "
                                            "Shadow Floor to soften contact darkening instead")
    bake_objects: BoolProperty(name="Bake Objects Too", default=True)
    clean_lightmap: BoolProperty(name="Clean LightMap First", default=True,
                                 description="Delete old atlas pages + .LIT in each tile's "
                                             "LightMap folder before writing, so a re-bake "
                                             "never mixes stale files (the client indexes the "
                                             "LIT's DDS list by position)")
    compress_dxt1: BoolProperty(name="Compress (DXT1)", default=True,
                                description="Write lightmap atlases and terrain maps as DXT1 "
                                            "(BC1) -- 8x smaller and the format stock ROSE and "
                                            "the map editor expect. Uncompressed atlases are "
                                            "~1 MB each and overflow the 32-bit editor on dense "
                                            "maps. Turn off only to inspect raw uncompressed bakes")
    output_dir: StringProperty(name="Output Zone Folder", subtype="DIR_PATH", default="",
                               description="Where to write (blank = the zone's own folder)")
    last_him_dir: StringProperty(default="")
    last_tile: StringProperty(default="")


# ===========================================================================
#  Operators -- single-tile (kept from v0.1)
# ===========================================================================
class ROSE_OT_import_him(Operator, ImportHelper):
    bl_idname = "rose.import_him"
    bl_label = "Import .HIM Terrain"
    bl_description = "Load a single ROSE .HIM heightmap as a terrain mesh"
    filename_ext = ".him"
    filter_glob: StringProperty(default="*.him;*.HIM", options={"HIDDEN"})

    def execute(self, context):
        props = context.scene.rose_lightmap
        try:
            w, h, gs, heights = read_him(self.filepath)
        except Exception as e:
            self.report({"ERROR"}, "HIM read failed: %s" % e)
            return {"CANCELLED"}
        tile = os.path.splitext(os.path.basename(self.filepath))[0]
        obj = build_terrain(w, h, gs, heights, tile, origin=(0.0, 0.0))
        props.last_him_dir = os.path.dirname(self.filepath)
        props.last_tile = tile
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        context.view_layer.objects.active = obj
        _frame_selected(context)
        self.report({"INFO"}, "Imported %s" % tile)
        return {"FINISHED"}


class ROSE_OT_setup_lighting(Operator):
    bl_idname = "rose.setup_lighting"
    bl_label = "Setup Sun + Sky"
    bl_description = "Create the outdoor lighting rig (Cycles)"

    def execute(self, context):
        setup_lighting(context.scene.rose_lightmap)
        self.report({"INFO"}, "Lighting rig ready (Cycles)")
        return {"FINISHED"}


# ===========================================================================
#  Operators -- zone (v0.2+)
# ===========================================================================
class ROSE_OT_scan_zones(Operator):
    bl_idname = "rose.scan_zones"
    bl_label = "Scan Zones"
    bl_description = "Read stb\\LIST_ZONE.STB under the 3Ddata ROOT and list every zone"

    def execute(self, context):
        props = context.scene.rose_lightmap
        try:
            mods = _load_modules(bpy.path.abspath(props.tools_dir) if props.tools_dir else "")
        except Exception as e:
            self.report({"ERROR"}, "Could not import rose_* modules: %s" % e)
            return {"CANCELLED"}
        root = bpy.path.abspath(props.data_root)
        if not os.path.isdir(root):
            self.report({"ERROR"}, "Set a valid 3Ddata ROOT first")
            return {"CANCELLED"}
        data_root = mods["paths"].DataRoot(root)
        stb = _stb_path(data_root, mods)
        if not stb:
            self.report({"ERROR"}, "stb\\LIST_ZONE.STB not found under ROOT")
            return {"CANCELLED"}
        try:
            zones = mods["stb"].read_zone_list(stb)
        except Exception as e:
            self.report({"ERROR"}, "LIST_ZONE.STB parse failed: %s" % e)
            return {"CANCELLED"}
        _ZONES.clear()
        _ZONES.extend(zones)
        self.report({"INFO"}, "Found %d zones" % len(zones))
        return {"FINISHED"}


class ROSE_OT_load_zone(Operator):
    bl_idname = "rose.load_zone"
    bl_label = "Load Zone"
    bl_description = "Assemble the selected zone: all terrain tiles + placed objects"

    def execute(self, context):
        props = context.scene.rose_lightmap
        if not _ZONES or props.zone in ("", "-1"):
            self.report({"ERROR"}, "Scan zones and pick one first")
            return {"CANCELLED"}
        try:
            mods = _load_modules(bpy.path.abspath(props.tools_dir) if props.tools_dir else "")
        except Exception as e:
            self.report({"ERROR"}, "Module import failed: %s" % e)
            return {"CANCELLED"}
        zone = _ZONES[int(props.zone)]
        data_root = mods["paths"].DataRoot(bpy.path.abspath(props.data_root))

        # Per-map settings: restore this zone's saved tool settings (litsave.txt, next to
        # its .zon / LightSources.txt) so its tuned look comes back; with no file the
        # managed settings reset to the tool defaults. Done BEFORE assembly so light
        # placement (power / colour / height / range) uses the map's own values.
        zon = data_root.resolve(zone.zon_path)
        if load_map_settings(os.path.dirname(zon) if zon else "", props):
            self.report({"INFO"}, "Loaded %s for this zone" % LITSAVE_FILE)

        _clear_rose_scene()   # fresh slate: never mix zones (shared tile names pollute the bake)
        parent, tile_dirs = assemble_zone(mods, data_root, zone, props, self.report)
        if parent is None:
            return {"CANCELLED"}

        # Focus the viewport on the FIRST tile of the map instead of framing the whole
        # zone (a multi-tile zone otherwise loads zoomed all the way out). Prefer the
        # first tile, in load order, that actually has placed objects, so you land on
        # content rather than an empty corner; fall back to the first tile, then the zone.
        terr_by_tile = dict((c.get("rose_tile"), c) for c in parent.children
                            if "rose_tile" in c.keys() and "rose_kind" not in c.keys())
        tiles_with_objs = set(c.get("rose_tile") for c in parent.children
                              if "rose_kind" in c.keys() and "rose_tile" in c.keys())
        focus = None
        for tile, _dir in tile_dirs:
            if tile in tiles_with_objs and tile in terr_by_tile:
                focus = terr_by_tile[tile]; break
        if focus is None and tile_dirs:
            focus = terr_by_tile.get(tile_dirs[0][0])
        if focus is None:
            focus = parent

        bpy.ops.object.select_all(action="DESELECT")
        focus.select_set(True)
        context.view_layer.objects.active = focus
        _frame_selected(context)
        return {"FINISHED"}


class ROSE_OT_bake_zone(Operator):
    bl_idname = "rose.bake_zone"
    bl_label = "Bake Zone"
    bl_description = ("Bake terrain (per tile -> PlaneLightingMap.dds) and, if enabled, "
                      "pack object lightmaps into <tile>\\LightMap\\ atlases + .LIT")

    def execute(self, context):
        props = context.scene.rose_lightmap
        if not _ZONES or props.zone in ("", "-1"):
            self.report({"ERROR"}, "Scan zones and pick one first")
            return {"CANCELLED"}
        zone = _ZONES[int(props.zone)]
        setup_lighting(props, underground=zone.is_underground)

        terrains = [o for o in bpy.data.objects
                    if o.type == "MESH" and "rose_tile" in o.keys()
                    and "rose_kind" not in o.keys()]
        objects = [o for o in bpy.data.objects
                   if o.type == "MESH" and "rose_kind" in o.keys()]
        if not terrains:
            self.report({"ERROR"}, "Load a zone first")
            return {"CANCELLED"}

        out_root = bpy.path.abspath(props.output_dir) if props.output_dir else ""
        if not out_root:
            for t in terrains:
                if t.get("rose_zone_dir"):
                    out_root = t["rose_zone_dir"]; break
        if not out_root or not os.path.isabs(out_root):
            self.report({"ERROR"},
                        "No writable output folder. Set 'Output Zone Folder' to the "
                        "zone's folder, or save the .blend first.")
            return {"CANCELLED"}

        # Record the settings this bake used, next to the map (litsave.txt), so its
        # tuned look reloads with the zone next time. Save into the SOURCE zone folder
        # (where the .zon / LightSources.txt live -- terrain carries it as rose_zone_dir),
        # not necessarily the output folder.
        litsave_dir = terrains[0].get("rose_zone_dir") or out_root
        if save_map_settings(litsave_dir, props):
            print("ROSE bake: saved settings -> %s" % os.path.join(litsave_dir, LITSAVE_FILE))

        # Load object modules up-front so we can total the work units. Non-fatal:
        # if rose_lit is missing we still bake terrain.
        mods = None
        if props.bake_objects and objects:
            try:
                mods = _load_modules(bpy.path.abspath(props.tools_dir)
                                     if props.tools_dir else "")
            except Exception as e:
                self.report({"WARNING"},
                            "Object atlases need rose_lit (%s); baking terrain only" % e)
                mods = None

        total = len(terrains) + (len(objects) if (props.bake_objects and mods) else 0)
        wm = context.window_manager
        ws = context.workspace
        done = [0]

        def tick(label):
            done[0] += 1
            wm.progress_update(done[0])
            msg = "ROSE bake %d/%d  %s" % (done[0], total, label)
            try: ws.status_text_set(msg)
            except Exception: pass
            print(msg)

        n_terr = n_obj = 0
        wm.progress_begin(0, max(1, total))
        print("ROSE bake: %d terrain tiles + %d object parts"
              % (len(terrains), len(objects) if (props.bake_objects and mods) else 0))
        try:
            for terr in terrains:
                tile = terr["rose_tile"]
                tile_dir = os.path.join(out_root, tile)
                os.makedirs(tile_dir, exist_ok=True)
                want = ("%s_planelightingmap.dds" % tile).lower()
                fname = "%s_PlaneLightingMap.dds" % tile
                for f in os.listdir(tile_dir):
                    if f.lower() == want:
                        fname = f; break
                try:
                    bake_object_to_dds(terr, os.path.join(tile_dir, fname),
                                       props.resolution, props)
                    n_terr += 1
                except Exception as e:
                    self.report({"WARNING"}, "Terrain %s bake failed: %s" % (tile, e))
                tick("terrain %s" % tile)

            if props.bake_objects and objects and mods:
                from collections import defaultdict
                by_tile = defaultdict(lambda: {"deco": [], "bld": []})
                for o in objects:
                    by_tile[o["rose_tile"]][o.get("rose_kind", "deco")].append(o)
                lit_obj_name = getattr(mods["lit"], "OBJECT_LIT_NAME", "ObjectLightMapData.lit")
                lit_bld_name = getattr(mods["lit"], "BUILDING_LIT_NAME", "BuildingLightMapData.lit")
                Lit = mods["lit"].Lit
                # Every tile in the zone must carry BOTH .lit files, even when a kind is
                # empty. The map editor loads <tile>\LightMap\OBJECTLIGHTMAPDATA.LIT and
                # BUILDINGLIGHTMAPDATA.LIT unconditionally and throws "Missing File" if
                # either is absent (the client tolerates a missing building lit; the
                # editor does not). So a deco-only tile still needs an empty building lit,
                # and a bare tile needs two empty lits. An empty Lit() is a valid 8-byte
                # file: objectCount=0, ddsCount=0.
                all_tiles = [t["rose_tile"] for t in terrains]
                for tile in all_tiles:
                    kinds = by_tile.get(tile, {"deco": [], "bld": []})
                    lm_dir = os.path.join(out_root, tile, "LightMap")
                    os.makedirs(lm_dir, exist_ok=True)
                    if props.clean_lightmap:
                        nrm = _clean_lightmap_dir(lm_dir)
                        if nrm:
                            print("ROSE bake: cleaned %d stale file(s) in %s" % (nrm, lm_dir))
                    for kind, prefix, litname in (("deco", "Object", lit_obj_name),
                                                  ("bld", "Building", lit_bld_name)):
                        group = kinds.get(kind) or []
                        litpath = os.path.join(lm_dir, litname)
                        try:
                            if group:
                                lit, cnt = bake_objects_atlas(mods, group, prefix, lm_dir,
                                                              props, self.report, progress=tick,
                                                              underground=zone.is_underground)
                                lit = lit if lit is not None else Lit()
                                lit.write(litpath)
                                n_obj += cnt
                            else:
                                Lit().write(litpath)   # empty but valid (0 objects, 0 atlases)
                        except Exception as e:
                            self.report({"WARNING"}, "%s atlas (%s) failed: %s"
                                        % (prefix, tile, e))
        finally:
            wm.progress_end()
            try: ws.status_text_set(None)
            except Exception: pass

        self.report({"INFO"}, "Baked %d terrain tiles, %d object lightmaps" % (n_terr, n_obj))
        return {"FINISHED"}


# ===========================================================================
#  Operator -- save per-map settings (litsave.txt) without baking
# ===========================================================================
class ROSE_OT_save_settings(Operator):
    bl_idname = "rose.save_settings"
    bl_label = "Save Map Settings"
    bl_description = ("Write the current tool settings to litsave.txt in the loaded zone's "
                     "folder (next to LightSources.txt). Load Zone restores them; Bake also "
                     "writes them; the game client ignores the file")

    def execute(self, context):
        props = context.scene.rose_lightmap
        zone_dir = ""
        for o in bpy.data.objects:                 # loaded terrain carries the source folder
            if o.type == "MESH" and o.get("rose_zone_dir"):
                zone_dir = o["rose_zone_dir"]; break
        if not zone_dir:
            self.report({"ERROR"}, "Load a zone first (no zone folder to save into)")
            return {"CANCELLED"}
        path = save_map_settings(zone_dir, props)
        if not path:
            self.report({"ERROR"}, "Could not write %s" % LITSAVE_FILE)
            return {"CANCELLED"}
        self.report({"INFO"}, "Saved %s" % path)
        return {"FINISHED"}


# ===========================================================================
#  Panel
# ===========================================================================
class ROSE_PT_lightmap(Panel):
    bl_label = "ROSE Lightmap"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ROSE Lightmap"

    def draw(self, context):
        props = context.scene.rose_lightmap
        layout = self.layout

        box = layout.box()
        box.label(text="Zone (whole map)", icon="WORLD")
        box.prop(props, "data_root")
        box.prop(props, "tools_dir")
        box.operator("rose.scan_zones", icon="FILE_REFRESH")
        box.prop(props, "zone")
        box.prop(props, "import_scale")
        box.operator("rose.load_zone", icon="IMPORT")

        lb = layout.box()
        lb.label(text="Lighting (outdoor)")
        lb.prop(props, "sun_strength")
        lb.prop(props, "sky_strength")
        lb.prop(props, "sky_color")
        lb.prop(props, "samples")
        lb.operator("rose.setup_lighting", icon="LIGHT_SUN")

        ib = layout.box()
        ib.label(text="Lighting (indoor / caves)", icon="LIGHT")
        ib.prop(props, "place_lights")
        ib.prop(props, "marker_lights")
        ib.prop(props, "marker_height")
        ib.prop(props, "light_energy")
        ib.prop(props, "light_color")
        ib.prop(props, "tint_by_name")
        ib.prop(props, "light_radius")
        ib.prop(props, "indoor_ambient")

        bb = layout.box()
        bb.label(text="Bake")
        bb.prop(props, "resolution")
        bb.prop(props, "object_resolution")
        bb.prop(props, "object_bake_mode")
        bb.prop(props, "object_brightness")
        bb.prop(props, "object_shadow_floor")
        bb.prop(props, "isolate_parts")
        bb.prop(props, "bake_objects")
        bb.prop(props, "clean_lightmap")
        bb.prop(props, "compress_dxt1")
        bb.prop(props, "output_dir")
        bb.operator("rose.bake_zone", icon="RENDER_STILL")
        bb.operator("rose.save_settings", icon="FILE_TICK")

        sb = layout.box()
        sb.label(text="Single tile (v0.1)")
        sb.operator("rose.import_him", icon="IMPORT")


# ===========================================================================
#  Register
# ===========================================================================
_classes = (
    ROSE_lightmap_props,
    ROSE_OT_import_him,
    ROSE_OT_setup_lighting,
    ROSE_OT_scan_zones,
    ROSE_OT_load_zone,
    ROSE_OT_bake_zone,
    ROSE_OT_save_settings,
    ROSE_PT_lightmap,
)


def register():
    for c in _classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.rose_lightmap = bpy.props.PointerProperty(type=ROSE_lightmap_props)


def unregister():
    del bpy.types.Scene.rose_lightmap
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()

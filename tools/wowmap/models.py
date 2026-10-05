"""#171: character models. Client M2 + skin -> a small mesh blob the drawer can render.

The viewer needs one body mesh and one skin texture per race/gender. They come from
the client MPQs (`extract_models.py`), land in MODELS_DIR as `<race>_<gender>.bin` and
`<race>_<gender>.png`, and are served at `/models/`. Nothing here is committed.

Formats, checked against the real 3.3.5a (build 12340) files for Human male
(`Character\\Human\\Male\\HumanMale.M2`, version 264, and `HumanMale00.skin`) and
documented in docs/CHARACTER-MODEL-SPIKE.md:

M2 header (little endian, offsets from file start; M2Array = u32 count + u32 offset)
    0 'MD20'   4 version (264)   60 vertices M2Array   68 u32 number of skin profiles
    80 textures M2Array
  vertex, 48 bytes: pos 3f, bone weights 4B, bone indices 4B, normal 3f, uv 2f, uv2 2f
  texture, 16 bytes: type u32 (1 = character skin), flags u32, filename M2Array
.skin ('SKIN'; geometry lives outside the M2 since 3.0)
    4 vertex lookup M2Array<u16>   12 indices M2Array<u16> (triangles, into the lookup)
    20 bones M2Array   28 submeshes M2Array (48 bytes each, first u16 = geoset id)
Geoset ids are group*100 + variant (group 0 = body/hair 0..n, 4 = hands, 5 = feet,
13 = trousers, 15 = cloak, ...); the default look is variant 1 of every group.

The blob (`.bin`): 'WMDL', u32 version (1), u32 vertex count, u32 index count, then
vertex count x (pos 3f, normal 3f, uv 2f) in M2 space (x forward, y left, z up), then
index count x u16. The browser swizzles M2 space to its own and sets `flipY = false`.
"""
import struct

BLOB_MAGIC = b"WMDL"
BLOB_VERSION = 1
M2_VERSION = 264
VERTEX_SIZE = 48
SUBMESH_SIZE = 48

# ChrRaces.ClientFileString for the playable races (3.3.5a ids).
RACE_FOLDERS = {1: "Human", 2: "Orc", 3: "Dwarf", 4: "NightElf", 5: "Scourge",
                6: "Tauren", 7: "Gnome", 8: "Troll", 10: "BloodElf", 11: "Draenei"}
GENDERS = {0: "Male", 1: "Female"}


def model_key(race, gender):
    """`<race>_<gender>` (ids as the characters table has them), or None if unplayable."""
    if race in RACE_FOLDERS and gender in GENDERS:
        return f"{race}_{gender}"
    return None


def mpq_paths(race, gender):
    """(m2, skin profile 0) paths inside the MPQs."""
    folder, sex = RACE_FOLDERS[race], GENDERS[gender]
    stem = f"Character\\{folder}\\{sex}\\{folder}{sex}"
    return f"{stem}.M2", f"{stem}00.skin"


def default_geoset(geoset_id):
    """The default look: body (0), the first hair style (1), and variant 1 of each group."""
    group, variant = divmod(geoset_id, 100)
    if group == 0:
        return variant in (0, 1)
    return variant == 1


def _array(data, at):
    count, offset = struct.unpack_from("<II", data, at)
    if offset > len(data):
        raise ValueError("array offset past end of file")
    return count, offset


def parse_m2(data):
    """(vertices, texture types): vertices are (pos, normal, uv) tuples."""
    magic, version = struct.unpack_from("<4sI", data, 0)
    if magic != b"MD20" or version != M2_VERSION:
        raise ValueError(f"not a 3.3.5a M2 (magic={magic!r}, version={version})")
    n, off = _array(data, 60)
    if off + n * VERTEX_SIZE > len(data):
        raise ValueError("truncated M2 vertices")
    verts = []
    for i in range(n):
        f = struct.unpack_from("<3f8x3f2f8x", data, off + i * VERTEX_SIZE)
        verts.append((f[0:3], f[3:6], f[6:8]))
    n, off = _array(data, 80)
    tex_types = [struct.unpack_from("<I", data, off + 16 * i)[0] for i in range(n)]
    return verts, tex_types


def parse_skin(data):
    """(vertex lookup, indices, [(geoset id, index start, index count)])."""
    if data[:4] != b"SKIN":
        raise ValueError("not a .skin file")
    n_look, off_look = _array(data, 4)
    n_idx, off_idx = _array(data, 12)
    n_sub, off_sub = _array(data, 28)
    if max(off_look + 2 * n_look, off_idx + 2 * n_idx, off_sub + SUBMESH_SIZE * n_sub) > len(data):
        raise ValueError("truncated .skin")
    lookup = struct.unpack_from(f"<{n_look}H", data, off_look)
    indices = struct.unpack_from(f"<{n_idx}H", data, off_idx)
    subs = []
    for i in range(n_sub):
        gid, _lvl, _vs, _vc, istart, icount = struct.unpack_from("<6H", data, off_sub + SUBMESH_SIZE * i)
        subs.append((gid, istart, icount))
    return lookup, indices, subs


def build_blob(m2, skin, wanted=default_geoset):
    """The mesh blob for the geosets `wanted(geoset_id)` accepts."""
    verts, _types = parse_m2(m2)
    lookup, indices, subs = parse_skin(skin)
    remap, out_verts, out_idx = {}, [], []
    for gid, start, count in subs:
        if not wanted(gid):
            continue
        for ix in indices[start:start + count]:
            if ix >= len(lookup) or lookup[ix] >= len(verts):
                raise ValueError("index out of range")
            v = lookup[ix]
            if v not in remap:
                remap[v] = len(out_verts)
                out_verts.append(verts[v])
            out_idx.append(remap[v])
    if not out_idx or len(out_verts) > 0xFFFF:
        raise ValueError("no usable geometry")
    body = b"".join(struct.pack("<8f", *p, *n, *uv) for p, n, uv in out_verts)
    return (struct.pack("<4sIII", BLOB_MAGIC, BLOB_VERSION, len(out_verts), len(out_idx))
            + body + struct.pack(f"<{len(out_idx)}H", *out_idx))


def parse_blob(blob):
    """Inverse of build_blob: (vertices as 8-float tuples, indices)."""
    magic, version, nv, ni = struct.unpack_from("<4sIII", blob, 0)
    if magic != BLOB_MAGIC or version != BLOB_VERSION or len(blob) != 16 + nv * 32 + ni * 2:
        raise ValueError("bad model blob")
    verts = [struct.unpack_from("<8f", blob, 16 + 32 * i) for i in range(nv)]
    return verts, struct.unpack_from(f"<{ni}H", blob, 16 + 32 * nv)


def base_skin_paths(chars_sections_dbc, race, gender):
    """Texture1 of the CharSections record for the base skin (type 0, variation 0, colour 0)."""
    magic, n_rec, n_field, rec_size, _str_size = struct.unpack_from("<4sIIII", chars_sections_dbc, 0)
    if magic != b"WDBC" or n_field != 10 or rec_size != 40:
        raise ValueError(f"unexpected CharSections layout ({n_field} fields)")
    strings = 20 + n_rec * rec_size
    for r in range(n_rec):
        f = struct.unpack_from("<10I", chars_sections_dbc, 20 + r * rec_size)
        if f[1] == race and f[2] == gender and f[3] == 0 and f[8] == 0 and f[9] == 0 and f[4]:
            end = chars_sections_dbc.index(b"\0", strings + f[4])
            return chars_sections_dbc[strings + f[4]:end].decode("utf-8", "replace")
    return None

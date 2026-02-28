"""
Minecraft region mover (Java 1.21.x): copy a 5x5 region square from SOURCE to TARGET.

- Does NOT modify SOURCE.
- Writes new/overwritten region/entity/poi files into TARGET.
- Shifts:
    - chunk xPos/zPos
    - block_entities x/z
    - block_ticks/fluid_ticks x/z
    - entities Pos + some common coordinate fields
    - POI packed block-pos longs under keys like "pos" when present (best-effort)

Shift is defined in Overworld blocks.
If MOVE_NETHER is enabled, we apply dx_nether = dx_overworld // 8 and dz_nether = dz_overworld // 8
so existing portal pairs still correspond (1 Nether block <-> 8 Overworld blocks in X/Z).

Run with Python 3.10+ on Windows.
"""

from __future__ import annotations

import io
import struct
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple, Optional, Any

# pip install nbtlib
import nbtlib


# =========================
# USER SETTINGS
# =========================
SOURCE_WORLD = Path(r"C:\Users\paulo\AppData\Roaming\ATLauncher\instances\Minecraft12111withFabric\saves\Neith")
TARGET_WORLD = Path(r"C:\Users\paulo\source\servers\server3\world")

# Which dimensions to process: "overworld", "nether", or "both"
DIMENSION_MODE = "nether"
_valid_modes = {"overworld", "nether", "both"}
if DIMENSION_MODE not in _valid_modes:
    raise ValueError(f"DIMENSION_MODE must be one of {_valid_modes}, got {DIMENSION_MODE!r}")

MOVE_OVERWORLD = DIMENSION_MODE in ("overworld", "both")
MOVE_NETHER = DIMENSION_MODE in ("nether", "both")  # if True, apply scaled shift so portals match
# MOVE_END = False  # optional future

# Region window (in region coords)
RX_MIN, RX_MAX = -2, 2
RZ_MIN, RZ_MAX = -2, 2

# Shift defined in OVERWORLD blocks
DX_BLOCKS = 5120
DZ_BLOCKS = 5120

COPY_ENTITIES = True
COPY_POI = False  # you said you don't care; keep as option

OVERWRITE_TARGET = True  # set True if you want to overwrite existing target files


# =========================
# CONSTANTS / CHECKS
# =========================
SECTOR_BYTES = 4096
HEADER_BYTES = 8192

# Must be chunk-aligned in overworld
assert DX_BLOCKS % 16 == 0 and DZ_BLOCKS % 16 == 0

# For portal-consistent Nether move, overworld shift must be divisible by 8
if MOVE_NETHER:
    assert DX_BLOCKS % 8 == 0 and DZ_BLOCKS % 8 == 0, \
        "Overworld DX/DZ must be divisible by 8 to preserve portal mapping."

# Optional but recommended
if DX_BLOCKS % 512 != 0 or DZ_BLOCKS % 512 != 0:
    print("WARNING: Overworld shift not multiple of 512; region files won't remain aligned.")
if MOVE_NETHER and ((DX_BLOCKS // 8) % 512 != 0 or (DZ_BLOCKS // 8) % 512 != 0):
    print("WARNING: Nether shift not multiple of 512; region files won't remain aligned in Nether.")

# Overworld derived shift values
dx_ow = DX_BLOCKS
dz_ow = DZ_BLOCKS
dchunks_ow = dx_ow // 16
dchunks_z_ow = dz_ow // 16
dregions_ow = dx_ow // 512
dregions_z_ow = dz_ow // 512

# Nether derived shift values (only used if MOVE_NETHER)
dx_ne = dx_ow // 8
dz_ne = dz_ow // 8
dchunks_ne = dx_ne // 16
dchunks_z_ne = dz_ne // 16
dregions_ne = dx_ne // 512
dregions_z_ne = dz_ne // 512


@dataclass
class DimSpec:
    name: str
    src_root: Path
    dst_root: Path
    dx_blocks: int
    dz_blocks: int


# =========================
# REGION FILE IO
# =========================
@dataclass
class RegionChunkRecord:
    timestamp: int
    nbt_file: nbtlib.File  # root is the chunk compound


def _index(cx: int, cz: int) -> int:
    return cx + cz * 32


def _read_u24(b: bytes) -> int:
    return (b[0] << 16) | (b[1] << 8) | b[2]


def _write_u24(n: int) -> bytes:
    return bytes([(n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF])


def read_region_file(path: Path) -> Dict[Tuple[int, int], RegionChunkRecord]:
    data = path.read_bytes()
    if len(data) < HEADER_BYTES:
        raise ValueError(f"Invalid region file (too small): {path}")

    loc_table = data[:4096]
    ts_table = data[4096:8192]

    chunks: Dict[Tuple[int, int], RegionChunkRecord] = {}

    for cz in range(32):
        for cx in range(32):
            i = _index(cx, cz)
            entry = loc_table[i * 4 : i * 4 + 4]
            offset = _read_u24(entry[:3])
            sectors = entry[3]
            if offset == 0 or sectors == 0:
                continue  # empty slot

            ts = struct.unpack(">I", ts_table[i * 4 : i * 4 + 4])[0]

            start = offset * SECTOR_BYTES
            if start + 5 > len(data):
                continue  # corrupted; skip

            length = struct.unpack(">I", data[start : start + 4])[0]
            ctype = data[start + 4]
            payload = data[start + 5 : start + 4 + length]
            if len(payload) != max(0, length - 1):
                continue  # corrupted; skip

            if ctype == 2:  # zlib
                raw = zlib.decompress(payload)
            elif ctype == 1:  # gzip
                import gzip
                raw = gzip.decompress(payload)
            elif ctype == 3:  # uncompressed (rare)
                raw = payload
            else:
                raise ValueError(f"Unknown compression type {ctype} in {path} at chunk ({cx},{cz})")

            nbt = nbtlib.File.parse(io.BytesIO(raw))
            chunks[(cx, cz)] = RegionChunkRecord(timestamp=ts, nbt_file=nbt)

    return chunks


def write_region_file(path: Path, chunks: Dict[Tuple[int, int], RegionChunkRecord]) -> None:
    # Build location and timestamp tables
    loc = bytearray(4096)
    ts = bytearray(4096)

    # We will write chunk payloads sequentially after the 2-sector header.
    # Sector 0-1 are header. First available sector is 2.
    out = bytearray(HEADER_BYTES)
    next_sector = 2

    # Deterministic ordering
    for cz in range(32):
        for cx in range(32):
            key = (cx, cz)
            i = _index(cx, cz)
            if key not in chunks:
                continue

            rec = chunks[key]
            # Serialize NBT
            bio = io.BytesIO()
            rec.nbt_file.write(bio)
            raw = bio.getvalue()

            comp = zlib.compress(raw)
            ctype = 2
            length = 1 + len(comp)  # includes compression-type byte
            blob = struct.pack(">I", length) + bytes([ctype]) + comp

            # Pad to full sectors
            sectors = (len(blob) + SECTOR_BYTES - 1) // SECTOR_BYTES
            pad_len = sectors * SECTOR_BYTES - len(blob)
            blob += b"\x00" * pad_len

            # Ensure output buffer big enough
            start = next_sector * SECTOR_BYTES
            end = start + len(blob)
            if end > len(out):
                out.extend(b"\x00" * (end - len(out)))

            out[start:end] = blob

            # Write location table entry
            loc[i * 4 : i * 4 + 3] = _write_u24(next_sector)
            loc[i * 4 + 3] = sectors & 0xFF

            # Write timestamp
            ts[i * 4 : i * 4 + 4] = struct.pack(">I", rec.timestamp)

            next_sector += sectors

    # Put header into out
    out[0:4096] = loc
    out[4096:8192] = ts

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out)


# =========================
# NBT SHIFT HELPERS
# =========================
def _as_int(tag: Any) -> Optional[int]:
    try:
        return int(tag)
    except Exception:
        return None


def shift_chunk_nbt(nbt: nbtlib.File, dchunks_x: int, dchunks_z: int, dx: int, dz: int) -> None:
    # chunk coords
    if "xPos" in nbt:
        nbt["xPos"] = nbtlib.Int(int(nbt["xPos"]) + dchunks_x)
    if "zPos" in nbt:
        nbt["zPos"] = nbtlib.Int(int(nbt["zPos"]) + dchunks_z)

    # block entities
    if "block_entities" in nbt and isinstance(nbt["block_entities"], nbtlib.List):
        for be in nbt["block_entities"]:
            if isinstance(be, nbtlib.Compound):
                if "x" in be:
                    be["x"] = nbtlib.Int(int(be["x"]) + dx)
                if "z" in be:
                    be["z"] = nbtlib.Int(int(be["z"]) + dz)

    # ticks
    for tick_key in ("block_ticks", "fluid_ticks"):
        if tick_key in nbt and isinstance(nbt[tick_key], nbtlib.List):
            for t in nbt[tick_key]:
                if isinstance(t, nbtlib.Compound):
                    if "x" in t:
                        t["x"] = nbtlib.Int(int(t["x"]) + dx)
                    if "z" in t:
                        t["z"] = nbtlib.Int(int(t["z"]) + dz)


def shift_entity_compound(ent: nbtlib.Compound, dx: int, dz: int) -> None:
    # Primary: Pos = [x, y, z]
    if "Pos" in ent and isinstance(ent["Pos"], nbtlib.List) and len(ent["Pos"]) >= 3:
        # Pos elements are usually Doubles
        ent["Pos"][0] = nbtlib.Double(float(ent["Pos"][0]) + dx)
        ent["Pos"][2] = nbtlib.Double(float(ent["Pos"][2]) + dz)

    # Common extra coordinate fields seen in entities
    for k in (
        "x", "z",                 # sometimes present
        "TileX", "TileZ",         # projectiles, etc.
        "SleepingX", "SleepingZ",
        "HomePosX", "HomePosZ",
        "BoundX", "BoundZ",
        "LeashX", "LeashZ",
    ):
        if k in ent:
            v = _as_int(ent[k])
            if v is not None:
                ent[k] = nbtlib.Int(v + (dx if k.endswith("X") or k == "x" else dz))

    # Some entities store block position as a compound
    # e.g. {"BlockPos":{"X":..,"Y":..,"Z":..}}
    if "BlockPos" in ent and isinstance(ent["BlockPos"], nbtlib.Compound):
        bp = ent["BlockPos"]
        if "X" in bp:
            bp["X"] = nbtlib.Int(int(bp["X"]) + dx)
        if "Z" in bp:
            bp["Z"] = nbtlib.Int(int(bp["Z"]) + dz)


def decode_blockpos_long(v: int) -> Tuple[int, int, int]:
    # Vanilla packed blockpos long: (x & 0x3FFFFFF)<<38 | (z & 0x3FFFFFF)<<12 | (y & 0xFFF)
    x = (v >> 38) & 0x3FFFFFF
    z = (v >> 12) & 0x3FFFFFF
    y = v & 0xFFF
    # sign extend
    if x >= 0x2000000:
        x -= 0x4000000
    if z >= 0x2000000:
        z -= 0x4000000
    if y >= 0x800:
        y -= 0x1000
    return x, y, z


def encode_blockpos_long(x: int, y: int, z: int) -> int:
    return ((x & 0x3FFFFFF) << 38) | ((z & 0x3FFFFFF) << 12) | (y & 0xFFF)


def shift_poi_nbt_best_effort(nbt: nbtlib.File, dx: int, dz: int) -> None:
    # POI format varies; best-effort:
    # - shift any Long tags under key "pos" that look like packed blockpos
    # - shift any compounds that have x/z
    def walk(node: Any) -> None:
        if isinstance(node, nbtlib.Compound):
            for k in list(node.keys()):
                v = node[k]
                # packed blockpos
                if k == "pos" and isinstance(v, nbtlib.Long):
                    x, y, z = decode_blockpos_long(int(v))
                    node[k] = nbtlib.Long(encode_blockpos_long(x + dx, y, z + dz))
                elif k in ("x", "z") and isinstance(v, (nbtlib.Int, nbtlib.Short, nbtlib.Byte, nbtlib.Long)):
                    node[k] = nbtlib.Int(int(v) + (dx if k == "x" else dz))
                else:
                    walk(v)
        elif isinstance(node, nbtlib.List):
            for it in node:
                walk(it)

    walk(nbt)


# =========================
# MAIN MOVE LOGIC
# =========================
def region_filename(rx: int, rz: int) -> str:
    return f"r.{rx}.{rz}.mca"


def move_folder(kind: str,
                src_dir: Path,
                dst_dir: Path,
                dx_blocks: int,
                dz_blocks: int) -> None:
    """
    kind in {"region","entities","poi"}
    """
    dchunks_x = dx_blocks // 16
    dchunks_z = dz_blocks // 16
    dregions_x = dx_blocks // 512
    dregions_z = dz_blocks // 512

    print(f"\n== Moving {kind} ==")
    for rz in range(RZ_MIN, RZ_MAX + 1):
        for rx in range(RX_MIN, RX_MAX + 1):
            src_name = region_filename(rx, rz)
            dst_name = region_filename(rx + dregions_x, rz + dregions_z)

            src_path = src_dir / src_name
            dst_path = dst_dir / dst_name

            if not src_path.exists():
                print(f"SKIP missing: {src_path}")
                continue

            if dst_path.exists() and not OVERWRITE_TARGET:
                raise FileExistsError(f"Target exists (set OVERWRITE_TARGET=True to overwrite): {dst_path}")

            chunks = read_region_file(src_path)

            # Transform each chunk NBT depending on file kind
            for (lcx, lcz), rec in chunks.items():
                # Shift chunk-internal coords depend on content type:
                # - region: chunk NBT is the full chunk, includes xPos/zPos and block_entities/ticks.
                # - entities: chunk NBT typically contains entity lists; may or may not have xPos/zPos.
                # - poi: poi data, may not have xPos/zPos.
                if kind == "region":
                    shift_chunk_nbt(rec.nbt_file, dchunks_x, dchunks_z, dx_blocks, dz_blocks)
                elif kind == "entities":
                    # shift root-level xPos/zPos if present
                    if "xPos" in rec.nbt_file:
                        rec.nbt_file["xPos"] = nbtlib.Int(int(rec.nbt_file["xPos"]) + dchunks_x)
                    if "zPos" in rec.nbt_file:
                        rec.nbt_file["zPos"] = nbtlib.Int(int(rec.nbt_file["zPos"]) + dchunks_z)

                    # common: Entities list
                    if "Entities" in rec.nbt_file and isinstance(rec.nbt_file["Entities"], nbtlib.List):
                        for ent in rec.nbt_file["Entities"]:
                            if isinstance(ent, nbtlib.Compound):
                                shift_entity_compound(ent, dx_blocks, dz_blocks)
                elif kind == "poi":
                    # POI is version-dependent; best-effort walker
                    shift_poi_nbt_best_effort(rec.nbt_file, dx_blocks, dz_blocks)

            # Keep timestamps (or refresh)
            now_ts = int(time.time())
            for rec in chunks.values():
                # optional: refresh timestamps to "now" so target world notices updates
                rec.timestamp = now_ts

            dst_dir.mkdir(parents=True, exist_ok=True)
            write_region_file(dst_path, chunks)
            print(f"WROTE: {dst_path}")


def main() -> None:
    # Sanity
    if not (SOURCE_WORLD / "level.dat").exists():
        raise FileNotFoundError(f"Not a world folder (missing level.dat): {SOURCE_WORLD}")
    if not (TARGET_WORLD / "level.dat").exists():
        print(f"WARNING: target level.dat not found at {TARGET_WORLD}. If this is a server world, ensure path is correct.")

    dims_to_process = []
    if MOVE_OVERWORLD:
        dims_to_process.append(
            DimSpec(
                name="overworld",
                src_root=SOURCE_WORLD,
                dst_root=TARGET_WORLD,
                dx_blocks=dx_ow,
                dz_blocks=dz_ow,
            )
        )
    if MOVE_NETHER:
        dims_to_process.append(
            DimSpec(
                name="nether",
                src_root=SOURCE_WORLD / "DIM-1",
                dst_root=TARGET_WORLD / "DIM-1",
                dx_blocks=dx_ne,
                dz_blocks=dz_ne,
            )
        )

    if not dims_to_process:
        print("Nothing to do: both MOVE_OVERWORLD and MOVE_NETHER are disabled.")
        return

    print("SOURCE:", SOURCE_WORLD)
    print("TARGET:", TARGET_WORLD)
    print(f"Dimension mode: {DIMENSION_MODE}")
    print(f"Regions: rx={RX_MIN}..{RX_MAX}, rz={RZ_MIN}..{RZ_MAX} (5x5)")

    for dim in dims_to_process:
        dx = dim.dx_blocks
        dz = dim.dz_blocks
        print(f"\n[{dim.name}] source root: {dim.src_root}")
        print(f"[{dim.name}] target root: {dim.dst_root}")
        print(f"[{dim.name}] shift blocks: ({dx},{dz})  chunks: ({dx // 16},{dz // 16})  regions: ({dx // 512},{dz // 512})")

        src_region = dim.src_root / "region"
        src_entities = dim.src_root / "entities"
        src_poi = dim.src_root / "poi"

        dst_region = dim.dst_root / "region"
        dst_entities = dim.dst_root / "entities"
        dst_poi = dim.dst_root / "poi"

        if not src_region.exists():
            print(f"NOTE: [{dim.name}] source region/ folder not found; skipping dimension.")
            continue

        move_folder("region", src_region, dst_region, dx, dz)

        if COPY_ENTITIES:
            if src_entities.exists():
                move_folder("entities", src_entities, dst_entities, dx, dz)
            else:
                print(f"NOTE: [{dim.name}] source entities/ folder not found; skipping entities.")

        if COPY_POI:
            if src_poi.exists():
                move_folder("poi", src_poi, dst_poi, dx, dz)
            else:
                print(f"NOTE: [{dim.name}] source poi/ folder not found; skipping poi.")

    print("\nDONE.")


if __name__ == "__main__":
    main()
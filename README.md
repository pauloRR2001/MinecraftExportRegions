exportRegionsTool
=================

A small Python tool to copy and shift Minecraft region data (region, entities, POI) from one world folder to another.

Description
-----------
The script reads an area of region files (.mca) from a source world and writes shifted copies into a target world. It updates chunk coordinates, block-entity positions, entity positions and some POI/packed blockpos values so the copied region appears at the new location. The script does NOT modify the source world files; it writes new files into the target world.

Requirements
------------
- Python 3.10+ (script header indicates Windows usage in examples)
- The Python package `nbtlib` (install with `pip install nbtlib`)

Files
-----
- exportRegionsTool.py - main script. Edit the USER SETTINGS section at the top of that file to configure source/target paths and behavior.

Quick usage
-----------
1. Edit the variables under "USER SETTINGS" near the top of exportRegionsTool.py (SOURCE_WORLD, TARGET_WORLD, DIMENSION_MODE, RX_MIN/RX_MAX/RZ_MIN/RZ_MAX, DX_BLOCKS/DZ_BLOCKS, COPY_ENTITIES, COPY_POI, OVERWRITE_TARGET).
2. Run the script with Python:

   python exportRegionsTool.py

Configuration notes
-------------------
- DIMENSION_MODE: one of "overworld", "nether", or "both". If "both" the script will move both overworld and nether data; if "nether" the nether shift is computed as (overworld shift // 8) to preserve portal correspondence.
- RX_MIN..RX_MAX and RZ_MIN..RZ_MAX define the window of region coordinates to copy (region coordinates, inclusive). Default values in the script define a 5x5 window.
- DX_BLOCKS/DZ_BLOCKS define the shift in OVERWORLD blocks. The script computes chunk and region shifts from these values. DX/DZ must be multiples of 16 (chunk-aligned). If MOVE_NETHER is enabled they must also be divisible by 8 to preserve portal mapping.
- COPY_ENTITIES and COPY_POI control whether entity and POI region folders are copied/shifted in addition to the main region files.
- OVERWRITE_TARGET controls whether existing files in the target will be overwritten.

Behavior
--------
- The script processes region (.mca) files and rewrites region/ and optionally entities/ and poi/ folders in the target world.
- It preserves timestamps by default (timestamps are optionally refreshed to current time in the script).
- It performs a best-effort shift of POI and packed block position longs; POI format is version-dependent so results may vary.

Warnings
--------
- Always back up your worlds before running this tool.
- Ensure the configured SOURCE_WORLD and TARGET_WORLD paths are correct and that the source world contains level.dat and a region/ folder.
- The script includes checks and prints warnings if shifts are not region-aligned.

Limitations
-----------
- The script is a best-effort tool for region/nbt transformations. It may not handle all NBT data formats or every entity/POI variant.

See the code
------------
Read exportRegionsTool.py for implementation details and exact behavior; most runtime options are configured in the USER SETTINGS block at the top of that file.

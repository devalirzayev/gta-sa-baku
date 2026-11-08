"""RenderWare / GTA San Andreas binary format writers and readers.

Modules:
    stream  - RW chunk helpers
    dff     - clump/geometry writer + reader (SA map objects, night colours, building pipeline)
    txd     - texture dictionary writer + reader (D3D9 platform, DXT1/DXT5/8888)
    col     - COL3 collision writer + reader (face groups, shadow mesh)
    img     - IMG VER2 archive writer + reader

Engine facts used here are documented in docs/notes/rw_formats.md.
"""

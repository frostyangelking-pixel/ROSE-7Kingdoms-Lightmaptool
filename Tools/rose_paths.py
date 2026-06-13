"""
rose_paths.py  --  resolve ROSE data-file references against a 3Ddata ROOT.

The STB and ZSC tables reference files in a handful of inconsistent styles:

    3DDATA\\Maps\\Junon\\JPT01\\JPT01.zon       (single backslash, leading 3DDATA)
    3DDATA\\\\JUNON\\\\LIST_DECO_JPT_N.ZSC      (DOUBLED backslashes)
    3Ddata/JUNON/HOUSE/church/church01.zms      (forward slashes, mixed case)

A `DataRoot` points at the actual `3Ddata` folder (e.g.
`D:\\Rose Source\\7Skies\\7kDev_HR_002\\3Ddata`) and turns any of those references
into a real path on disk. It:

  * collapses doubled separators and unifies `\\` and `/`,
  * drops a redundant leading `3ddata` component (ROOT already is 3Ddata),
  * matches each path component case-insensitively, so references written in any
    case resolve on a case-sensitive filesystem too (Windows doesn't care; Linux /
    a packed VFS would).

Directory listings are cached, so resolving thousands of mesh paths for a whole
map stays cheap.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional


def normalize_ref(ref: str) -> List[str]:
    """Turn a raw STB/ZSC path reference into clean path components.

    Unifies separators and collapses empties (which also handles the doubled
    backslashes seen in the STB Decoration/Building columns).
    """
    if ref is None:
        return []
    unified = ref.replace("\\", "/")
    return [c for c in unified.split("/") if c not in ("", ".")]


class DataRoot:
    """Resolves ROSE file references relative to a 3Ddata root directory."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self._root_base = os.path.basename(self.root.rstrip("/\\")).lower()
        self._dir_cache: Dict[str, Dict[str, str]] = {}

    # ----- directory listing cache (case-insensitive) ----- #
    def _listdir_ci(self, dirpath: str) -> Dict[str, str]:
        """{lowercase_name: real_name} for a directory, cached."""
        cached = self._dir_cache.get(dirpath)
        if cached is None:
            try:
                cached = {name.lower(): name for name in os.listdir(dirpath)}
            except (FileNotFoundError, NotADirectoryError, PermissionError):
                cached = {}
            self._dir_cache[dirpath] = cached
        return cached

    def _descend(self, components: List[str]) -> Optional[str]:
        """Walk components from root, matching each case-insensitively."""
        cur = self.root
        for comp in components:
            # Fast path: exact name exists as-is.
            candidate = os.path.join(cur, comp)
            if os.path.exists(candidate):
                cur = candidate
                continue
            real = self._listdir_ci(cur).get(comp.lower())
            if real is None:
                return None
            cur = os.path.join(cur, real)
        return cur if os.path.exists(cur) else None

    # ----- public API ----- #
    def resolve(self, ref: str) -> Optional[str]:
        """Return the real on-disk path for a reference, or None if not found."""
        components = normalize_ref(ref)
        if not components:
            return None

        # The reference may or may not lead with the redundant '3ddata' component.
        candidates = [components]
        if components[0].lower() == self._root_base or components[0].lower() == "3ddata":
            candidates.insert(0, components[1:])     # prefer the stripped form

        for comp_list in candidates:
            if not comp_list:
                continue
            hit = self._descend(comp_list)
            if hit:
                return hit
        return None

    def exists(self, ref: str) -> bool:
        return self.resolve(ref) is not None

    def clear_cache(self) -> None:
        self._dir_cache.clear()


# --------------------------------------------------------------------------- #
#  CLI self-test
#    python rose_paths.py <3ddata_root> <ref> [<ref> ...]
# --------------------------------------------------------------------------- #
def _main(argv: List[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        print("usage: python rose_paths.py <3ddata_root> <ref> [<ref> ...]")
        return 1
    root = DataRoot(argv[0])
    print(f"ROOT = {root.root}")
    for ref in argv[1:]:
        hit = root.resolve(ref)
        print(f"  {ref!r}\n    -> {hit if hit else 'NOT FOUND'}")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))

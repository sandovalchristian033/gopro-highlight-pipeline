"""How GoPro names files, and what that implies about recording order.

This lives on its own because two different layers need it and neither should
import the other: `ride` builds the file list, `segments` orders the cuts that
come out of those files. Both have to agree on what "chronological" means, and
when they disagreed the reel played a chaptered recording out of sequence.
"""

from __future__ import annotations

import re
from pathlib import Path

# GoPro names files as [GX|GH|GP]<chapter:2><recording:4>.MP4 -- note that the
# chapter comes *before* the recording number, which is the whole problem: a
# long recording split into chapters produces GX011134 and GX021134, and those
# two belong next to each other even though a plain alphabetical sort will drop
# anything numbered 1135 or higher in between them.
GOPRO_NAME = re.compile(r"^(G[XHP])(\d{2})(\d{4})$", re.IGNORECASE)


def recording_order(path: Path) -> tuple:
    """Sort key that puts files in the order the camera actually recorded them.

    Sorts by recording number first and chapter second, so the chapters of one
    long clip stay together and in sequence. Files that are not GoPro-named
    sort alphabetically after everything that is, which keeps a hand-renamed
    file from silently landing in the middle of the ride.
    """
    match = GOPRO_NAME.match(path.stem)
    if match:
        return (0, match.group(3), match.group(2), "")
    return (1, "", "", path.stem.lower())


def recording_id(path: Path) -> str:
    """Chapters of one long recording share this id."""
    match = GOPRO_NAME.match(path.stem)
    return match.group(3) if match else path.stem

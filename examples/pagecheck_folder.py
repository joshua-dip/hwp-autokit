"""Check every PDF/HWP/HWPX in a folder for empty or under-filled pages.

    python examples/pagecheck_folder.py <folder> [expected_pages]

Same as the CLI:  hwp-pagecheck <folder> --expect N --json report.json
For a layout with a sidebar on the right and a legend at the bottom, measure only
the body:          hwp-pagecheck <folder> --col 0-0.72 --band 0.04-0.88 --low 0.4
Calibrate the thresholds on one known-good and one known-bad file first.
"""
import sys

from hwp_autokit.overflow import check_paths

folder = sys.argv[1]
expect = int(sys.argv[2]) if len(sys.argv) > 2 else None
bad = 0
for rep in check_paths([folder], expect=expect, queue_dir=None):
    bad += not rep.ok
    status = "ok" if rep.ok else "!!"
    print(f"{status} {rep.file} ({len(rep.pages)} pages, {rep.engine})")
    for issue in rep.issues:
        print("    -", issue.message)
sys.exit(1 if bad else 0)

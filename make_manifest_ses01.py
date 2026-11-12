#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Create a TMS ses01-only manifest. Other sessions (e.g. ses02) are excluded."""
import argparse, csv, re
from pathlib import Path

DEFAULT_T1_ROOT = "/data02/linzhenzong/TMS-MRI/Data_flip"
DEFAULT_LESION_ROOT = "/data02/linzhenzong/TMS-MRI/Lesion/lesion_mask_MNI1mm_flip"
DEFAULT_OUT = "/data02/tanghui/TMS-MRI/R2SN_BN246_lesionaware_v2/manifest_TMS_ses01_only.csv"

def is_ses01(subject_id):
    # Accept both sub-TMS001ses01 and sub-TMS001_ses-01 naming conventions.
    m = re.search(r"ses-?0*(\d+)", subject_id, flags=re.IGNORECASE)
    return bool(m and int(m.group(1)) == 1)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--t1-root",default=DEFAULT_T1_ROOT)
    p.add_argument("--lesion-root",default=DEFAULT_LESION_ROOT)
    p.add_argument("--out",default=DEFAULT_OUT)
    a=p.parse_args()
    t1root, lesionroot=Path(a.t1_root),Path(a.lesion_root)
    if not t1root.is_dir(): raise SystemExit(f"T1 root not found: {t1root}")
    if not lesionroot.is_dir(): raise SystemExit(f"Lesion root not found: {lesionroot}")

    all_t1=sorted(t1root.glob("sub-*/anat/*_T1w.nii.gz"))
    if not all_t1:
        raise SystemExit(f"No T1 files found under {t1root}/sub-*/anat/*_T1w.nii.gz")

    selected=[]; excluded=[]; duplicate=[]
    seen=set()
    for t1 in all_t1:
        # Use the T1 filename stem as the canonical ID; this works for both
        # sub-TMS001ses01/anat/sub-TMS001ses01_T1w.nii.gz and BIDS-style names.
        sid=t1.name[:-len("_T1w.nii.gz")]
        if not is_ses01(sid):
            excluded.append((sid,str(t1)))
            continue
        if sid in seen:
            duplicate.append((sid,str(t1)))
            continue
        seen.add(sid)

        exact=lesionroot/f"{sid}_space-MNI152NLin6Asym_res-01_label-lesion_mask.nii.gz"
        if exact.is_file():
            lesion=exact
        else:
            matches=sorted(lesionroot.glob(f"{sid}*label-lesion_mask.nii.gz"))
            if len(matches)==1:
                lesion=matches[0]
            elif len(matches)>1:
                duplicate.append((sid,"MULTIPLE_LESION_FILES:"+";".join(map(str,matches))))
                continue
            else:
                lesion=None

        selected.append({
            "id":sid,
            "code":sid,
            "t1":str(t1.resolve()),
            "lesion":str(lesion.resolve()) if lesion else "NONE",
            "lesion_mode":"MNI_MASK" if lesion else "NO_LESION"
        })

    if not selected:
        raise SystemExit("No ses01 T1s found. Check T1 filename/session naming.")

    out=Path(a.out); out.parent.mkdir(parents=True,exist_ok=True)
    with out.open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=["id","code","t1","lesion","lesion_mode"])
        w.writeheader(); w.writerows(selected)

    n_les=sum(x["lesion_mode"]=="MNI_MASK" for x in selected)
    print(f"All T1 files discovered : {len(all_t1)}")
    print(f"ses01 included          : {len(selected)}")
    print(f"other sessions excluded : {len(excluded)}")
    print(f"lesion masks matched    : {n_les}")
    print(f"NO_LESION               : {len(selected)-n_les}")
    print(f"Manifest                : {out}")
    if excluded:
        print("\nExcluded examples (first 10):")
        for x in excluded[:10]: print("  ",x[0],x[1])
    if duplicate:
        print("\nDuplicate/ambiguous records (must resolve manually):")
        for x in duplicate: print("  ",x)
    print("\nIMPORTANT: NO_LESION means no matching mask file was found; verify these IDs against the clinical group list.")

if __name__=="__main__":
    main()

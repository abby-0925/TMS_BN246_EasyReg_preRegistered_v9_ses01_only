#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Summarize only ses01 preprocessing and R2SN status."""
import argparse,csv,json,re
from pathlib import Path
from collections import Counter

def is_ses01(sid):
    m=re.search(r"ses-?0*(\d+)",sid,flags=re.IGNORECASE)
    return bool(m and int(m.group(1))==1)

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--manifest",default="/data02/tanghui/TMS-MRI/R2SN_BN246_lesionaware_v2/manifest_TMS_ses01_only.csv")
    p.add_argument("--prep-root",default="/data02/tanghui/TMS-MRI/R2SN_BN246_lesionaware_v2/pre_registered")
    p.add_argument("--out-root",default="/data02/tanghui/TMS-MRI/R2SN_BN246_lesionaware_v2/subjects")
    p.add_argument("--tag",default="TMS_BN246_preRegistered_v9_ses01")
    a=p.parse_args()
    with open(a.manifest,newline="",encoding="utf-8-sig") as f: rows=[r for r in csv.DictReader(f) if is_ses01(r["id"])]
    prep=Counter();r2=Counter();bad=[]
    for row in rows:
        sid=row["id"];ps=Path(a.prep_root)/sid/"preprocess_summary.json"
        if ps.is_file():
            try: s=json.loads(ps.read_text()).get("status","UNKNOWN")
            except Exception:s="BAD_JSON"
        else:s="NOT_STARTED"
        prep[s]+=1
        rs=Path(a.out_root)/sid/a.tag/"qc"/"run_summary.json"
        if rs.is_file():
            try:
                m=json.loads(rs.read_text());s2=m.get("status","UNKNOWN")
                if s2 not in ("SUCCESS","SUCCESS_WITH_MISSING_NODES"):bad.append((sid,s2,m.get("error","")))
            except Exception:s2="BAD_JSON";bad.append((sid,s2,""))
        else:s2="NOT_STARTED"
        r2[s2]+=1
    print("SES01_SUBJECTS",len(rows))
    print("PREPROCESS",dict(prep))
    print("R2SN",dict(r2))
    if bad:
        print("\nR2SN failures:")
        for x in bad:print(*x,sep=" | ")

if __name__=="__main__":main()

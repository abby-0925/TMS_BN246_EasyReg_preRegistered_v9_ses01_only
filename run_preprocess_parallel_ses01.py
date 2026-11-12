#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ses01-only, bounded parallel EasyReg preprocessing with reliable Ctrl+C cleanup."""
import argparse, csv, os, re, signal, subprocess, sys, time
from pathlib import Path
from collections import Counter

DEFAULT_MANIFEST="/data02/tanghui/TMS-MRI/R2SN_BN246_lesionaware_v2/manifest_TMS_ses01_only.csv"
DEFAULT_PREP_ROOT="/data02/tanghui/TMS-MRI/R2SN_BN246_lesionaware_v2/pre_registered"
DEFAULT_MNI="/data01/software/fsl/data/standard/MNI152_T1_1mm.nii.gz"
DEFAULT_FS="/usr/local/freesurfer/8.0.0"

def is_ses01(sid):
    m=re.search(r"ses-?0*(\d+)",sid,flags=re.IGNORECASE)
    return bool(m and int(m.group(1))==1)

def read_manifest(path):
    with open(path,newline="",encoding="utf-8-sig") as f: rows=list(csv.DictReader(f))
    need={"id","t1","lesion","lesion_mode"}
    if not rows or not need.issubset(rows[0]):
        raise ValueError(f"Manifest must contain columns: {sorted(need)}")
    ids=[r["id"] for r in rows]
    if len(ids)!=len(set(ids)): raise ValueError("Duplicate IDs in manifest")
    return rows

def write_progress(path,rows):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+".tmp")
    fields=["id","state","returncode","seconds","log","detail"]
    with tmp.open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
    tmp.replace(path)

def stop_active(active):
    # Each worker is launched in its own process group. This terminates the
    # preprocess_one.py process and its mri_easyreg/mri_synthstrip descendants.
    for item in active.values():
        try: os.killpg(item["proc"].pid,signal.SIGTERM)
        except ProcessLookupError: pass
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        alive=[x for x in active.values() if x["proc"].poll() is None]
        if not alive: break
        time.sleep(0.25)
    for item in active.values():
        if item["proc"].poll() is None:
            try: os.killpg(item["proc"].pid,signal.SIGKILL)
            except ProcessLookupError: pass
    for item in active.values():
        try: item["proc"].wait(timeout=3)
        except Exception: pass
        try: item["log_handle"].close()
        except Exception: pass

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest",default=DEFAULT_MANIFEST)
    p.add_argument("--mni",default=DEFAULT_MNI)
    p.add_argument("--out-root",default=DEFAULT_PREP_ROOT)
    p.add_argument("--freesurfer-home",default=DEFAULT_FS)
    p.add_argument("--workers",type=int,default=4)
    p.add_argument("--threads-per-worker",type=int,default=2)
    p.add_argument("--limit",type=int,default=0)
    p.add_argument("--start",type=int,default=0)
    p.add_argument("--id",default=None)
    p.add_argument("--force",action="store_true")
    p.add_argument("--keep-transforms",action="store_true")
    p.add_argument("--dry-run",action="store_true")
    a=p.parse_args()

    if a.workers<1 or a.threads_per_worker<1 or a.limit<0 or a.start<0:
        p.error("workers/threads must be >=1; start/limit must be >=0")
    if a.id is not None and not a.id.strip():
        p.error("--id was supplied but is empty; refusing to process the whole cohort")
    if a.id is not None and not is_ses01(a.id):
        p.error(f"--id {a.id!r} is not a ses01 ID; only ses01 is allowed")
    rows=read_manifest(a.manifest)
    ses_rows=[r for r in rows if is_ses01(r["id"])]
    excluded=len(rows)-len(ses_rows)
    if not ses_rows: raise SystemExit("No ses01 rows in manifest.")
    if a.id is not None:
        rows=[r for r in ses_rows if r["id"]==a.id]
        if not rows: raise SystemExit(f"ID not found in ses01 manifest: {a.id}")
    else:
        rows=ses_rows[a.start:]
        if a.limit: rows=rows[:a.limit]
    if not rows: raise SystemExit("No subjects selected.")
    print(f"Manifest rows={len(read_manifest(a.manifest))}; ses01={len(ses_rows)}; excluded other sessions={excluded}")
    print(f"Selected={len(rows)}; workers={a.workers}; threads/worker={a.threads_per_worker}")
    print("First selected IDs:",", ".join(r["id"] for r in rows[:12]))
    if a.dry_run:
        print("DRY RUN only: no process launched.")
        return

    script=Path(__file__).with_name("preprocess_one.py")
    if not script.is_file(): raise SystemExit(f"Missing worker script: {script}")
    if not Path(a.mni).is_file(): raise SystemExit(f"MNI template not found: {a.mni}")
    if not (Path(a.freesurfer_home)/"bin"/"mri_easyreg").is_file():
        raise SystemExit(f"mri_easyreg not found under {a.freesurfer_home}")

    root=Path(a.out_root); root.mkdir(parents=True,exist_ok=True)
    logdir=root.parent/"logs_preprocess_ses01"; logdir.mkdir(parents=True,exist_ok=True)
    progress=root.parent/"preprocess_progress_ses01.csv"
    results=[]; active={}; next_idx=0; total=len(rows); start_time=time.monotonic()

    def launch(row):
        sid=row["id"]
        out=Path(a.out_root)/sid
        summary=out/"preprocess_summary.json"
        expected=[out/"T1_EasyReg_MNI.nii.gz",out/"T1_EasyReg_MNI_brain.nii.gz",
                  out/"T1_EasyReg_MNI_brainmask.nii.gz",out/"lesion_MNI_on_T1grid.nii.gz",
                  out/"T1prep_lesion_overlay.png"]
        if not a.force and summary.is_file() and all(x.is_file() and x.stat().st_size>0 for x in expected):
            try:
                import json
                if json.loads(summary.read_text()).get("status")=="SUCCESS":
                    return {"id":sid,"state":"SKIP_SUCCESS","returncode":0,"seconds":0,"log":"","detail":"existing outputs valid"}
            except Exception: pass
        logpath=logdir/f"{sid}.log"
        cmd=[sys.executable,"-u",str(script),"--id",sid,"--t1",row["t1"],
             "--lesion",row["lesion"],"--lesion-mode",row["lesion_mode"],
             "--mni",a.mni,"--out-root",a.out_root,
             "--freesurfer-home",a.freesurfer_home,
             "--threads",str(a.threads_per_worker)]
        if a.keep_transforms: cmd.append("--keep-transforms")
        if a.force: cmd.append("--force")
        env=os.environ.copy()
        for k in ("OMP_NUM_THREADS","ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS",
                  "OPENBLAS_NUM_THREADS","MKL_NUM_THREADS","NUMEXPR_NUM_THREADS"):
            env[k]=str(a.threads_per_worker)
        lf=logpath.open("a",encoding="utf-8")
        lf.write(f"\n===== START {time.strftime('%F %T')} {sid} =====\n")
        lf.write(" ".join(cmd)+"\n"); lf.flush()
        proc=subprocess.Popen(cmd,stdout=lf,stderr=subprocess.STDOUT,env=env,start_new_session=True)
        active[sid]={"proc":proc,"log_handle":lf,"start":time.monotonic(),"log":str(logpath)}
        print(f"[LAUNCH] {sid} pid={proc.pid}",flush=True)
        return None

    def handle_sigterm(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, handle_sigterm)

    try:
        while next_idx<total or active:
            while next_idx<total and len(active)<a.workers:
                row=rows[next_idx]; next_idx+=1
                immediate=launch(row)
                if immediate:
                    results.append(immediate); write_progress(progress,results)
                    print(f"[{len(results)}/{total}] {row['id']} {immediate['state']}",flush=True)
            completed=[]
            for sid,item in list(active.items()):
                rc=item["proc"].poll()
                if rc is None: continue
                item["log_handle"].close()
                state="SUCCESS" if rc==0 else "FAILED"
                detail=""
                try:
                    import json
                    sf=Path(a.out_root)/sid/"preprocess_summary.json"
                    if sf.is_file():
                        sm=json.loads(sf.read_text())
                        state=sm.get("status",state); detail=sm.get("error","")
                except Exception: pass
                res={"id":sid,"state":state,"returncode":rc,
                     "seconds":round(time.monotonic()-item["start"],1),
                     "log":item["log"],"detail":detail}
                results.append(res); completed.append(sid)
                write_progress(progress,results)
                print(f"[{len(results)}/{total}] {sid} {state} {res['seconds']}s {detail}",flush=True)
            for sid in completed: active.pop(sid,None)
            if active: time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nCtrl+C received: terminating all active subject process groups; queued subjects will NOT start.",flush=True)
        stop_active(active)
        for sid,item in active.items():
            results.append({"id":sid,"state":"CANCELLED","returncode":-signal.SIGTERM,
                            "seconds":round(time.monotonic()-item["start"],1),
                            "log":item["log"],"detail":"Stopped by user"})
        write_progress(progress,results)
        print(f"Stopped. Completed={len(results)}; not started={total-next_idx}. Progress: {progress}",flush=True)
        raise SystemExit(130)

    counts=Counter(x["state"] for x in results)
    print("COUNTS",dict(counts))
    print("PROGRESS",progress)

if __name__=="__main__": main()

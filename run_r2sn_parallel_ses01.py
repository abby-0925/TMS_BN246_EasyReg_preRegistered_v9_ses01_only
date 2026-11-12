#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ses01-only, bounded parallel R2SN runner with reliable Ctrl+C cleanup."""
import argparse,csv,json,os,re,signal,subprocess,sys,time
from pathlib import Path
from collections import Counter

DEFAULT_MANIFEST="/data02/tanghui/TMS-MRI/R2SN_BN246_lesionaware_v2/manifest_TMS_ses01_only.csv"
DEFAULT_PREP_ROOT="/data02/tanghui/TMS-MRI/R2SN_BN246_lesionaware_v2/pre_registered"
DEFAULT_OUT_ROOT="/data02/tanghui/TMS-MRI/R2SN_BN246_lesionaware_v2/subjects"
DEFAULT_TAG="TMS_BN246_preRegistered_v9_ses01"

def is_ses01(sid):
    m=re.search(r"ses-?0*(\d+)",sid,flags=re.IGNORECASE)
    return bool(m and int(m.group(1))==1)

def write_progress(path,rows):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+".tmp")
    fields=["id","state","returncode","seconds","log","detail"]
    with tmp.open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
    tmp.replace(path)

def stop_active(active):
    for item in active.values():
        try: os.killpg(item["proc"].pid,signal.SIGTERM)
        except ProcessLookupError: pass
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        if not any(x["proc"].poll() is None for x in active.values()): break
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
    p.add_argument("--prep-root",default=DEFAULT_PREP_ROOT)
    p.add_argument("--out-root",default=DEFAULT_OUT_ROOT)
    p.add_argument("--tag",default=DEFAULT_TAG)
    p.add_argument("--tmp-root",default="/data02/tanghui/TMS-MRI/R2SN_BN246_lesionaware_v2/tmp")
    p.add_argument("--workers",type=int,default=5)
    p.add_argument("--threads-per-worker",type=int,default=2)
    p.add_argument("--margin-mm",type=float,default=0.0)
    p.add_argument("--min-voxels",type=int,default=200)
    p.add_argument("--limit",type=int,default=0);p.add_argument("--start",type=int,default=0)
    p.add_argument("--id",default=None);p.add_argument("--force",action="store_true")
    p.add_argument("--keep-qc-images",action="store_true")
    p.add_argument("--atlas");p.add_argument("--target");p.add_argument("--params");p.add_argument("--picked")
    p.add_argument("--dry-run",action="store_true")
    a=p.parse_args()
    if a.workers<1 or a.threads_per_worker<1 or a.limit<0 or a.start<0:
        p.error("workers/threads must be >=1; start/limit must be >=0")
    if a.id is not None and not a.id.strip(): p.error("--id is empty; refusing to run all subjects")
    if a.id is not None and not is_ses01(a.id): p.error("Only ses01 IDs are allowed")
    with open(a.manifest,newline="",encoding="utf-8-sig") as f: allrows=list(csv.DictReader(f))
    need={"id","code","lesion_mode"}
    if not allrows or not need.issubset(allrows[0]): raise SystemExit(f"Manifest needs {need}")
    if len({r["id"] for r in allrows})!=len(allrows): raise SystemExit("Duplicate IDs in manifest")
    sesrows=[r for r in allrows if is_ses01(r["id"])]
    if not sesrows: raise SystemExit("No ses01 rows in manifest")
    if a.id is not None:
        rows=[r for r in sesrows if r["id"]==a.id]
        if not rows: raise SystemExit(f"ID not found: {a.id}")
    else:
        rows=sesrows[a.start:]
        if a.limit: rows=rows[:a.limit]
    if not rows: raise SystemExit("No subjects selected")
    print(f"Manifest rows={len(allrows)}; ses01={len(sesrows)}; excluded other sessions={len(allrows)-len(sesrows)}")
    print(f"Selected={len(rows)}; workers={a.workers}; threads/worker={a.threads_per_worker}")
    print("First selected IDs:",", ".join(r["id"] for r in rows[:12]))
    if a.dry_run:
        print("DRY RUN only: no R2SN process launched."); return

    script=Path(__file__).with_name("run_one_preregistered.py")
    if not script.is_file(): raise SystemExit(f"Missing worker script: {script}")
    logdir=Path(a.out_root).parent/"logs_r2sn_ses01";logdir.mkdir(parents=True,exist_ok=True)
    progress=Path(a.out_root).parent/f"{a.tag}_progress.csv"
    results=[];active={};next_idx=0;total=len(rows)

    def launch(row):
        sid=row["id"];prep=Path(a.prep_root)/sid
        required=[prep/"T1_EasyReg_MNI_brain.nii.gz",prep/"T1_EasyReg_MNI_brainmask.nii.gz",
                  prep/"lesion_MNI_on_T1grid.nii.gz",prep/"preprocess_summary.json"]
        if not all(x.is_file() for x in required):
            return {"id":sid,"state":"PREPROCESS_MISSING","returncode":"","seconds":0,"log":"","detail":str(prep)}
        try:
            if json.loads(required[-1].read_text()).get("status")!="SUCCESS":
                return {"id":sid,"state":"PREPROCESS_NOT_SUCCESS","returncode":"","seconds":0,"log":"","detail":str(required[-1])}
        except Exception:
            return {"id":sid,"state":"PREPROCESS_SUMMARY_INVALID","returncode":"","seconds":0,"log":"","detail":str(required[-1])}
        base=Path(a.out_root)/sid/a.tag
        summary=base/"qc"/"run_summary.json"
        if not a.force and summary.is_file():
            try:
                old=json.loads(summary.read_text())
                if old.get("status") in ("SUCCESS","SUCCESS_WITH_MISSING_NODES") and \
                   (base/"network"/f"{sid}_network.npy").is_file() and \
                   (base/"radiomics"/"ROI_features_246.csv").is_file():
                    return {"id":sid,"state":"SKIP_SUCCESS","returncode":0,"seconds":0,"log":"","detail":"existing outputs valid"}
            except Exception: pass
        cmd=[sys.executable,"-u",str(script),"--id",sid,"--code",row.get("code",sid),
             "--prep-dir",str(prep),"--lesion-mode",row["lesion_mode"],
             "--out-root",a.out_root,"--tag",a.tag,"--tmp-root",a.tmp_root,
             "--margin-mm",str(a.margin_mm),"--min-voxels",str(a.min_voxels)]
        for key in ("atlas","target","params","picked"):
            val=getattr(a,key)
            if val: cmd += ["--"+key,str(val)]
        if a.keep_qc_images:cmd.append("--keep-qc-images")
        if a.force:cmd.append("--force")
        env=os.environ.copy()
        for k in ("OMP_NUM_THREADS","ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS",
                  "OPENBLAS_NUM_THREADS","MKL_NUM_THREADS","NUMEXPR_NUM_THREADS"):
            env[k]=str(a.threads_per_worker)
        logpath=logdir/f"{sid}.log";lf=logpath.open("a",encoding="utf-8")
        lf.write(f"\n===== START {time.strftime('%F %T')} {sid} =====\n")
        lf.write(" ".join(cmd)+"\n");lf.flush()
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
                row=rows[next_idx];next_idx+=1
                immediate=launch(row)
                if immediate:
                    results.append(immediate);write_progress(progress,results)
                    print(f"[{len(results)}/{total}] {row['id']} {immediate['state']} {immediate.get('detail','')}",flush=True)
            completed=[]
            for sid,item in list(active.items()):
                rc=item["proc"].poll()
                if rc is None:continue
                item["log_handle"].close()
                state="SUCCESS" if rc==0 else "FAILED";detail=""
                try:
                    sm=json.loads((Path(a.out_root)/sid/a.tag/"qc"/"run_summary.json").read_text())
                    state=sm.get("status",state)
                    detail=sm.get("error","") or f"valid={sm.get('n_valid','?')}, zero={sm.get('n_zero','?')}, missing={sm.get('n_other_missing','?')}"
                except Exception:pass
                res={"id":sid,"state":state,"returncode":rc,"seconds":round(time.monotonic()-item["start"],1),"log":item["log"],"detail":detail}
                results.append(res);completed.append(sid);write_progress(progress,results)
                print(f"[{len(results)}/{total}] {sid} {state} {res['seconds']}s {detail}",flush=True)
            for sid in completed:active.pop(sid,None)
            if active:time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nCtrl+C received: terminating active R2SN process groups; queued subjects will NOT start.",flush=True)
        stop_active(active)
        for sid,item in active.items():
            results.append({"id":sid,"state":"CANCELLED","returncode":-signal.SIGTERM,
                            "seconds":round(time.monotonic()-item["start"],1),"log":item["log"],"detail":"Stopped by user"})
        write_progress(progress,results)
        print(f"Stopped. Completed={len(results)}; not started={total-next_idx}. Progress: {progress}",flush=True)
        raise SystemExit(130)

    print("COUNTS",dict(Counter(x["state"] for x in results)))
    print("PROGRESS",progress)

if __name__=="__main__":main()

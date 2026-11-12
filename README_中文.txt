TMS BN246 v9：只处理 ses01 的完整并行流程
==========================================

本版本针对 v8 两个问题修订：
1. 只纳入 ses01，明确排除 ses02 及其他时间点。
2. 并行调度改为主进程管理 Popen 进程组；Ctrl+C 会终止当前所有活动被试及其 EasyReg 子进程，不会继续执行队列中未启动的被试。
3. workers 默认 4（预处理）/5（R2SN），不再默认 1。
4. 如果命令显式传入空 --id，立即报错，不会误启动全队列。
5. 运行器本身再次强制过滤 ses01，即使误传入旧的 115 行 manifest，也不会处理 ses02。

目录
----
Raw T1:
  /data02/linzhenzong/TMS-MRI/Data_flip/
Lesion:
  /data02/linzhenzong/TMS-MRI/Lesion/lesion_mask_MNI1mm_flip/
MNI:
  /data01/software/fsl/data/standard/MNI152_T1_1mm.nii.gz
FreeSurfer:
  /usr/local/freesurfer/8.0.0
Output:
  /data02/tanghui/TMS-MRI/R2SN_BN246_lesionaware_v2/

环境
----
conda activate r2sn
export FREESURFER_HOME=/usr/local/freesurfer/8.0.0
source "$FREESURFER_HOME/SetUpFreeSurfer.sh"
python -c 'import R2SN, ants, nibabel, pandas, numpy, radiomics; print("OK")'
which mri_easyreg
which mri_synthstrip

步骤 1：生成 ses01-only manifest
--------------------------------
python make_manifest_ses01.py

默认输出：
  /data02/tanghui/TMS-MRI/R2SN_BN246_lesionaware_v2/manifest_TMS_ses01_only.csv

检查行数和 ID：
  wc -l /data02/tanghui/TMS-MRI/R2SN_BN246_lesionaware_v2/manifest_TMS_ses01_only.csv
  cut -d, -f1 /data02/tanghui/TMS-MRI/R2SN_BN246_lesionaware_v2/manifest_TMS_ses01_only.csv | head -30

检查是否有 ses02：
  grep -E 'ses-?02' /data02/tanghui/TMS-MRI/R2SN_BN246_lesionaware_v2/manifest_TMS_ses01_only.csv
正常情况不应有输出。

检查 NO_LESION：
  awk -F',' 'NR==1 || $5=="NO_LESION" {print $1","$5}' \
  /data02/tanghui/TMS-MRI/R2SN_BN246_lesionaware_v2/manifest_TMS_ses01_only.csv

NO_LESION 表示没找到同名病灶文件，不等于自动确认健康对照。需与临床分组核对。

步骤 2：先 dry-run，不启动计算
------------------------------
python run_preprocess_parallel_ses01.py --dry-run

必须看到：
  Selected = ses01 数量
  First selected IDs 全部以 ses01 结尾
  workers=4（默认）

如需单例测试，明确填写完整 ID，不要使用可能为空的 shell 变量：
  python run_preprocess_parallel_ses01.py --id sub-TMS002ses01 --workers 1

步骤 3：全队列 EasyReg + SynthStrip 并行
----------------------------------------
建议先 4 workers × 2 threads：
  nohup python -u run_preprocess_parallel_ses01.py \
    --workers 4 --threads-per-worker 2 \
    > /data02/tanghui/TMS-MRI/R2SN_BN246_lesionaware_v2/preprocess_ses01.log \
    2>&1 < /dev/null &

查看：
  tail -f /data02/tanghui/TMS-MRI/R2SN_BN246_lesionaware_v2/preprocess_ses01.log

Ctrl+C：
  前台运行时按 Ctrl+C；脚本会终止全部活动被试进程组，未启动队列不再继续。
  nohup 后台运行时先查 PID：
    ps -ef | grep '[r]un_preprocess_parallel_ses01.py'
  然后 kill -TERM <调度器PID>。若要确保停止其活动子进程组，建议登录运行任务的终端前台启动，或在第二终端按 ps -eo pid,ppid,pgid,cmd 查看并终止对应进程组。

步骤 4：QC
-----------
python check_status_ses01.py

每名被试检查：
  pre_registered/<ID>/preprocess_summary.json
  pre_registered/<ID>/T1prep_lesion_overlay.png
  pre_registered/<ID>/T1_EasyReg_MNI_brain.nii.gz
  pre_registered/<ID>/lesion_MNI_on_T1grid.nii.gz

确认至少一名病灶患者和一名无病灶对照的 overlay 正常，再进入 R2SN。

步骤 5：ses01-only R2SN 并行
-----------------------------
先 dry-run：
  python run_r2sn_parallel_ses01.py --dry-run

单例：
  python run_r2sn_parallel_ses01.py --id sub-TMS002ses01 --workers 1 --keep-qc-images

全队列：
  nohup python -u run_r2sn_parallel_ses01.py \
    --workers 5 --threads-per-worker 2 --keep-qc-images \
    > /data02/tanghui/TMS-MRI/R2SN_BN246_lesionaware_v2/R2SN_ses01.log \
    2>&1 < /dev/null &

监控：
  tail -f /data02/tanghui/TMS-MRI/R2SN_BN246_lesionaware_v2/R2SN_ses01.log
  python check_status_ses01.py

输出
----
预处理：
  pre_registered/<ID>/
R2SN：
  subjects/<ID>/TMS_BN246_preRegistered_v9_ses01/
    qc/
    radiomics/
    network/

网络主矩阵：
  network/<ID>_network.npy
有效节点掩膜：
  network/<ID>_valid_roi_mask.npy

请使用 network.npy + valid_roi_mask.npy 进行正式分析；
network_zeroFilled_DISPLAY_ONLY.npy 仅用于展示，不应用于推断。

注意
----
- v9 只处理 ses01；旧 manifest 不需要删除，但本版本默认使用新的 manifest_TMS_ses01_only.csv。
- 不要同时启动全队列预处理和 R2SN；必须先完成预处理和 QC。
- 预处理默认 4×2，R2SN 默认 5×2。若内存/CPU负载高，降低 workers。
- 已有预处理成功结果会跳过；如需重算需显式 --force。
- v9 的 R2SN 使用独立 tag，不覆盖 v8/v7。

# 单项与合并归因：从标签 v1.4-asrun 出发构造一个版本并运行。
# 用法（在主仓库根目录）：
#   powershell -File docs\audit\attribution\build_version.ps1 -Name B
#   powershell -File docs\audit\attribution\build_version.ps1 -Name B_fBW -Patches f_BW.patch
#   powershell -File docs\audit\attribution\build_version.ps1 -Name B_flabel -Patches f_label.patch
#   powershell -File docs\audit\attribution\build_version.ps1 -Name B_fBW_flabel -Patches f_BW.patch,f_label.patch
# 每个版本都从标签出发、只打列出的补丁；不使用主仓库连续提交后的累计状态。
# 只读开发期输入（截至 2016-12-30）；补充历史的两条命令只用 2009-09-30 以前的价格。
param(
    [Parameter(Mandatory = $true)][string]$Name,
    [string[]]$Patches = @(),
    [string]$Base = "v1.4-asrun",
    [string]$WorktreeRoot = "C:\Users\stone\PycharmProjects\market-risk-wt",
    [string]$Git = "C:\Execute\Git\bin\git.exe"
)
$ErrorActionPreference = "Stop"
$main = (Get-Location).Path
$wt = Join-Path $WorktreeRoot $Name
$python = Join-Path $main ".venv\Scripts\python.exe"
$outputs = "reports\research\wavewarn_v14"
if (Test-Path $wt) { throw "工作目录已存在：$wt" }

# 1. 从标签检出独立工作目录（不做换行转换，补丁按提交内容逐字节套用）
& $Git -c core.autocrlf=false worktree add --detach $wt $Base | Out-Null
$Patches = @($Patches | ForEach-Object { $_ -split "," } | Where-Object { $_ })   # -File 调用时逗号分隔的列表是一个字符串
$applied = @()
foreach ($patch in $Patches) {
    $file = Join-Path $main "patches\$patch"
    & $Git -C $wt -c core.autocrlf=false apply $file
    if ($LASTEXITCODE -ne 0) { throw "补丁未能套用：$patch" }
    $applied += [ordered]@{ file = "patches/$patch"; sha256 = (Get-FileHash $file -Algorithm SHA256).Hash }
}

# 2. 代码树哈希（套用补丁之后、运行之前的完整工作树）
& $Git -C $wt -c core.autocrlf=false add -A
$tree = (& $Git -C $wt -c core.autocrlf=false write-tree).Trim()
$changed = @(& $Git -C $wt -c core.autocrlf=false -c core.quotepath=false diff --cached --name-only $Base)

# 3. 未入库的输入：NDTW 由原始导出文件整理而成，不在 git 中；各版本用主仓库的同一份文件
$ndtw = "data\processed\tradingview\NDTW.csv"
New-Item -ItemType Directory -Force (Split-Path (Join-Path $wt $ndtw)) | Out-Null
Copy-Item (Join-Path $main $ndtw) (Join-Path $wt $ndtw)

# 4. 删去标签里已入库的旧输出，让命令能够写出
foreach ($dir in "evaluation_development", "diagnostics_round2", "extended_nav") {
    Remove-Item -Recurse -Force -Confirm:$false (Join-Path $wt "$outputs\$dir")
}

# 5. 用该版本的源码运行四条命令与固定设定汇总
$env:PYTHONPATH = Join-Path $wt "src"
$env:PYTHONIOENCODING = "utf-8"
Push-Location $wt
try {
    $where = (& $python -c "import market_risk, pathlib; print(pathlib.Path(market_risk.__file__).resolve().parents[2])").Trim()
    if ($where -ne $wt) { throw "运行的不是该版本的源码：$where" }
    foreach ($command in "evaluate-v14-development", "v14-extended-history", "v14-diagnostics-round2", "v14-extended-nav") {
        & $python -c "from market_risk.cli import app; app()" wavewarn $command | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "命令失败：$command" }
    }
    & $python (Join-Path $main "docs\audit\attribution\fixed_setting.py") $wt (Join-Path $wt "fixed_setting.json")
    if ($LASTEXITCODE -ne 0) { throw "固定设定汇总失败" }
}
finally { Pop-Location; Remove-Item Env:PYTHONPATH }

# 6. 版本元数据（白名单第 3 项）
$meta = [ordered]@{
    name = $Name; worktree = $wt; base_tag = $Base
    base_commit = (& $Git -C $main rev-parse $Base).Trim()
    patches = $applied; code_tree = $tree; changed_files = $changed; python = $python
    ndtw_sha256 = (Get-FileHash (Join-Path $main $ndtw) -Algorithm SHA256).Hash
}
[System.IO.File]::WriteAllText((Join-Path $wt "version.json"), ($meta | ConvertTo-Json -Depth 5), (New-Object System.Text.UTF8Encoding($false)))
"{0}: tree {1}; changed: {2}" -f $Name, $tree, ($changed -join ", ")

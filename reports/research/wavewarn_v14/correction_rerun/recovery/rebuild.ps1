# 由本目录保存的材料重建一个归因版本，并核对代码树哈希与固定设定汇总的输出哈希。
# 用法（在主仓库根目录）：
#   powershell -File reports\research\wavewarn_v14\correction_rerun\recovery\rebuild.ps1 -Name B_fBW -WorktreeRoot <一个空的临时目录>
# -Name 取 versions.json 里的版本名：B、B_fBW、B_flabel、B_fBW_flabel。
# 只读开发期输入（截至 2016-12-30）。任何一项核对不符即报错停下，不删除任何东西。
param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string]$WorktreeRoot,
    [string]$Git = "C:\Execute\Git\bin\git.exe"
)
$ErrorActionPreference = "Stop"
$main = (Get-Location).Path
$recovery = Join-Path $main "reports\research\wavewarn_v14\correction_rerun\recovery"
$python = Join-Path $main ".venv\Scripts\python.exe"
$wt = Join-Path $WorktreeRoot $Name
if (Test-Path $wt) { throw "工作目录已存在：$wt" }
function Sha([string]$path) { (Get-FileHash $path -Algorithm SHA256).Hash }

$versions = Get-Content -Raw -Encoding UTF8 (Join-Path $recovery "versions.json") | ConvertFrom-Json
$manifest = Get-Content -Raw -Encoding UTF8 (Join-Path $recovery "inputs_manifest.json") | ConvertFrom-Json
$version = $versions.versions.$Name
if ($null -eq $version) { throw "versions.json 里没有版本：$Name" }

# 1. 从标签检出（不做换行转换），套用本目录保存的补丁
& $Git -c core.autocrlf=false worktree add --detach $wt $version.base_tag | Out-Null
if ($LASTEXITCODE -ne 0) { throw "检出失败" }
foreach ($patch in $version.patches) {
    $file = Join-Path $recovery "patches\$($patch.file)"
    if ((Sha $file) -ne $patch.sha256) { throw "保存的补丁哈希与记录不符：$($patch.file)" }
    & $Git -C $wt -c core.autocrlf=false apply $file
    if ($LASTEXITCODE -ne 0) { throw "补丁未能套用：$($patch.file)" }
}

# 2. 代码树哈希
& $Git -C $wt -c core.autocrlf=false add -A
$tree = (& $Git -C $wt -c core.autocrlf=false write-tree).Trim()
if ($tree -ne $version.code_tree) { throw "代码树哈希不符：$tree，记录为 $($version.code_tree)" }

# 3. 配置：用本目录保存的文件（先核对哈希，再覆盖到工作目录）
foreach ($item in $manifest.configs.PSObject.Properties) {
    $saved = Join-Path $recovery ("config\" + (Split-Path $item.Name -Leaf))
    if ((Sha $saved) -ne $item.Value) { throw "保存的配置哈希与清单不符：$($item.Name)" }
    Copy-Item $saved (Join-Path $wt $item.Name) -Force
}

# 4. 输入：逐项核对清单；NDTW 不入库，取主仓库的文件，缺失或不符时由已入库的原始导出重新生成
foreach ($item in $manifest.inputs) {
    if ($null -ne $item.raw_export) {
        $source = Join-Path $main $item.path
        if ((Sha (Join-Path $main $item.raw_export.path)) -ne $item.raw_export.sha256) { throw "原始导出文件的哈希与清单不符" }
        if (-not (Test-Path $source) -or (Sha $source) -ne $item.sha256) {
            & $python -c "from market_risk.cli import app; app()" tv validate | Out-Null
            if (-not (Test-Path $source) -or (Sha $source) -ne $item.sha256) { throw "重新生成的 $($item.path) 与清单不符" }
        }
        New-Item -ItemType Directory -Force (Split-Path (Join-Path $wt $item.path)) | Out-Null
        Copy-Item $source (Join-Path $wt $item.path)
    }
    elseif ((Sha (Join-Path $wt $item.path)) -ne $item.sha256) { throw "输入哈希与清单不符：$($item.path)" }
}

# 5. 用该版本的源码重算固定设定（K=5、θ_P=2.5%）的汇总
$env:PYTHONPATH = Join-Path $wt "src"
$env:PYTHONIOENCODING = "utf-8"
Push-Location $wt
try {
    $where = (& $python -c "import market_risk, pathlib; print(pathlib.Path(market_risk.__file__).resolve().parents[2])").Trim()
    if ($where -ne $wt) { throw "运行的不是该版本的源码：$where" }
    & $python (Join-Path $main "docs\audit\attribution\fixed_setting.py") $wt (Join-Path $wt "fixed_setting.json")
    if ($LASTEXITCODE -ne 0) { throw "固定设定汇总失败" }
}
finally { Pop-Location; Remove-Item Env:PYTHONPATH }

# 6. 输出哈希
$actual = Sha (Join-Path $wt "fixed_setting.json")
if ($actual -ne $version.fixed_setting_sha256) { throw "固定设定汇总的哈希不符：$actual，记录为 $($version.fixed_setting_sha256)" }
"{0}: code_tree {1} OK; fixed_setting {2} OK" -f $Name, $tree, $actual

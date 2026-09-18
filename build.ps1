<#
.SYNOPSIS
    把「发票批量查验」打包成免安装的 Windows 程序。

.DESCRIPTION
    在项目根目录执行。脚本会自动建虚拟环境、装依赖、调用 PyInstaller，
    最后在 dist\ 下产出可以直接拷走的程序目录。

    exe 默认使用系统自带的 Edge，不捆绑 Chromium（省约 300MB）。
    目标机器需要有 Microsoft Edge（Win10/11 自带）或 Google Chrome。

.PARAMETER OneFile
    打成单个 exe。拷贝方便，但每次启动要解压到临时目录，慢几秒。

.PARAMETER NoOcr
    不打包验证码自动识别（ddddocr/onnxruntime/opencv）。
    体积少约 200MB，代价是验证码全部要人工输入——功能不受影响。

.PARAMETER SkipInstall
    跳过依赖安装，只重新打包（改完代码快速重出包时用）。

.EXAMPLE
    .\build.ps1
    .\build.ps1 -OneFile
    .\build.ps1 -NoOcr -SkipInstall
#>
[CmdletBinding()]
param(
    [switch]$OneFile,
    [switch]$NoOcr,
    [switch]$SkipInstall,
    [string]$PythonExe = "",
    [string]$OutDir = "dist"
)

# 注意：这里刻意用 'Continue' 而不是 'Stop'。
# pip 和 PyInstaller 会把**正常日志写到 stderr**；在 'Stop' 策略下，
# 调用方一旦重定向了输出（例如 .\build.ps1 2>&1 | Tee-Object build.log），
# 这些 stderr 行会被 PowerShell 当成终止错误，脚本在半路莫名中止。
# 所以成败一律看退出码，用 Assert-Exit 显式检查。
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Info($m) { Write-Host "  $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "`n  ✗ $m`n" -ForegroundColor Red; exit 1 }

# 原生命令的成败只看退出码（原因见文件开头关于 ErrorActionPreference 的说明）
function Assert-Exit($what) {
    if ($LASTEXITCODE -ne 0) { Die "$what 失败（退出码 $LASTEXITCODE）。" }
}

Write-Host ""
Write-Host "=================================================="
Write-Host "  发票批量查验 —— 打包"
Write-Host "=================================================="
Write-Host ""

# ---------------------------------------------------------------- 1. Python
Info "[1/5] 查找 Python…"

$candidates = @()
if ($PythonExe) { $candidates += , @($PythonExe) }
$candidates += , @('py', '-3')
$candidates += , @('python')
$candidates += , @('python3')

$pyCmd = $null
$pyPre = @()
foreach ($c in $candidates) {
    $exe = $c[0]
    $pre = @($c | Select-Object -Skip 1)
    try {
        $v = & $exe @pre -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v) {
            $parts = $v.Trim().Split('.')
            if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10) {
                $pyCmd = $exe; $pyPre = $pre
                Ok "使用 $exe $($pre -join ' ')  →  Python $($v.Trim())"
                break
            }
            Warn "$exe 是 Python $($v.Trim())，需要 3.10 或更高"
        }
    }
    catch { }
}

if (-not $pyCmd) {
    Die @"
找不到 Python 3.10+。

请从 https://www.python.org/downloads/ 安装，
安装时务必勾选 "Add python.exe to PATH"，然后重开 PowerShell 再试。
"@
}

# ---------------------------------------------------------------- 2. 虚拟环境
$venv = Join-Path $root ".build-venv"
$venvPy = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $venvPy)) {
    Info "[2/5] 创建虚拟环境 .build-venv …"
    & $pyCmd @pyPre -m venv $venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) {
        Die "创建虚拟环境失败。"
    }
    Ok "虚拟环境已创建"
}
else {
    Info "[2/5] 复用已有虚拟环境 .build-venv"
}

# ---------------------------------------------------------------- 3. 依赖
if (-not $SkipInstall) {
    Info "[3/5] 安装依赖（第一次会比较久，要下载 PyInstaller/Playwright 等）…"
    & $venvPy -m pip install --upgrade pip --quiet
    & $venvPy -m pip install -r requirements.txt
    Assert-Exit "安装依赖"

    if ($NoOcr) {
        Info "按 -NoOcr 移除验证码识别相关依赖…"
        & $venvPy -m pip uninstall -y ddddocr onnxruntime opencv-python-headless | Out-Null
    }
    Ok "依赖就绪"
}
else {
    Warn "[3/5] 已跳过依赖安装（-SkipInstall）"
}

# ---------------------------------------------------------------- 4. 打包
Info "[4/5] 调用 PyInstaller 打包（几分钟）…"

$appName = "InvoiceCheck"     # 先用 ASCII 名，避免部分环境下中文名出问题
$pyi = @('--noconfirm', '--clean', '--windowed')
if ($OneFile) { $pyi += '--onefile' }
$pyi += @('--name', $appName, '--distpath', $OutDir,
    '--workpath', 'build', '--specpath', 'build')

# 这些是「延迟导入」的库：PyInstaller 静态分析看不到，必须显式声明
$pyi += @('--hidden-import', 'yaml')
$pyi += @('--hidden-import', 'pdfplumber')
$pyi += @('--hidden-import', 'PIL', '--hidden-import', 'PIL.Image')
$pyi += @('--hidden-import', 'pypdfium2')
$pyi += @('--hidden-import', 'defusedxml', '--hidden-import', 'defusedxml.ElementTree')
$pyi += @('--hidden-import', 'playwright', '--hidden-import', 'playwright.sync_api')

# playwright 自带 Node 驱动、pypdfium2 自带 PDFium、pdfminer 带 cmap 数据，
# 都必须整包收进去，否则运行时才报错
$pyi += @('--collect-all', 'playwright')
$pyi += @('--collect-all', 'pypdfium2')
$pyi += @('--collect-all', 'pdfminer')

if (-not $NoOcr) {
    $pyi += @('--collect-all', 'ddddocr')
    $pyi += @('--collect-all', 'onnxruntime')
    $pyi += @('--collect-all', 'cv2')
    $pyi += @('--hidden-import', 'numpy')
}

$pyi += '发票查验.pyw'

& $venvPy -m PyInstaller @pyi
Assert-Exit "PyInstaller 打包"

# ---------------------------------------------------------------- 5. 收尾
Info "[5/5] 整理产物…"

if ($OneFile) {
    $built = Join-Path $OutDir "$appName.exe"
    $finalDir = $OutDir
}
else {
    $finalDir = Join-Path $OutDir $appName
    $built = Join-Path $finalDir "$appName.exe"
}

if (-not (Test-Path $built)) { Die "打包似乎成功了，但找不到产物：$built" }

$finalExe = Join-Path $finalDir "发票查验.exe"
if ($built -ne $finalExe) {
    Move-Item -LiteralPath $built -Destination $finalExe -Force
}

if (Test-Path "config.example.yaml") {
    Copy-Item "config.example.yaml" (Join-Path $finalDir "config.example.yaml") -Force
}

$sizeMb = [math]::Round(
    ((Get-ChildItem $finalDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)

Write-Host ""
Write-Host "=================================================="
Write-Host "  打包完成" -ForegroundColor Green
Write-Host "=================================================="
Write-Host ""
Write-Host "  程序：$finalExe"
Write-Host "  体积：$sizeMb MB"
Write-Host ""
Write-Host "  用法：把「发票查验.exe」拷到放发票的目录，双击运行。"
Write-Host "        没有 config.yaml 也能跑（默认查程序所在目录）。"
Write-Host ""
if (-not $NoOcr) {
    Write-Host "  验证码：默认弹窗人工输入（想试自动识别，把 config.yaml 里" -ForegroundColor Cyan
    Write-Host "          captcha.auto_ocr 改成 true）。位数不固定，输完点提交。" -ForegroundColor Cyan
} else {
    Write-Host "  验证码：本次为 -NoOcr 构建，只能人工输入。" -ForegroundColor Yellow
}
Write-Host "  浏览器：使用系统自带的 Edge，未捆绑 Chromium。" -ForegroundColor Cyan
Write-Host ""

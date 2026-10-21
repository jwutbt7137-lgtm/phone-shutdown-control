# 构建手机端 APK
#
# 用法：  powershell -ExecutionPolicy Bypass -File .\build-apk.ps1
#
# 工具链目录（JDK 21 / Gradle 8.14.3 / Android SDK 35）：
#   - 默认读环境变量 PHONE_SHUTDOWN_TOOLS
#   - 没设就用本机历史位置 E:\dsh\.tools\phone-shutdown
# 之所以不用 gradlew，是因为离线环境没有预置 Gradle；wrapper 需要联网拉发行包，
# 直接用一份本地 Gradle 更稳。要换位置：$env:PHONE_SHUTDOWN_TOOLS = 'D:\android-toolchain'

$ErrorActionPreference = 'Stop'

$Tools = if ($env:PHONE_SHUTDOWN_TOOLS) { $env:PHONE_SHUTDOWN_TOOLS } else { 'E:\dsh\.tools\phone-shutdown' }
$JdkHome    = Join-Path $Tools 'jdk'
$SdkRoot    = Join-Path $Tools 'sdk'
$GradleHome = Join-Path $Tools 'gradle-home'
$AndroidUserHome = Join-Path $Tools 'android-user-home'
$GradleBin  = Join-Path $Tools 'gradle\bin\gradle.bat'
$ProjectDir = Join-Path $PSScriptRoot 'android'

function Assert-Path($path, $what) {
    if (-not (Test-Path $path)) { throw "缺少 $what：$path" }
}

Assert-Path $JdkHome    'JDK'
Assert-Path $SdkRoot    'Android SDK'
Assert-Path $GradleBin  'Gradle'
Assert-Path $ProjectDir 'Android 工程目录'

New-Item -ItemType Directory -Force -Path $GradleHome | Out-Null
New-Item -ItemType Directory -Force -Path $AndroidUserHome | Out-Null

# 生成 local.properties（Gradle 用它定位 SDK）
$sdkDir = $SdkRoot -replace '\\', '/'
Set-Content -Path (Join-Path $ProjectDir 'local.properties') `
    -Value "# 由 build-apk.ps1 自动生成`nsdk.dir=$sdkDir" -Encoding ascii

$env:JAVA_HOME        = $JdkHome
$env:ANDROID_HOME     = $SdkRoot
$env:ANDROID_SDK_ROOT = $SdkRoot
$env:GRADLE_USER_HOME = $GradleHome
# 把 AGP 的缓存/analytics 也挪到可写目录，避免刷 "拒绝访问" 噪音
$env:ANDROID_USER_HOME = $AndroidUserHome
# Kotlin 编译器要在系统 TEMP 下写 kotlin-compiler-*.alive，本机该路径会被拒绝访问；
# 统一改到工程内的临时目录，保证编译中间产物能落盘。
$BuildTemp = Join-Path $PSScriptRoot '_build-tmp'
New-Item -ItemType Directory -Force -Path $BuildTemp | Out-Null
$env:TEMP = $BuildTemp
$env:TMP  = $BuildTemp

Write-Host ""
Write-Host "工具链：" -ForegroundColor Cyan
Write-Host "  JDK    : $JdkHome"
Write-Host "  SDK    : $SdkRoot"
Write-Host "  Gradle : $GradleBin"
Write-Host ""

Push-Location $ProjectDir
try {
    & $GradleBin --no-daemon --console=plain assembleDebug
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($code -ne 0) {
    Write-Host ""
    Write-Host "构建失败（退出码 $code）" -ForegroundColor Red
    exit $code
}

$apk = Get-ChildItem -Path (Join-Path $ProjectDir 'app\build\outputs\apk') -Recurse -Filter '*.apk' |
       Sort-Object LastWriteTime -Descending | Select-Object -First 1

Write-Host ""
if ($apk) {
    # 复制到 dist\，文件名固定，文档和 adb 命令都指向这里
    $dist = Join-Path $PSScriptRoot 'dist'
    New-Item -ItemType Directory -Force -Path $dist | Out-Null
    $dest = Join-Path $dist '电脑关机-debug.apk'
    Copy-Item -Path $apk.FullName -Destination $dest -Force
    Write-Host "构建成功 ✓" -ForegroundColor Green
    Write-Host ("  APK : {0}" -f $apk.FullName)
    Write-Host ("  复制: {0}" -f $dest)
    Write-Host ("  大小: {0:N1} MB" -f ($apk.Length / 1MB))
    Write-Host ("  时间: {0}" -f $apk.LastWriteTime)
} else {
    Write-Host "构建完成但没找到 APK，请检查输出目录" -ForegroundColor Yellow
}

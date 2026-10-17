$ErrorActionPreference = 'Stop'
$traceNativeBuild = Join-Path $PSScriptRoot 'build-native'
& cmake -S $PSScriptRoot -B $traceNativeBuild -G Ninja -DCMAKE_BUILD_TYPE=Release `
    -DCMAKE_C_COMPILER=C:/msys64/mingw64/bin/gcc.exe -DCMAKE_CXX_COMPILER=C:/msys64/mingw64/bin/g++.exe
if ($LASTEXITCODE -ne 0) { throw '解码器配置失败。' }
& cmake --build $traceNativeBuild --parallel 4
if ($LASTEXITCODE -ne 0) { throw '解码器构建失败。' }

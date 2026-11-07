#!/usr/bin/env bash
# Windows MSYS2 build of the pinned OpenOCD and libusb sources for Emscripten.
set -euo pipefail

if [ "$#" -ne 4 ]; then
    echo 'Usage: build-openocd-core-wasm.sh EMSDK OPENOCD_SOURCE LIBUSB_SOURCE OUTPUT' >&2
    exit 2
fi

sdk=$(cygpath -u "$1")
openocd=$(cygpath -u "$2")
usb=$(cygpath -u "$3")
output=$(cygpath -u "$4")

# Jim's configure runs a native Tcl interpreter, which needs a native Bash
# executable before the emcc shell script. Keep the compiler paths space-free.
for path in "$sdk" "$openocd" "$usb" "$output"; do
    case "$path" in
        /*) ;;
        *) echo "Use absolute build paths: $path" >&2; exit 2 ;;
    esac
    case "$path" in
        *" "*) echo "Use build paths without spaces: $path" >&2; exit 2 ;;
    esac
done

openocd_revision=${OPENOCD_REVISION:-9ea7f3d647c8ecf6b0f1424002dfc3f4504a162c}
jim_revision=$(git -C "$openocd" ls-tree HEAD -- jimtcl | awk '{print $3}')
if [ "$(git -C "$openocd" rev-parse HEAD)" != "$openocd_revision" ] ||
   [ -z "$jim_revision" ] ||
   [ "$(git -C "$openocd/jimtcl" rev-parse HEAD)" != "$jim_revision" ] ||
   [ "$(git -C "$usb" rev-parse HEAD)" != 87a55632db62c9bdc58cd31d3ccfa673f1bb017f ]; then
    echo "Expected OpenOCD $openocd_revision with its pinned Jim Tcl and libusb v1.0.30" >&2
    exit 2
fi

mkdir "$output"
mkdir "$output/scratch" "$output/empty-pkgconfig" "$output/libusb" "$output/openocd"
export PATH="$sdk/upstream/emscripten:$sdk/upstream/bin:/usr/bin:/mingw64/bin:$PATH"
export PYTHON="$sdk/python/3.13.3_64bit/python.exe"
export EM_CONFIG="$(cygpath -m "$sdk/.emscripten")"
export TEMP="$(cygpath -m "$output/scratch")" TMP="$(cygpath -m "$output/scratch")"
export TMPDIR="$output/scratch"
export AR=emar RANLIB=emranlib NM=llvm-nm CC_FOR_BUILD=/mingw64/bin/gcc
export PKG_CONFIG_LIBDIR="$output/empty-pkgconfig"
trap 'echo "Build failed. Logs are in $output" >&2' ERR

cd "$usb"
./bootstrap.sh > "$output/bootstrap-usb.log" 2>&1
cd "$output/libusb"
CC=emcc CXX=em++ "$usb/configure" --host=wasm32-emscripten --disable-shared --enable-static \
    --prefix="$output/usb-prefix" CFLAGS=-O2 CXXFLAGS=-O2 > "$output/configure-usb.log" 2>&1
make -j 8 -C libusb install > "$output/build-usb.log" 2>&1
mkdir -p "$output/usb-prefix/lib/pkgconfig"
cp libusb-1.0.pc "$output/usb-prefix/lib/pkgconfig/"

export PKG_CONFIG_LIBDIR="$output/usb-prefix/lib/pkgconfig"
cd "$openocd"
./bootstrap nosubmodule > "$output/bootstrap-openocd.log" 2>&1
cd "$output/openocd"
CC="$(cygpath -m /usr/bin/bash.exe) $sdk/upstream/emscripten/emcc" \
    "$openocd/configure" --host=wasm32-unknown-emscripten --disable-werror \
    --enable-internal-jimtcl --disable-internal-libjaylink --enable-dummy --enable-cmsis-dap-v2 \
    CFLAGS=-O1 > "$output/configure-openocd.log" 2>&1
make -j 8 -C jimtcl libjim.a > "$output/build-jim.log" 2>&1
make src/startup_tcl.inc > "$output/build-startup.log" 2>&1
make -j 12 src/libopenocd.la > "$output/build-openocd.log" 2>&1

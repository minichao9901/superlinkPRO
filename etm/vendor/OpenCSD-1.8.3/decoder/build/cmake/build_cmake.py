#!/usr/bin/env python3
"""
Wrapper around the OpenCSD CMake build.

Supports the common repo-local workflows:
- configure + build
- build only from a previously configured build tree
- run the CMake clean target or remove a build tree
- optional Debug configuration
- optional minimal library mode
- optional install / uninstall targets without rebuilding
- dedicated install-only / uninstall-only actions
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys


SCRIPT_PATH = pathlib.Path(__file__).resolve()
SCRIPT_DIR = SCRIPT_PATH.parent


def find_repo_root(start_dir: pathlib.Path) -> pathlib.Path:
    for candidate in (start_dir, *start_dir.parents):
        if (candidate / "CMakeLists.txt").is_file() and (
            candidate / "decoder" / "build" / "cmake"
        ).is_dir():
            return candidate
    raise RuntimeError(
        f"could not locate repository root from script path {start_dir}"
    )


REPO_ROOT = find_repo_root(SCRIPT_DIR)


def default_generator() -> str:
    if sys.platform == "win32":
        return "Visual Studio 17 2022"
    return "Unix Makefiles"


def is_multi_config_generator(generator: str) -> bool:
    return (
        generator.startswith("Visual Studio")
        or generator == "Xcode"
        or generator.endswith("Multi-Config")
    )


def default_build_config(args: argparse.Namespace) -> str:
    if args.config:
        return args.config
    return "Debug" if args.debug else "Release"


def default_platform(generator: str) -> str | None:
    if sys.platform == "win32" and generator.startswith("Visual Studio"):
        return "x64"
    return None


def find_windows_cmake() -> str | None:
    program_files_x86 = pathlib.Path(
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    )
    vswhere = program_files_x86 / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if vswhere.is_file():
        try:
            result = subprocess.run(
                [
                    str(vswhere),
                    "-latest",
                    "-products",
                    "*",
                    "-find",
                    r"Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            cmake_path = result.stdout.strip()
            if cmake_path:
                return cmake_path
        except (OSError, subprocess.CalledProcessError):
            pass

    for edition in ("Enterprise", "Professional", "Community", "BuildTools"):
        candidate = (
            pathlib.Path(r"C:\Program Files")
            / "Microsoft Visual Studio"
            / "2022"
            / edition
            / "Common7"
            / "IDE"
            / "CommonExtensions"
            / "Microsoft"
            / "CMake"
            / "CMake"
            / "bin"
            / "cmake.exe"
        )
        if candidate.is_file():
            return str(candidate)

    return None


def resolve_cmake() -> str:
    cmake = shutil.which("cmake")
    if cmake:
        return cmake

    if sys.platform == "win32":
        cmake = find_windows_cmake()
        if cmake:
            return cmake

    raise RuntimeError(
        "could not locate 'cmake' on PATH"
        + (
            " or in the Visual Studio bundled CMake location"
            if sys.platform == "win32"
            else ""
        )
    )


def default_build_dir(args: argparse.Namespace) -> pathlib.Path:
    name = "build-cmake"
    if args.minimal:
        name += "-min"
    if args.debug:
        name += "-debug"
    return SCRIPT_DIR / name


def run_cmd(cmd: list[str], cwd: pathlib.Path) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd), check=True)


def is_configured_build_dir(build_dir: pathlib.Path) -> bool:
    return (build_dir / "CMakeCache.txt").is_file()


def remove_build_dir(build_dir: pathlib.Path) -> None:
    if build_dir.exists():
        print(f"+ remove build directory {build_dir}")
        shutil.rmtree(build_dir)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Configure and drive the OpenCSD CMake build."
    )
    parser.add_argument(
        "--build-dir",
        type=pathlib.Path,
        help=(
            "Out-of-source build directory. Defaults to a mode-specific "
            "subdirectory under decoder/build/cmake."
        ),
    )
    parser.add_argument(
        "--generator",
        help=(
            "CMake generator to use. Defaults to "
            f'"{default_generator()}" on this platform.'
        ),
    )
    parser.add_argument(
        "--platform",
        help=(
            "Platform passed to CMake via -A for Visual Studio generators. "
            'Defaults to "x64" on Windows Visual Studio builds.'
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Use a Debug build. For single-config generators this sets "
            "-DCMAKE_BUILD_TYPE=Debug; for multi-config generators it "
            "selects --config Debug."
        ),
    )
    parser.add_argument(
        "--config",
        choices=("Debug", "Release"),
        help=(
            "Build configuration. Defaults to Release, or Debug when "
            "--debug is set."
        ),
    )
    parser.add_argument(
        "--minimal",
        action="store_true",
        help="Configure with -DOPENCSD_BUILD_MIN_LIB_STATIC=ON.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        help="Parallel build job count passed to cmake --build --parallel.",
    )
    parser.add_argument(
        "--configure-only",
        action="store_true",
        help="Run CMake configure only.",
    )
    parser.add_argument(
        "--clean-all",
        action="store_true",
        help="Run the generated CMake clean target on an existing configured build tree, then remove the selected build directory before any other action, or do both and exit if no other action is requested.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Run the generated CMake clean target from an already configured build tree without configuring or building.",
    )
    parser.add_argument(
        "--no-configure",
        action="store_true",
        help="Reuse an already configured build directory and skip CMake configure.",
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="Skip cmake --build so install/uninstall can operate on a previous build.",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Run cmake --install after building.",
    )
    parser.add_argument(
        "--install-only",
        action="store_true",
        help="Run cmake --install from an already configured build tree without configuring or building.",
    )
    parser.add_argument(
        "--install-prefix",
        type=pathlib.Path,
        help="Installation prefix passed to cmake --install.",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Run the generated uninstall target after the main action.",
    )
    parser.add_argument(
        "--uninstall-only",
        action="store_true",
        help="Run the generated uninstall target from an already configured build tree without configuring or building.",
    )
    parser.add_argument(
        "--build-testing",
        choices=("ON", "OFF"),
        help="Explicit BUILD_TESTING setting. If omitted, project defaults apply.",
    )
    parser.add_argument(
        "--full-project-layout",
        choices=("ON", "OFF"),
        help="Explicit OPENCSD_FULL_PROJECT_LAYOUT setting. If omitted, project defaults apply.",
    )
    args = parser.parse_args()

    if args.configure_only and args.no_configure:
        parser.error("--configure-only cannot be used with --no-configure")
    if args.install_prefix and not (args.install or args.install_only):
        parser.error("--install-prefix requires --install or --install-only")
    if args.no_build and args.configure_only:
        parser.error("--no-build cannot be used with --configure-only")
    if args.clean_all and args.no_configure:
        parser.error("--clean-all cannot be combined with --no-configure")
    if args.clean_all and args.no_build:
        parser.error("--clean-all cannot be combined with --no-build")
    if args.clean and args.configure_only:
        parser.error("--clean cannot be combined with --configure-only")
    if args.clean and args.no_configure:
        parser.error("--clean already skips configure; do not combine it with --no-configure")
    if args.clean and args.no_build:
        parser.error("--clean already skips the main build; do not combine it with --no-build")
    if args.no_build and not (args.install or args.uninstall):
        parser.error(
            "--no-build requires one of --install or --uninstall"
        )
    if args.install_only and args.install:
        parser.error("--install-only cannot be combined with --install")
    if args.uninstall_only and args.uninstall:
        parser.error("--uninstall-only cannot be combined with --uninstall")
    if args.install_only and args.uninstall_only:
        parser.error("--install-only cannot be combined with --uninstall-only")
    if args.install_only and args.configure_only:
        parser.error("--install-only cannot be combined with --configure-only")
    if args.uninstall_only and args.configure_only:
        parser.error("--uninstall-only cannot be combined with --configure-only")
    if args.install_only and args.no_configure:
        parser.error("--install-only already skips configure; do not combine it with --no-configure")
    if args.uninstall_only and args.no_configure:
        parser.error("--uninstall-only already skips configure; do not combine it with --no-configure")
    if args.install_only and args.no_build:
        parser.error("--install-only already skips build; do not combine it with --no-build")
    if args.uninstall_only and args.no_build:
        parser.error("--uninstall-only already skips build; do not combine it with --no-build")
    if args.install_only and args.uninstall:
        parser.error("--install-only cannot be combined with --uninstall")
    if args.uninstall_only and args.install:
        parser.error("--uninstall-only cannot be combined with --install")
    if args.clean_all and (args.install_only or args.uninstall_only):
        parser.error("--clean-all cannot be combined with --install-only or --uninstall-only")
    if args.clean_all and args.clean:
        parser.error("--clean-all cannot be combined with --clean")
    if args.clean and (
        args.install
        or args.install_only
        or args.uninstall
        or args.uninstall_only
    ):
        parser.error("--clean cannot be combined with install or uninstall actions")

    return args


def main() -> int:
    args = parse_args()
    cmake = resolve_cmake()
    generator = args.generator or default_generator()
    build_config = default_build_config(args)
    platform_name = args.platform or default_platform(generator)
    build_dir = (args.build_dir or default_build_dir(args)).resolve()

    if args.debug and args.config == "Release":
        raise RuntimeError("--debug cannot be combined with --config Release")
    if platform_name and not generator.startswith("Visual Studio"):
        raise RuntimeError("--platform is only supported with Visual Studio generators")

    if args.clean_all:
        if is_configured_build_dir(build_dir):
            clean_cmd = [cmake, "--build", str(build_dir), "--target", "clean"]
            if is_multi_config_generator(generator):
                clean_cmd.extend(["--config", build_config])
            run_cmd(clean_cmd, REPO_ROOT)
        remove_build_dir(build_dir)
        if not (
            args.configure_only
            or args.install
            or args.install_only
            or args.uninstall
            or args.uninstall_only
            or args.clean
            or args.debug
            or args.minimal
            or args.build_testing
            or args.full_project_layout
        ):
            return 0

    skip_configure = (
        args.no_configure or args.install_only or args.uninstall_only or args.clean
    )
    skip_build = args.no_build or args.install_only or args.uninstall_only or args.clean

    if skip_configure:
        if not is_configured_build_dir(build_dir):
            raise RuntimeError(
                f"build directory is not configured: {build_dir}. "
                "Run a configure/build first."
            )
    else:
        cmake_args = [
            cmake,
            "-S",
            str(REPO_ROOT),
            "-B",
            str(build_dir),
            "-G",
            generator,
        ]

        if platform_name:
            cmake_args.extend(["-A", platform_name])
        if is_multi_config_generator(generator):
            pass
        else:
            cmake_args.append(f"-DCMAKE_BUILD_TYPE={build_config}")
        if args.minimal:
            cmake_args.append("-DOPENCSD_BUILD_MIN_LIB_STATIC=ON")
        if args.build_testing:
            cmake_args.append(f"-DBUILD_TESTING={args.build_testing}")
        if args.full_project_layout:
            cmake_args.append(
                f"-DOPENCSD_FULL_PROJECT_LAYOUT={args.full_project_layout}"
            )

        build_dir.mkdir(parents=True, exist_ok=True)
        run_cmd(cmake_args, REPO_ROOT)

    if not args.configure_only and not skip_build:
        build_cmd = [cmake, "--build", str(build_dir)]
        if is_multi_config_generator(generator):
            build_cmd.extend(["--config", build_config])
        if args.jobs:
            build_cmd.extend(["--parallel", str(args.jobs)])
        run_cmd(build_cmd, REPO_ROOT)

    if args.install or args.install_only:
        install_cmd = [cmake, "--install", str(build_dir)]
        if is_multi_config_generator(generator):
            install_cmd.extend(["--config", build_config])
        if args.install_prefix:
            install_cmd.extend(["--prefix", str(args.install_prefix.resolve())])
        run_cmd(install_cmd, REPO_ROOT)

    if args.clean:
        run_cmd(
            [
                cmake,
                "--build",
                str(build_dir),
                "--target",
                "clean",
            ]
            + (
                ["--config", build_config]
                if is_multi_config_generator(generator)
                else []
            ),
            REPO_ROOT,
        )
    elif args.uninstall or args.uninstall_only:
        run_cmd(
            [
                cmake,
                "--build",
                str(build_dir),
                "--target",
                "uninstall",
            ]
            + (
                ["--config", build_config]
                if is_multi_config_generator(generator)
                else []
            ),
            REPO_ROOT,
        )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"command failed with exit code {exc.returncode}", file=sys.stderr)
        raise SystemExit(exc.returncode)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130)

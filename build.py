from __future__ import annotations

import argparse
import importlib.metadata as metadata
import importlib.util
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys

APP_NAME_DEFAULT = "HabitatLite"
HABIT_VERSION_REQUIRED = "2.0.0"
PYINSTALLER_REQUIREMENT = "PyInstaller>=6.10,<7"

# These are package import names (not necessarily distribution names).
# collect-all gathers Python modules, data files, binaries and submodules.
COLLECT_PACKAGES = [
    "streamlit",
    "habit",
    "SimpleITK",
    "numpy",
    "pandas",
    "scipy",
    "sklearn",
    "matplotlib",
    "pyarrow",
    "altair",
    "tornado",
    "watchdog",
    "PIL",
    "joblib",
    "skimage",       # Optional in some HABIT workflows; include when installed.
]

# Modules used dynamically by Streamlit/HABIT or imported from the bundled app source.
HIDDEN_IMPORTS = [
    "habitatlite",
    "habitatlite.core",
    "habit",
    "habit.recipes",
    "habit.spec",
    "habit.contracts",
    "habit.adapters",
    "habit.voxel_features",
    "habit.supervoxel",
    "habit.habitat_model",
    "habit.pipeline",
    "habit.habitat_features",
    "habit.execution",
    "streamlit.web.cli",
    "skimage.segmentation",
    "sklearn.utils._typedefs",
    "sklearn.neighbors._partition_nodes",
    "sklearn.tree._utils",
    "scipy.special._ufuncs",
    "scipy.linalg.cython_blas",
    "pyarrow._compute",
]

# importlib.metadata is used by HabitatLite/HABIT at runtime to report versions.
# Copy metadata for every installed distribution queried by the application.
METADATA_DISTRIBUTIONS = [
    "habitat-analysis",
    "streamlit",
    "SimpleITK",
    "numpy",
    "pandas",
    "scipy",
    "scikit-learn",
    "matplotlib",
    "pyarrow",
    "altair",
    "watchdog",
    "Pillow",
    "joblib",
    "scikit-image",
    "pyradiomics",
]

REQUIRED_IMPORTS = [
    "streamlit",
    "habit",
    "SimpleITK",
    "numpy",
    "pandas",
    "scipy",
    "sklearn",
    "matplotlib",
]


def print_command(command: list[str]) -> None:
    print("\n> " + subprocess.list2cmdline([str(x) for x in command]), flush=True)


def run_checked(command: list[str], cwd: Path, dry_run: bool = False) -> None:
    print_command(command)
    if dry_run:
        return
    subprocess.run(command, cwd=str(cwd), check=True)


def validate_ico(path: Path) -> None:
    """Check the ICO container header before passing it to PyInstaller."""
    if not path.is_file():
        raise FileNotFoundError(
            f"未找到程序图标：{path}\n"
            "请将有效的 app.ico 放在 build.py 同一目录。"
        )
    if path.stat().st_size < 22:
        raise ValueError(f"app.ico 文件过小或不完整：{path}")
    with path.open("rb") as stream:
        header = stream.read(6)
    reserved, image_type, image_count = struct.unpack("<HHH", header)
    if reserved != 0 or image_type != 1 or image_count < 1:
        raise ValueError(
            "app.ico 不是有效的 Windows ICO 文件。请使用真正的 .ico 文件，"
            "不要直接把 PNG/JPG 改名为 .ico。"
        )


def verify_project_layout(project_root: Path) -> Path:
    required = [
        "app.py",
        "run_streamlit.py",
        "habitatlite/core.py",
        "habitatlite/__init__.py",
    ]
    missing = [name for name in required if not (project_root / name).is_file()]
    if missing:
        raise FileNotFoundError(
            "项目目录不完整，缺少以下文件：\n  - " + "\n  - ".join(missing)
            + "\n请把更新包中的 build.py、run_streamlit.py 放在项目根目录，"
              "并确认 app.py 与 habitatlite/core.py 存在。"
        )
    icon = project_root / "app.ico"
    validate_ico(icon)
    return icon


def verify_runtime_environment() -> None:
    """Fail early if the active interpreter is not the prepared HabitatLite env."""
    missing = [name for name in REQUIRED_IMPORTS if importlib.util.find_spec(name) is None]
    if missing:
        raise RuntimeError(
            "当前 Python 环境缺少核心依赖模块：" + ", ".join(missing)
            + "\n请在已配置 HabitatLite/HABIT 的 diabetes 环境中执行 build.py。"
        )

    try:
        habit_version = metadata.version("habitat-analysis")
    except metadata.PackageNotFoundError as exc:
        raise RuntimeError("当前环境未安装 habitat-analysis。") from exc
    if habit_version != HABIT_VERSION_REQUIRED:
        raise RuntimeError(
            f"HABIT 版本不匹配：需要 {HABIT_VERSION_REQUIRED}，当前为 {habit_version}。"
        )

    # Import the real public API now, so a broken environment fails before packaging.
    import streamlit  # noqa: F401
    import SimpleITK  # noqa: F401
    import numpy  # noqa: F401
    import pandas  # noqa: F401
    import scipy  # noqa: F401
    import sklearn  # noqa: F401
    import matplotlib  # noqa: F401
    from habit.spec import HabitatSpec, Spec, Stage  # noqa: F401
    from habit.recipes import Study  # noqa: F401

    print("\n当前打包 Python 环境检查通过：")
    print(f"  Python: {sys.version.split()[0]}")
    print(f"  Executable: {sys.executable}")
    print(f"  habitat-analysis: {habit_version}")
    for distribution in METADATA_DISTRIBUTIONS:
        try:
            version = metadata.version(distribution)
        except metadata.PackageNotFoundError:
            continue
        print(f"  {distribution}: {version}")

    # SLIC is optional for the default voxel-wise mode. Warn rather than fail.
    if importlib.util.find_spec("skimage") is None:
        print(
            "[提示] 当前环境未安装 scikit-image。Voxel-wise 模式仍可运行；"
            "Two-step + SLIC 将不可用。若需 SLIC，请安装 scikit-image 后重新打包。"
        )


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("未检测到 PyInstaller，正在当前 Python 环境安装……", flush=True)
        subprocess.run(
            [sys.executable, "-m", "pip", "install", PYINSTALLER_REQUIREMENT],
            check=True,
        )


def add_package_collection(command: list[str], dry_run: bool) -> None:
    for package in COLLECT_PACKAGES:
        try:
            spec = importlib.util.find_spec(package)
        except (ImportError, ModuleNotFoundError, ValueError):
            spec = None
        if spec is not None:
            command.extend(["--collect-all", package])
        elif package in REQUIRED_IMPORTS:
            if dry_run:
                print(f"[dry-run] 当前环境没有 {package}；请在 Windows diabetes 环境中正式打包。")
            else:
                raise RuntimeError(f"打包环境缺少核心依赖模块：{package}")


def add_metadata_collection(command: list[str]) -> None:
    for distribution in METADATA_DISTRIBUTIONS:
        try:
            metadata.version(distribution)
        except metadata.PackageNotFoundError:
            continue
        command.extend(["--copy-metadata", distribution])


def add_hidden_imports(command: list[str]) -> None:
    for module in HIDDEN_IMPORTS:
        try:
            spec = importlib.util.find_spec(module)
        except (ImportError, ModuleNotFoundError, ValueError):
            spec = None
        if spec is not None:
            command.extend(["--hidden-import", module])


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build HabitatLite (Streamlit + HABIT) as a Windows onedir EXE. "
            "A console window is kept by default for logs and debugging."
        )
    )
    parser.add_argument("--name", default=APP_NAME_DEFAULT, help="EXE and distribution folder name")
    parser.add_argument(
        "--windowed", action="store_true",
        help="Hide the console window (default is console enabled).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the PyInstaller command without building")
    parser.add_argument(
        "--skip-runtime-check", action="store_true",
        help="Skip HABIT/scientific Python environment checks (not recommended).",
    )
    parser.add_argument(
        "--clean", action="store_true",
        help="Explicitly request a clean build (clean build is the default).",
    )
    parser.add_argument(
        "--no-clean", action="store_true",
        help="Keep the previous build work directory and dist/<name> before building.",
    )
    args = parser.parse_args()

    if os.name != "nt" and not args.dry_run:
        raise RuntimeError(
            "此脚本用于生成 Windows EXE，请在 Windows 的 Conda diabetes 环境中运行。"
            "当前非 Windows 环境仅支持 --dry-run。"
        )

    project_root = Path(__file__).resolve().parent
    icon_file = verify_project_layout(project_root)

    if not args.skip_runtime_check:
        print("检查当前 Python/HABIT 环境（要求 habitat-analysis==2.0.0）……", flush=True)
        verify_runtime_environment()

    if not args.dry_run:
        ensure_pyinstaller()

    build_root = project_root / "build"
    dist_root = project_root / "dist"
    work_path = build_root / "pyinstaller_work"
    spec_path = build_root / "spec"
    app_dist = dist_root / args.name

    if not args.no_clean:
        if work_path.exists():
            shutil.rmtree(work_path, ignore_errors=True)
        if app_dist.exists():
            shutil.rmtree(app_dist, ignore_errors=True)

    work_path.mkdir(parents=True, exist_ok=True)
    spec_path.mkdir(parents=True, exist_ok=True)
    dist_root.mkdir(parents=True, exist_ok=True)

    separator = ";" if os.name == "nt" else ":"
    app_file = project_root / "app.py"
    launcher = project_root / "run_streamlit.py"

    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name", args.name,
        "--distpath", str(dist_root),
        "--workpath", str(work_path),
        "--specpath", str(spec_path),
        "--paths", str(project_root),
        "--icon", str(icon_file),
        "--add-data", f"{app_file}{separator}app_source",
        "--add-data", f"{icon_file}{separator}app_source",
        "--noupx",
        "--noconfirm",
    ]

    # Keep the requested command window by default; logs remain visible in the console.
    command.append("--windowed" if args.windowed else "--console")

    add_package_collection(command, dry_run=args.dry_run)
    add_metadata_collection(command)
    add_hidden_imports(command)

    # The launcher imports habitatlite.core explicitly; --paths above lets PyInstaller
    # analyse its dependency graph instead of leaving it as an unresolved runtime import.
    command.append(str(launcher))
    print("\n开始生成 Windows onedir 程序……", flush=True)
    run_checked(command, project_root, dry_run=args.dry_run)

    if args.dry_run:
        print("\n[dry-run] 命令检查结束；未生成 EXE。")
        return 0

    app_dist.mkdir(parents=True, exist_ok=True)
    for folder in ("images", "masks", "habitat_results"):
        (app_dist / folder).mkdir(exist_ok=True)

    # Keep a convenient icon beside the EXE and a copy inside the bundled _internal data.
    shutil.copy2(icon_file, app_dist / "app.ico")

    exe_path = app_dist / f"{args.name}.exe"
    if not exe_path.is_file():
        raise FileNotFoundError(
            f"PyInstaller 命令结束但未找到预期 EXE：{exe_path}\n"
            "请检查上方完整 PyInstaller 日志。"
        )

    print("\n打包完成：", flush=True)
    print(f"  EXE: {exe_path}")
    print(f"  数据目录: {app_dist / 'images'}")
    print(f"  Mask 目录: {app_dist / 'masks'}")
    print(f"  结果目录: {app_dist / 'habitat_results'}")
    print(f"  图标: {app_dist / 'app.ico'}")
    print("\n请分发整个 dist/" + args.name + " 文件夹，不要只复制 EXE。")
    print("首次运行建议从命令提示符启动，以便保留并查看诊断日志。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"\n[打包失败] 命令返回非零退出码：{exc.returncode}", file=sys.stderr)
        raise SystemExit(exc.returncode or 1)
    except Exception as exc:
        print(f"\n[打包失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)

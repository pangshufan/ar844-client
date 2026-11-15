"""Build and smoke test a native desktop bundle on the current runner."""
from pathlib import Path
import os
import platform
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
APP = "AR844-Monitor"
ARTIFACTS = DIST / "artifacts"


def run(*args, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=True, **kwargs)


def main():
    options = ["--osx-bundle-identifier", "org.ar844.monitor"] if sys.platform == "darwin" else []
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--name", APP, "--windowed", "--onedir", *options, "ar844/__main__.py")
    ARTIFACTS.mkdir(exist_ok=True)
    artifact = ARTIFACTS / f"{APP}-{platform.system().lower()}-{platform.machine().lower()}"
    bundle = DIST / APP
    if sys.platform == "darwin":
        bundle = DIST / f"{APP}.app"
        executable = bundle / "Contents" / "MacOS" / APP
    elif sys.platform == "win32":
        executable = bundle / f"{APP}.exe"
    else:
        executable = bundle / APP
        shutil.copy2(ROOT / "packaging" / "70-ar844.rules", bundle)

    with TemporaryDirectory(prefix="ar844-smoke-") as temp:
        path = Path(temp) / "smoke.sqlite3"
        run(str(executable), "--smoke-test", "--database", str(path),
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, timeout=90)
        if not path.is_file():
            raise RuntimeError("Packaged client did not create its smoke-test database")

    if sys.platform == "darwin":
        run("ditto", "-c", "-k", "--keepParent", str(bundle), f"{artifact}.zip")
    else:
        shutil.make_archive(str(artifact), "zip" if sys.platform == "win32" else "gztar",
                            root_dir=DIST, base_dir=bundle.name)


if __name__ == "__main__":
    main()

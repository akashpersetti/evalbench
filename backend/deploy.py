"""Build the Lambda deployment package shared by the api and runner functions.

Requires Docker (to install dependencies against the Lambda Python 3.12
runtime image, matching its manylinux ABI) and `uv`.
"""

import shutil
import subprocess
import zipfile
from pathlib import Path

BACKEND_DIR = Path(__file__).parent
BUILD_DIR = BACKEND_DIR / "lambda-package"
ZIP_PATH = BACKEND_DIR / "lambda-deployment.zip"


def main() -> None:
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    BUILD_DIR.mkdir()

    requirements_path = BUILD_DIR / "requirements.txt"
    subprocess.run(
        [
            "uv", "export", "--no-dev", "--no-hashes", "--no-emit-project",
            "-o", str(requirements_path),
        ],
        check=True,
        cwd=BACKEND_DIR.parent,
    )

    print("Installing dependencies for the Lambda runtime...")
    subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{BUILD_DIR}:/var/task",
            "--platform", "linux/amd64",
            "--entrypoint", "",
            "public.ecr.aws/lambda/python:3.12",
            "/bin/sh", "-c",
            "pip install --target /var/task -r /var/task/requirements.txt "
            "--platform manylinux2014_x86_64 --only-binary=:all: --upgrade",
        ],
        check=True,
    )
    requirements_path.unlink()

    print("Copying application code...")
    shutil.copytree(BACKEND_DIR / "evalbench", BUILD_DIR / "evalbench")

    print("Creating zip file...")
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in BUILD_DIR.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(BUILD_DIR))

    print(f"Built {ZIP_PATH}")


if __name__ == "__main__":
    main()

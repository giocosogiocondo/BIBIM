"""Download the requested GitHub benchmark, resolving and verifying Git LFS."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

from bibim_config import MODEL_ROOT, BUILD_VERSION

REPOSITORY = "sylvainHellin/ifc-bench"
SHARED_MODELS = MODEL_ROOT


def local_target(root, relative):
    """Use models/<project>/<file> for the shared store; keep external snapshots compatible."""
    root = Path(root).resolve()
    local = relative.removeprefix("projects/") if root == SHARED_MODELS.resolve() else relative
    target = (root / local).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"Invalid repository path: {relative}")
    return target


def fetch(url):
    for attempt in range(3):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": BUILD_VERSION,
                    "Accept": "application/vnd.github+json" if "api.github.com" in url else "*/*",
                },
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1 + attempt)


def needed(path, documents=False):
    if path in ("README.md", ".gitattributes", "LICENSE") or path.startswith(
        ("questions/", "docs/")
    ):
        return True
    if not path.startswith("projects/"):
        return False
    return path.endswith(
        (".ifc", "/license.txt", "/model_card.csv", "/model_card.md", ".bcp")
    ) or (documents and path.lower().endswith((".pdf", ".xlsx", ".xls")))


def download_v1(root, *, documents=False, log=print):
    root = Path(root).resolve()
    tree = json.loads(
        fetch(f"https://api.github.com/repos/{REPOSITORY}/git/trees/main?recursive=1")
    )
    if tree.get("truncated"):
        raise RuntimeError("GitHub 파일 목록이 잘려 있습니다. 다운로드를 중단합니다.")
    commit = tree["sha"]
    files = [x for x in tree["tree"] if x["type"] == "blob" and needed(x["path"], documents)]
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "repository": f"https://github.com/{REPOSITORY}",
        "commit": commit,
        "documents": documents,
        "files": [],
    }
    for i, entry in enumerate(files, 1):
        relative = entry["path"]
        target = local_target(root, relative)
        if not target.is_relative_to(root):
            raise ValueError(f"잘못된 저장소 경로: {relative}")
        log(f"다운로드 {i}/{len(files)}: {relative}")
        path = urllib.parse.quote(relative, safe="/")
        data = fetch(f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}/{path}")
        git_hash = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        if git_hash != entry["sha"]:
            raise RuntimeError(f"Git blob 해시 불일치: {relative}")
        lfs_sha = None
        if data.startswith(b"version https://git-lfs.github.com/spec/"):
            fields = dict(line.split(" ", 1) for line in data.decode().strip().splitlines())
            lfs_sha = fields["oid"].removeprefix("sha256:")
            expected_size = int(fields["size"])
            data = fetch(f"https://media.githubusercontent.com/media/{REPOSITORY}/{commit}/{path}")
            if len(data) != expected_size or hashlib.sha256(data).hexdigest() != lfs_sha:
                raise RuntimeError(f"Git LFS 무결성 검사 실패: {relative}")
        if target.exists() and target.read_bytes() != data:
            raise FileExistsError(f"기존 파일 내용이 다릅니다. 별도 폴더를 선택하세요: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".download")
        part.write_bytes(data)
        os.replace(part, target)
        manifest["files"].append(
            {
                "path": relative,
                "local_path": target.relative_to(root).as_posix(),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "git_blob_sha": entry["sha"],
                "lfs_sha256": lfs_sha,
            }
        )
        (root / "download_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    log(
        f'다운로드 완료: {sum(x["path"].endswith(".ifc") for x in manifest["files"])}개 IFC / 커밋 {commit}'
    )
    return manifest


def file_hash(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def download_v2(root, *, documents=False, log=print):
    root = Path(root).resolve()
    api = "https://huggingface.co/api/datasets/" + REPOSITORY
    info = json.loads(fetch(api))
    commit = info["sha"]
    url = f"{api}/tree/{commit}?recursive=true&limit=1000"
    tree = []
    while url:
        request = urllib.request.Request(url, headers={"User-Agent": BUILD_VERSION})
        with urllib.request.urlopen(request, timeout=120) as response:
            tree.extend(json.load(response))
            link = response.headers.get("Link", "")
        match = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = match.group(1) if match else None
    files = [x for x in tree if x["type"] == "file" and needed(x["path"], documents)]
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "repository": "https://huggingface.co/datasets/" + REPOSITORY,
        "version": "v2",
        "commit": commit,
        "documents": documents,
        "expected_files": len(files),
        "expected_ifc": sum(x["path"].endswith(".ifc") for x in files),
        "files": [],
    }
    log(
        f'V2: IFC {manifest["expected_ifc"]}개 / 다운로드 {len(files)}개 파일 / {sum(x["size"] for x in files)/1e9:.2f} GB'
    )
    for i, entry in enumerate(files, 1):
        relative = entry["path"]
        target = local_target(root, relative)
        if not target.is_relative_to(root):
            raise ValueError(f"잘못된 저장소 경로: {relative}")
        expected_sha = entry.get("lfs", {}).get("oid")
        digest = None
        log(f'다운로드 {i}/{len(files)}: {relative} ({entry["size"]/1e6:.1f} MB)')
        if target.exists():
            digest = file_hash(target)
            if expected_sha and target.stat().st_size == entry["size"] and digest == expected_sha:
                log("  해시 확인된 기존 파일 재사용")
            else:
                digest = None
        if digest is None:
            target.parent.mkdir(parents=True, exist_ok=True)
            part = target.with_name(target.name + ".download")
            path = urllib.parse.quote(relative, safe="/")
            for attempt in range(3):
                try:
                    # Fresh signed CDN redirects avoid cached expired URLs.
                    url = f"https://huggingface.co/datasets/{REPOSITORY}/resolve/{commit}/{path}?download=true&request_time={time.time_ns()}"
                    request = urllib.request.Request(url, headers={"User-Agent": BUILD_VERSION})
                    sha = hashlib.sha256()
                    with (
                        urllib.request.urlopen(request, timeout=120) as response,
                        part.open("wb") as handle,
                    ):
                        while chunk := response.read(4 * 1024 * 1024):
                            handle.write(chunk)
                            sha.update(chunk)
                    digest = sha.hexdigest()
                    if part.stat().st_size != entry["size"] or (
                        expected_sha and digest != expected_sha
                    ):
                        raise RuntimeError(f"크기/SHA-256 불일치: {relative}")
                    if not expected_sha:
                        data = part.read_bytes()
                        blob = hashlib.sha1(
                            b"blob " + str(len(data)).encode() + b"\0" + data
                        ).hexdigest()
                        if blob != entry["oid"]:
                            raise RuntimeError(f"Git blob 해시 불일치: {relative}")
                    if target.exists() and file_hash(target) != digest:
                        raise FileExistsError(
                            f"기존 파일 내용이 다릅니다. 별도 폴더를 선택하세요: {target}"
                        )
                    os.replace(part, target)
                    break
                except FileExistsError:
                    raise
                except Exception:
                    if attempt == 2:
                        raise
                    log(f"  재시도 {attempt+1}/2: {relative}")
                    time.sleep(2)
        manifest["files"].append(
            {
                "path": relative,
                "local_path": target.relative_to(root).as_posix(),
                "bytes": entry["size"],
                "sha256": digest,
                "git_blob_sha": entry["oid"],
                "lfs_sha256": expected_sha,
            }
        )
        manifest["complete"] = len(manifest["files"]) == len(files)
        (root / "download_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    log(f'V2 다운로드 완료: {manifest["expected_ifc"]}개 IFC / 커밋 {commit}')
    return manifest


def download(root, *, documents=False, log=print, version="v2"):
    if version == "v1":
        return download_v1(root, documents=documents, log=log)
    if version != "v2":
        raise ValueError(f"지원하지 않는 버전: {version}")
    return download_v2(root, documents=documents, log=log)


if __name__ == "__main__":
    # Windows redirects can otherwise use cp1252 and fail on Korean progress.
    import sys

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    dl = sub.add_parser("download")
    dl.add_argument("folder", type=Path)
    dl.add_argument("--documents", action="store_true")
    dl.add_argument("--version", choices=("v1", "v2"), default="v2")
    cv = sub.add_parser("convert")
    cv.add_argument("source", type=Path)
    cv.add_argument("output", type=Path)
    cv.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.action == "download":
        download(args.folder, documents=args.documents, version=args.version)
    else:
        from ifc_sql_views.import_ifc import convert

        convert(args.source, args.output, overwrite=args.overwrite)

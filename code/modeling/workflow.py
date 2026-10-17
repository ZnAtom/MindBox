"""Local video review and resumable, serial Hyper3D generation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time

import av
import cv2
import httpx
import numpy as np
from PIL import Image, ImageDraw

API = "https://api.hyper3d.com/api/v2"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = Path(__file__).resolve().parent / "var"


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def log(data):
    print(json.dumps(data, ensure_ascii=True), flush=True)


def inspect_videos(folder, out, interval=2.0):
    out = Path(out)
    summary = []
    for source in sorted(Path(folder).glob("*")):
        if source.suffix.lower() not in {".mp4", ".mov", ".mkv"}:
            continue
        dest = out / "review" / source.stem
        dest.mkdir(parents=True, exist_ok=True)
        rows = []
        with av.open(str(source)) as container:
            stream = container.streams.video[0]
            duration = float(stream.duration * stream.time_base) if stream.duration else container.duration / av.time_base
            for index, target in enumerate(np.arange(0.2, duration, interval)):
                # Seeking avoids decoding every frame of high-resolution captures.
                container.seek(int(float(target) / stream.time_base), stream=stream, backward=True)
                frame = next((frame for frame in container.decode(stream) if frame.time is not None and frame.time >= target), None)
                if frame is None:
                    continue
                image = frame.to_image()
                if frame.rotation:
                    image = image.rotate(frame.rotation, expand=True)
                image.thumbnail((1600, 1600))
                gray = cv2.cvtColor(np.array(image.resize((320, 240))), cv2.COLOR_RGB2GRAY)
                name = f"{index:03d}-{frame.time:.2f}s.jpg"
                image.save(dest / name, quality=92)
                rows.append({"file": name, "seconds": frame.time, "sharpness": round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 2)})
            metadata = {"source": str(source.resolve()), "width": stream.width, "height": stream.height,
                        "duration_seconds": round(duration, 3), "fps": str(stream.average_rate), "frames": rows}
        sheet_width, cell_width, cell_height = 1200, 300, 248
        sheet = Image.new("RGB", (sheet_width, max(1, (len(rows) + 3) // 4) * cell_height), "#eceeef")
        draw = ImageDraw.Draw(sheet)
        for i, row in enumerate(rows):
            image = Image.open(dest / row["file"]).convert("RGB")
            image.thumbnail((cell_width - 12, cell_height - 32))
            x, y = (i % 4) * cell_width, (i // 4) * cell_height
            sheet.paste(image, (x + (cell_width - image.width) // 2, y))
            draw.text((x + 8, y + cell_height - 25), f"{row['file']}  Q:{row['sharpness']}", fill="#28292c")
        sheet.save(dest / "contact-sheet.jpg", quality=88)
        write_json(dest / "review.json", metadata)
        summary.append({key: value for key, value in metadata.items() if key != "frames"} | {"sample_count": len(rows), "sheet": str(dest / "contact-sheet.jpg")})
    write_json(out / "review" / "summary.json", summary)
    log(summary)


def prepare(manifest_path, out):
    manifest_path = Path(manifest_path).resolve()
    manifest = read_json(manifest_path)
    output = Path(out) / "inputs" / manifest["id"]
    output.mkdir(parents=True, exist_ok=True)
    images = manifest["images"]
    if not 1 <= len(images) <= 5:
        raise ValueError("Each generation needs one to five images")
    if len({row.get("state", "closed") for row in images}) != 1:
        raise ValueError("Reference images must share the same drawer state")
    prepared = []
    for index, reference in enumerate(images):
        source = (manifest_path.parent / reference["file"]).resolve()
        if source.suffix.lower() in {".mp4", ".mov", ".mkv"}:
            with av.open(str(source)) as container:
                stream = container.streams.video[0]
                seconds = float(reference["seconds"])
                container.seek(int(seconds / stream.time_base), stream=stream, backward=True)
                frame = next(frame for frame in container.decode(stream) if frame.time is not None and frame.time >= seconds)
                original = frame.to_image().rotate(frame.rotation, expand=True)
                original.thumbnail((1600, 1600))
        else:
            original = Image.open(source).convert("RGB")
        image = original.copy()
        crop = reference.get("crop")
        if crop:
            left, top, right, bottom = crop
            if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height):
                raise ValueError(f"Crop outside source image: {source.name}")
            image = image.crop(crop)
        image.thumbnail((1400, 1400))
        if reference.get("mask"):
            # Optional manually reviewed silhouette, in original image coordinates.
            mask = Image.new("L", original.size)
            ImageDraw.Draw(mask).polygon([tuple(point) for point in reference["mask"]], fill=255)
            for hole in reference.get("mask_holes", []):
                ImageDraw.Draw(mask).polygon([tuple(point) for point in hole], fill=0)
            backdrop = Image.new("RGB", original.size, "#f0f0f0")
            image = Image.composite(original, backdrop, mask)
            if crop:
                image = image.crop(crop)
            image.thumbnail((1400, 1400))
        canvas = Image.new("RGB", (1536, 1536), "#f0f0f0")
        canvas.paste(image, ((1536 - image.width) // 2, (1536 - image.height) // 2))
        name = f"{index + 1:02d}-{reference.get('label', '?').replace('?', 'unknown')}.png"
        canvas.save(output / name)
        prepared.append({"file": str((output / name).resolve()), "label": reference.get("label", "?"), "source": str(source), "state": reference.get("state", "closed")})
    manifest["prepared_images"] = prepared
    manifest["source_manifest"] = str(manifest_path)
    write_json(output / "prepared.json", manifest)
    log({"prepared": str(output / "prepared.json"), "images": len(prepared)})


class ApiError(RuntimeError):
    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class Client:
    def __init__(self):
        key = os.environ.get("RODIN_API_KEY", "").strip()
        if not key:
            raise ValueError("Set RODIN_API_KEY in the process environment")
        self.client = httpx.Client(timeout=httpx.Timeout(180, connect=25), follow_redirects=False,
                                   headers={"Authorization": f"Bearer {key}"})

    def call(self, method, endpoint, **kwargs):
        # Generation POSTs are intentionally never retried: a timeout can follow acceptance.
        try:
            response = self.client.request(method, f"{API}/{endpoint}", **kwargs)
        except httpx.HTTPError as error:
            raise ApiError(f"{endpoint}: network error ({type(error).__name__}); inspect checkpoint before resubmitting", uncertain=True) from None
        try:
            data = response.json()
        except ValueError:
            raise ApiError(f"{endpoint}: HTTP {response.status_code}, non-JSON response", uncertain=True) from None
        if response.is_error or data.get("error"):
            code = data.get("error") or f"HTTP_{response.status_code}"
            raise ApiError(f"{endpoint}: {code}")
        return data

    def balance(self):
        data = self.call("GET", "check_balance")
        log({"authenticated": True, "balance": data.get("balance")})
        return data


def generation_fields(manifest):
    if not 1 <= len(manifest["prepared_images"]) <= 5:
        raise ValueError("Each generation needs one to five prepared images")
    if not 500 <= int(manifest.get("target_faces", 10000)) <= 2000000:
        raise ValueError("Target face count outside provider bounds")
    fields = {"tier": manifest.get("tier", "Gen-2.5-Medium"), "geometry_file_format": "glb",
              "mesh_mode": "Raw", "quality_override": str(manifest.get("target_faces", 10000)),
              "material": "None", "seed": str(manifest.get("seed", 42)),
              "image_label": json.dumps([image["label"] for image in manifest["prepared_images"]])}
    if manifest.get("prompt"):
        fields["prompt"] = manifest["prompt"]
    if manifest.get("bbox_condition"):
        fields["bbox_condition"] = json.dumps(manifest["bbox_condition"])
    return fields


def submit(manifest_path, out, *, split_from=None, split_model=None):
    manifest = read_json(manifest_path)
    if split_from and split_model:
        raise ValueError("Use either a Rodin asset or an uploaded model for splitting")
    folder = Path(out) / "tasks" / (manifest["id"] + ("-split-upload" if split_model else "-split" if split_from else ""))
    folder.mkdir(parents=True, exist_ok=True)
    checkpoint = folder / "task.json"
    fields = generation_fields(manifest)
    uploads = [("images", Path(image["file"]), "image/png") for image in manifest["prepared_images"]]
    endpoint = "rodin"
    if split_from or split_model:
        endpoint = "bang"
        fields = {"geometry_file_format": "glb", "material": "None", "mesh_mode": "Raw",
                  "quality_override": str(manifest.get("target_faces", 10000)),
                  "instruction": manifest.get("split_instruction", "Separate the fixed cabinet frame and each complete drawer. Preserve assembled positions.")}
        if split_model:
            uploads = [("model", Path(split_model), "model/gltf-binary"),
                       ("image", Path(manifest["prepared_images"][0]["file"]), "image/png")]
        else:
            fields["asset_id"] = read_json(split_from)["uuid"]
            uploads = []
    fingerprint = hashlib.sha256(json.dumps(fields, sort_keys=True).encode())
    legacy_fingerprint = hashlib.sha256(json.dumps(fields, sort_keys=True).encode())
    for field, file, _ in uploads:
        fingerprint.update(field.encode())
        fingerprint.update(file.read_bytes())
        legacy_fingerprint.update(file.read_bytes())
    if checkpoint.exists():
        existing = read_json(checkpoint)
        same_request = existing.get("fingerprint") == fingerprint.hexdigest() or (
            existing.get("settings") == fields and existing.get("fingerprint") == legacy_fingerprint.hexdigest())
        if existing.get("uuid") and same_request:
            log({"reused_checkpoint": str(checkpoint), "uuid": existing["uuid"]})
            return checkpoint
        raise ValueError("A different or unresolved submission exists; inspect it or use a new manifest id")
    client = Client()
    metadata = {"state": "submitting", "fingerprint": fingerprint.hexdigest(), "manifest": str(Path(manifest_path).resolve()), "endpoint": endpoint, "created_at": time.time()}
    write_json(checkpoint, metadata)
    handles = []
    try:
        files = []
        for field, path, mime in uploads:
            handle = open(path, "rb")
            handles.append(handle)
            files.append((field, (path.name, handle, mime)))
        result = client.call("POST", endpoint, data=fields, **({"files": files} if files else {}))
        if not result.get("uuid") or not result.get("jobs", {}).get("subscription_key"):
            raise ApiError("Submission returned no usable task identifiers", uncertain=True)
        result.update(state="accepted", fingerprint=fingerprint.hexdigest(), manifest=str(Path(manifest_path).resolve()), settings=fields)
        write_json(checkpoint, result)
        log({"accepted": True, "uuid": result["uuid"], "consumed": result.get("consumed"), "checkpoint": str(checkpoint)})
    except ApiError as error:
        write_json(checkpoint, metadata | {"state": "unconfirmed" if error.uncertain else "rejected", "error": str(error)})
        raise
    finally:
        for handle in handles:
            handle.close()
    return checkpoint


def status(task_path, wait=False, deadline=1200):
    task_path = Path(task_path)
    task = read_json(task_path)
    client = Client()
    started = time.monotonic()
    while True:
        result = client.call("POST", "status", json={"subscription_key": task["jobs"]["subscription_key"]})
        jobs = result.get("jobs", [])
        states = [job.get("status") for job in jobs]
        write_json(task_path.parent / "status.json", {"checked_at": time.time(), "jobs": jobs})
        log({"uuid": task["uuid"], "states": states})
        if not states:
            raise ApiError("Status returned no jobs")
        if "Failed" in states:
            raise ApiError("Generation failed; no automatic paid retry")
        if all(state == "Done" for state in states):
            return True
        if not wait:
            return False
        if time.monotonic() - started >= deadline:
            raise ApiError("Polling deadline reached; resume using the saved checkpoint")
        time.sleep(15)


def download(task_path):
    task_path = Path(task_path)
    if not status(task_path):
        raise ApiError("All jobs must finish before downloading")
    task = read_json(task_path)
    listing = Client().call("POST", "download", json={"task_uuid": task["uuid"]})
    downloaded = []
    folder = task_path.parent / "downloads"
    folder.mkdir(exist_ok=True)
    # Asset URLs are signed; do not forward API credentials to the storage host.
    with httpx.Client(timeout=180, follow_redirects=True) as client:
        for index, entry in enumerate(listing.get("list", [])):
            name = Path(entry.get("name", f"asset-{index}")).name
            if not name or name in {".", ".."}:
                raise ApiError("Invalid download filename")
            response = client.get(entry["url"])
            response.raise_for_status()
            (folder / name).write_bytes(response.content)
            downloaded.append({"name": name, "bytes": len(response.content)})
    write_json(task_path.parent / "downloads.json", downloaded)
    log({"downloaded": downloaded, "folder": str(folder)})


def read_glb(path):
    data = Path(path).read_bytes()
    magic, version, size = struct.unpack_from("<III", data)
    if magic != 0x46546C67 or version != 2 or size != len(data):
        raise ValueError("Invalid GLB 2.0 header")
    offset, document, binary = 12, None, b""
    while offset < size:
        length, kind = struct.unpack_from("<II", data, offset)
        chunk = data[offset + 8:offset + 8 + length]
        if kind == 0x4E4F534A:
            document = json.loads(chunk)
        elif kind == 0x004E4942:
            binary = chunk
        offset += 8 + length
    if document is None:
        raise ValueError("GLB has no scene document")
    return document, binary


def style_glb(source, target):
    document, binary = read_glb(source)
    document["materials"] = [{"name": "Mind Box neutral", "pbrMetallicRoughness": {
        "baseColorFactor": [0.68, 0.71, 0.74, 1], "metallicFactor": 0.04, "roughnessFactor": 0.7}, "doubleSided": True}]
    for mesh in document.get("meshes", []):
        for primitive in mesh.get("primitives", []):
            primitive["material"] = 0
            primitive.get("attributes", {}).pop("COLOR_0", None)
    for key in ("textures", "images", "samplers"):
        document.pop(key, None)
    for key in ("extensionsUsed", "extensionsRequired"):
        if key in document:
            document[key] = [extension for extension in document[key] if not extension.startswith("KHR_materials_")]
    encoded = json.dumps(document, separators=(",", ":")).encode()
    encoded += b" " * (-len(encoded) % 4)
    binary += b"\0" * (-len(binary) % 4)
    chunks = struct.pack("<II", len(encoded), 0x4E4F534A) + encoded
    if binary:
        chunks += struct.pack("<II", len(binary), 0x004E4942) + binary
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    Path(target).write_bytes(struct.pack("<III", 0x46546C67, 2, 12 + len(chunks)) + chunks)
    faces = 0
    for mesh in document.get("meshes", []):
        for primitive in mesh.get("primitives", []):
            accessor = document["accessors"][primitive.get("indices", primitive["attributes"]["POSITION"])]
            count = accessor["count"]
            mode = primitive.get("mode", 4)
            faces += count // 3 if mode == 4 else max(0, count - 2) if mode in (5, 6) else 0
    stats = {"file": str(target), "bytes": Path(target).stat().st_size, "mesh_faces": faces,
             "meshes": len(document.get("meshes", [])), "nodes": len(document.get("nodes", [])),
             "textures": 0, "materials": 1}
    write_json(Path(target).with_suffix(".stats.json"), stats)
    log(stats)


def finish(task_path, out):
    status(task_path, wait=True)
    download(task_path)
    task_path = Path(task_path)
    results = []
    for source in sorted((task_path.parent / "downloads").glob("*.glb")):
        target = Path(out) / "results" / task_path.parent.name / source.name
        style_glb(source, target)
        results.append(str(target.resolve()))
    if not results:
        raise ApiError("No GLB files were returned; inspect the download manifest")
    write_json(Path(out) / "results" / task_path.parent.name / "result.json", {"task": str(task_path.resolve()), "files": results})
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("folder", type=Path)
    inspect.add_argument("--interval", type=float, default=2)
    prep = commands.add_parser("prepare")
    prep.add_argument("manifest", type=Path)
    commands.add_parser("balance")
    generation = commands.add_parser("submit")
    generation.add_argument("manifest", type=Path)
    generation.add_argument("--split-from", type=Path)
    generation.add_argument("--split-model", type=Path)
    polling = commands.add_parser("status")
    polling.add_argument("task", type=Path)
    polling.add_argument("--wait", action="store_true")
    polling.add_argument("--deadline", type=int, default=1200)
    fetch = commands.add_parser("download")
    fetch.add_argument("task", type=Path)
    resume = commands.add_parser("finish")
    resume.add_argument("task", type=Path)
    styling = commands.add_parser("style")
    styling.add_argument("source", type=Path)
    styling.add_argument("target", type=Path)
    batch = commands.add_parser("batch")
    batch.add_argument("manifests", nargs="+", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            inspect_videos(args.folder, args.out, args.interval)
        elif args.command == "prepare":
            prepare(args.manifest, args.out)
        elif args.command == "balance":
            Client().balance()
        elif args.command == "submit":
            submit(args.manifest, args.out, split_from=args.split_from, split_model=args.split_model)
        elif args.command == "status":
            status(args.task, args.wait, args.deadline)
        elif args.command == "download":
            download(args.task)
        elif args.command == "finish":
            finish(args.task, args.out)
        elif args.command == "style":
            style_glb(args.source, args.target)
        elif args.command == "batch":
            for manifest in args.manifests:
                data = read_json(manifest)
                if "prepared_images" not in data:
                    prepare(manifest, args.out)
                    manifest = args.out / "inputs" / data["id"] / "prepared.json"
                task = submit(manifest, args.out)
                finish(task, args.out)
    except (ApiError, ValueError, OSError, httpx.HTTPError) as error:
        log({"error": type(error).__name__, "message": str(error) if not isinstance(error, httpx.HTTPError) else "Asset download failed; retry from checkpoint"})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

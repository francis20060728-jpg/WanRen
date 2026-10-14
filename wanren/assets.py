from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .database import Database, new_id, utc_now
from .security import validate_upload

# 孤立代理字符（U+D800–U+DFFF）：损坏编码的 PDF 抽取文本里常见，
# 无法 UTF-8 编码，直接写库会触发 UnicodeEncodeError（前端表现为"创建失败"）
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def clean_unicode(value: str) -> str:
    if not value:
        return value
    return _SURROGATE_RE.sub("�", value)


def clean_json(value: Any) -> str:
    return clean_unicode(json.dumps(value, ensure_ascii=False))


@dataclass
class ParsedAsset:
    asset_type: str
    extracted_text: str
    structured_data: dict[str, Any]
    metadata_json: dict[str, Any]
    processing_status: str
    derived_assets: list[DerivedAsset] = field(default_factory=list)


@dataclass
class DerivedAsset:
    asset_type: str
    file_name: str
    mime_type: str
    payload: bytes
    metadata_json: dict[str, Any]


def detect_asset_type(suffix: str) -> str:
    mapping = {
        ".txt": "TEXT",
        ".md": "TEXT",
        ".csv": "CSV",
        ".json": "JSON",
        ".xlsx": "XLSX",
        ".pdf": "PDF",
        ".docx": "DOCX",
        ".png": "IMAGE",
        ".jpg": "IMAGE",
        ".jpeg": "IMAGE",
        ".webp": "IMAGE",
        ".mp4": "VIDEO",
    }
    return mapping.get(suffix, "OTHER")


def _parse_text(payload: bytes) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Text files must be UTF-8 encoded") from exc


def _parse_csv(payload: bytes) -> tuple[str, dict[str, Any]]:
    text = _parse_text(payload)
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for index, row in enumerate(reader):
        if index >= 500:
            break
        rows.append(dict(row))
    return text, {
        "columns": reader.fieldnames or [],
        "row_count": len(rows),
        "truncated": len(rows) == 500,
        "rows": rows,
    }


def _parse_json(payload: bytes) -> tuple[str, dict[str, Any]]:
    text = _parse_text(payload)
    value = json.loads(text)
    if not isinstance(value, (dict, list)):
        raise ValueError("JSON root must be an object or array")
    return text, {"value": value}


def _parse_xlsx(payload: bytes) -> tuple[str, dict[str, Any]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise ValueError("XLSX parser is not installed") from exc
    workbook = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    sheets = []
    for worksheet in workbook.worksheets:
        rows = []
        for row in worksheet.iter_rows(values_only=True):
            rows.append([None if cell is None else str(cell) for cell in row])
            if len(rows) >= 500:
                break
        sheets.append(
            {
                "name": worksheet.title,
                "row_count": len(rows),
                "truncated": len(rows) >= 500,
                "rows": rows,
            }
        )
    workbook.close()
    rendered = "\n\n".join(
        f"# {sheet['name']}\n" + "\n".join("\t".join(row) for row in sheet["rows"])
        for sheet in sheets
    )
    return rendered, {"sheets": sheets}


def _parse_pdf(payload: bytes) -> tuple[str, dict[str, Any]]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("PDF parser is not installed") from exc
    try:
        reader = PdfReader(io.BytesIO(payload))
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs are not supported")
        pages = [page.extract_text() or "" for page in reader.pages[:100]]
        return "\n\n".join(pages), {
            "page_count": len(reader.pages),
            "extracted_pages": len(pages),
            "truncated": len(reader.pages) > 100,
        }
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Invalid or corrupted PDF") from exc


def _extract_pdf_figures(payload: bytes) -> tuple[list[DerivedAsset], list[dict[str, Any]]]:
    """Extract large embedded raster figures while ignoring journal logos."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("PDF parser is not installed") from exc

    reader = PdfReader(io.BytesIO(payload))
    figures: list[DerivedAsset] = []
    summaries: list[dict[str, Any]] = []
    for page_number, page in enumerate(reader.pages[:100], start=1):
        try:
            images = list(page.images)
        except Exception:
            continue
        for image_index, image in enumerate(images, start=1):
            width, height = getattr(image.image, "size", (0, 0))
            if len(image.data) < 20_000 or width < 400 or height < 200:
                continue
            suffix = Path(image.name).suffix.lower()
            if suffix in {".jpeg", ".jpg"}:
                mime_type = "image/jpeg"
                file_name = f"figure-{len(figures) + 1}.jpg"
            elif suffix == ".png":
                mime_type = "image/png"
                file_name = f"figure-{len(figures) + 1}.png"
            else:
                continue
            summary = {
                "page": page_number,
                "image_index": image_index,
                "width": width,
                "height": height,
                "size_bytes": len(image.data),
            }
            figures.append(
                DerivedAsset(
                    "IMAGE",
                    file_name,
                    mime_type,
                    image.data,
                    {
                        "derivative_type": "PDF_FIGURE",
                        **summary,
                    },
                )
            )
            summaries.append(summary)
            if len(figures) >= 20:
                return figures, summaries
    return figures, summaries


def _merge_boxes(boxes: list[list[float]]) -> list[list[float]]:
    """按 8pt 间隙并查集聚合相交的绘图包围盒，直到稳定。"""
    changed = True
    while changed:
        changed = False
        merged: list[list[float]] = []
        for box in boxes:
            hit = False
            for other in merged:
                if box[0] < other[2] and other[0] < box[2] and box[1] < other[3] and other[1] < box[3]:
                    other[0] = min(other[0], box[0])
                    other[1] = min(other[1], box[1])
                    other[2] = max(other[2], box[2])
                    other[3] = max(other[3], box[3])
                    hit = True
                    changed = True
                    break
            if not hit:
                merged.append(list(box))
        boxes = merged
    return boxes


def _extract_pdf_vector_figures(payload: bytes) -> tuple[list[DerivedAsset], list[dict[str, Any]]]:
    """矢量图抽取：matplotlib/Origin 导出的曲线图在 PDF 里是路径而非内嵌位图，
    旧位图抽取器对它们束手无策（论文图表型 PDF 常见）。这里用 pdfplumber 读出
    线/矩形/曲线的几何，聚合成图块（排除大面积表格与纯文字页），再用
    pypdfium2 以 200dpi 渲染成 PNG——与位图路径产出同构的派生配图。"""
    try:
        import pdfplumber
        import pypdfium2 as pdfium
    except ImportError:
        return [], []

    figures: list[DerivedAsset] = []
    summaries: list[dict[str, Any]] = []
    scale = 200 / 72
    try:
        with pdfplumber.open(io.BytesIO(payload)) as plumber_doc:
            render_doc = pdfium.PdfDocument(payload)
            for page_number, page in enumerate(plumber_doc.pages[:100], start=1):
                elements = [
                    (e["x0"], e["top"], e["x1"], e["bottom"])
                    for e in (page.rects + page.lines + page.curves)
                ]
                if not elements:
                    continue
                chars = [(c["x0"], c["top"], c["x1"], c["bottom"]) for c in page.chars]
                gap = 8.0
                boxes = _merge_boxes([[x0 - gap, top - gap, x1 + gap, bottom + gap] for x0, top, x1, bottom in elements])
                for cluster_index, (x0, top, x1, bottom) in enumerate(boxes, start=1):
                    width_pt, height_pt = x1 - x0, bottom - top
                    if width_pt < 150 or height_pt < 100:
                        continue
                    if width_pt * height_pt > 0.85 * page.width * page.height:
                        continue  # 整页边框/满页装饰
                    char_count = sum(
                        1
                        for cx0, ctop, cx1, cbottom in chars
                        if cx0 < x1 and x0 < cx1 and ctop < bottom and top < cbottom
                    )
                    if char_count > 500:
                        continue  # 表格或正文文本块
                    try:
                        bitmap = render_doc[page_number - 1].render(scale=scale)
                    except Exception:
                        continue
                    image = bitmap.to_pil()
                    crop = image.crop(
                        (
                            max(0, int(x0 * scale)),
                            max(0, int(top * scale)),
                            min(image.width, int(x1 * scale)),
                            min(image.height, int(bottom * scale)),
                        )
                    )
                    output = io.BytesIO()
                    crop.save(output, format="PNG")
                    data = output.getvalue()
                    if crop.width < 400 or crop.height < 200 or len(data) < 20_000:
                        continue
                    # 图块正下方 2–30pt 内的文字通常是图注（「图 N xxx」/ Fig. N），抓给模型做选图依据
                    caption_guess = ""
                    try:
                        caption_area = (
                            max(0.0, x0 - 10),
                            bottom + 2,
                            min(float(page.width), x1 + 10),
                            min(float(page.height), bottom + 30),
                        )
                        if caption_area[3] > caption_area[1]:
                            caption_text = page.crop(caption_area).extract_text() or ""
                            caption_guess = " ".join(caption_text.split())[:120]
                    except Exception:
                        caption_guess = ""
                    summary = {
                        "page": page_number,
                        "image_index": cluster_index,
                        "width": crop.width,
                        "height": crop.height,
                        "size_bytes": len(data),
                        "source": "vector",
                        **({"caption_guess": caption_guess} if caption_guess else {}),
                    }
                    figures.append(
                        DerivedAsset(
                            "IMAGE",
                            f"figure-v{len(figures) + 1}.png",
                            "image/png",
                            data,
                            {
                                "derivative_type": "PDF_FIGURE",
                                **summary,
                            },
                        )
                    )
                    summaries.append(summary)
                    if len(figures) >= 20:
                        return figures, summaries
    except Exception:
        return figures, summaries
    return figures, summaries


def _parse_docx(payload: bytes) -> tuple[str, dict[str, Any]]:
    try:
        from docx import Document
    except ImportError as exc:
        raise ValueError("DOCX parser is not installed") from exc
    try:
        document = Document(io.BytesIO(payload))
        paragraphs = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
        tables = []
        for table in document.tables[:50]:
            tables.append([[cell.text for cell in row.cells] for row in table.rows])
        rendered = "\n\n".join(paragraphs)
        if tables:
            rendered += "\n\n" + "\n\n".join(
                "\n".join("\t".join(row) for row in table) for table in tables
            )
        return rendered, {"paragraph_count": len(paragraphs), "table_count": len(document.tables)}
    except Exception as exc:
        raise ValueError("Invalid or corrupted DOCX") from exc


def _parse_image(payload: bytes) -> tuple[dict[str, Any]]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise ValueError("Image parser is not installed") from exc
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image.verify()
        with Image.open(io.BytesIO(payload)) as image:
            return {
                "width": image.width,
                "height": image.height,
                "format": image.format or "UNKNOWN",
                "mode": image.mode,
            }
    except Exception as exc:
        raise ValueError("Invalid or corrupted image") from exc


def _parse_video(payload: bytes) -> dict[str, Any]:
    from .security import validate_mp4_signature

    validate_mp4_signature(payload)
    major_brand = payload[8:12].decode("ascii", errors="replace").strip()
    return {
        "container": "mp4",
        "major_brand": major_brand,
    }


def _parse_video_with_pipeline(payload: bytes) -> tuple[dict[str, Any], list[DerivedAsset]]:
    try:
        import imageio_ffmpeg
        from PIL import Image
    except ImportError as exc:
        raise ValueError("Video processing dependencies are not installed") from exc

    temporary = Path(tempfile.mkstemp(suffix=".mp4")[1])
    temporary.write_bytes(payload)
    frames = None
    try:
        frames = imageio_ffmpeg.read_frames(str(temporary))
        metadata = next(frames, None)
        if not metadata:
            raise ValueError("Video stream has no readable metadata")
        frame = next(frames, None)
        size = metadata.get("size") or metadata.get("source_size")
        if not size or len(size) != 2:
            raise ValueError("Video stream has no readable dimensions")
        width, height = int(size[0]), int(size[1])
        if width <= 0 or height <= 0:
            raise ValueError("Video stream has invalid dimensions")

        result = {
            "container": "mp4",
            "width": width,
            "height": height,
            "fps": float(metadata.get("fps") or 0),
            "duration_seconds": float(metadata.get("duration") or 0),
            "codec": metadata.get("codec"),
            "pixel_format": metadata.get("pix_fmt"),
            "rotation": int(metadata.get("rotate") or 0),
            "ffmpeg_version": metadata.get("ffmpeg_version"),
        }
        derived: list[DerivedAsset] = []
        if frame is not None and len(frame) >= width * height * 3:
            image = Image.frombytes("RGB", (width, height), frame)
            thumbnail_width = min(640, width)
            thumbnail_height = max(1, round(height * thumbnail_width / width))
            image = image.resize((thumbnail_width, thumbnail_height))
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=86, optimize=True)
            result["thumbnail"] = {
                "width": thumbnail_width,
                "height": thumbnail_height,
                "frame": "first",
            }
            derived.append(
                DerivedAsset(
                    "IMAGE",
                    "video-cover.jpg",
                    "image/jpeg",
                    output.getvalue(),
                    {
                        "derivative_type": "VIDEO_COVER_FRAME",
                        "source_frame": "first",
                        "source_metadata": {
                            "width": width,
                            "height": height,
                            "duration_seconds": result["duration_seconds"],
                        },
                    },
                )
            )
        else:
            result["thumbnail_unavailable_reason"] = "No decodable first frame"
        return result, derived
    except Exception as exc:
        raise ValueError(f"Invalid or undecodable MP4: {exc}") from exc
    finally:
        if frames is not None:
            frames.close()
        for _ in range(20):
            try:
                temporary.unlink(missing_ok=True)
                break
            except PermissionError:
                time.sleep(0.05)
        # On Windows, ffmpeg can release the handle asynchronously after generator.close().
        # A lingering temporary file is preferable to failing an otherwise valid upload.


def parse_payload(asset_type: str, payload: bytes) -> ParsedAsset:
    if asset_type in {"TEXT", "PDF", "DOCX", "CSV", "JSON"}:
        figures: list[DerivedAsset] = []
        if asset_type == "CSV":
            text, structured = _parse_csv(payload)
        elif asset_type == "JSON":
            text, structured = _parse_json(payload)
        elif asset_type == "PDF":
            text, structured = _parse_pdf(payload)
            figures, figure_summaries = _extract_pdf_figures(payload)
            if len(figures) < 20:
                vector_figures, vector_summaries = _extract_pdf_vector_figures(payload)
                room = 20 - len(figures)
                figures.extend(vector_figures[:room])
                figure_summaries.extend(vector_summaries[:room])
            structured["figure_count"] = len(figures)
            structured["figures"] = figure_summaries
        elif asset_type == "DOCX":
            text, structured = _parse_docx(payload)
        else:
            text, structured = _parse_text(payload), {}
        return ParsedAsset(asset_type, text, structured, {}, "READY", figures)
    if asset_type == "XLSX":
        text, structured = _parse_xlsx(payload)
        return ParsedAsset(asset_type, text, structured, {}, "READY")
    if asset_type == "IMAGE":
        metadata = _parse_image(payload)
        return ParsedAsset(asset_type, "", {}, metadata, "READY")
    if asset_type == "VIDEO":
        metadata, derived = _parse_video_with_pipeline(payload)
        return ParsedAsset(asset_type, "", {}, metadata, "READY", derived)
    raise ValueError("Unsupported asset type")


def save_asset(
    db: Database,
    storage_root: Path,
    session_id: str,
    file_name: str,
    mime_type: str,
    payload: bytes,
    max_bytes: int,
    article_id: str | None = None,
) -> dict[str, Any]:
    cleaned_name = clean_unicode(validate_upload(file_name, mime_type, len(payload), max_bytes))
    suffix = Path(cleaned_name).suffix.lower()
    asset_type = detect_asset_type(suffix)
    try:
        parsed = parse_payload(asset_type, payload)
    except ValueError as exc:
        parsed = ParsedAsset(asset_type, "", {}, {"error": str(exc)}, "FAILED")
    extracted_text = clean_unicode(parsed.extracted_text)

    asset_id = new_id("asset")
    relative_key = Path(session_id) / f"{asset_id}{suffix}"
    absolute_path = storage_root / "assets" / relative_key
    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    absolute_path.write_bytes(payload)
    now = utc_now()

    derived_rows: list[dict[str, Any]] = []
    figure_counter = 0
    for index, derived in enumerate(parsed.derived_assets):
        derived_id = new_id("asset")
        derived_suffix = Path(derived.file_name).suffix.lower() or ".jpg"
        derivative_type = derived.metadata_json.get("derivative_type")
        derived_label = "figure" if derivative_type == "PDF_FIGURE" else "cover"
        derived_name = f"{Path(cleaned_name).stem}-{derived_label}{index + 1}{derived_suffix}"
        # 论文配图编号写进 structured_data：前端编辑器与发布渲染器都读 figure_index 生成「论文图 N（第 M 页）」
        derived_structured: dict[str, Any] = {}
        if derivative_type == "PDF_FIGURE":
            figure_counter += 1
            derived_structured["figure_index"] = figure_counter
            if derived.metadata_json.get("page"):
                derived_structured["page"] = derived.metadata_json["page"]
        derived_key = Path(session_id) / f"{derived_id}{derived_suffix}"
        derived_path = storage_root / "assets" / derived_key
        derived_path.parent.mkdir(parents=True, exist_ok=True)
        derived_path.write_bytes(derived.payload)
        derived_metadata = {
            **derived.metadata_json,
            "source_asset_id": asset_id,
            "source_file_name": cleaned_name,
        }
        if derived_metadata.get("derivative_type") == "VIDEO_COVER_FRAME":
            parsed.metadata_json["thumbnail_asset_id"] = derived_id
            parsed.metadata_json["thumbnail_status"] = "READY"
        if derived_metadata.get("derivative_type") == "PDF_FIGURE":
            parsed.metadata_json.setdefault("figure_asset_ids", []).append(derived_id)
            parsed.metadata_json["figure_status"] = "READY"
        derived_rows.append(
            {
                "id": derived_id,
                "asset_type": derived.asset_type,
                "file_name": derived_name,
                "mime_type": derived.mime_type,
                "size_bytes": len(derived.payload),
                "storage_key": derived_key.as_posix(),
                "structured_data": derived_structured,
                "metadata_json": derived_metadata,
                "absolute_path": derived_path,
            }
        )

    with db.tx():
        db.execute(
            """
            INSERT INTO session_asset
            (id, session_id, asset_type, file_name, mime_type, size_bytes, storage_key,
             processing_status, extracted_text, structured_data, vision_result,
             metadata_json, created_at, updated_at, article_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                session_id,
                asset_type,
                cleaned_name,
                mime_type or "",
                len(payload),
                relative_key.as_posix(),
                parsed.processing_status,
                extracted_text,
                clean_json(parsed.structured_data),
                "{}",
                clean_json(parsed.metadata_json),
                now,
                now,
                article_id,
            ),
        )
        for derived in derived_rows:
            db.execute(
                """
                INSERT INTO session_asset
                (id,session_id,asset_type,file_name,mime_type,size_bytes,storage_key,
                 processing_status,extracted_text,structured_data,vision_result,
                 metadata_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,'READY','',?,'{}',?,?,?)
                """,
                (
                    derived["id"],
                    session_id,
                    derived["asset_type"],
                    derived["file_name"],
                    derived["mime_type"],
                    derived["size_bytes"],
                    derived["storage_key"],
                    clean_json(derived["structured_data"]),
                    json.dumps(derived["metadata_json"], ensure_ascii=False),
                    now,
                    now,
                ),
            )
    row = db.query_one("SELECT * FROM session_asset WHERE id = ?", (asset_id,))
    assert row is not None
    return row


def load_asset_payload(storage_root: Path, storage_key: str) -> bytes:
    relative = Path(storage_key)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Invalid storage key")
    path = storage_root / "assets" / relative
    if not path.is_file():
        raise FileNotFoundError("Asset payload not found")
    return path.read_bytes()


VIDEO_TRANSCODE_PRESETS = {
    "wechat_720p": {
        "label": "公众号 720p",
        "max_width": 1280,
        "video_bitrate_kbps": 1800,
        "audio_bitrate_kbps": 128,
        "crf": 23,
    },
    "preview_360p": {
        "label": "预览 360p",
        "max_width": 640,
        "video_bitrate_kbps": 700,
        "audio_bitrate_kbps": 96,
        "crf": 28,
    },
}


def transcode_video_file(source: Path, preset: str) -> tuple[Path, dict[str, Any]]:
    """Render a WeChat-friendly MP4 and return its temporary path and metadata."""
    import imageio_ffmpeg

    configuration = VIDEO_TRANSCODE_PRESETS.get(preset)
    if configuration is None:
        raise ValueError(f"Unsupported video transcode preset: {preset}")
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    output_fd, output_name = tempfile.mkstemp(suffix=".mp4")
    os.close(output_fd)
    output = Path(output_name)
    output.unlink(missing_ok=True)
    scale = f"scale=min(iw\\,{configuration['max_width']}):-2"
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vf",
        scale,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(configuration["crf"]),
        "-maxrate",
        f"{configuration['video_bitrate_kbps']}k",
        "-bufsize",
        f"{configuration['video_bitrate_kbps'] * 2}k",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        f"{configuration['audio_bitrate_kbps']}k",
        "-movflags",
        "+faststart",
        str(output),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise ValueError(f"ffmpeg transcode failed: {completed.stderr[-1000:]}")
        metadata, _ = _parse_video_with_pipeline(output.read_bytes())
        metadata["preset"] = preset
        metadata["preset_label"] = configuration["label"]
        return output, metadata
    except Exception:
        for _ in range(20):
            try:
                output.unlink(missing_ok=True)
                break
            except PermissionError:
                time.sleep(0.05)
        raise


def store_derived_video(
    db: Database,
    storage_root: Path,
    session_id: str,
    source_asset: dict[str, Any],
    output_path: Path,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    derived_id = new_id("asset")
    storage_key = Path(session_id) / f"{derived_id}.mp4"
    destination = storage_root / "assets" / storage_key
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(output_path, destination)
    now = utc_now()
    source_metadata = json.loads(source_asset["metadata_json"] or "{}")
    renditions = source_metadata.get("renditions", [])
    renditions.append(
        {
            "asset_id": derived_id,
            "preset": metadata.get("preset"),
            "width": metadata.get("width"),
            "height": metadata.get("height"),
            "duration_seconds": metadata.get("duration_seconds"),
        }
    )
    source_metadata["renditions"] = renditions
    derived_metadata = {
        **metadata,
        "derivative_type": "VIDEO_RENDITION",
        "source_asset_id": source_asset["id"],
        "source_file_name": source_asset["file_name"],
    }
    with db.tx():
        db.execute(
            """
            INSERT INTO session_asset
            (id,session_id,asset_type,file_name,mime_type,size_bytes,storage_key,
             processing_status,extracted_text,structured_data,vision_result,metadata_json,
             created_at,updated_at)
            VALUES (?,?,'VIDEO',?,?,?,?,'READY','','{}','{}',?,?,?)
            """,
            (
                derived_id,
                session_id,
                f"{Path(source_asset['file_name']).stem}-{metadata.get('preset')}.mp4",
                "video/mp4",
                destination.stat().st_size,
                storage_key.as_posix(),
                clean_json(derived_metadata),
                now,
                now,
            ),
        )
        db.execute(
            "UPDATE session_asset SET metadata_json=?,updated_at=? WHERE id=?",
            (json.dumps(source_metadata, ensure_ascii=False), now, source_asset["id"]),
        )
    row = db.query_one("SELECT * FROM session_asset WHERE id = ?", (derived_id,))
    assert row is not None
    return row

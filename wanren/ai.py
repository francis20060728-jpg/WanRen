from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .context import SessionContext
from .editorial import build_editorial_metadata


@dataclass
class GeneratedSection:
    id: str
    heading: str
    paragraphs: list[str]
    asset_refs: list[str]


@dataclass
class GeneratedArticle:
    title: str
    summary: str
    sections: list[GeneratedSection]
    editorial: dict[str, Any] = field(default_factory=dict)

    def to_content_json(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "sections": [section.__dict__ for section in self.sections],
            "editorial": self.editorial,
        }


@dataclass
class GenerationResult:
    article: GeneratedArticle
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    attempts: int = 1
    fallback_from: str | None = None
    error: dict[str, Any] | None = None


class GenerationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        latency_ms: int,
        attempts: int,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.input_tokens = input_tokens
        self.latency_ms = latency_ms
        self.attempts = attempts


class TextModelAdapter(Protocol):
    name: str
    provider: str

    def generate(self, context: SessionContext, instruction: str) -> GenerationResult:
        ...


def _requirements(session: dict[str, Any]) -> list[str]:
    try:
        value = json.loads(session.get("requirements") or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _terms(*values: Any) -> str:
    return " ".join(str(value) for value in values).lower()


def _has(terms: str, *words: str) -> bool:
    return any(word in terms for word in words)


def _metric(metrics: list[Any], *words: str) -> dict[str, Any]:
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        haystack = f"{metric.get('value', '')} {metric.get('label', '')} {metric.get('statement', '')}".lower()
        if all(word in haystack for word in words):
            return metric
    return {}


def _paper_title(metadata: dict[str, Any], terms: str) -> str:
    if _has(terms, "multi-camera", "cross-view", "多摄像头", "跨视角") and _has(terms, "identity", "tracking", "身份", "跟踪"):
        return "多摄像头目标跟踪的新解法"
    title = str((metadata.get("source") or {}).get("title") or "")
    main = title.split(":", 1)[0].strip()
    if main and len(main) <= 30:
        return main
    return "一篇值得拆解的研究"


def _paper_problem_paragraphs(digest: dict[str, Any], terms: str, depth: str) -> list[str]:
    paragraphs = [
        "这个领域长期被同一个矛盾困住：持续观察需要人力和设备，而被寄予厚望的自动化方案又容易在真实环境里失去稳定性。"
    ]
    if _has(terms, "occlusion", "viewpoint", "camera", "遮挡", "视角"):
        paragraphs.append(
            "摄像头本来看起来是非侵入式的答案，可一旦目标互相遮挡、视角频繁变化，单镜头下的身份线索就会不断断裂。"
        )
    if _has(terms, "re-identification", "reid", "appearance"):
        paragraphs.append(
            "常见思路是用外观重识别特征把丢失的身份找回来。但在目标外观高度相似的场景里，这条路线不仅脆弱，还会把计算压力推高，边缘设备很难长期实时运行。"
        )
    if depth == "BRIEF" and len(paragraphs) > 2:
        paragraphs = paragraphs[:2]
    return paragraphs


def _paper_method_paragraphs(digest: dict[str, Any], terms: str, depth: str) -> list[str]:
    lead = (
        "论文的核心取舍很清楚：不继续堆叠更重的外观特征，而是把身份一致性建立在使用成本更低的几何与时序证据上。"
        if _has(terms, "reid", "appearance", "spatio-temporal", "geometry")
        else "论文的方法不是单点替换某个模型，而是把检测、跟踪和跨视角融合拆成职责明确的三层。"
    )
    details: list[str] = []
    if _has(terms, "yolo", "detector"):
        details.append("检测模块负责先找到目标并给出位置与类别")
    if _has(terms, "tracker", "byteTrack".lower()):
        details.append("单视角跟踪负责在遮挡后恢复同一条轨迹")
    if _has(terms, "homography", "ground-plane", "global id pool"):
        details.append("跨视角模块把不同画面投影到统一地面坐标，再由全局身份池协调")
    if details:
        lead += "具体来说，" + "，".join(details) + "。"
    else:
        lead += "从证据看，它先建立可靠的基础表示，再用系统级约束减少身份漂移。"
    paragraphs = [lead]
    if _has(terms, "global id pool", "homography"):
        paragraphs.append(
            "这套设计的关键在于跨镜头时不问“两张图看起来像不像”，而问“这两条轨迹在同一个地面坐标系里能不能解释同一个目标的运动”。后者牺牲了一点表面灵活性，换来复杂环境下的稳定性。"
        )
    if depth == "DEEP":
        paragraphs.append(
            "也因此，论文的价值不只在某个模块的精度，而在于系统边界被重新划分：检测解决位置，单视角跟踪解决局部连续性，几何融合解决跨镜头一致性。对于部署人员来说，这种划分比一个笼统的端到端模型更容易排查。"
        )
    return paragraphs


def _paper_result_paragraphs(digest: dict[str, Any], terms: str, depth: str) -> list[str]:
    paragraphs: list[str] = []
    map_metric = _metric(digest.get("metrics", []), "map")
    fps_metric = _metric(digest.get("metrics", []), "fps")
    switch_metric = _metric(digest.get("metrics", []), "identity", "switch")
    consistency_metric = _metric(digest.get("metrics", []), "consistency")
    if map_metric:
        paragraphs.append(
            f"检测端最重要的数字是 {map_metric.get('value')}。mAP50 表示在 IoU 0.5 阈值下的平均检测精度，它关心的不是单张图里有没有框住目标，而是整个测试集上定位和分类的稳定程度。"
        )
    if fps_metric:
        paragraphs.append(
            f"完整管线约 {fps_metric.get('value')}，这个数字比峰值算力更有工程意义：它说明论文没有把可用性建立在实验室高端显卡上，而是考虑了消费级设备的持续运行。"
        )
    if switch_metric:
        paragraphs.append(
            f"跨摄像头融合带来的变化体现在身份切换{switch_metric.get('value')}。简单说，系统越少把一条轨迹误接成另一条轨迹，长时程行为统计就越可信。"
        )
    if consistency_metric:
        paragraphs.append(
            f"论文还报告跨视角身份一致性 {consistency_metric.get('value')}，这说明同一实体在不同镜头中的身份能够被稳定映射，而不是只在少数片段上有效。"
        )
    if not paragraphs:
        result_statements = digest.get("result_statements") or []
        if result_statements:
            paragraphs.append("实验部分的结论主要集中在稳定性和可部署性上，而不是只强调单点指标。")
        else:
            paragraphs.append("本次素材没有提供足够完整的实验段落，发布前应补充可核查的结果数据。")
    if depth != "BRIEF":
        paragraphs.append(
            "这些指标要放在一起读：检测精度说明输入端可靠，速度说明边缘部署可行，身份切换和一致性说明长期行为统计有基础。任何一项单独看都可能漂亮，合在一起才构成实际落地条件。"
        )
    return paragraphs


def _limitation_paragraphs(digest: dict[str, Any], terms: str) -> list[str]:
    paragraphs = [
        "论文结论依赖其验证场景，迁移到其他规模或环境前仍需要重新校准和采样验证。"
    ]
    if _has(terms, "homography", "calibration"):
        paragraphs.append("跨视角融合依赖相机标定和地面坐标假设，摄像头位置变动后需要维护标定关系，这是部署成本的一部分。")
    paragraphs.append("更公平地说，这篇论文提供的是一条资源受限条件下的工程路线，而不是宣告身份追踪问题已经彻底解决。")
    return paragraphs


def _paper_information_paragraph(metadata: dict[str, Any]) -> str:
    source = metadata.get("source") or {}
    parts = ["论文题为", f"《{source.get('title') or '未识别题目'}》"]
    if source.get("journal"):
        parts.append(f"，发表于 {source['journal']}")
    if source.get("doi"):
        parts.append(f"，DOI：{source['doi']}")
    if source.get("license"):
        parts.append(f"，许可：{source['license']}")
    parts.append("。以上信息来自本次上传的原文素材，适合继续查证原始实验设置和消融分析。")
    return "".join(parts)


def _image_refs(context: SessionContext, limit: int) -> list[str]:
    images = [
        asset["id"]
        for asset in context.assets
        if asset["asset_type"] == "IMAGE" and asset["processing_status"] == "READY"
    ]
    return images[:limit]


class LocalTextAdapter:
    provider = "builtin"
    name = "local-editorial-v2"

    def generate(self, context: SessionContext, instruction: str) -> GenerationResult:
        started = time.perf_counter()
        metadata = build_editorial_metadata(context, instruction)
        digest = context.evidence_digest or {}
        requirements = _requirements(context.session)
        requirement_text = "；".join(requirements) if requirements else "内容需可验证、避免夸大表述"
        focus = instruction.strip() or context.session["goal"] or "围绕任务目标完成一篇公众号文章"
        audience = context.session["audience"] or "公众号读者"
        tone = context.session["tone"] or "专业、清晰、克制"
        terms = _terms(
            metadata,
            digest,
            instruction,
            context.session.get("title"),
            context.session.get("goal"),
            context.session.get("tone"),
            *requirements,
        )
        style = metadata.get("style", "GENERAL_EXPLAINER")
        depth = metadata.get("depth", "STANDARD")
        source = metadata.get("source") or {}
        image_iter = iter(
            [
                asset["id"]
                for asset in context.assets
                if asset["asset_type"] == "IMAGE" and asset["processing_status"] == "READY"
            ]
        )

        def take_images(limit: int) -> list[str]:
            return [asset_id for asset_id in [next(image_iter, None) for _ in range(limit)] if asset_id]

        if style == "ORG_ANNOUNCEMENT":
            title = "我院科研成果被国际期刊录用"
            if _has(terms, "top", "一区", "q1"):
                title = "我院科研成果被国际TOP期刊录用"
            summary = (
                f"《{source.get('title') or context.session['title']}》进入同行评审流程后，"
                "这项工作也展示了问题定义、系统工程和真实场景验证如何互相支撑。"
            )[:120]
            sections = [
                GeneratedSection("sec_01", "成果速递", [
                    f"近日，{context.session['title']}相关工作形成正式论文《{source.get('title') or '待补充题目'}》。",
                    _paper_information_paragraph(metadata),
                ], []),
                GeneratedSection("sec_02", "研究要解决的关键问题", _paper_problem_paragraphs(digest, terms, depth), []),
                GeneratedSection("sec_03", "方法与实验亮点", _paper_method_paragraphs(digest, terms, depth) + _paper_result_paragraphs(digest, terms, "BRIEF"), take_images(2)),
                GeneratedSection("sec_04", "这项成果的意义", [
                    "对学院而言，这项成果的价值不只在于录用结果，也在于学生完整经历了从文献调研、算法设计到真实场景验证的科研闭环。",
                    "对相关方向的学习者来说，它提供了一个可讨论的案例：当算力和环境约束并存时，系统设计和证据组织会变得和模型选型同样重要。",
                ], []),
            ]
        elif style == "PAPER_INTERPRETATION":
            title = _paper_title(metadata, terms)
            summary = (
                f"这篇解读面向{audience}，用{tone}拆解《{source.get('title') or context.session['title']}》："
                "先看它解决的问题，再看方法取舍、实验边界和可迁移的经验。"
            )[:120]
            sections = [
                GeneratedSection("sec_01", "为什么值得读", [
                    f"{focus}。与常见的模型清单不同，这篇论文值得读的地方在于它把真实环境里的长期身份漂移当作系统工程来处理。",
                    f"目标读者是{audience}。下文尽量把术语放回场景中解释，并保留论文原始信息，方便继续核查。",
                ], take_images(1 if depth == "DEEP" else 0)),
                GeneratedSection("sec_02", "原有问题与场景约束", _paper_problem_paragraphs(digest, terms, depth), take_images(1)),
                GeneratedSection("sec_03", "方法核心", _paper_method_paragraphs(digest, terms, depth), take_images(1)),
                GeneratedSection("sec_04", "实验结果与部署边界", _paper_result_paragraphs(digest, terms, depth), take_images(1)),
            ]
            if depth != "BRIEF":
                sections.append(
                    GeneratedSection("sec_05", "局限与适用范围", _limitation_paragraphs(digest, terms), [])
                )
            sections.append(
                GeneratedSection("sec_06" if depth != "BRIEF" else "sec_05", "论文信息", [_paper_information_paragraph(metadata)], [])
            )
            if depth == "BRIEF" and style == "PAPER_INTERPRETATION":
                info_section = sections.pop()
                sections[-1].paragraphs.extend(info_section.paragraphs)

        else:
            title = context.session["title"][:32] or "一篇来自任务素材的整理"
            summary = (
                f"本文面向{audience}整理{context.session['title']}，优先保留可核查信息，"
                "再给出可执行的下一步和需要人工确认的边界。"
            )[:120]
            statements = [
                *(digest.get("problem_statements") or [])[:3],
                *(digest.get("method_statements") or [])[:2],
                *(digest.get("result_statements") or [])[:2],
            ]
            video_paragraph = ""
            videos = [asset for asset in context.assets if asset["asset_type"] == "VIDEO"]
            if videos:
                descriptions = []
                for video in videos:
                    metadata = json.loads(video.get("metadata_json") or "{}")
                    descriptions.append(
                        f"{video['file_name']} 为 {metadata.get('width')}x{metadata.get('height')} · "
                        f"duration={float(metadata.get('duration_seconds') or 0):.1f}s · "
                        f"fps={metadata.get('fps')} · codec={metadata.get('codec')}"
                    )
                video_paragraph = "任务素材中还包含视频证据：" + "；".join(descriptions) + "。"
            evidence_paragraphs = ([video_paragraph] if video_paragraph else []) + statements[:5] if statements or video_paragraph else [
                "本次任务尚未形成足够的可核查素材，文章不能为了完整感而补充未经来源确认的细节。",
                "建议先上传原始材料，或补充目标、受众、使用场景和必须保留的数据，再进入正式生成。",
            ]
            sections = [
                GeneratedSection("sec_01", "为什么现在值得关注", [
                    f"{focus}。这里不把素材堆成清单，而是先判断它对读者的决策有什么影响。",
                    f"表达方式保持{tone}，目标是让读者看完知道下一步怎么做。",
                ], take_images(1)),
                GeneratedSection("sec_02", "素材能确认什么", evidence_paragraphs, take_images(2)),
                GeneratedSection("sec_03", "怎么使用这些信息", [
                    "先确认核心事实和数据口径，再决定对外表达的强度；能自动整理的部分进入正文，不能确认的部分留在审核清单里。",
                    "如果文章用于发布，建议为关键数字保留来源截图或原始文档位置，方便复核和后续更新。",
                ], []),
                GeneratedSection("sec_04", "边界与下一步", [
                    f"{requirement_text}。完成这一步后，再进入人工终审和公众号草稿流程。",
                    "当前版本会避免把推测写成事实；若需要加入行业判断，应在人工审核时明确标注为观点。",
                ], []),
            ]

        template_sections = (
            context.template.get("structure", [])
            if isinstance(context.template, dict)
            else []
        )
        template_sections = [
            item
            for item in template_sections
            if isinstance(item, dict) and str(item.get("heading", "")).strip()
        ][:8]
        if template_sections:
            all_image_refs = list(
                dict.fromkeys(
                    asset_id
                    for section in sections
                    for asset_id in section.asset_refs
                )
            )
            sections = [
                GeneratedSection(
                    str(template_section.get("id") or f"sec_{index:02d}")[:60],
                    str(template_section["heading"]).strip()[:120],
                    sections[min(index, len(sections) - 1)].paragraphs
                    or [str(template_section.get("guidance") or "请补充本节内容。")],
                    all_image_refs[index - 1:index] if template_section.get("image_policy") in {"required", "optional"} else [],
                )
                for index, template_section in enumerate(template_sections, start=1)
            ]

        if depth == "BRIEF":
            sections = sections[:5]
        article = GeneratedArticle(title[:32], summary[:120], sections, metadata)
        input_tokens = max(
            1,
            len(json.dumps(context.to_model_payload(), ensure_ascii=False) + instruction) // 4,
        )
        output_tokens = max(1, len(json.dumps(article.to_content_json(), ensure_ascii=False)) // 4)
        return GenerationResult(
            article=article,
            provider=self.provider,
            model=self.name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    def revise(
        self,
        context: SessionContext,
        instruction: str,
        current: dict[str, Any],
    ) -> GenerationResult:
        """Apply deterministic local edits before falling back to regeneration."""
        started = time.perf_counter()
        content = json.loads(json.dumps(current, ensure_ascii=False))
        instruction = instruction.strip()
        lower = instruction.lower()

        title_match = re.search(
            r"标题(?:改为|改成|换成|设置为)?[:：]?\s*(?:[\"“']([^\"”'{\n]{2,60})[\"”']?|([^，。；！？\n]{2,60}))",
            instruction,
        )
        if title_match:
            content["title"] = (title_match.group(1) or title_match.group(2)).strip()[:32]

        summary_match = re.search(
            r"摘要(?:改为|改成|换成|设置为)?[:：]?\s*(?:[\"“']([^\"”'{\n]{10,180})[\"”']?|([^，。；！？\n]{10,180}))",
            instruction,
        )
        if summary_match:
            content["summary"] = (summary_match.group(1) or summary_match.group(2)).strip()[:120]

        regenerate = any(word in lower for word in ("重新生成", "整篇重写", "换个角度", "重写一版"))
        make_shorter = any(word in instruction for word in ("更短", "精简", "压缩", "短读", "收敛"))
        if regenerate:
            return self.generate(context, instruction)

        if make_shorter:
            for section in content.get("sections", []):
                heading = str(section.get("heading", ""))
                if heading in {"论文信息", "来源信息"}:
                    continue
                paragraphs = section.get("paragraphs", [])[:2]
                compressed: list[str] = []
                for paragraph in paragraphs:
                    text = str(paragraph)
                    if len(text) <= 240:
                        compressed.append(text)
                        continue
                    sentences = re.split(r"(?<=[。！？.!?])", text)
                    output = ""
                    for sentence in sentences:
                        if len(output) + len(sentence) > 240:
                            break
                        output += sentence
                    compressed.append(output or text[:240])
                section["paragraphs"] = compressed or ["这一节需要人工补充内容。"]
            content["editorial"] = content.get("editorial") or build_editorial_metadata(
                context, instruction
            )
            content["editorial"]["depth"] = "BRIEF"
        elif any(word in instruction for word in ("更详细", "扩充", "深读", "加强")):
            generated = self.generate(context, instruction)
            generated.article.editorial = content.get("editorial") or generated.article.editorial
            return generated

        article = GeneratedArticle(
            title=str(content.get("title", ""))[:32],
            summary=str(content.get("summary", ""))[:120],
            sections=[
                GeneratedSection(
                    str(section.get("id", f"sec_{index:02d}"))[:60],
                    str(section.get("heading", ""))[:120],
                    [str(paragraph) for paragraph in section.get("paragraphs", [])],
                    [str(asset_id) for asset_id in section.get("asset_refs", [])],
                )
                for index, section in enumerate(content.get("sections", []), start=1)
            ],
            editorial=content.get("editorial") or build_editorial_metadata(context, instruction),
        )
        return GenerationResult(
            article=article,
            provider=self.provider,
            model=f"{self.name}-revision",
            input_tokens=max(1, (len(json.dumps(current, ensure_ascii=False)) + len(instruction)) // 4),
            output_tokens=max(1, len(json.dumps(article.to_content_json(), ensure_ascii=False)) // 4),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


    def propose(
        self,
        context: SessionContext,
        instruction: str,
        previous: dict[str, Any] | None = None,
        feedback: str | None = None,
    ) -> dict[str, Any]:
        """本地适配器的离线架构提案：按素材信号给出保守大纲。"""
        goal = str(context.session.get("goal", "") or instruction or "")
        terms = _terms(goal, str(context.session.get("title", "")))
        paper_like = _has(terms, "论文", "paper", "深读", "解读", "方法")
        if previous and feedback:
            sections = [
                {
                    "id": str(section.get("id", f"sec_{index:02d}"))[:60],
                    "heading": str(section.get("heading", ""))[:120],
                    "plan": str(section.get("plan", "")),
                }
                for index, section in enumerate(previous.get("sections", []), start=1)
            ] or None
        elif paper_like:
            sections = [
                {"id": "problem", "heading": "论文要解决什么问题", "plan": "交代研究动机与现有方法的局限。"},
                {"id": "method", "heading": "方法是怎么设计的", "plan": "拆解核心模块与流程，解释关键取舍。"},
                {"id": "results", "heading": "效果有多好", "plan": "引用指标与实验条件，解释工程意义。"},
                {"id": "limits", "heading": "边界与局限", "plan": "说明适用范围与未覆盖场景。"},
            ]
        else:
            sections = [
                {"id": "opening", "heading": "开篇", "plan": "从场景或问题切入，点出主线。"},
                {"id": "body", "heading": "核心内容", "plan": "围绕素材要点展开，逐层递进。"},
                {"id": "closing", "heading": "收尾", "plan": "总结观点，给出行动或思考。"},
            ]
        if paper_like:
            style_id, reason = "academic-frame", "论文/方法解读，工具书感的线框样式最贴合"
            alternates = [
                {"style_id": "blue-magazine", "reason": "想要更杂志化的编辑感"},
                {"style_id": "mono-minimal", "reason": "想要更安静克制的黑白"},
            ]
        else:
            style_id, reason = "mono-minimal", "通用内容的安静默认样式"
            alternates = [
                {"style_id": "fresh-healing", "reason": "内容偏轻知识，可以更轻盈"},
                {"style_id": "biz-navy", "reason": "内容偏分析，可以更商务"},
            ]
        return {
            "title": (goal[:24] or "素材文章"),
            "summary": "根据上传素材拟定的写作架构（离线提案）。",
            "sections": sections
            or [
                {"id": "opening", "heading": "开篇", "plan": "从场景或问题切入。"},
                {"id": "body", "heading": "核心内容", "plan": "围绕素材展开。"},
                {"id": "closing", "heading": "收尾", "plan": "总结与行动。"},
            ],
            "typesetting": {"style_id": style_id, "reason": reason, "alternates": alternates},
            "notes": "本地适配器离线提案；接入远端模型后由大模型现场设计。",
        }


class OpenAICompatibleAdapter:
    provider = "openai_compatible"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        api_base: str = "https://api.openai.com/v1",
        timeout_seconds: float = 120,
        max_retries: int = 2,
        fallback_local: bool = False,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.name = model
        self.model = model
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.max_retries = max_retries
        self.fallback_local = fallback_local
        self.http = http_client or httpx.Client(timeout=timeout_seconds)

    @staticmethod
    def _system_prompt() -> str:
        return (
            "你是微信公众号内容编辑。只返回一个合法 JSON 对象，不要 Markdown。"
            "JSON schema: {\"title\":string, \"summary\":string, \"sections\":["
            "{\"id\":string, \"heading\":string, \"paragraphs\":string[], \"asset_refs\":string[]}]}. "
            "title 不超过32个汉字；summary 不超过120个汉字；sections 3-6个；"
            "asset_refs 只能使用输入素材ID；不得编造无法溯源的数据。"
            "写作要求：像真人编辑写给读者，而不是执行任务清单；不用 bullet 罗列事实；"
            "每段自然衔接，段落长度随内容伸缩；关键指标必须解释含义、条件和工程意义；"
            "论文解读需覆盖问题、方法取舍、结果、局限和来源信息；机构成果稿需有成果速递、"
            "编号章节、科研训练视角和审核 footer；短读请求则自然收束，不硬凑结构。"
            "禁止输出提示词、schema、内部素材ID、文件名或原文长句；禁止“测试”“终验”等内部痕迹。"
        )

    @staticmethod
    def _extract_json(content: str) -> dict[str, Any]:
        cleaned = content.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
        if fenced:
            cleaned = fenced.group(1)
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("model response does not contain a JSON object")
        value = json.loads(cleaned[start : end + 1])
        if not isinstance(value, dict):
            raise ValueError("model JSON root must be an object")
        return value

    @staticmethod
    def _validate_article(payload: dict[str, Any], context: SessionContext) -> GeneratedArticle:
        title = str(payload.get("title", "")).strip()
        summary = str(payload.get("summary", "")).strip()
        if not title:
            raise ValueError("model article has no title")
        raw_sections = payload.get("sections")
        if not isinstance(raw_sections, list) or not raw_sections:
            raise ValueError("model article has no sections")
        valid_asset_ids = {
            asset["id"] for asset in context.assets if asset["processing_status"] == "READY"
        }
        sections: list[GeneratedSection] = []
        for index, raw_section in enumerate(raw_sections[:6], start=1):
            if not isinstance(raw_section, dict):
                raise ValueError(f"section {index} is not an object")
            heading = str(raw_section.get("heading", "")).strip()
            if not heading:
                raise ValueError(f"section {index} has no heading")
            raw_paragraphs = raw_section.get("paragraphs", [])
            if not isinstance(raw_paragraphs, list):
                raise ValueError(f"section {index} paragraphs must be an array")
            paragraphs = [str(item).strip() for item in raw_paragraphs if str(item).strip()]
            if not paragraphs:
                raise ValueError(f"section {index} has no paragraphs")
            raw_refs = raw_section.get("asset_refs", [])
            if not isinstance(raw_refs, list):
                raise ValueError(f"section {index} asset_refs must be an array")
            asset_refs = [str(item) for item in raw_refs if str(item) in valid_asset_ids]
            section_id = str(raw_section.get("id", "")).strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{3,60}", section_id):
                section_id = f"sec_{index:02d}"
            sections.append(
                GeneratedSection(
                    id=section_id,
                    heading=heading,
                    paragraphs=paragraphs,
                    asset_refs=asset_refs,
                )
            )
        return GeneratedArticle(title=title[:32], summary=summary[:120], sections=sections, editorial={})

    @staticmethod
    def _estimate_tokens(*values: Any) -> int:
        return max(1, len(json.dumps(values, ensure_ascii=False, default=str)) // 4)

    def _post_chat(self, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        """流式聚合模型回复：读超时只约束相邻 chunk 的间隔（httpx 流式语义），
        长思维链/长文生成不再因总耗时撞 120s 上限而断线；兼容非流式返回完整 message 的网关。"""
        stream_body = {**body, "stream": True, "stream_options": {"include_usage": True}}
        pieces: list[str] = []
        usage: dict[str, Any] = {}
        with self.http.stream(
            "POST", f"{self.api_base}/chat/completions", headers=headers, json=stream_body
        ) as response:
            if response.status_code >= 400:
                response.read()
                raise httpx.HTTPStatusError(
                    f"model status {response.status_code}: {response.text[:500]}",
                    request=response.request,
                    response=response,
                )
            if "text/event-stream" not in response.headers.get("content-type", ""):
                # 网关忽略 stream 参数回退为整块 JSON：按非流式解析
                response.read()
                return response.json()
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[len("data: "):].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
                for choice in event.get("choices", []):
                    if not isinstance(choice, dict):
                        continue
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:
                        pieces.append(str(piece))
                    elif not pieces and choice.get("message"):
                        message = choice["message"].get("content")
                        if message:
                            pieces.append(str(message))
        content = "".join(pieces)
        if not content:
            raise ValueError("model stream returned no content")
        return {"choices": [{"message": {"content": content}}], "usage": usage}

    def generate(self, context: SessionContext, instruction: str) -> GenerationResult:
        payload = context.to_model_payload()
        user_content = json.dumps(
            {"task": instruction or payload.get("goal"), "context": payload},
            ensure_ascii=False,
        )
        template = payload.get("template") if isinstance(payload.get("template"), dict) else {}
        outline = template.get("structure") if isinstance(template.get("structure"), list) else []
        if outline:
            user_content += (
                "\n\n已确认大纲（必须严格遵守：逐节使用完全相同的 id 与 heading，"
                "不得增删章节、不得改写标题；guidance 是该节的内容要求）："
                + json.dumps(outline, ensure_ascii=False)
            )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.4,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        started = time.perf_counter()
        attempts = 0
        input_tokens = self._estimate_tokens(body["messages"])
        last_error: Exception | None = None

        while attempts <= self.max_retries:
            attempts += 1
            try:
                completion = self._post_chat(body, headers)
                content = completion["choices"][0]["message"]["content"]
                article = self._validate_article(self._extract_json(content), context)
                article.editorial = build_editorial_metadata(context, instruction)
                # Editorial metadata is derived locally so model style claims cannot override evidence.
                usage = completion.get("usage") or {}
                output_tokens = int(usage.get("completion_tokens") or self._estimate_tokens(article))
                input_tokens = int(usage.get("prompt_tokens") or input_tokens)
                return GenerationResult(
                    article=article,
                    provider=self.provider,
                    model=self.model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    attempts=attempts,
                )
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError, ValueError, KeyError, json.JSONDecodeError) as exc:
                last_error = exc
                # Authentication/authorization and other permanent 4xx errors should not be retried.
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in {429, 500, 502, 503, 504}:
                    break

        latency_ms = int((time.perf_counter() - started) * 1000)
        if self.fallback_local:
            fallback = LocalTextAdapter().generate(context, instruction)
            fallback.provider = "builtin-fallback"
            fallback.fallback_from = f"{self.provider}/{self.model}"
            fallback.attempts = attempts
            fallback.error = {
                "type": type(last_error).__name__ if last_error else "Unknown",
                "message": str(last_error) if last_error else "unknown model failure",
            }
            fallback.latency_ms += latency_ms
            return fallback

        raise GenerationError(
            f"Text model failed after {attempts} attempt(s): {last_error}",
            provider=self.provider,
            model=self.model,
            input_tokens=input_tokens,
            latency_ms=latency_ms,
            attempts=attempts,
        )


    @staticmethod
    def _proposal_system_prompt() -> str:
        return (
            "你是微信公众号内容架构师。结合任务描述与素材，先想清楚这篇文章的整体架构再作答。"
            "只返回一个合法 JSON 对象，不要 Markdown。"
            'schema: {"title":string, "summary":string, '
            '"sections":[{"id":string,"heading":string,"plan":string}], '
            '"typesetting":{"style_id":string,"reason":string,'
            '"alternates":[{"style_id":string,"reason":string}]}, "notes":string}。'
            "要求：title 拟用标题（不超过32个汉字）；summary 一句话主旨；sections 3-6 节，"
            "heading 是章节标题，plan 说明这一节写什么、用哪些素材或数据、大约多长；"
            "typesetting 必须从提供的样式目录里选最贴合内容气质的 style_id，reason 一句话讲清为什么，"
            "alternates 给 2 个备选；notes 一句话说明写作基调与目标读者。"
            "id 用小写字母数字下划线。素材里没有的信息不得编造。"
        )

    @staticmethod
    def _validate_proposal(payload: dict[str, Any], valid_style_ids: set[str]) -> dict[str, Any]:
        default_style = "mono-minimal"
        title = str(payload.get("title", "")).strip()[:32] or "未命名文章"
        summary = str(payload.get("summary", "")).strip()[:120]
        raw_sections = payload.get("sections")
        if not isinstance(raw_sections, list) or not raw_sections:
            raise ValueError("proposal has no sections")
        sections: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_sections[:6], start=1):
            if not isinstance(raw, dict):
                continue
            heading = str(raw.get("heading", "")).strip()
            if not heading:
                continue
            section_id = str(raw.get("id", "")).strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{3,60}", section_id):
                section_id = f"sec_{index:02d}"
            sections.append(
                {
                    "id": section_id,
                    "heading": heading[:120],
                    "plan": str(raw.get("plan", "")).strip()[:300],
                }
            )
        if len(sections) < 3:
            raise ValueError("proposal needs at least 3 usable sections")
        raw_typesetting = payload.get("typesetting") if isinstance(payload.get("typesetting"), dict) else {}
        style_id = str(raw_typesetting.get("style_id", "")).strip()
        if style_id not in valid_style_ids:
            style_id = default_style
        alternates = [
            {"style_id": str(item.get("style_id", "")), "reason": str(item.get("reason", ""))[:120]}
            for item in (raw_typesetting.get("alternates") or [])
            if isinstance(item, dict) and str(item.get("style_id", "")) in valid_style_ids
        ][:2]
        return {
            "title": title,
            "summary": summary,
            "sections": sections,
            "typesetting": {
                "style_id": style_id,
                "reason": str(raw_typesetting.get("reason", "")).strip()[:200],
                "alternates": alternates,
            },
            "notes": str(payload.get("notes", "")).strip()[:200],
        }

    def propose(
        self,
        context: SessionContext,
        instruction: str,
        previous: dict[str, Any] | None = None,
        feedback: str | None = None,
    ) -> dict[str, Any]:
        """让模型现场设计文章架构提案（大纲 + 样式模版推荐）。"""
        from .typesetting import STYLES as TYPESETTING_STYLES

        payload = context.to_model_payload()
        catalog = [
            {"id": s["id"], "name": s["name"], "vibe": s["vibe"], "suits": s["suits"]}
            for s in TYPESETTING_STYLES.values()
        ]
        request: dict[str, Any] = {
            "task": instruction or payload.get("goal"),
            "styles_catalog": catalog,
            "context": payload,
        }
        if previous or feedback:
            request["previous_proposal"] = previous
            request["user_feedback"] = feedback or ""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self._proposal_system_prompt()},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ],
            "temperature": 0.3,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        started = time.perf_counter()
        attempts = 0
        last_error: Exception | None = None
        while attempts <= self.max_retries:
            attempts += 1
            try:
                completion = self._post_chat(body, headers)
                content = completion["choices"][0]["message"]["content"]
                return self._validate_proposal(
                    self._extract_json(content), set(TYPESETTING_STYLES.keys())
                )
            except (
                httpx.TimeoutException,
                httpx.TransportError,
                httpx.HTTPStatusError,
                ValueError,
                KeyError,
                json.JSONDecodeError,
            ) as exc:
                last_error = exc
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in {429, 500, 502, 503, 504}:
                    break
        raise GenerationError(
            f"Architecture proposal failed after {attempts} attempt(s): {last_error}",
            provider=self.provider,
            model=self.model,
            input_tokens=0,
            latency_ms=int((time.perf_counter() - started) * 1000),
            attempts=attempts,
        )

def build_text_adapter(settings: Any) -> TextModelAdapter:
    if settings.text_provider == "openai_compatible":
        return OpenAICompatibleAdapter(
            model=settings.text_model,
            api_key=settings.text_api_key,
            api_base=settings.text_api_base,
            timeout_seconds=settings.text_timeout_seconds,
            max_retries=settings.text_max_retries,
            fallback_local=settings.text_fallback_local,
        )
    return LocalTextAdapter()

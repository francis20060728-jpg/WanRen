import json
from pathlib import Path

from scripts.validate_flow_with_vidlens import PROMPTS, verdict


def test_verdict_parses_explicit_final_marker():
    assert verdict("证据...\nVERDICT: PASS\noutput_path=x") == "PASS"
    assert verdict("证据...\nVERDICT: FAIL") == "FAIL"
    assert verdict("没有明确结论") == "UNKNOWN"


def test_full_flow_visual_prompt_matrix_is_complete():
    expected = [
        "01-landing", "02-register", "03-workspace", "04-task-created",
        "05-assets", "06-video-transcode", "06-article-review", "07-human-approved",
        "08-scheduled", "09-wechat-draft", *(f"10-stage-{i}" for i in range(1, 9)),
        "11-monitor", "01-account-wizard", "02-template-library",
        "03-template-builder", "04-ai-template-generator",
        "05-workspace-template-selector", "06-template-web-search",
        "07-search-reference-preview", "08-template-version-editor",
        "09-template-version-history",
    ]
    assert list(PROMPTS) == expected


def test_preserved_visual_verification_summary_is_all_pass():
    path = Path("docs/verification/2026-08-16-flow-validation.json")
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["status"] == "PASS"
    assert len(report["checks"]) == 18
    assert all(check["verdict"] == "PASS" for check in report["checks"])

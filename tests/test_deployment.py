from pathlib import Path


def test_docker_runtime_uses_non_root_user_and_healthcheck():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "USER wanren" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "python:3.12-slim" in dockerfile
    assert "COPY scripts ./scripts" in dockerfile


def test_compose_runs_separate_worker_and_persistent_volumes():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert "WANREN_EMBEDDED_WORKER: \"false\"" in compose
    assert "python\", \"-m\", \"wanren.worker" in compose
    assert "wanren-data:/app/data" in compose
    assert "wanren-storage:/app/storage" in compose
    assert "disable: true" in compose


def test_compose_reuses_one_built_image_for_api_and_worker():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    shared_image = "image: wanren:${WANREN_IMAGE_TAG:-latest}"
    assert compose.count(shared_image) == 2
    worker_section = compose.split("  worker:", 1)[1]
    assert "build:" not in worker_section


def test_compose_loads_shared_local_env_for_api_and_worker():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert compose.count("path: .env") == 2
    assert compose.count("required: false") == 2


def test_compose_uses_project_owned_persistent_volumes():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    data_section = compose.split("  wanren-data:", 1)[1].split("  wanren-storage:", 1)[0]
    storage_section = compose.split("  wanren-storage:", 1)[1]
    assert "external" not in data_section
    assert "external" not in storage_section

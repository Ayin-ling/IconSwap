"""首次使用风险确认逻辑的测试（不弹真实对话框，走注入的 asker）。"""

from __future__ import annotations

import json
import shutil
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from iconswap import risk_consent as rc  # noqa: E402

TEMP_ROOT = ROOT / "tests" / "_tmp"


@pytest.fixture()
def workdir():
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    directory = TEMP_ROOT / f"risk-{uuid.uuid4().hex[:10]}"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(rc.SKIP_ENV, raising=False)


# --------------------------------------------------------------------------- #
# 配置路径与读取
# --------------------------------------------------------------------------- #
def test_config_path_is_in_home():
    assert rc.config_path() == Path.home() / ".iconswap_config.json"
    assert rc.config_path().name == rc.CONFIG_NAME


def test_missing_config_means_not_accepted(workdir):
    target = workdir / ".iconswap_config.json"
    assert rc.is_risk_accepted(target) is False
    assert rc.load_config(target) == {}


def test_corrupt_or_non_object_config_is_ignored(workdir):
    target = workdir / ".iconswap_config.json"

    target.write_text("{not json", encoding="utf-8")
    assert rc.is_risk_accepted(target) is False

    target.write_text('["risk_accepted"]', encoding="utf-8")
    assert rc.is_risk_accepted(target) is False

    target.write_text('{"risk_accepted": "true"}', encoding="utf-8")
    assert rc.is_risk_accepted(target) is False  # 必须是布尔 true


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #
def test_accept_risk_writes_expected_payload(workdir):
    target = workdir / ".iconswap_config.json"

    returned = rc.accept_risk(target)

    assert returned == target
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload == {"risk_accepted": True, "version": 1}
    assert not target.with_name(target.name + ".tmp").exists()  # 原子写不留残file
    assert rc.is_risk_accepted(target) is True


def test_reset_risk_removes_file(workdir):
    target = workdir / ".iconswap_config.json"
    rc.accept_risk(target)

    assert rc.reset_risk(target) is True
    assert target.exists() is False
    assert rc.is_risk_accepted(target) is False
    assert rc.reset_risk(target) is False  # 再删一次返回 False


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def test_declining_keeps_program_closed(workdir):
    target = workdir / ".iconswap_config.json"

    allowed = rc.ensure_risk_consent(path=target, asker=lambda: False)

    assert allowed is False
    assert target.exists() is False


def test_accepting_writes_config_and_allows_start(workdir):
    target = workdir / ".iconswap_config.json"
    calls = {"n": 0}

    def asker():
        calls["n"] += 1
        return True

    allowed = rc.ensure_risk_consent(path=target, asker=asker)

    assert allowed is True
    assert calls["n"] == 1
    assert rc.is_risk_accepted(target) is True


def test_second_launch_skips_the_prompt(workdir):
    target = workdir / ".iconswap_config.json"
    rc.accept_risk(target)

    def asker():  # 被调用就说明逻辑错了
        raise AssertionError("已确认过风险，不应再弹窗")

    assert rc.ensure_risk_consent(path=target, asker=asker) is True


def test_skip_env_bypasses_prompt(workdir, monkeypatch):
    target = workdir / ".iconswap_config.json"
    monkeypatch.setenv(rc.SKIP_ENV, "1")

    assert rc.ensure_risk_consent(path=target, asker=lambda: False) is True
    assert target.exists() is False  # 跳过时也不写配置


def test_message_text_matches_specification():
    assert "修改 exe 文件图标" in rc.RISK_MESSAGE
    assert "备份" in rc.RISK_MESSAGE
    assert rc.RISK_MESSAGE.endswith("是否继续使用？")

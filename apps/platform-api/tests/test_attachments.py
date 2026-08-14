import hashlib

import pytest
from fastapi import HTTPException

from pydantic import ValidationError

from workama_platform.modules.session.router import SessionCreate, _safe_name, _validate_attachment


def test_text_attachment_is_decoded_and_hashed():
    content = "WorkAMA 文件问答".encode()
    digest, extracted = _validate_attachment(content, "text/plain")
    assert digest == hashlib.sha256(content).hexdigest()
    assert extracted == "WorkAMA 文件问答"


@pytest.mark.parametrize("content_type", ["application/octet-stream", "application/pdf", "image/png"])
def test_unsupported_temporary_attachment_is_rejected(content_type):
    with pytest.raises(HTTPException) as error:
        _validate_attachment(b"data", content_type)
    assert error.value.status_code == 415


def test_eicar_signature_is_rejected():
    with pytest.raises(HTTPException) as error:
        _validate_attachment(b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE", "text/plain")
    assert error.value.status_code == 422


def test_invalid_utf8_is_rejected():
    with pytest.raises(HTTPException) as error:
        _validate_attachment(b"\xff\xfe", "text/plain")
    assert error.value.status_code == 422


def test_attachment_filename_is_storage_safe():
    safe = _safe_name("../../report 2026?.txt")
    assert "/" not in safe and "\\" not in safe and safe.endswith(".txt")


def test_chat_shape_accepts_bounded_model_parameters():
    shape = SessionCreate.model_validate({"model_config": {"temperature": 0.2, "max_tokens": 1024}, "toolset": ["file.read"], "canvas_enabled": False})
    assert shape.parameters["max_tokens"] == 1024
    assert shape.toolset == ["file.read"]


@pytest.mark.parametrize("config", [{"temperature": 3}, {"top_p": 0}, {"unknown": True}])
def test_chat_shape_rejects_unsafe_model_parameters(config):
    with pytest.raises(ValidationError):
        SessionCreate.model_validate({"model_config": config})

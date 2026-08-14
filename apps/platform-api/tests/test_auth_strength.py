import jwt

from workama_platform.core import create_access_token, decode_token, settings


def test_access_token_carries_bounded_authentication_strength():
    basic = decode_token(create_access_token("usr_test", "wsp_test", "owner"))
    stepped_up = decode_token(create_access_token("usr_test", "wsp_test", "owner", auth_strength=2))
    assert basic["auth_strength"] == 1
    assert stepped_up["auth_strength"] == 2


def test_new_access_tokens_use_rs256():
    token = create_access_token("usr_test", "wsp_test", "owner")
    # dev 模式未配置 RSA 密钥时用 HS256（多 worker 共享 jwt_secret），生产模式用 RS256
    expected_alg = "RS256" if (settings.jwt_private_key and settings.jwt_public_key) else "HS256"
    assert jwt.get_unverified_header(token)["alg"] == expected_alg

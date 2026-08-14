from workama_platform.core import hash_password, hash_secret, new_id, verify_password


def test_prefixed_ids_are_sortable_shape():
    first = new_id("usr")
    second = new_id("usr")
    assert first.startswith("usr_")
    assert len(first) == 30
    assert first != second


def test_password_hashing_round_trip():
    hashed = hash_password("correct horse battery staple")
    assert verify_password(hashed, "correct horse battery staple")
    assert not verify_password(hashed, "incorrect")


def test_key_hash_is_deterministic_and_not_plaintext():
    value = "sk-wama-example"
    assert hash_secret(value) == hash_secret(value)
    assert value not in hash_secret(value)

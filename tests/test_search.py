import pytest


def search(client, first, last):
    return client.get("/user/search", params={"first_name": first, "last_name": last})


def test_search_both_prefixes_and_sort_by_id(client, register):
    # Unique prefixes keep the test independent of existing data.
    first_prefix, last_prefix = "ТестИмя", "ТестФамилия"
    ids = [
        register(first_name=first_prefix + ending, second_name=last_prefix + "ов")[0]
        for ending in ("А", "Б", "В")
    ]
    register(first_name=first_prefix + "Г", second_name="ДругаяФамилия")
    register(first_name="ДругоеИмя", second_name=last_prefix + "ова")
    register(first_name="До" + first_prefix, second_name=last_prefix)
    response = search(client, first_prefix, last_prefix)
    assert response.status_code == 200
    assert [profile["id"] for profile in response.json()] == sorted(ids)
    for profile in response.json():
        assert "password" not in profile
        assert "password_hash" not in profile


@pytest.mark.parametrize("literal", ["%", "_", "!", "\\", "' OR 1=1 --"])
def test_search_escapes_like_patterns_and_sql(client, register, literal):
    first, last = "Спец" + literal, "ОсобаяФамилия"
    user_id, _ = register(first_name=first + "ов", second_name=last)
    register(first_name="СпецДругой", second_name=last)
    response = search(client, first, last)
    assert response.status_code == 200
    assert [profile["id"] for profile in response.json()] == [user_id]


def test_search_is_case_sensitive_and_empty_result_is_200(client, register):
    register(first_name="Регистрозависимый", second_name="Тестовый")
    response = search(client, "регистрозависимый", "Тестовый")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"first_name": "Ив"},
        {"last_name": "Пет"},
        {"first_name": "", "last_name": "Пет"},
        {"first_name": "   ", "last_name": "Пет"},
        {"first_name": "Ив", "last_name": "x" * 101},
        {"first_name": "\x00", "last_name": "Пет"},
    ],
)
def test_invalid_search_parameters(client, params):
    assert client.get("/user/search", params=params).status_code == 400

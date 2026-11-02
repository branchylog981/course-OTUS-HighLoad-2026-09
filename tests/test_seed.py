from datetime import date

import pytest

from benchmarks.seed import people, seed


def test_seed_reader_preserves_names_and_skips_corruption(tmp_path):
    source = tmp_path / "people.csv"
    source.write_bytes(
        "Абрамов Роберт,2012-01-01,Воткинск\n".encode()
        + "Иванова Мария,1990-01-01,Москва\n".encode()
        + "Неверный Формат,not-a-date,Москва\n".encode()
        + "Яшина Елена,1967-01-01,Н".encode()
        + b"\xd0"
    )
    assert list(people(source)) == [
        ("Роберт", "Абрамов", date(2012, 1, 1), "Воткинск"),
        ("Мария", "Иванова", date(1990, 1, 1), "Москва"),
        None,
        None,
    ]


def test_seed_refuses_nonempty_database_without_changing_it(client, register, tmp_path):
    user_id, _ = register()
    source = tmp_path / "people.csv"
    source.write_text("Абрамов Роберт,2012-01-01,Воткинск\n")
    with pytest.raises(RuntimeError, match="existing data was preserved"):
        seed(source, 1, tmp_path / "seed.json")
    assert client.get(f"/user/get/{user_id}").status_code == 200

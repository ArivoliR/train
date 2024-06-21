from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias

from train.db import cur

if TYPE_CHECKING:
    from collections.abc import Iterable

RawStation: TypeAlias = tuple[int, str, int]


@dataclass(frozen=True)
class PartialStation:
    name: str

    block_id: int


@dataclass(frozen=True)
class Station:
    id: int
    name: str

    block_id: int

    @staticmethod
    def find_by_id(id: int) -> Station | None:
        payload = {"id": id}

        cur.execute(
            """
            SELECT station.* FROM station
            WHERE
                station.id = :id
            """,
            payload,
        )

        raw = cur.fetchone()
        if raw is None:
            return None

        return Station.decode(raw)

    @staticmethod
    def find_by_name(name: str) -> Station | None:
        payload = {"name": name}

        cur.execute(
            """
            SELECT station.* FROM station
            WHERE
                station.name = :name
            """,
            payload,
        )

        raw = cur.fetchone()
        if raw is None:
            return None

        return Station.decode(raw)

    @staticmethod
    def insert_many(stations: Iterable[PartialStation]) -> None:
        payload = [
            {"name": station.name, "block_id": station.block_id} for station in stations
        ]

        cur.executemany(
            """
            INSERT INTO station (name, block_id)
            VALUES (:name, :block_id)
            ON CONFLICT DO NOTHING
            """,
            payload,
        )

    @staticmethod
    def decode(raw: RawStation) -> Station:
        id, name, block_id = raw
        return Station(id, name, block_id)

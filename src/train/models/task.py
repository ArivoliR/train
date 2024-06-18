from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from heapq import heapify, heappop, heappush
from time import perf_counter
from typing import TYPE_CHECKING, Self, TypeAlias

from train.db import cur

if TYPE_CHECKING:
    from train.models.maintenance_window import MaintenanceWindow

RawTask: TypeAlias = tuple[int, str, str, str, str, int, int, int]


@dataclass
class TaskQ:
    priority: int
    requested_duration: timedelta
    preferred_starts_at: time
    preferred_ends_at: time
    starts_at: datetime = field(default=datetime.now())

    @property
    def preferred_range(self) -> timedelta:
        return (
            datetime.combine(date.min, self.preferred_ends_at)
            - datetime.combine(date.min, self.preferred_starts_at)
            + timedelta(days=int(self.preferred_starts_at >= self.preferred_ends_at))
        )

    def __lt__(self, other: Self) -> bool:
        """
        Priority Order.

        Priority (Hi)
        -> Requested Duration (Hi)
        -> Preferred Range (Lo)
        -> Starts At (Lo)
        """
        if self.priority == other.priority:
            if self.requested_duration == other.requested_duration:
                if self.preferred_range == other.preferred_range:
                    return self.starts_at < other.starts_at
                return self.preferred_range < other.preferred_range
            return self.requested_duration > other.requested_duration
        return self.priority > other.priority


@dataclass(frozen=True)
class Task:
    id: int
    starts_at: datetime
    ends_at: datetime
    preferred_starts_at: time
    preferred_ends_at: time
    requested_duration: timedelta
    priority: int

    maintenance_window_id: int

    def maintenance_window(self) -> MaintenanceWindow:
        from train.models.maintenance_window import MaintenanceWindow

        maintenance_window = MaintenanceWindow.find_by_id(self.maintenance_window_id)
        assert maintenance_window is not None

        return maintenance_window

    @classmethod
    def insert_one(  # noqa: PLR0913
        cls,
        starts_at: datetime,
        ends_at: datetime,
        requested_duration: timedelta,
        priority: int,
        maintenance_window_id: int,
        preferred_starts_at: time | None = None,
        preferred_ends_at: time | None = None,
    ) -> Self:
        payload = {
            "starts_at": starts_at,
            "ends_at": ends_at,
            "preferred_starts_at": preferred_starts_at,
            "preferred_ends_at": preferred_ends_at,
            "requested_duration": requested_duration,
            "priority": priority,
            "maintenance_window_id": maintenance_window_id,
        }

        res = cur.execute(
            """
            INSERT INTO task (
                starts_at,
                ends_at,
                preferred_starts_at,
                preferred_ends_at,
                requested_duration,
                priority,
                maintenance_window_id
            ) VALUES (
                :starts_at,
                :ends_at,
                :preferred_starts_at,
                :preferred_ends_at,
                :requested_duration,
                :priority,
                :maintenance_window_id
            ) RETURNING *
            """,
            payload,
        )

        task = cls.decode(res.fetchone())
        assert task is not None

        return task

    @classmethod
    def insert_preferred(  # noqa: PLR0913, PLR0912, C901
        cls,
        preferred_starts_at: time,
        preferred_ends_at: time,
        priority: int,
        section_id: int,
        requested_duration: timedelta | None = None,
    ) -> list[Self]:
        def s():
            return
            res = cur.execute(
                """
                SELECT * from task;
                """,
            )

            print([cls.decode(x) for x in res.fetchall()])

        tasks = []

        if requested_duration is None:
            requested_duration = (
                datetime.combine(date.min, preferred_ends_at)
                - datetime.combine(date.min, preferred_starts_at)
                + (preferred_ends_at <= preferred_starts_at) * timedelta(hours=24)
            )

        queue: list[TaskQ] = [
            TaskQ(
                priority,
                requested_duration,
                preferred_starts_at,
                preferred_ends_at,
            ),
        ]

        heapify(queue)
        s()
        while queue:
            taskq = heappop(queue)

            payload = {
                "requested_duration": taskq.requested_duration,
                "preferred_starts_at": taskq.preferred_starts_at.isoformat(),
                "preferred_ends_at": taskq.preferred_ends_at.isoformat(),
                "priority": taskq.priority,
                "section_id": section_id,
            }

            t0 = perf_counter()
            res = cur.execute(
                """
                SELECT
                UNIXEPOCH(:preferred_starts_at) -
                    86400 * (:preferred_ends_at <= :preferred_starts_at)
                    AS p_starts,
                UNIXEPOCH(:preferred_ends_at) AS p_ends,
                UNIXEPOCH(TIME(window_start)) -
                    86400 * (TIME(window_end) <= TIME(window_start))
                    AS m_starts,
                UNIXEPOCH(TIME(window_end)) AS m_ends,
                window_id,
                window_start,
                window_end
                FROM (
                    SELECT
                        window_start,
                        COALESCE(
                            (
                                SELECT task.starts_at FROM task
                                JOIN maintenance_window
                                    ON task.maintenance_window_id = window_id
                                WHERE
                                    task.priority >= :priority
                                    AND task.starts_at >= window_start
                                ORDER BY
                                    task.starts_at ASC
                                LIMIT 1
                            ),
                            m_window_end
                        ) AS window_end,
                        window_id
                    FROM (
                        SELECT
                            maintenance_window.starts_at AS window_start,
                            maintenance_window.ends_at AS m_window_end,
                            maintenance_window.id as window_id,
                            maintenance_window.section_id as section_id
                        FROM maintenance_window
                        UNION
                        SELECT
                            task.ends_at AS window_start,
                            maintenance_window.ends_at AS m_window_end,
                            maintenance_window.id as window_id,
                            maintenance_window.section_id as section_id
                        FROM task
                        JOIN maintenance_window
                            ON maintenance_window.id = task.maintenance_window_id
                        WHERE
                            task.priority >= :priority
                    )
                    WHERE
                        section_id = :section_id
                        AND window_start >= DATETIME('now', '+30 minutes')
                        AND UNIXEPOCH(window_end) - UNIXEPOCH(window_start)
                            >= :requested_duration
                )
                ORDER BY
                    MIN(
                        MAX(
                            (p_ends - p_starts)
                            - MAX(p_ends - m_ends, 0)
                            - MAX(m_starts - p_starts, 0),
                            (p_ends - p_starts)
                            - MAX(p_ends - m_ends + 86400, 0)
                            - MAX(m_starts - p_starts - 86400, 0),
                            (p_ends - p_starts)
                            - MAX(p_ends - m_ends - 86400, 0)
                            - MAX(m_starts - p_starts + 86400, 0)
                        ),
                        :requested_duration
                    ) DESC,
                    window_start ASC
                LIMIT
                    1
                """,
                payload,
            )

            t1 = perf_counter()
            print(t1 - t0)
            x: (
                tuple[
                    int,
                    int,
                    int,
                    int,
                    int,
                    str,
                    str,
                ]
                | None
            ) = res.fetchone()
            if x is None:
                raise Exception  # noqa: TRY002

            s()
            (
                p_starts,
                p_ends,
                m_starts,
                m_ends,
                window_id,
                window_start,
                window_end,
            ) = x

            window_start = datetime.fromisoformat(window_start)
            window_end = datetime.fromisoformat(window_end)
            intersection = timedelta(
                seconds=max(
                    (p_ends - p_starts)
                    - max(p_ends - m_ends, 0)
                    - max(m_starts - p_starts, 0),
                    (p_ends - p_starts)
                    - max(p_ends - m_ends + 86400, 0)
                    - max(m_starts - p_starts - 86400, 0),
                    (p_ends - p_starts)
                    - max(p_ends - m_ends - 86400, 0)
                    - max(m_starts - p_starts + 86400, 0),
                ),
            )

            possible_preferred_dates = (
                window_start + timedelta(days=-1),
                window_start + timedelta(days=-0),
                window_end + timedelta(days=+0),
                window_end + timedelta(days=+1),
            )

            possible_preferred_windows = (
                (
                    possible_preferred_date.combine(
                        possible_preferred_date.date(),
                        taskq.preferred_starts_at,
                    ),
                    possible_preferred_date.combine(
                        possible_preferred_date.date(),
                        taskq.preferred_ends_at,
                    )
                    + timedelta(
                        days=(
                            1
                            if taskq.preferred_starts_at >= taskq.preferred_ends_at
                            else 0
                        ),
                    ),
                )
                for possible_preferred_date in possible_preferred_dates
            )

            closest_preferred_start: datetime
            closest_preferred_end: datetime
            for pws, pwe in possible_preferred_windows:
                current_intersection = (
                    (pwe - pws)
                    - max(pwe - window_end, timedelta())
                    - max(window_start - pws, timedelta())
                )
                if current_intersection == intersection:
                    closest_preferred_start = pws
                    closest_preferred_end = pwe
                    break
            else:
                raise Exception  # noqa: TRY002

            if intersection > taskq.requested_duration:
                starts_at = window_start
                ends_at = window_start + taskq.requested_duration

                intsxn = (
                    taskq.requested_duration
                    - max(ends_at - closest_preferred_end, timedelta())
                    - max(closest_preferred_start - starts_at, timedelta())
                )

                if intsxn < taskq.requested_duration:
                    starts_at = closest_preferred_start
                    ends_at = closest_preferred_start + taskq.requested_duration

            elif intersection == taskq.requested_duration:
                starts_at = closest_preferred_start
                ends_at = closest_preferred_end
            elif intersection > timedelta():
                if closest_preferred_end < window_end:
                    starts_at = window_start
                    ends_at = window_start + taskq.requested_duration
                else:
                    starts_at = window_end - taskq.requested_duration
                    ends_at = window_end
            elif closest_preferred_start > window_end:
                starts_at = window_end - taskq.requested_duration
                ends_at = window_end
            else:
                starts_at = window_start
                ends_at = window_start + taskq.requested_duration

            payload = {
                "window_id": window_id,
                "starts_at": starts_at,
                "ends_at": ends_at,
            }

            print(starts_at, ends_at)

            res = cur.execute(
                """
                DELETE FROM task
                WHERE
                    task.maintenance_window_id = :window_id
                    AND (
                        task.starts_at BETWEEN :starts_at AND :ends_at
                        OR task.ends_at BETWEEN :starts_at AND :ends_at
                        OR :starts_at BETWEEN task.starts_at AND task.ends_at
                    )
                RETURNING *
                """,
                payload,
            )

            for task in [cls.decode(x) for x in res.fetchall()]:
                heappush(
                    queue,
                    TaskQ(
                        task.priority,
                        task.requested_duration,
                        task.preferred_starts_at,
                        task.preferred_ends_at,
                        task.starts_at,
                    ),
                )

            task = Task.insert_one(
                starts_at,
                ends_at,
                taskq.requested_duration,
                taskq.priority,
                window_id,
                preferred_starts_at=taskq.preferred_starts_at,
                preferred_ends_at=taskq.preferred_ends_at,
            )

            tasks.append(task)

        return tasks

    @classmethod
    def find_by_id(cls, id: int) -> Self | None:
        payload = {"id": id}

        res = cur.execute(
            """
            SELECT task.* FROM task
            WHERE
                task.id = :id
            """,
            payload,
        )

        raw = res.fetchone()
        if raw is None:
            return None

        return cls.decode(res.fetchone())

    @classmethod
    def decode(cls, raw: RawTask) -> Self:
        (
            id,
            starts_at,
            ends_at,
            preferred_starts_at,
            preferred_ends_at,
            requested_duration,
            priority,
            maintenance_window_id,
        ) = raw

        return cls(
            id,
            datetime.fromisoformat(starts_at),
            datetime.fromisoformat(ends_at),
            time.fromisoformat(preferred_starts_at),
            time.fromisoformat(preferred_ends_at),
            timedelta(seconds=requested_duration),
            priority,
            maintenance_window_id,
        )

    @staticmethod
    def init() -> None:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS task (
                id INTEGER PRIMARY KEY,
                starts_at DATETIME NOT NULL,
                ends_at DATETIME NOT NULL,
                preferred_starts_at TIME DEFAULT '00:00:00',
                preferred_ends_at TIME DEFAULT '00:00:00',
                requested_duration INTEGER NOT NULL,
                priority INTEGER NOT NULL,

                maintenance_window_id INTEGER NOT NULL,
                FOREIGN KEY(maintenance_window_id) REFERENCES maintenance_window(id)
            )
            """,
        )

    @classmethod
    def delete_future_tasks(cls, section_id: int) -> list[Self]:
        payload = {"section_id": section_id}

        res = cur.execute(
            """
            DELETE FROM task
            WHERE
                id in (
                    SELECT task.id FROM task
                    JOIN maintenance_window ON
                        maintenance_window.id = task.maintenance_window_id
                    JOIN section ON
                        section.id = maintenance_window.section_id
                    WHERE
                        section.id = :section_id
                        AND task.starts_at >= DATETIME('now', '+30 minutes')
                )
            RETURNING *
            """,
            payload,
        )

        return [cls.decode(raw) for raw in res.fetchall()]

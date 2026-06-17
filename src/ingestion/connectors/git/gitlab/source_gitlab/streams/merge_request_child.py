from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping
from functools import cache
from typing import Any

from airbyte_cdk.models import AirbyteMessage, SyncMode
from airbyte_cdk.sources.streams import IncrementalMixin

from source_gitlab.streams.base import GitlabStream, ScopedGitlabStream
from source_gitlab.streams.scope import (
    advance_cursor,
    compute_floor,
    scope_bases,
    scope_key,
    scope_params,
    scope_path,
)
from source_gitlab.streams.windowing import UpdatedAtWindowing


class _ScopeMergeRequestEnumerator(UpdatedAtWindowing, GitlabStream):
    name = "_mr_enum_internal"

    @cache
    def get_json_schema(self) -> Mapping[str, Any]:
        return {}

    def read_records(
        self,
        sync_mode: SyncMode,
        cursor_field: list[str] | None = None,
        stream_slice: Mapping[str, Any] | None = None,
        stream_state: Mapping[str, Any] | None = None,
    ) -> Iterable[Mapping[str, Any] | AirbyteMessage]:
        yield from self._windowed_records(
            sync_mode, cursor_field, stream_slice, stream_state
        )

    def _path(self, *, stream_slice: Mapping[str, Any] | None) -> str:
        return scope_path("merge_requests", stream_slice)

    def _initial_params(
        self, stream_slice: Mapping[str, Any] | None
    ) -> Mapping[str, Any]:
        return scope_params(self.page_size, stream_slice)

    def _record_key(
        self, record: Mapping[str, Any], stream_slice: Mapping[str, Any] | None
    ) -> list[str]:
        return [str(record["project_id"]), str(record["iid"])]

    def _project(
        self, record: Mapping[str, Any], stream_slice: Mapping[str, Any] | None
    ) -> Mapping[str, Any]:
        return {
            "project_id": record.get("project_id"),
            "iid": record.get("iid"),
            "updated_at": record.get("updated_at"),
        }


class MergeRequestChildStream(ScopedGitlabStream, IncrementalMixin):
    cursor_field = "mr_updated_at"
    skippable_statuses = frozenset({404})

    def __init__(
        self,
        *,
        parent: GitlabStream,
        groups: tuple[str, ...],
        projects: tuple[str, ...],
        start_date: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(groups=groups, projects=projects, **kwargs)
        self._parent = parent
        self._start_date = start_date
        self._mr_enum = _ScopeMergeRequestEnumerator(
            base_url=self._base_url,
            token=self._token,
            tenant_id=self._tenant_id,
            source_id=self._source_id,
        )
        self._state: MutableMapping[str, Any] = {}

    @property
    def state(self) -> MutableMapping[str, Any]:
        return self._state

    @state.setter
    def state(self, value: MutableMapping[str, Any]) -> None:
        self._state = value or {}

    def _scope_state(self, key: str) -> MutableMapping[str, Any]:
        scopes: dict[str, Any] = self._state.setdefault("scopes", {})
        entry: dict[str, Any] = scopes.setdefault(key, {})
        return entry

    def stream_slices(self, **kwargs: Any) -> Iterable[Mapping[str, Any] | None]:
        for base in scope_bases(self._groups, self._projects, self._parent):
            key = scope_key(base)
            watermark = self._state.get("scopes", {}).get(key, {}).get("updated_at")
            enum_scope = {
                **(base or {}),
                "updated_after": compute_floor(self._start_date, watermark),
            }
            for mr in self._mr_enum.read_records(
                sync_mode=SyncMode.full_refresh, stream_slice=enum_scope
            ):
                if (
                    not isinstance(mr, Mapping)
                    or mr.get("iid") is None
                    or mr.get("project_id") is None
                ):
                    continue
                yield {
                    "scope_key": key,
                    "project_id": mr["project_id"],
                    "mr_iid": mr["iid"],
                    "mr_updated_at": mr.get("updated_at"),
                }

    def _initial_params(
        self, stream_slice: Mapping[str, Any] | None
    ) -> Mapping[str, Any]:
        return {"per_page": self.page_size}

    def read_records(
        self,
        sync_mode: SyncMode,
        cursor_field: list[str] | None = None,
        stream_slice: Mapping[str, Any] | None = None,
        stream_state: Mapping[str, Any] | None = None,
    ) -> Iterable[Mapping[str, Any] | AirbyteMessage]:
        yield from super().read_records(
            sync_mode,
            cursor_field=cursor_field,
            stream_slice=stream_slice,
            stream_state=stream_state,
        )
        slice_ = stream_slice or {}
        key = slice_.get("scope_key")
        if key is not None:
            advance_cursor(self._scope_state(key), slice_.get("mr_updated_at"))

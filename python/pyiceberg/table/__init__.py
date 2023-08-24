# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import itertools
from abc import ABC, abstractmethod
from copy import copy
from dataclasses import dataclass
from enum import Enum
from functools import cached_property
from itertools import chain
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Literal,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)

from pydantic import Field, SerializeAsAny
from sortedcontainers import SortedList

from pyiceberg.expressions import (
    AlwaysTrue,
    And,
    BooleanExpression,
    EqualTo,
    parser,
    visitors,
)
from pyiceberg.expressions.visitors import _InclusiveMetricsEvaluator, inclusive_projection
from pyiceberg.io import FileIO, load_file_io
from pyiceberg.manifest import (
    POSITIONAL_DELETE_SCHEMA,
    DataFile,
    DataFileContent,
    ManifestContent,
    ManifestEntry,
    ManifestFile,
)
from pyiceberg.partitioning import PartitionSpec
from pyiceberg.schema import (
    Schema,
    SchemaVisitor,
    assign_fresh_schema_ids,
    visit,
)
from pyiceberg.table.metadata import INITIAL_SEQUENCE_NUMBER, TableMetadata
from pyiceberg.table.snapshots import Snapshot, SnapshotLogEntry
from pyiceberg.table.sorting import SortOrder
from pyiceberg.typedef import (
    EMPTY_DICT,
    IcebergBaseModel,
    Identifier,
    KeyDefaultDict,
    Properties,
)
from pyiceberg.types import (
    IcebergType,
    ListType,
    MapType,
    NestedField,
    PrimitiveType,
    StructType,
)
from pyiceberg.utils.concurrent import ExecutorFactory

if TYPE_CHECKING:
    import pandas as pd
    import pyarrow as pa
    import ray
    from duckdb import DuckDBPyConnection

    from pyiceberg.catalog import Catalog

ALWAYS_TRUE = AlwaysTrue()
TABLE_ROOT_ID = -1


class Transaction:
    _table: Table
    _updates: Tuple[TableUpdate, ...]
    _requirements: Tuple[TableRequirement, ...]

    def __init__(
        self,
        table: Table,
        actions: Optional[Tuple[TableUpdate, ...]] = None,
        requirements: Optional[Tuple[TableRequirement, ...]] = None,
    ):
        self._table = table
        self._updates = actions or ()
        self._requirements = requirements or ()

    def __enter__(self) -> Transaction:
        """Starts a transaction to update the table."""
        return self

    def __exit__(self, _: Any, value: Any, traceback: Any) -> None:
        """Closes and commits the transaction."""
        self.commit_transaction()

    def _append_updates(self, *new_updates: TableUpdate) -> Transaction:
        """Appends updates to the set of staged updates.

        Args:
            *new_updates: Any new updates.

        Raises:
            ValueError: When the type of update is not unique.

        Returns:
            Transaction object with the new updates appended.
        """
        for new_update in new_updates:
            type_new_update = type(new_update)
            if any(type(update) == type_new_update for update in self._updates):
                raise ValueError(f"Updates in a single commit need to be unique, duplicate: {type_new_update}")
        self._updates = self._updates + new_updates
        return self

    def _append_requirements(self, *new_requirements: TableRequirement) -> Transaction:
        """Appends requirements to the set of staged requirements.

        Args:
            *new_requirements: Any new requirements.

        Raises:
            ValueError: When the type of requirement is not unique.

        Returns:
            Transaction object with the new requirements appended.
        """
        for requirement in new_requirements:
            type_new_requirement = type(requirement)
            if any(type(requirement) == type_new_requirement for update in self._requirements):
                raise ValueError(f"Requirements in a single commit need to be unique, duplicate: {type_new_requirement}")
        self._requirements = self._requirements + new_requirements
        return self

    def set_table_version(self, format_version: Literal[1, 2]) -> Transaction:
        """Sets the table to a certain version.

        Args:
            format_version: The newly set version.

        Returns:
            The alter table builder.
        """
        raise NotImplementedError("Not yet implemented")

    def set_properties(self, **updates: str) -> Transaction:
        """Set properties.

        When a property is already set, it will be overwritten.

        Args:
            updates: The properties set on the table.

        Returns:
            The alter table builder.
        """
        return self._append_updates(SetPropertiesUpdate(updates=updates))

    def update_schema(self) -> UpdateSchema:
        """Create a new UpdateSchema to alter the columns of this table.

        Returns:
            A new UpdateSchema.
        """
        return UpdateSchema(self._table.schema(), self._table, self)

    def remove_properties(self, *removals: str) -> Transaction:
        """Removes properties.

        Args:
            removals: Properties to be removed.

        Returns:
            The alter table builder.
        """
        return self._append_updates(RemovePropertiesUpdate(removals=removals))

    def update_location(self, location: str) -> Transaction:
        """Sets the new table location.

        Args:
            location: The new location of the table.

        Returns:
            The alter table builder.
        """
        raise NotImplementedError("Not yet implemented")

    def commit_transaction(self) -> Table:
        """Commits the changes to the catalog.

        Returns:
            The table with the updates applied.
        """
        # Strip the catalog name
        if len(self._updates) > 0:
            self._table._do_commit(  # pylint: disable=W0212
                CommitTableRequest(
                    identifier=self._table.identifier[1:],
                    requirements=self._requirements,
                    updates=self._updates,
                )
            )
            return self._table
        else:
            return self._table


class TableUpdateAction(Enum):
    upgrade_format_version = "upgrade-format-version"
    add_schema = "add-schema"
    set_current_schema = "set-current-schema"
    add_spec = "add-spec"
    set_default_spec = "set-default-spec"
    add_sort_order = "add-sort-order"
    set_default_sort_order = "set-default-sort-order"
    add_snapshot = "add-snapshot"
    set_snapshot_ref = "set-snapshot-ref"
    remove_snapshots = "remove-snapshots"
    remove_snapshot_ref = "remove-snapshot-ref"
    set_location = "set-location"
    set_properties = "set-properties"
    remove_properties = "remove-properties"


class TableUpdate(IcebergBaseModel):
    action: TableUpdateAction


class UpgradeFormatVersionUpdate(TableUpdate):
    action: TableUpdateAction = TableUpdateAction.upgrade_format_version
    format_version: int = Field(alias="format-version")


class AddSchemaUpdate(TableUpdate):
    action: TableUpdateAction = TableUpdateAction.add_schema
    schema_: Schema = Field(alias="schema")
    # This field is required: https://github.com/apache/iceberg/pull/7445
    last_column_id: int = Field(alias="last-column-id")


class SetCurrentSchemaUpdate(TableUpdate):
    action: TableUpdateAction = TableUpdateAction.set_current_schema
    schema_id: int = Field(
        alias="schema-id", description="Schema ID to set as current, or -1 to set last added schema", default=-1
    )


class AddPartitionSpecUpdate(TableUpdate):
    action: TableUpdateAction = TableUpdateAction.add_spec
    spec: PartitionSpec


class SetDefaultSpecUpdate(TableUpdate):
    action: TableUpdateAction = TableUpdateAction.set_default_spec
    spec_id: int = Field(
        alias="spec-id", description="Partition spec ID to set as the default, or -1 to set last added spec", default=-1
    )


class AddSortOrderUpdate(TableUpdate):
    action: TableUpdateAction = TableUpdateAction.add_sort_order
    sort_order: SortOrder = Field(alias="sort-order")


class SetDefaultSortOrderUpdate(TableUpdate):
    action: TableUpdateAction = TableUpdateAction.set_default_sort_order
    sort_order_id: int = Field(
        alias="sort-order-id", description="Sort order ID to set as the default, or -1 to set last added sort order", default=-1
    )


class AddSnapshotUpdate(TableUpdate):
    action: TableUpdateAction = TableUpdateAction.add_snapshot
    snapshot: Snapshot


class SetSnapshotRefUpdate(TableUpdate):
    action: TableUpdateAction = TableUpdateAction.set_snapshot_ref
    ref_name: str = Field(alias="ref-name")
    type: Literal["tag", "branch"]
    snapshot_id: int = Field(alias="snapshot-id")
    max_age_ref_ms: int = Field(alias="max-ref-age-ms")
    max_snapshot_age_ms: int = Field(alias="max-snapshot-age-ms")
    min_snapshots_to_keep: int = Field(alias="min-snapshots-to-keep")


class RemoveSnapshotsUpdate(TableUpdate):
    action: TableUpdateAction = TableUpdateAction.remove_snapshots
    snapshot_ids: List[int] = Field(alias="snapshot-ids")


class RemoveSnapshotRefUpdate(TableUpdate):
    action: TableUpdateAction = TableUpdateAction.remove_snapshot_ref
    ref_name: str = Field(alias="ref-name")


class SetLocationUpdate(TableUpdate):
    action: TableUpdateAction = TableUpdateAction.set_location
    location: str


class SetPropertiesUpdate(TableUpdate):
    action: TableUpdateAction = TableUpdateAction.set_properties
    updates: Dict[str, str]


class RemovePropertiesUpdate(TableUpdate):
    action: TableUpdateAction = TableUpdateAction.remove_properties
    removals: List[str]


class TableRequirement(IcebergBaseModel):
    type: str


class AssertCreate(TableRequirement):
    """The table must not already exist; used for create transactions."""

    type: Literal["assert-create"] = Field(default="assert-create")


class AssertTableUUID(TableRequirement):
    """The table UUID must match the requirement's `uuid`."""

    type: Literal["assert-table-uuid"] = Field(default="assert-table-uuid")
    uuid: str


class AssertRefSnapshotId(TableRequirement):
    """The table branch or tag identified by the requirement's `ref` must reference the requirement's `snapshot-id`.

    if `snapshot-id` is `null` or missing, the ref must not already exist.
    """

    type: Literal["assert-ref-snapshot-id"] = Field(default="assert-ref-snapshot-id")
    ref: str
    snapshot_id: int = Field(..., alias="snapshot-id")


class AssertLastAssignedFieldId(TableRequirement):
    """The table's last assigned column id must match the requirement's `last-assigned-field-id`."""

    type: Literal["assert-last-assigned-field-id"] = Field(default="assert-last-assigned-field-id")
    last_assigned_field_id: int = Field(..., alias="last-assigned-field-id")


class AssertCurrentSchemaId(TableRequirement):
    """The table's current schema id must match the requirement's `current-schema-id`."""

    type: Literal["assert-current-schema-id"] = Field(default="assert-current-schema-id")
    current_schema_id: int = Field(..., alias="current-schema-id")


class AssertLastAssignedPartitionId(TableRequirement):
    """The table's last assigned partition id must match the requirement's `last-assigned-partition-id`."""

    type: Literal["assert-last-assigned-partition-id"] = Field(default="assert-last-assigned-partition-id")
    last_assigned_partition_id: int = Field(..., alias="last-assigned-partition-id")


class AssertDefaultSpecId(TableRequirement):
    """The table's default spec id must match the requirement's `default-spec-id`."""

    type: Literal["assert-default-spec-id"] = Field(default="assert-default-spec-id")
    default_spec_id: int = Field(..., alias="default-spec-id")


class AssertDefaultSortOrderId(TableRequirement):
    """The table's default sort order id must match the requirement's `default-sort-order-id`."""

    type: Literal["assert-default-sort-order-id"] = Field(default="assert-default-sort-order-id")
    default_sort_order_id: int = Field(..., alias="default-sort-order-id")


class CommitTableRequest(IcebergBaseModel):
    identifier: Identifier = Field()
    requirements: List[SerializeAsAny[TableRequirement]] = Field(default_factory=list)
    updates: List[SerializeAsAny[TableUpdate]] = Field(default_factory=list)


class CommitTableResponse(IcebergBaseModel):
    metadata: TableMetadata
    metadata_location: str = Field(alias="metadata-location")


class Table:
    identifier: Identifier = Field()
    metadata: TableMetadata
    metadata_location: str = Field()
    io: FileIO
    catalog: Catalog

    def __init__(
        self, identifier: Identifier, metadata: TableMetadata, metadata_location: str, io: FileIO, catalog: Catalog
    ) -> None:
        self.identifier = identifier
        self.metadata = metadata
        self.metadata_location = metadata_location
        self.io = io
        self.catalog = catalog

    def transaction(self) -> Transaction:
        return Transaction(self)

    def refresh(self) -> Table:
        """Refresh the current table metadata."""
        fresh = self.catalog.load_table(self.identifier[1:])
        self.metadata = fresh.metadata
        self.io = fresh.io
        self.metadata_location = fresh.metadata_location
        return self

    def name(self) -> Identifier:
        """Return the identifier of this table."""
        return self.identifier

    def scan(
        self,
        row_filter: Union[str, BooleanExpression] = ALWAYS_TRUE,
        selected_fields: Tuple[str, ...] = ("*",),
        case_sensitive: bool = True,
        snapshot_id: Optional[int] = None,
        options: Properties = EMPTY_DICT,
        limit: Optional[int] = None,
    ) -> DataScan:
        return DataScan(
            table=self,
            row_filter=row_filter,
            selected_fields=selected_fields,
            case_sensitive=case_sensitive,
            snapshot_id=snapshot_id,
            options=options,
            limit=limit,
        )

    def schema(self) -> Schema:
        """Return the schema for this table."""
        return next(schema for schema in self.metadata.schemas if schema.schema_id == self.metadata.current_schema_id)

    def schemas(self) -> Dict[int, Schema]:
        """Return a dict of the schema of this table."""
        return {schema.schema_id: schema for schema in self.metadata.schemas}

    def spec(self) -> PartitionSpec:
        """Return the partition spec of this table."""
        return next(spec for spec in self.metadata.partition_specs if spec.spec_id == self.metadata.default_spec_id)

    def specs(self) -> Dict[int, PartitionSpec]:
        """Return a dict the partition specs this table."""
        return {spec.spec_id: spec for spec in self.metadata.partition_specs}

    def sort_order(self) -> SortOrder:
        """Return the sort order of this table."""
        return next(
            sort_order for sort_order in self.metadata.sort_orders if sort_order.order_id == self.metadata.default_sort_order_id
        )

    def sort_orders(self) -> Dict[int, SortOrder]:
        """Return a dict of the sort orders of this table."""
        return {sort_order.order_id: sort_order for sort_order in self.metadata.sort_orders}

    @property
    def properties(self) -> Dict[str, str]:
        """Properties of the table."""
        return self.metadata.properties

    def location(self) -> str:
        """Return the table's base location."""
        return self.metadata.location

    def current_snapshot(self) -> Optional[Snapshot]:
        """Get the current snapshot for this table, or None if there is no current snapshot."""
        if snapshot_id := self.metadata.current_snapshot_id:
            return self.snapshot_by_id(snapshot_id)
        return None

    def snapshot_by_id(self, snapshot_id: int) -> Optional[Snapshot]:
        """Get the snapshot of this table with the given id, or None if there is no matching snapshot."""
        try:
            return next(snapshot for snapshot in self.metadata.snapshots if snapshot.snapshot_id == snapshot_id)
        except StopIteration:
            return None

    def snapshot_by_name(self, name: str) -> Optional[Snapshot]:
        """Returns the snapshot referenced by the given name or null if no such reference exists."""
        if ref := self.metadata.refs.get(name):
            return self.snapshot_by_id(ref.snapshot_id)
        return None

    def history(self) -> List[SnapshotLogEntry]:
        """Get the snapshot history of this table."""
        return self.metadata.snapshot_log

    def update_schema(self) -> UpdateSchema:
        return UpdateSchema(self.schema(), self)

    def _do_commit(self, request: CommitTableRequest) -> None:
        response = self.catalog._commit_table(request)  # pylint: disable=W0212
        self.metadata = response.metadata
        self.metadata_location = response.metadata_location

    def __eq__(self, other: Any) -> bool:
        """Returns the equality of two instances of the Table class."""
        return (
            self.identifier == other.identifier
            and self.metadata == other.metadata
            and self.metadata_location == other.metadata_location
            if isinstance(other, Table)
            else False
        )


class StaticTable(Table):
    """Load a table directly from a metadata file (i.e., without using a catalog)."""

    def refresh(self) -> Table:
        """Refresh the current table metadata."""
        raise NotImplementedError("To be implemented")

    @classmethod
    def from_metadata(cls, metadata_location: str, properties: Properties = EMPTY_DICT) -> StaticTable:
        io = load_file_io(properties=properties, location=metadata_location)
        file = io.new_input(metadata_location)

        from pyiceberg.serializers import FromInputFile

        metadata = FromInputFile.table_metadata(file)

        from pyiceberg.catalog.noop import NoopCatalog

        return cls(
            identifier=("static-table", metadata_location),
            metadata_location=metadata_location,
            metadata=metadata,
            io=load_file_io({**properties, **metadata.properties}),
            catalog=NoopCatalog("static-table"),
        )


def _parse_row_filter(expr: Union[str, BooleanExpression]) -> BooleanExpression:
    """Accepts an expression in the form of a BooleanExpression or a string.

    In the case of a string, it will be converted into a unbound BooleanExpression.

    Args:
        expr: Expression as a BooleanExpression or a string.

    Returns: An unbound BooleanExpression.
    """
    return parser.parse(expr) if isinstance(expr, str) else expr


S = TypeVar("S", bound="TableScan", covariant=True)


class TableScan(ABC):
    table: Table
    row_filter: BooleanExpression
    selected_fields: Tuple[str, ...]
    case_sensitive: bool
    snapshot_id: Optional[int]
    options: Properties
    limit: Optional[int]

    def __init__(
        self,
        table: Table,
        row_filter: Union[str, BooleanExpression] = ALWAYS_TRUE,
        selected_fields: Tuple[str, ...] = ("*",),
        case_sensitive: bool = True,
        snapshot_id: Optional[int] = None,
        options: Properties = EMPTY_DICT,
        limit: Optional[int] = None,
    ):
        self.table = table
        self.row_filter = _parse_row_filter(row_filter)
        self.selected_fields = selected_fields
        self.case_sensitive = case_sensitive
        self.snapshot_id = snapshot_id
        self.options = options
        self.limit = limit

    def snapshot(self) -> Optional[Snapshot]:
        if self.snapshot_id:
            return self.table.snapshot_by_id(self.snapshot_id)
        return self.table.current_snapshot()

    def projection(self) -> Schema:
        snapshot_schema = self.table.schema()
        if snapshot := self.snapshot():
            if snapshot_schema_id := snapshot.schema_id:
                snapshot_schema = self.table.schemas()[snapshot_schema_id]

        if "*" in self.selected_fields:
            return snapshot_schema

        return snapshot_schema.select(*self.selected_fields, case_sensitive=self.case_sensitive)

    @abstractmethod
    def plan_files(self) -> Iterable[ScanTask]:
        ...

    @abstractmethod
    def to_arrow(self) -> pa.Table:
        ...

    @abstractmethod
    def to_pandas(self, **kwargs: Any) -> pd.DataFrame:
        ...

    def update(self: S, **overrides: Any) -> S:
        """Creates a copy of this table scan with updated fields."""
        return type(self)(**{**self.__dict__, **overrides})

    def use_ref(self: S, name: str) -> S:
        if self.snapshot_id:
            raise ValueError(f"Cannot override ref, already set snapshot id={self.snapshot_id}")
        if snapshot := self.table.snapshot_by_name(name):
            return self.update(snapshot_id=snapshot.snapshot_id)

        raise ValueError(f"Cannot scan unknown ref={name}")

    def select(self: S, *field_names: str) -> S:
        if "*" in self.selected_fields:
            return self.update(selected_fields=field_names)
        return self.update(selected_fields=tuple(set(self.selected_fields).intersection(set(field_names))))

    def filter(self: S, expr: Union[str, BooleanExpression]) -> S:
        return self.update(row_filter=And(self.row_filter, _parse_row_filter(expr)))

    def with_case_sensitive(self: S, case_sensitive: bool = True) -> S:
        return self.update(case_sensitive=case_sensitive)


class ScanTask(ABC):
    pass


@dataclass(init=False)
class FileScanTask(ScanTask):
    file: DataFile
    delete_files: Set[DataFile]
    start: int
    length: int

    def __init__(
        self,
        data_file: DataFile,
        delete_files: Optional[Set[DataFile]] = None,
        start: Optional[int] = None,
        length: Optional[int] = None,
    ) -> None:
        self.file = data_file
        self.delete_files = delete_files or set()
        self.start = start or 0
        self.length = length or data_file.file_size_in_bytes


def _open_manifest(
    io: FileIO,
    manifest: ManifestFile,
    partition_filter: Callable[[DataFile], bool],
    metrics_evaluator: Callable[[DataFile], bool],
) -> List[ManifestEntry]:
    return [
        manifest_entry
        for manifest_entry in manifest.fetch_manifest_entry(io, discard_deleted=True)
        if partition_filter(manifest_entry.data_file) and metrics_evaluator(manifest_entry.data_file)
    ]


def _min_data_file_sequence_number(manifests: List[ManifestFile]) -> int:
    try:
        return min(
            manifest.min_sequence_number or INITIAL_SEQUENCE_NUMBER
            for manifest in manifests
            if manifest.content == ManifestContent.DATA
        )
    except ValueError:
        # In case of an empty iterator
        return INITIAL_SEQUENCE_NUMBER


def _match_deletes_to_datafile(data_entry: ManifestEntry, positional_delete_entries: SortedList[ManifestEntry]) -> Set[DataFile]:
    """This method will check if the delete file is relevant for the data file.

    Using the column metrics to see if the filename is in the lower and upper bound.

    Args:
        data_entry (ManifestEntry): The manifest entry path of the datafile.
        positional_delete_entries (List[ManifestEntry]): All the candidate positional deletes manifest entries.

    Returns:
        A set of files that are relevant for the data file.
    """
    relevant_entries = positional_delete_entries[positional_delete_entries.bisect_right(data_entry) :]

    if len(relevant_entries) > 0:
        evaluator = _InclusiveMetricsEvaluator(POSITIONAL_DELETE_SCHEMA, EqualTo("file_path", data_entry.data_file.file_path))
        return {
            positional_delete_entry.data_file
            for positional_delete_entry in relevant_entries
            if evaluator.eval(positional_delete_entry.data_file)
        }
    else:
        return set()


class DataScan(TableScan):
    def __init__(
        self,
        table: Table,
        row_filter: Union[str, BooleanExpression] = ALWAYS_TRUE,
        selected_fields: Tuple[str, ...] = ("*",),
        case_sensitive: bool = True,
        snapshot_id: Optional[int] = None,
        options: Properties = EMPTY_DICT,
        limit: Optional[int] = None,
    ):
        super().__init__(table, row_filter, selected_fields, case_sensitive, snapshot_id, options, limit)

    def _build_partition_projection(self, spec_id: int) -> BooleanExpression:
        project = inclusive_projection(self.table.schema(), self.table.specs()[spec_id])
        return project(self.row_filter)

    @cached_property
    def partition_filters(self) -> KeyDefaultDict[int, BooleanExpression]:
        return KeyDefaultDict(self._build_partition_projection)

    def _build_manifest_evaluator(self, spec_id: int) -> Callable[[ManifestFile], bool]:
        spec = self.table.specs()[spec_id]
        return visitors.manifest_evaluator(spec, self.table.schema(), self.partition_filters[spec_id], self.case_sensitive)

    def _build_partition_evaluator(self, spec_id: int) -> Callable[[DataFile], bool]:
        spec = self.table.specs()[spec_id]
        partition_type = spec.partition_type(self.table.schema())
        partition_schema = Schema(*partition_type.fields)
        partition_expr = self.partition_filters[spec_id]

        evaluator = visitors.expression_evaluator(partition_schema, partition_expr, self.case_sensitive)
        return lambda data_file: evaluator(data_file.partition)

    def _check_sequence_number(self, min_data_sequence_number: int, manifest: ManifestFile) -> bool:
        """A helper function to make sure that no manifests are loaded that contain deletes that are older than the data.

        Args:
            min_data_sequence_number (int): The minimal sequence number.
            manifest (ManifestFile): A ManifestFile that can be either data or deletes.

        Returns:
            Boolean indicating if it is either a data file, or a relevant delete file.
        """
        return manifest.content == ManifestContent.DATA or (
            # Not interested in deletes that are older than the data
            manifest.content == ManifestContent.DELETES
            and (manifest.sequence_number or INITIAL_SEQUENCE_NUMBER) >= min_data_sequence_number
        )

    def plan_files(self) -> Iterable[FileScanTask]:
        """Plans the relevant files by filtering on the PartitionSpecs.

        Returns:
            List of FileScanTasks that contain both data and delete files.
        """
        snapshot = self.snapshot()
        if not snapshot:
            return iter([])

        io = self.table.io

        # step 1: filter manifests using partition summaries
        # the filter depends on the partition spec used to write the manifest file, so create a cache of filters for each spec id

        manifest_evaluators: Dict[int, Callable[[ManifestFile], bool]] = KeyDefaultDict(self._build_manifest_evaluator)

        manifests = [
            manifest_file
            for manifest_file in snapshot.manifests(io)
            if manifest_evaluators[manifest_file.partition_spec_id](manifest_file)
        ]

        # step 2: filter the data files in each manifest
        # this filter depends on the partition spec used to write the manifest file

        partition_evaluators: Dict[int, Callable[[DataFile], bool]] = KeyDefaultDict(self._build_partition_evaluator)
        metrics_evaluator = _InclusiveMetricsEvaluator(
            self.table.schema(), self.row_filter, self.case_sensitive, self.options.get("include_empty_files") == "true"
        ).eval

        min_data_sequence_number = _min_data_file_sequence_number(manifests)

        data_entries: List[ManifestEntry] = []
        positional_delete_entries = SortedList(key=lambda entry: entry.data_sequence_number or INITIAL_SEQUENCE_NUMBER)

        executor = ExecutorFactory.get_or_create()
        for manifest_entry in chain(
            *executor.map(
                lambda args: _open_manifest(*args),
                [
                    (
                        io,
                        manifest,
                        partition_evaluators[manifest.partition_spec_id],
                        metrics_evaluator,
                    )
                    for manifest in manifests
                    if self._check_sequence_number(min_data_sequence_number, manifest)
                ],
            )
        ):
            data_file = manifest_entry.data_file
            if data_file.content == DataFileContent.DATA:
                data_entries.append(manifest_entry)
            elif data_file.content == DataFileContent.POSITION_DELETES:
                positional_delete_entries.add(manifest_entry)
            elif data_file.content == DataFileContent.EQUALITY_DELETES:
                raise ValueError("PyIceberg does not yet support equality deletes: https://github.com/apache/iceberg/issues/6568")
            else:
                raise ValueError(f"Unknown DataFileContent ({data_file.content}): {manifest_entry}")

        return [
            FileScanTask(
                data_entry.data_file,
                delete_files=_match_deletes_to_datafile(
                    data_entry,
                    positional_delete_entries,
                ),
            )
            for data_entry in data_entries
        ]

    def to_arrow(self) -> pa.Table:
        from pyiceberg.io.pyarrow import project_table

        return project_table(
            self.plan_files(),
            self.table,
            self.row_filter,
            self.projection(),
            case_sensitive=self.case_sensitive,
            limit=self.limit,
        )

    def to_pandas(self, **kwargs: Any) -> pd.DataFrame:
        return self.to_arrow().to_pandas(**kwargs)

    def to_duckdb(self, table_name: str, connection: Optional[DuckDBPyConnection] = None) -> DuckDBPyConnection:
        import duckdb

        con = connection or duckdb.connect(database=":memory:")
        con.register(table_name, self.to_arrow())

        return con

    def to_ray(self) -> ray.data.dataset.Dataset:
        import ray

        return ray.data.from_arrow(self.to_arrow())


class MoveOperation(Enum):
    First = 1
    Before = 2
    After = 3


@dataclass
class Move:
    field_id: int
    op: MoveOperation
    other_field_id: Optional[int] = None


class UpdateSchema:
    _table: Table
    _schema: Schema
    _last_column_id: itertools.count[int]
    _identifier_field_names: List[str]

    _adds: Dict[int, List[NestedField]] = {}
    _updates: Dict[int, NestedField] = {}
    _deletes: Set[int] = set()
    _moves: Dict[int, List[Move]] = {}

    _added_name_to_id: Dict[str, int] = {}
    _id_to_parent: Dict[int, str] = {}
    _allow_incompatible_changes: bool
    _case_sensitive: bool
    _transaction: Optional[Transaction]

    def __init__(
        self,
        schema: Schema,
        table: Table,
        transaction: Optional[Transaction] = None,
        allow_incompatible_changes: bool = False,
        case_sensitive: bool = True,
    ) -> None:
        self._table = table
        self._schema = schema
        self._last_column_id = itertools.count(schema.highest_field_id + 1)
        self._identifier_field_names = schema.column_names

        self._adds = {}
        self._updates = {}
        self._deletes = set()
        self._moves = {}

        self._added_name_to_id = {}
        self._id_to_parent = {}

        self._allow_incompatible_changes = allow_incompatible_changes
        self._case_sensitive = case_sensitive
        self._transaction = transaction

    def __exit__(self, _: Any, value: Any, traceback: Any) -> None:
        """Closes and commits the change."""
        return self.commit()

    def __enter__(self) -> UpdateSchema:
        """Update the table."""
        return self

    def case_sensitive(self, case_sensitive: bool) -> UpdateSchema:
        """Determines if the case of schema needs to be considered when comparing column names.

        Args:
            case_sensitive: When false case is not considered in column name comparisons.

        Returns:
            This for method chaining
        """
        self._case_sensitive = case_sensitive
        return self

    def add_column(
        self, path: Union[str, Tuple[str, ...]], field_type: IcebergType, doc: Optional[str] = None, required: bool = False
    ) -> UpdateSchema:
        """Add a new column to a nested struct or Add a new top-level column.

        Args:
            path: Name for the new column.
            field_type: Type for the new column.
            doc: Documentation string for the new column.
            required: Whether the new column is required.

        Returns:
            This for method chaining.
        """
        path = (path,) if isinstance(path, str) else path

        if "." in path[-1]:
            raise ValueError(f"Cannot add column with ambiguous name: {path[-1]}, provide a tuple instead")

        if required and not self._allow_incompatible_changes:
            # Table format version 1 and 2 cannot add required column because there is no initial value
            raise ValueError(f'Incompatible change: cannot add required column: {".".join(path)}')

        name = path[-1]
        parent = path[:-1]

        full_name = ".".join(path)
        parent_id: int = TABLE_ROOT_ID

        if len(parent) > 0:
            parent_field = self._schema.find_field(".".join(parent), self._case_sensitive)
            parent_type = parent_field.field_type
            if isinstance(parent_type, MapType):
                parent_field = parent_type.value_field
            elif isinstance(parent_type, ListType):
                parent_field = parent_type.element_field

            if not parent_field.field_type.is_struct:
                raise ValueError(f"Cannot add column '{name}' to non-struct type: {'.'.join(parent)}")

            parent_id = parent_field.field_id

        exists = False
        try:
            exists = self._schema.find_field(full_name, self._case_sensitive) is not None
        except ValueError:
            pass

        if exists:
            raise ValueError(f"Cannot add column, name already exists: {full_name}")

        # assign new IDs in order
        new_id = self.assign_new_column_id()

        # update tracking for moves
        self._added_name_to_id[full_name] = new_id

        new_type = assign_fresh_schema_ids(field_type, self.assign_new_column_id)
        field = NestedField(field_id=new_id, name=name, field_type=new_type, required=required, doc=doc)

        self._adds[parent_id] = self._adds.get(parent_id, []) + [field]

        return self

    def delete_column(self, path: Union[str, Tuple[str, ...]]) -> UpdateSchema:
        """Deletes a column from a table.

        Args:
            path: The path to the column.

        Returns:
            The UpdateSchema with the delete operation staged.
        """
        name = (path,) if isinstance(path, str) else path
        full_name = ".".join(name)

        field = self._schema.find_field(full_name, self._case_sensitive)

        if field.field_id in self._adds:
            raise ValueError(f"Cannot delete a column that has additions: {full_name}")
        if field.field_id in self._updates:
            raise ValueError(f"Cannot delete a column that has updates: {full_name}")

        self._deletes.add(field.field_id)

        return self

    def rename_column(self, path_from: Union[str, Tuple[str, ...]], path_to: Union[str, Tuple[str, ...]]) -> UpdateSchema:
        """Updates the name of a column.

        Args:
            path_from: The path to the column to be renamed.
            path_to: The new path of the column.

        Returns:
            The UpdateSchema with the rename operation staged.
        """
        name_from = (path_from,) if isinstance(path_from, str) else path_from
        name_to = (path_to,) if isinstance(path_to, str) else path_to

        full_name_from = ".".join(name_from)
        full_name_to = ".".join(name_to)

        from_field = self._schema.find_field(full_name_from, self._case_sensitive)

        if from_field.field_id in self._deletes:
            raise ValueError(f"Cannot rename a column that will be deleted: {full_name_from}")

        if updated := self._updates.get(from_field.field_id):
            self._updates[from_field.field_id] = NestedField(
                field_id=updated.field_id,
                name=full_name_to,
                field_type=updated.field_type,
                doc=updated.doc,
                required=updated.required,
            )
        else:
            self._updates[from_field.field_id] = NestedField(
                field_id=from_field.field_id,
                name=full_name_to,
                field_type=from_field.field_type,
                doc=from_field.doc,
                required=from_field.required,
            )

        if path_from in self._identifier_field_names:
            self._identifier_field_names.remove(full_name_from)
            self._identifier_field_names.append(full_name_to)

        return self

    def require_column(self, path: Union[str, Tuple[str, ...]]) -> UpdateSchema:
        """Makes a column required.

        This is a breaking change since writers have to make sure that
        this value is not-null.

        Args:
            path: The path to the field

        Returns:
            The UpdateSchema with the requirement change staged.
        """
        self._set_column_requirement(path, True)
        return self

    def make_column_optional(self, path: Union[str, Tuple[str, ...]]) -> UpdateSchema:
        """Makes a column optional.

        Args:
            path: The path to the field.

        Returns:
            The UpdateSchema with the requirement change staged.
        """
        self._set_column_requirement(path, False)
        return self

    def _set_column_requirement(self, path: Union[str, Tuple[str, ...]], required: bool) -> None:
        path = (path,) if isinstance(path, str) else path
        name = ".".join(path)

        field = self._schema.find_field(name)

        if (field.required and required) or (field.optional and not required):
            # if the change is a noop, allow it even if allowIncompatibleChanges is false
            return

        if self._allow_incompatible_changes and not required:
            raise ValueError(f"Cannot change column nullability: {name}: optional -> required")

        if field.field_id in self._deletes:
            raise ValueError(f"Cannot update a column that will be deleted: {name}")

        if updated := self._updates.get(field.field_id):
            self._updates[field.field_id] = NestedField(
                field_id=updated.field_id,
                name=updated.name,
                field_type=updated.field_type,
                doc=updated.doc,
                required=required,
            )
        else:
            self._updates[field.field_id] = NestedField(
                field_id=field.field_id,
                name=field.name,
                field_type=field.field_type,
                doc=field.doc,
                required=required,
            )

    def update_column(self, path: Union[str, Tuple[str, ...]], field_type: IcebergType) -> UpdateSchema:
        """Update the type of column.

        Args:
            path: The path to the field.
            field_type: The new type

        Returns:
            The UpdateSchema with the type update staged.
        """
        path = (path,) if isinstance(path, str) else path
        full_name = ".".join(path)

        field = self._schema.find_field(full_name)

        if field.field_id in self._deletes:
            raise ValueError(f"Cannot update a column that will be deleted: {full_name}")

        if field.field_type == field_type:
            # Nothing changed
            return self

        if updated := self._updates.get(field.field_id):
            self._updates[field.field_id] = NestedField(
                field_id=updated.field_id,
                name=updated.name,
                field_type=field_type,
                doc=updated.doc,
                required=updated.required,
            )
        else:
            self._updates[field.field_id] = NestedField(
                field_id=field.field_id,
                name=field.name,
                field_type=field_type,
                doc=field.doc,
                required=field.required,
            )

        return self

    def update_column_doc(self, path: Union[str, Tuple[str, ...]], doc: str) -> UpdateSchema:
        """Update the documentation of column.

        Args:
            path: The path to the field.
            doc: The new documentation of the column

        Returns:
            The UpdateSchema with the doc update staged.
        """
        path = (path,) if isinstance(path, str) else path
        full_name = ".".join(path)

        field = self._schema.find_field(full_name)

        if field.field_id in self._deletes:
            raise ValueError(f"Cannot update a column that will be deleted: {full_name}")

        if field.doc == doc:
            # Noop
            return self

        if updated := self._updates.get(field.field_id):
            self._updates[field.field_id] = NestedField(
                field_id=updated.field_id,
                name=updated.name,
                field_type=updated.field_type,
                doc=doc,
                required=updated.required,
            )
        else:
            self._updates[field.field_id] = NestedField(
                field_id=field.field_id,
                name=field.name,
                field_type=field.field_type,
                doc=doc,
                required=field.required,
            )

        return self

    def _find_for_move(self, name: str) -> Optional[int]:
        try:
            return self._schema.find_field(name, self._case_sensitive).field_id
        except ValueError:
            pass

        return self._added_name_to_id.get(name)

    def _move(self, full_name: str, move: Move) -> None:
        if parent_name := self._id_to_parent.get(move.field_id):
            parent_field = self._schema.find_field(parent_name)
            if not parent_field.is_struct:
                raise ValueError(f"Cannot move fields in non-struct type: {parent_field}")

            if move.op == MoveOperation.After or move.op == MoveOperation.Before:
                if move.other_field_id is None:
                    raise ValueError("Expected other field when performing before/after move")

                if self._id_to_parent.get(move.field_id) != self._id_to_parent.get(move.other_field_id):
                    raise ValueError(f"Cannot move field {full_name} to a different struct")

            self._moves[parent_field.field_id] = self._moves.get(parent_field.field_id, []) + [move]
        else:
            if move.op == MoveOperation.After or move.op == MoveOperation.Before:
                if move.other_field_id is None:
                    raise ValueError("Expected other field when performing before/after move")

                if self._id_to_parent.get(move.other_field_id) is not None:
                    raise ValueError(f"Cannot move field {full_name} to a different struct")

            self._moves[TABLE_ROOT_ID] = self._moves.get(TABLE_ROOT_ID, []) + [move]

    def move_first(self, path: Union[str, Tuple[str, ...]]) -> UpdateSchema:
        """Moves the field to the first position of the parent struct.

        Args:
            path: The path to the field.

        Returns:
            The UpdateSchema with the move operation staged.
        """
        path = (path,) if isinstance(path, str) else path
        full_name = ".".join(path)

        field_id = self._find_for_move(full_name)

        if field_id is None:
            raise ValueError(f"Cannot move missing column: {full_name}")

        self._move(full_name, Move(field_id=field_id, op=MoveOperation.First))

        return self

    def move_before(self, path: Union[str, Tuple[str, ...]], before_path: Union[str, Tuple[str, ...]]) -> UpdateSchema:
        """Moves the field to before another field.

        Args:
            path: The path to the field.

        Returns:
            The UpdateSchema with the move operation staged.
        """
        path = (path,) if isinstance(path, str) else path
        full_name = ".".join(path)

        field_id = self._find_for_move(full_name)

        if field_id is None:
            raise ValueError(f"Cannot move missing column: {full_name}")

        before_path = (before_path,) if isinstance(before_path, str) else before_path
        before_full_name = ".".join(before_path)
        before_field_id = self._find_for_move(before_full_name)

        if before_field_id is None:
            raise ValueError(f"Cannot move before missing column: {before_full_name}")

        if field_id == before_field_id:
            raise ValueError(f"Cannot move {full_name} before itself")

        self._move(full_name, Move(field_id=field_id, other_field_id=before_field_id, op=MoveOperation.Before))

        return self

    def move_after(self, path: Union[str, Tuple[str, ...]], after_name: Union[str, Tuple[str, ...]]) -> UpdateSchema:
        """Moves the field to after another field.

        Args:
            path: The path to the field.

        Returns:
            The UpdateSchema with the move operation staged.
        """
        path = (path,) if isinstance(path, str) else path
        full_name = ".".join(path)

        field_id = self._find_for_move(full_name)

        if field_id is None:
            raise ValueError(f"Cannot move missing column: {full_name}")

        after_path = (after_name,) if isinstance(after_name, str) else after_name
        after_full_name = ".".join(after_path)
        after_field_id = self._find_for_move(after_full_name)

        if after_field_id is None:
            raise ValueError(f"Cannot move after missing column: {after_full_name}")

        if field_id == after_field_id:
            raise ValueError(f"Cannot move {full_name} after itself")

        self._move(full_name, Move(field_id=field_id, other_field_id=after_field_id, op=MoveOperation.After))

        return self

    def allow_incompatible_changes(self) -> UpdateSchema:
        """Allow incompatible changes to the schema.

        Returns:
            This for method chaining
        """
        self._allow_incompatible_changes = True
        return self

    def commit(self) -> None:
        """Apply the pending changes and commit."""
        new_schema = self._apply()
        updates = [
            AddSchemaUpdate(schema=new_schema, last_column_id=new_schema.highest_field_id),
            SetCurrentSchemaUpdate(schema_id=-1),
        ]
        requirements = [AssertCurrentSchemaId(current_schema_id=self._schema.schema_id)]

        if self._transaction is not None:
            self._transaction._append_updates(*updates)  # pylint: disable=W0212
            self._transaction._append_requirements(*requirements)  # pylint: disable=W0212
        else:
            self._table._do_commit(  # pylint: disable=W0212
                CommitTableRequest(identifier=self._table.identifier[1:], updates=updates, requirements=requirements)
            )

    def _apply(self) -> Schema:
        """Apply the pending changes to the original schema and returns the result.

        Returns:
            the result Schema when all pending updates are applied
        """
        struct = visit(self._schema, _ApplyChanges(self._adds, self._updates, self._deletes, self._moves))
        if struct is None:
            # Should never happen
            raise ValueError("Could not apply changes")

        schema = Schema(*struct.fields)
        for name in self._identifier_field_names:
            try:
                _ = schema.find_field(name)
            except ValueError as e:
                raise ValueError(
                    f"Cannot add field {name} as an identifier field: not found in current schema or added columns"
                ) from e

        return schema

    def assign_new_column_id(self) -> int:
        return next(self._last_column_id)


class _ApplyChanges(SchemaVisitor[Optional[IcebergType]]):
    _adds: Dict[int, List[NestedField]]
    _updates: Dict[int, NestedField]
    _deletes: Set[int]
    _moves: Dict[int, List[Move]]

    def __init__(
        self, adds: Dict[int, List[NestedField]], updates: Dict[int, NestedField], deletes: Set[int], moves: Dict[int, List[Move]]
    ) -> None:
        self._adds = adds
        self._updates = updates
        self._deletes = deletes
        self._moves = moves

    def schema(self, schema: Schema, struct_result: Optional[IcebergType]) -> Optional[IcebergType]:
        if new_fields := _add_fields(struct_result.fields if struct_result else [], self._adds.get(TABLE_ROOT_ID)):
            return StructType(*new_fields)
        else:
            return struct_result

    def struct(self, struct: StructType, field_results: List[Optional[IcebergType]]) -> Optional[IcebergType]:
        has_changes = False
        new_fields = []

        for idx, result_type in enumerate(field_results):
            result_type = field_results[idx]

            # Has been deleted
            if result_type is None:
                has_changes = True
                continue

            field = struct.fields[idx]

            name = field.name
            doc = field.doc
            required = field.required

            # There is an update
            if update := self._updates.get(field):
                name = update.name
                doc = update.doc
                required = update.required

            if field.name == name and field.field_type == result_type and field.required == required and field.doc == doc:
                new_fields.append(field)
            else:
                has_changes = True
                new_fields.append(
                    NestedField(
                        field_id=field.field_id, name=field.name, field_type=result_type, required=field.required, doc=field.doc
                    )
                )

        if has_changes:
            return StructType(*new_fields)

        return struct

    def field(self, field: NestedField, field_result: Optional[IcebergType]) -> Optional[IcebergType]:
        # the API validates deletes, updates, and additions don't conflict handle deletes
        if field.field_id in self._deletes:
            return None

        # handle updates
        if (update := self._updates.get(field.field_id)) and field.field_type != update.field_type:
            return update.field_type

        # handle add & moves
        added = self._adds.get(field.field_id)
        moves = self._moves.get(field.field_id)
        if added is not None or moves is not None:
            if not isinstance(field.field_type, StructType):
                raise ValueError(f"Cannot add fields to non-struct: {field}")

            if new_fields := _add_and_move_fields(field.field_type.fields, added or [], moves or []):
                return StructType(*new_fields)

        return field_result

    def list(self, list_type: ListType, element_result: Optional[IcebergType]) -> Optional[IcebergType]:
        element_type = self.field(list_type.element_field, element_result)
        if element_type is None:
            raise ValueError(f"Cannot delete element type from list: {element_result}")

        return ListType(element_id=list_type.element_id, element=element_type, element_required=list_type.element_required)

    def map(
        self, map_type: MapType, key_result: Optional[IcebergType], value_result: Optional[IcebergType]
    ) -> Optional[IcebergType]:
        key_id: int = map_type.key_field.field_id
        if key_id in self._adds:
            raise ValueError(f"Cannot add fields to map keys: {map_type}")

        value_field: NestedField = map_type.value_field
        value_type = self.field(value_field, value_result)
        if value_type is None:
            raise ValueError(f"Cannot delete value type from map: {value_field}")

        return MapType(
            key_id=map_type.key_id,
            key_type=map_type.key_type,
            value_id=map_type.value_id,
            value_type=value_type,
            value_required=map_type.value_required,
        )

    def primitive(self, primitive: PrimitiveType) -> Optional[IcebergType]:
        return primitive


def _add_fields(fields: Tuple[NestedField, ...], adds: Optional[List[NestedField]]) -> Optional[Tuple[NestedField, ...]]:
    adds = adds or []
    return None if len(adds) == 0 else tuple(*fields, *adds)


def _move_fields(fields: Tuple[NestedField, ...], moves: List[Move]) -> Tuple[NestedField, ...]:
    reordered = list(copy(fields))
    for move in moves:
        # Find the field that we're about to move
        field = next(field for field in reordered if field.field_id == move.field_id)
        # Remove the field that we're about to move from the list
        reordered = [field for field in reordered if field.field_id != move.field_id]

        if move.op == MoveOperation.First:
            reordered = [field] + reordered
        elif move.op == MoveOperation.Before or move.op == MoveOperation.After:
            other_field_id = move.other_field_id
            other_field_pos = next(i for i, field in enumerate(reordered) if field.field_id == other_field_id)
            if move.op == MoveOperation.Before:
                reordered.insert(other_field_pos, field)
            else:
                reordered.insert(other_field_pos + 1, field)
        else:
            raise ValueError(f"Unknown operation: {move.op}")

    return tuple(reordered)


def _add_and_move_fields(
    fields: Tuple[NestedField, ...], adds: List[NestedField], moves: List[Move]
) -> Optional[Tuple[NestedField, ...]]:
    if adds:
        # always apply adds first so that added fields can be moved
        added = _add_fields(fields, adds)
        if moves:
            return _move_fields(added, moves)  # type: ignore
        else:
            return added
    # add fields
    elif moves:
        return _move_fields(fields, moves)
    return None if len(adds) == 0 else tuple(*fields, *adds)

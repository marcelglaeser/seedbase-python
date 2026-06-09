from __future__ import annotations

import ast
import csv
import json
import operator
import random
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

DATA_TYPES = ("string", "int", "float", "bool", "date", "datetime")
MODES = (
    "fixed",
    "enum",
    "random",
    "date_rebase",
    "sequence",
    "reference",
    "derived",
    "masked",
)

DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d")
DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S.%f",
)


@dataclass
class ColumnProfile:
    name: str
    inferred_type: str
    mode: str
    enum_values: list[str] = field(default_factory=list)
    fixed_value: str | None = None
    ref_table: str | None = None
    ref_column: str | None = None
    sequence_start: int = 1
    observed_min: float | int | None = None
    observed_max: float | int | None = None
    unique_count: int = 0
    derived_expression: str | None = None
    mask_kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "inferred_type": self.inferred_type,
            "mode": self.mode,
            "enum_values": self.enum_values,
            "fixed_value": self.fixed_value,
            "ref_table": self.ref_table,
            "ref_column": self.ref_column,
            "sequence_start": self.sequence_start,
            "observed_min": self.observed_min,
            "observed_max": self.observed_max,
            "unique_count": self.unique_count,
            "derived_expression": self.derived_expression,
            "mask_kind": self.mask_kind,
        }


@dataclass
class TableProfile:
    name: str
    row_count: int
    columns: dict[str, ColumnProfile]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "row_count": self.row_count,
            "columns": {key: col.to_dict() for key, col in self.columns.items()},
        }


@dataclass
class DatasetProfile:
    tables: dict[str, TableProfile]

    def to_dict(self) -> dict[str, Any]:
        return {"tables": {key: table.to_dict() for key, table in self.tables.items()}}

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


@dataclass
class GenerationPlan:
    rows_per_table: int
    table_row_counts: dict[str, int]
    rebase_to: str
    delta: timedelta
    table_order: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows_per_table": self.rows_per_table,
            "table_row_counts": self.table_row_counts,
            "rebase_to": self.rebase_to,
            "delta_seconds": int(self.delta.total_seconds()),
            "table_order": self.table_order,
        }


def load_json(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_csv_tables(input_dir: str | Path) -> dict[str, list[dict[str, str | None]]]:
    root = Path(input_dir)
    if not root.exists():
        raise FileNotFoundError(f"Input directory does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Expected a directory, got: {root}")

    tables: dict[str, list[dict[str, str | None]]] = {}
    csv_files = sorted(root.glob("*.csv"))
    if not csv_files:
        raise FileNotFoundError(f"No CSV files found in: {root}")

    for csv_path in csv_files:
        table_name = csv_path.stem
        rows: list[dict[str, str | None]] = []
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise ValueError(f"CSV without header: {csv_path}")
            for raw_row in reader:
                row: dict[str, str | None] = {}
                for key, value in raw_row.items():
                    if key is None:
                        continue
                    row[key] = _clean_value(value)
                rows.append(row)
        tables[table_name] = rows
    return tables


def build_profile(
    tables: dict[str, list[dict[str, str | None]]], config: dict[str, Any] | None = None
) -> DatasetProfile:
    config = config or {}
    table_profiles: dict[str, TableProfile] = {}

    for table_name, rows in tables.items():
        columns = _collect_columns(rows)
        col_profiles: dict[str, ColumnProfile] = {}
        for column_name in columns:
            values = [row.get(column_name) for row in rows]
            non_null = [value for value in values if value is not None]
            inferred_type = _infer_type(non_null)
            unique_values = sorted({value for value in non_null})
            unique_count = len(unique_values)

            observed_min: float | int | None = None
            observed_max: float | int | None = None
            if inferred_type == "int":
                ints = [int(value) for value in non_null]
                if ints:
                    observed_min = min(ints)
                    observed_max = max(ints)
            elif inferred_type == "float":
                floats = [float(value) for value in non_null]
                if floats:
                    observed_min = min(floats)
                    observed_max = max(floats)

            mode = _suggest_mode(
                column_name=column_name,
                inferred_type=inferred_type,
                values=non_null,
                unique_count=unique_count,
            )

            enum_values = unique_values if mode == "enum" else []
            fixed_value = unique_values[0] if mode == "fixed" and unique_values else None
            mask_kind = _mask_kind_for_column(column_name) if mode == "masked" else None
            sequence_start = 1
            if inferred_type == "int":
                ints = [int(value) for value in non_null]
                if ints:
                    sequence_start = min(ints)

            col_profiles[column_name] = ColumnProfile(
                name=column_name,
                inferred_type=inferred_type,
                mode=mode,
                enum_values=enum_values,
                fixed_value=fixed_value,
                sequence_start=sequence_start,
                observed_min=observed_min,
                observed_max=observed_max,
                unique_count=unique_count,
                mask_kind=mask_kind,
            )

        table_profiles[table_name] = TableProfile(
            name=table_name,
            row_count=len(rows),
            columns=col_profiles,
        )

    dataset = DatasetProfile(tables=table_profiles)
    _detect_references(tables, dataset)
    _apply_overrides(dataset, config)
    return dataset


def build_generation_plan(
    tables: dict[str, list[dict[str, str | None]]],
    profile: DatasetProfile,
    rows_per_table: int = 100,
    table_row_counts: dict[str, int] | None = None,
    rebase_to: str = "today",
) -> GenerationPlan:
    counts = dict(table_row_counts or {})
    for table_name in profile.tables:
        counts.setdefault(table_name, rows_per_table)

    delta = _compute_rebase_delta(tables, profile, rebase_to)
    table_order = _topological_table_order(profile)
    return GenerationPlan(
        rows_per_table=rows_per_table,
        table_row_counts=counts,
        rebase_to=rebase_to,
        delta=delta,
        table_order=table_order,
    )


def generate_dataset(
    tables: dict[str, list[dict[str, str | None]]],
    profile: DatasetProfile,
    plan: GenerationPlan,
    seed: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    rng = random.Random(seed)
    generated: dict[str, list[dict[str, Any]]] = {}

    for table_name in plan.table_order:
        table_profile = profile.tables[table_name]
        source_rows = tables.get(table_name, [])
        target_count = plan.table_row_counts.get(table_name, plan.rows_per_table)
        if target_count < 0:
            raise ValueError(f"Negative row count for table {table_name}: {target_count}")

        result_rows: list[dict[str, Any]] = []
        for index in range(target_count):
            source_row = source_rows[index % len(source_rows)] if source_rows else {}
            generated_row: dict[str, Any] = {}
            for column_name, column_profile in table_profile.columns.items():
                if column_profile.mode == "derived":
                    continue
                generated_row[column_name] = _generate_column_value(
                    table_name=table_name,
                    column=column_profile,
                    source_row=source_row,
                    index=index,
                    delta=plan.delta,
                    original_tables=tables,
                    generated_tables=generated,
                    row_context=generated_row,
                    rng=rng,
                )
            for column_name, column_profile in table_profile.columns.items():
                if column_profile.mode != "derived":
                    continue
                generated_row[column_name] = _generate_column_value(
                    table_name=table_name,
                    column=column_profile,
                    source_row=source_row,
                    index=index,
                    delta=plan.delta,
                    original_tables=tables,
                    generated_tables=generated,
                    row_context=generated_row,
                    rng=rng,
                )
            result_rows.append(generated_row)
        generated[table_name] = result_rows
    return generated


def _clean_value(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized == "":
        return None
    if normalized.upper() == "NULL":
        return None
    return normalized


def _collect_columns(rows: list[dict[str, str | None]]) -> list[str]:
    columns: set[str] = set()
    for row in rows:
        columns.update(row.keys())
    return sorted(columns)


def _is_bool(value: str) -> bool:
    return value.lower() in {"true", "false", "0", "1", "yes", "no", "y", "n"}


def _parse_bool(value: str) -> bool:
    return value.lower() in {"true", "1", "yes", "y"}


def _is_int(value: str) -> bool:
    if value.startswith(("+", "-")):
        return value[1:].isdigit()
    return value.isdigit()


def _is_float(value: str) -> bool:
    if _is_int(value):
        return True
    if value.strip().lower().lstrip("+-") in {"nan", "inf", "infinity"}:
        return False
    try:
        float(value)
        return True
    except ValueError:
        return False


def _parse_datetime(value: str) -> datetime | None:
    candidate = value.strip()
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    iso = candidate.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.replace(tzinfo=None)
    return parsed


def _parse_date(value: str) -> date | None:
    candidate = value.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    maybe_dt = _parse_datetime(candidate)
    if maybe_dt is None:
        return None
    if maybe_dt.time().hour == 0 and maybe_dt.time().minute == 0 and maybe_dt.time().second == 0:
        return maybe_dt.date()
    return None


def _infer_type(non_null_values: list[str]) -> str:
    if not non_null_values:
        return "string"

    if all(_is_bool(value) for value in non_null_values):
        return "bool"
    if all(_is_int(value) for value in non_null_values):
        return "int"
    if all(_is_float(value) for value in non_null_values):
        return "float"

    parsed_dates = [_parse_date(value) for value in non_null_values]
    if all(parsed is not None for parsed in parsed_dates):
        return "date"

    parsed_datetimes = [_parse_datetime(value) for value in non_null_values]
    if all(parsed is not None for parsed in parsed_datetimes):
        return "datetime"

    return "string"


def _is_sequence(values: list[str]) -> bool:
    if len(values) < 2:
        return False
    if not all(_is_int(value) for value in values):
        return False
    ints = [int(value) for value in values]
    if len(set(ints)) != len(ints):
        return False
    sorted_ints = sorted(ints)
    expected = list(range(sorted_ints[0], sorted_ints[0] + len(sorted_ints)))
    return sorted_ints == expected


def _is_enum_candidate(sample_size: int, unique_count: int) -> bool:
    if sample_size >= 100:
        return unique_count <= 20
    if sample_size >= 30:
        return unique_count <= 10
    return unique_count <= 5


def _suggest_mode(
    column_name: str,
    inferred_type: str,
    values: list[str],
    unique_count: int,
) -> str:
    lowered = column_name.lower()
    if _mask_kind_for_column(column_name) is not None:
        return "masked"
    if unique_count == 1:
        return "fixed"
    if _is_sequence(values):
        return "sequence"
    if inferred_type in {"date", "datetime"}:
        return "date_rebase"
    if _is_enum_candidate(len(values), unique_count):
        return "enum"
    if inferred_type in {"int", "float"} and any(
        token in lowered for token in ("price", "amount", "total")
    ):
        return "random"
    return "random"


def _mask_kind_for_column(column_name: str) -> str | None:
    lowered = column_name.lower()
    if "email" in lowered or "mail" in lowered:
        return "email"
    if (
        "first_name" in lowered
        or "last_name" in lowered
        or lowered == "name"
        or lowered.endswith("_name")
    ):
        return "name"
    if "phone" in lowered or "tel" in lowered:
        return "phone"
    return None


def _detect_references(
    tables: dict[str, list[dict[str, str | None]]],
    profile: DatasetProfile,
) -> None:
    pk_values: dict[tuple[str, str], set[str]] = {}
    for table_name, table_profile in profile.tables.items():
        rows = tables.get(table_name, [])
        for column_name, column_profile in table_profile.columns.items():
            values = [row.get(column_name) for row in rows]
            non_null = [value for value in values if value is not None]
            if not non_null:
                continue
            if len(set(non_null)) != len(non_null):
                continue
            if column_name == "id" or column_profile.mode == "sequence":
                pk_values[(table_name, column_name)] = set(non_null)

    table_names = set(profile.tables.keys())
    for table_name, table_profile in profile.tables.items():
        rows = tables.get(table_name, [])
        for column_name, column_profile in table_profile.columns.items():
            if not column_name.lower().endswith("_id"):
                continue
            values = [row.get(column_name) for row in rows]
            non_null = [value for value in values if value is not None]
            if not non_null:
                continue

            base_name = column_name[:-3].lower()
            preferred_table_names = _table_name_candidates(base_name)

            matches: list[tuple[int, str, str]] = []
            for (candidate_table, candidate_column), candidate_values in pk_values.items():
                if set(non_null).issubset(candidate_values):
                    score = 0
                    if candidate_table.lower() in preferred_table_names:
                        score += 10
                    if candidate_column == "id":
                        score += 2
                    matches.append((score, candidate_table, candidate_column))

            if not matches:
                continue

            matches.sort(reverse=True)
            _, ref_table, ref_column = matches[0]
            if ref_table not in table_names:
                continue
            column_profile.mode = "reference"
            column_profile.ref_table = ref_table
            column_profile.ref_column = ref_column


def _table_name_candidates(base_name: str) -> set[str]:
    candidates = {base_name}
    if base_name.endswith("y"):
        candidates.add(base_name[:-1] + "ies")
    candidates.add(base_name + "s")
    candidates.add(base_name + "es")
    return candidates


def _apply_overrides(profile: DatasetProfile, config: dict[str, Any]) -> None:
    columns_cfg = config.get("columns", {})
    if not isinstance(columns_cfg, dict):
        return

    for key, override in columns_cfg.items():
        if "." not in key:
            continue
        table_name, column_name = key.split(".", 1)
        table_profile = profile.tables.get(table_name)
        if table_profile is None:
            continue
        column = table_profile.columns.get(column_name)
        if column is None:
            continue
        if not isinstance(override, dict):
            continue

        mode = override.get("mode")
        if isinstance(mode, str) and mode in MODES:
            column.mode = mode
        enum_values = override.get("enum_values")
        if isinstance(enum_values, list):
            column.enum_values = [str(value) for value in enum_values]
        if "fixed_value" in override:
            fixed_value = override.get("fixed_value")
            column.fixed_value = None if fixed_value is None else str(fixed_value)
        ref_table = override.get("ref_table")
        ref_column = override.get("ref_column")
        if isinstance(ref_table, str):
            column.ref_table = ref_table
        if isinstance(ref_column, str):
            column.ref_column = ref_column
        sequence_start = override.get("sequence_start")
        if isinstance(sequence_start, int):
            column.sequence_start = sequence_start
        observed_min = override.get("observed_min")
        observed_max = override.get("observed_max")
        if isinstance(observed_min, (int, float)):
            column.observed_min = observed_min
        if isinstance(observed_max, (int, float)):
            column.observed_max = observed_max
        derived_expression = override.get("derived_expression")
        if isinstance(derived_expression, str):
            column.derived_expression = derived_expression
        mask_kind = override.get("mask_kind")
        if isinstance(mask_kind, str):
            column.mask_kind = mask_kind
        elif column.mode == "masked" and column.mask_kind is None:
            column.mask_kind = _mask_kind_for_column(column.name) or "generic"


def _compute_rebase_delta(
    tables: dict[str, list[dict[str, str | None]]],
    profile: DatasetProfile,
    rebase_to: str,
) -> timedelta:
    candidates: list[datetime] = []
    for table_name, table_profile in profile.tables.items():
        source_rows = tables.get(table_name, [])
        for column_name, column in table_profile.columns.items():
            if column.mode != "date_rebase":
                continue
            for row in source_rows:
                raw = row.get(column_name)
                if raw is None:
                    continue
                parsed = _parse_temporal(raw, column.inferred_type)
                if parsed is None:
                    continue
                candidates.append(parsed)

    if not candidates:
        return timedelta(0)

    reference_dt = max(candidates)
    target_dt = _resolve_target_datetime(reference_dt, rebase_to)
    return target_dt - reference_dt


def _resolve_target_datetime(reference_dt: datetime, rebase_to: str) -> datetime:
    normalized = rebase_to.strip().lower()
    if normalized == "today":
        return datetime.combine(date.today(), reference_dt.time())

    explicit_dt = _parse_datetime(rebase_to)
    if explicit_dt is not None:
        return explicit_dt

    explicit_date = _parse_date(rebase_to)
    if explicit_date is not None:
        return datetime.combine(explicit_date, reference_dt.time())

    raise ValueError(
        "Could not parse --rebase-to value. Use 'today' or a date like YYYY-MM-DD."
    )


def _parse_temporal(value: str, inferred_type: str) -> datetime | None:
    if inferred_type == "date":
        parsed_date = _parse_date(value)
        if parsed_date is None:
            return None
        return datetime.combine(parsed_date, datetime.min.time())
    parsed_dt = _parse_datetime(value)
    if parsed_dt is not None:
        return parsed_dt
    parsed_date = _parse_date(value)
    if parsed_date is not None:
        return datetime.combine(parsed_date, datetime.min.time())
    return None


def _topological_table_order(profile: DatasetProfile) -> list[str]:
    dependencies: dict[str, set[str]] = {}
    for table_name, table_profile in profile.tables.items():
        refs = {
            column.ref_table
            for column in table_profile.columns.values()
            if column.mode == "reference" and column.ref_table
        }
        dependencies[table_name] = {ref for ref in refs if ref != table_name}

    order: list[str] = []
    queue = sorted([table for table, deps in dependencies.items() if not deps])
    visited: set[str] = set()

    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        order.append(current)
        for table_name, deps in dependencies.items():
            if current in deps:
                deps.remove(current)
                if not deps and table_name not in visited:
                    queue.append(table_name)
        queue.sort()

    remaining = sorted([table for table in dependencies if table not in visited])
    return order + remaining


def _generate_column_value(
    table_name: str,
    column: ColumnProfile,
    source_row: dict[str, str | None],
    index: int,
    delta: timedelta,
    original_tables: dict[str, list[dict[str, str | None]]],
    generated_tables: dict[str, list[dict[str, Any]]],
    row_context: dict[str, Any],
    rng: random.Random,
) -> Any:
    raw_source = source_row.get(column.name)
    mode = column.mode

    if mode == "fixed":
        if column.fixed_value is not None:
            return _cast_value(column.fixed_value, column.inferred_type)
        return _cast_value(raw_source, column.inferred_type)

    if mode == "enum":
        pool = column.enum_values or ([raw_source] if raw_source is not None else [])
        if not pool:
            return None
        choice = rng.choice(pool)
        return _cast_value(choice, column.inferred_type)

    if mode == "sequence":
        return column.sequence_start + index

    if mode == "reference":
        candidates = _reference_values(
            table_name=table_name,
            column=column,
            original_tables=original_tables,
            generated_tables=generated_tables,
        )
        if candidates:
            return rng.choice(candidates)
        return _cast_value(raw_source, column.inferred_type)

    if mode == "date_rebase":
        if raw_source is None:
            return None
        parsed = _parse_temporal(raw_source, column.inferred_type)
        if parsed is None:
            return raw_source
        rebased = parsed + delta
        return _format_temporal_like_source(rebased, raw_source, column.inferred_type)

    if mode == "masked":
        return _masked_value(column, raw_source, index, rng)

    if mode == "derived":
        return _derived_value(column, source_row, row_context)

    return _random_value(column, raw_source, index, rng)


def _reference_values(
    table_name: str,
    column: ColumnProfile,
    original_tables: dict[str, list[dict[str, str | None]]],
    generated_tables: dict[str, list[dict[str, Any]]],
) -> list[Any]:
    if not column.ref_table or not column.ref_column:
        return []
    if column.ref_table == table_name:
        return []

    generated_rows = generated_tables.get(column.ref_table, [])
    if generated_rows:
        values = [
            row.get(column.ref_column)
            for row in generated_rows
            if row.get(column.ref_column) is not None
        ]
        if values:
            return values

    original_rows = original_tables.get(column.ref_table, [])
    return [
        _cast_value(row.get(column.ref_column), column.inferred_type)
        for row in original_rows
        if row.get(column.ref_column) is not None
    ]


def _masked_value(
    column: ColumnProfile,
    raw_source: str | None,
    index: int,
    rng: random.Random,
) -> Any:
    kind = (column.mask_kind or _mask_kind_for_column(column.name) or "generic").lower()
    if kind == "email":
        return f"user{index + 1}{rng.randint(100, 999)}@example.com"
    if kind == "name":
        first_names = ["Alex", "Jamie", "Taylor", "Sam", "Jordan", "Chris", "Robin", "Casey"]
        last_names = ["Miller", "Brown", "Walker", "Bauer", "Smith", "Schmidt", "Taylor", "Garcia"]
        return f"{rng.choice(first_names)} {rng.choice(last_names)}"
    if kind == "phone":
        return f"+49-{rng.randint(100,999)}-{rng.randint(1000000,9999999)}"
    if raw_source:
        cleaned = re.sub(r"\w", "x", str(raw_source))
        return f"{cleaned}_{index + 1}"
    return f"masked_{column.name}_{index + 1}"


def _derived_value(
    column: ColumnProfile,
    source_row: dict[str, str | None],
    row_context: dict[str, Any],
) -> Any:
    expression = column.derived_expression
    if expression:
        return _safe_eval_expression(expression, row_context)
    if source_row.get(column.name) is not None:
        return _cast_value(source_row[column.name], column.inferred_type)
    return None


def _safe_eval_expression(expression: str, context: dict[str, Any]) -> Any:
    tree = ast.parse(expression, mode="eval")
    return _safe_eval_node(tree.body, context)


def _safe_eval_node(node: ast.AST, context: dict[str, Any]) -> Any:
    binary_ops = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }
    unary_ops = {
        ast.UAdd: operator.pos,
        ast.USub: operator.neg,
    }
    allowed_functions = {
        "round": round,
        "int": int,
        "float": float,
        "abs": abs,
        "max": max,
        "min": min,
    }

    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in context:
            raise ValueError(f"Unknown variable in derived expression: {node.id}")
        return context[node.id]
    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in binary_ops:
            raise ValueError("Unsupported operator in derived expression")
        left = _safe_eval_node(node.left, context)
        right = _safe_eval_node(node.right, context)
        return binary_ops[op_type](left, right)
    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in unary_ops:
            raise ValueError("Unsupported unary operator in derived expression")
        operand = _safe_eval_node(node.operand, context)
        return unary_ops[op_type](operand)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in allowed_functions:
            raise ValueError("Unsupported function in derived expression")
        args = [_safe_eval_node(arg, context) for arg in node.args]
        return allowed_functions[node.func.id](*args)
    raise ValueError("Unsupported expression in derived expression")


def _random_value(
    column: ColumnProfile,
    raw_source: str | None,
    index: int,
    rng: random.Random,
) -> Any:
    inferred_type = column.inferred_type
    if inferred_type == "int":
        low = int(column.observed_min) if column.observed_min is not None else 0
        high = int(column.observed_max) if column.observed_max is not None else max(low + 10, 10)
        if high < low:
            low, high = high, low
        return rng.randint(low, high)

    if inferred_type == "float":
        low = float(column.observed_min) if column.observed_min is not None else 0.0
        high = float(column.observed_max) if column.observed_max is not None else max(low + 10.0, 10.0)
        if high < low:
            low, high = high, low
        return round(rng.uniform(low, high), 2)

    if inferred_type == "bool":
        if raw_source is None:
            return rng.choice([True, False])
        return rng.choice([True, False, _parse_bool(raw_source)])

    if inferred_type in {"date", "datetime"}:
        base = _parse_temporal(raw_source, inferred_type) if raw_source else None
        if base is None:
            base = datetime.combine(date.today(), datetime.min.time())
        shift_days = rng.randint(-14, 14)
        shifted = base + timedelta(days=shift_days)
        return _format_temporal_like_source(shifted, raw_source or "", inferred_type)

    if raw_source is None:
        return f"{column.name}_{index + 1}"
    return f"{raw_source}_{rng.randint(100, 999)}"


def _cast_value(value: Any, inferred_type: str) -> Any:
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        return value
    value = str(value)
    if inferred_type == "int" and _is_int(value):
        return int(value)
    if inferred_type == "float" and _is_float(value):
        return float(value)
    if inferred_type == "bool" and _is_bool(value):
        return _parse_bool(value)
    return value


def _format_temporal_like_source(
    value: datetime,
    source_text: str,
    inferred_type: str,
) -> str:
    if inferred_type == "date":
        return value.date().isoformat()

    if "T" in source_text:
        if "." in source_text:
            return value.strftime("%Y-%m-%dT%H:%M:%S.%f")
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    if "." in source_text:
        return value.strftime("%Y-%m-%d %H:%M:%S.%f")
    return value.strftime("%Y-%m-%d %H:%M:%S")

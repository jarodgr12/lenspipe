#!/usr/bin/env python3
"""Input discovery and validation for DifMAP Spectral Pipeline Stage 3."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

STAGE2_MANIFEST_SUFFIX = ".stage2.manifest.json"


@dataclass(frozen=True)
class Stage2Dataset:
    """One manifest-defined Stage 2 spectral product."""

    project_root: Path
    source: str
    epoch: str
    observation_prefix: str
    product_tag: str
    prefix: str
    stage2_directory: Path
    manifest_json: Path
    spectrum_csv: Path
    metadata_json: Path
    model_json: Path | None
    output_directory: Path


def safe_label(name: str) -> str:
    """Apply the same CSV-label normalization used by Stage 2."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", str(name).strip()).strip("_")
    if not cleaned:
        raise ValueError(f"Invalid empty label derived from: {name!r}")
    return cleaned


def _load_json_object(path: Path, description: str) -> dict:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {description}: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{description} must contain a JSON object: {path}")
    return payload


def _resolve_manifest_file(
    stage2_directory: Path,
    value: object,
    field: str,
    manifest_path: Path,
) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"Stage 2 manifest field {field!r} is missing or invalid: "
            f"{manifest_path}"
        )
    candidate = Path(value)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (stage2_directory / candidate).resolve()

    # Stage 2 v1.2 writes product files inside the epoch directory. Refuse a
    # relative path that escapes that directory through '..'.
    if not candidate.is_absolute():
        try:
            resolved.relative_to(stage2_directory.resolve())
        except ValueError as exc:
            raise ValueError(
                f"Stage 2 manifest field {field!r} escapes its epoch directory: "
                f"{manifest_path}"
            ) from exc
    return resolved


def _derive_product_tag(
    manifest_path: Path,
    source: str,
    epoch: str,
) -> tuple[str, str, str]:
    observation_prefix = f"{source}.{epoch}"
    filename = manifest_path.name
    if not filename.endswith(STAGE2_MANIFEST_SUFFIX):
        raise ValueError(f"Not a Stage 2 manifest filename: {manifest_path}")

    file_prefix = filename[: -len(STAGE2_MANIFEST_SUFFIX)]
    expected_start = f"{observation_prefix}."
    if not file_prefix.startswith(expected_start):
        raise ValueError(
            "Stage 2 manifest filename is inconsistent with its source/epoch: "
            f"{manifest_path.name} versus {observation_prefix}"
        )

    product_tag = file_prefix[len(expected_start):]
    if not product_tag or "/" in product_tag or "\\" in product_tag:
        raise ValueError(f"Invalid Stage 2 product tag in {manifest_path.name}")
    return observation_prefix, product_tag, file_prefix


def _expected_product_family(metadata: dict) -> str:
    mode = str(metadata.get("mode", "")).lower()
    if mode == "channel":
        return "channel"
    if mode == "if":
        try:
            channels_per_if = int(metadata["channels_per_if"])
            excluded = int(metadata.get("excluded_edge_channels_per_side", 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "Stage 2 IF metadata lacks valid channels_per_if or "
                "excluded_edge_channels_per_side."
            ) from exc
        return f"if{channels_per_if}_edge{excluded}"
    raise ValueError(f"Unsupported or missing Stage 2 mode: {metadata.get('mode')!r}")


def _dataset_from_manifest(project_root: Path, manifest_path: Path) -> Stage2Dataset:
    stage2_directory = manifest_path.parent.resolve()
    manifest = _load_json_object(manifest_path, "Stage 2 manifest")

    if manifest.get("stage") != 2:
        raise ValueError(f"Manifest is not a Stage 2 product: {manifest_path}")

    source = str(manifest.get("source", "")).strip()
    epoch = str(manifest.get("epoch", "")).strip()
    if not source or not epoch:
        raise ValueError(f"Stage 2 manifest lacks source or epoch: {manifest_path}")

    observation_prefix, product_tag, product_prefix = _derive_product_tag(
        manifest_path, source, epoch
    )

    expected_directory_name = observation_prefix
    if stage2_directory.name != expected_directory_name:
        raise ValueError(
            "Stage 2 manifest is not inside its expected epoch directory: "
            f"expected {expected_directory_name!r}, found {stage2_directory.name!r}"
        )

    spectrum_csv = _resolve_manifest_file(
        stage2_directory, manifest.get("spectrum"), "spectrum", manifest_path
    )
    metadata_json = _resolve_manifest_file(
        stage2_directory, manifest.get("metadata"), "metadata", manifest_path
    )
    if not spectrum_csv.is_file():
        raise FileNotFoundError(
            f"Stage 2 spectrum referenced by manifest is missing: {spectrum_csv}"
        )
    if not metadata_json.is_file():
        raise FileNotFoundError(
            f"Stage 2 metadata referenced by manifest is missing: {metadata_json}"
        )

    metadata = _load_json_object(metadata_json, "Stage 2 metadata")
    if metadata.get("stage") != 2:
        raise ValueError(f"Metadata is not a Stage 2 product: {metadata_json}")
    if str(metadata.get("source", "")) != source or str(metadata.get("epoch", "")) != epoch:
        raise ValueError(
            "Stage 2 manifest and metadata source/epoch disagree: "
            f"{manifest_path} and {metadata_json}"
        )

    product_family = _expected_product_family(metadata)
    if product_family == "channel":
        if product_tag != "channel" and not product_tag.startswith("channel_"):
            raise ValueError(
                f"Product tag {product_tag!r} is inconsistent with channel metadata."
            )
    elif product_tag != product_family:
        raise ValueError(
            f"Product tag {product_tag!r} is inconsistent with metadata; "
            f"expected {product_family!r}."
        )

    possible_model_json = (
        project_root / "stage1" / observation_prefix / f"{observation_prefix}.model.json"
    ).resolve()

    return Stage2Dataset(
        project_root=project_root,
        source=source,
        epoch=epoch,
        observation_prefix=observation_prefix,
        product_tag=product_tag,
        prefix=product_prefix,
        stage2_directory=stage2_directory,
        manifest_json=manifest_path.resolve(),
        spectrum_csv=spectrum_csv,
        metadata_json=metadata_json,
        model_json=possible_model_json if possible_model_json.is_file() else None,
        output_directory=(
            project_root / "stage3" / observation_prefix / product_tag
        ).resolve(),
    )


def discover_stage2_datasets(
    project_root: Path,
    requested_epochs: set[str] | None = None,
    requested_products: set[str] | None = None,
) -> list[Stage2Dataset]:
    """Discover manifest-defined Stage 2 products.

    When no product is requested, discovery is allowed only if the matching
    manifests contain one unique product tag. This prevents an accidental mix
    of channel and IF analyses.
    """
    project_root = project_root.resolve()
    stage2_root = project_root / "stage2"
    if not stage2_root.is_dir():
        raise FileNotFoundError(f"Stage 2 directory not found: {stage2_root}")

    candidates: list[Stage2Dataset] = []
    errors: list[str] = []
    for manifest_path in sorted(stage2_root.glob(f"*/*{STAGE2_MANIFEST_SUFFIX}")):
        try:
            dataset = _dataset_from_manifest(project_root, manifest_path)
        except Exception as exc:
            errors.append(f"{manifest_path}: {exc}")
            continue
        if requested_epochs and dataset.epoch not in requested_epochs:
            continue
        candidates.append(dataset)

    if not candidates:
        selection = []
        if requested_epochs:
            selection.append(f"epochs={sorted(requested_epochs)}")
        if requested_products:
            selection.append(f"products={sorted(requested_products)}")
        detail = f" ({', '.join(selection)})" if selection else ""
        if errors:
            raise FileNotFoundError(
                f"No valid Stage 2 manifests found under {stage2_root}{detail}. "
                f"First manifest error: {errors[0]}"
            )
        raise FileNotFoundError(
            f"No Stage 2 manifests found under {stage2_root}{detail}."
        )

    available_products = sorted({dataset.product_tag for dataset in candidates})
    if requested_products:
        missing = sorted(set(requested_products) - set(available_products))
        if missing:
            raise FileNotFoundError(
                f"Requested Stage 2 product(s) not found: {missing}. "
                f"Available products: {available_products}"
            )
        candidates = [
            dataset for dataset in candidates
            if dataset.product_tag in requested_products
        ]
    elif len(available_products) > 1:
        raise ValueError(
            "Multiple Stage 2 products are available: "
            f"{available_products}. Select one with --product PRODUCT."
        )

    # There must be no duplicate manifest identity for one source/epoch/product.
    identities: dict[tuple[str, str, str], Path] = {}
    for dataset in candidates:
        identity = (dataset.source, dataset.epoch, dataset.product_tag)
        previous = identities.get(identity)
        if previous is not None:
            raise ValueError(
                "Duplicate Stage 2 product identity for "
                f"{identity}: {previous} and {dataset.manifest_json}"
            )
        identities[identity] = dataset.manifest_json

    return sorted(
        candidates,
        key=lambda item: (item.source, item.epoch, item.product_tag),
    )


def available_stage2_products(
    project_root: Path,
    requested_epochs: set[str] | None = None,
) -> dict[str, list[str]]:
    """Return available product tags keyed by ``source.epoch``."""
    project_root = project_root.resolve()
    stage2_root = project_root / "stage2"
    if not stage2_root.is_dir():
        raise FileNotFoundError(f"Stage 2 directory not found: {stage2_root}")

    products: dict[str, list[str]] = defaultdict(list)
    first_error: str | None = None
    for manifest_path in sorted(stage2_root.glob(f"*/*{STAGE2_MANIFEST_SUFFIX}")):
        try:
            dataset = _dataset_from_manifest(project_root, manifest_path)
        except Exception as exc:
            # Old pre-v1.2 manifests may coexist with the new product-tagged
            # files. Ignore them when valid v1.2 products are also present.
            if first_error is None:
                first_error = f"{manifest_path}: {exc}"
            continue
        if requested_epochs and dataset.epoch not in requested_epochs:
            continue
        products[dataset.observation_prefix].append(dataset.product_tag)

    if not products:
        detail = f" First manifest error: {first_error}" if first_error else ""
        raise FileNotFoundError(
            f"No valid Stage 2 products found under {stage2_root}.{detail}"
        )
    return {
        observation: sorted(set(tags))
        for observation, tags in sorted(products.items())
    }


def load_stage2_metadata(dataset: Stage2Dataset) -> dict:
    payload = _load_json_object(dataset.metadata_json, "Stage 2 metadata")
    if payload.get("stage") != 2:
        raise ValueError(f"Not Stage 2 metadata: {dataset.metadata_json}")
    if str(payload.get("source", "")) != dataset.source:
        raise ValueError(f"Stage 2 metadata source mismatch: {dataset.metadata_json}")
    if str(payload.get("epoch", "")) != dataset.epoch:
        raise ValueError(f"Stage 2 metadata epoch mismatch: {dataset.metadata_json}")
    if not isinstance(payload.get("groups"), dict) or not payload["groups"]:
        raise ValueError(
            "Stage 2 metadata contains no valid group hierarchy: "
            f"{dataset.metadata_json}"
        )
    _expected_product_family(payload)
    return payload


def _validate_constant_column(
    frame: pd.DataFrame,
    column: str,
    expected: str,
    csv_path: Path,
) -> None:
    if column not in frame.columns:
        return
    values = {
        str(value).strip()
        for value in frame[column].dropna().unique()
        if str(value).strip()
    }
    if values and values != {expected}:
        raise ValueError(
            f"{csv_path} has inconsistent {column} values: {sorted(values)}; "
            f"expected {expected!r}."
        )


def load_spectrum(
    dataset: Stage2Dataset,
    metadata: dict | None = None,
    excluded_fit_indices: set[int] | None = None,
) -> pd.DataFrame:
    frame = pd.read_csv(dataset.spectrum_csv, dtype=str)
    required = {"frequency_ghz", "rms_jy_per_beam", "fit_status"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(
            f"{dataset.spectrum_csv} lacks required columns: {sorted(missing)}"
        )

    _validate_constant_column(frame, "source", dataset.source, dataset.spectrum_csv)
    _validate_constant_column(frame, "epoch", dataset.epoch, dataset.spectrum_csv)
    if metadata is not None:
        _validate_constant_column(
            frame, "mode", str(metadata.get("mode", "")), dataset.spectrum_csv
        )

    frame = frame.loc[frame["fit_status"].astype(str).str.lower() == "ok"].copy()
    frame["frequency_ghz"] = pd.to_numeric(
        frame["frequency_ghz"], errors="coerce"
    )
    frame["rms_jy_per_beam"] = pd.to_numeric(
        frame["rms_jy_per_beam"], errors="coerce"
    )

    if "fit_index" in frame.columns:
        frame["fit_index"] = pd.to_numeric(frame["fit_index"], errors="coerce")
    elif excluded_fit_indices:
        raise ValueError(
            f"{dataset.spectrum_csv} does not contain fit_index, so channel "
            "exclusions cannot be applied."
        )

    if excluded_fit_indices:
        if frame["fit_index"].isna().any():
            raise ValueError(
                f"{dataset.spectrum_csv} contains non-numeric fit_index values, "
                "so channel exclusions cannot be applied."
            )
        frame = frame.loc[
            ~frame["fit_index"].astype(int).isin(sorted(excluded_fit_indices))
        ].copy()

    required_numeric = ["frequency_ghz", "rms_jy_per_beam"]
    if "fit_index" in frame.columns:
        required_numeric.append("fit_index")
    frame = frame.dropna(subset=required_numeric)
    frame = frame.loc[
        (frame["frequency_ghz"] > 0) & (frame["rms_jy_per_beam"] > 0)
    ].sort_values("frequency_ghz")
    if frame.empty:
        raise ValueError(f"No valid Stage 2 measurements in {dataset.spectrum_csv}")
    return frame.reset_index(drop=True)


def grouped_flux_columns(
    metadata: dict,
    frame: pd.DataFrame,
) -> list[tuple[str, str]]:
    groups = list(metadata["groups"].keys())
    columns: list[tuple[str, str]] = []
    used_columns: set[str] = set()
    for group in groups:
        column = f"{safe_label(group)}_jy"
        if column in used_columns:
            raise ValueError(
                f"Stage 2 group labels collide after sanitization at {column!r}."
            )
        if column not in frame.columns:
            raise ValueError(
                f"Grouped flux column {column!r} for group {group!r} is absent "
                "from the Stage 2 CSV."
            )
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if frame[column].notna().sum() < 3:
            raise ValueError(
                f"Grouped flux column {column!r} has fewer than three finite values."
            )
        columns.append((str(group), column))
        used_columns.add(column)
    return columns

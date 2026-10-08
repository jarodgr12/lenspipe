"""Clean and residual map figures from the Stage 1 FITS images.

Stage 1 leaves two images per epoch: the restored (clean) map and the residual
map that DifMAP's ``wdmap`` writes. Stage 3 shows a cutout of each around a
chosen centre with the restoring beam drawn: one A4 page per map per visit,
and in the combined product A4 pages per map kind with every visit on them.
Axes are offsets from the cutout centre in arcseconds (east to the left). The
clean and residual maps have their own colour-scale percentile; on the
all-epochs pages one visit, the reference, fixes the scale for every panel so
the same colour means the same flux throughout. No APLpy: astropy's WCS and
Cutout2D do the geometry, matplotlib the drawing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.nddata import Cutout2D
from astropy.wcs import WCS
from astropy.wcs.utils import proj_plane_pixel_scales
from matplotlib import pyplot as plt
from matplotlib.patches import Ellipse

from lenspipe.stage3 import plotting

__all__ = [
    "A4_LANDSCAPE",
    "A4_PORTRAIT",
    "ImageSettings",
    "MapImage",
    "beam_from_header",
    "colour_limits",
    "load_map",
    "parse_center",
    "stage1_images_for",
    "write_all_epochs_images",
    "write_visit_images",
]

BEAM_KEYS = ("BMAJ", "BMIN", "BPA")
A4_PORTRAIT = (8.27, 11.69)  # inches
A4_LANDSCAPE = (11.69, 8.27)
GRID_COLUMNS = 2  # visits per row on the all-epochs pages
GRID_ROWS = 3  # rows per page, so six visits per A4 portrait page
KINDS = ("clean", "residual")


@dataclass(frozen=True)
class ImageSettings:
    """What to show: a cutout of ``size_arcsec`` around ``center`` with percentile colour scales."""

    enabled: bool = True
    center: str | None = None  # None: image centre; "ra_deg,dec_deg"; or "15h58m00s +37d20m00s"
    size_arcsec: tuple[float, float] = (2.0, 2.0)  # width, height
    cmap: str = "viridis"
    pmax: float = 99.5  # clean map
    residual_pmax: float = 99.5  # residual map
    vmin: float | None = 0.0
    reference_epoch: str | None = None  # visit fixing the shared scale on the all-epochs pages
    transparent_all_epochs: bool = True

    @classmethod
    def from_config(cls, config: Any) -> ImageSettings:
        size = config.size_arcsec
        if isinstance(size, (int, float)):
            pair = (float(size), float(size))
        else:
            values = [float(v) for v in size]
            pair = (values[0], values[0]) if len(values) == 1 else (values[0], values[1])
        reference = getattr(config, "reference_epoch", None)
        return cls(
            enabled=bool(config.enabled),
            center=(str(config.center).strip() or None) if config.center is not None else None,
            size_arcsec=pair,
            cmap=str(config.cmap),
            pmax=float(config.pmax),
            residual_pmax=float(config.residual_pmax),
            vmin=None if config.vmin is None else float(config.vmin),
            reference_epoch=(str(reference).strip() or None) if reference is not None else None,
            transparent_all_epochs=bool(getattr(config, "transparent_all_epochs", True)),
        )

    def pmax_for(self, kind: str) -> float:
        return self.residual_pmax if kind == "residual" else self.pmax

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "center": self.center,
            "size_arcsec": list(self.size_arcsec),
            "cmap": self.cmap,
            "pmax": self.pmax,
            "residual_pmax": self.residual_pmax,
            "vmin": self.vmin,
            "reference_epoch": self.reference_epoch,
            "transparent_all_epochs": self.transparent_all_epochs,
            "axes": "offsets from the cutout centre in arcsec, east to the left",
            "page_inches": {"visit": list(A4_PORTRAIT), "all_epochs": list(A4_PORTRAIT)},
        }


@dataclass
class MapImage:
    data: np.ndarray  # 2-D, Dec along axis 0
    wcs: WCS  # celestial only
    beam: tuple[float, float, float] | None  # BMAJ, BMIN, BPA in degrees
    bunit: str
    center_pixel: tuple[float, float] | None = None  # (x, y) of the reference point, zero-based pixels

    @property
    def pixel_scale_deg(self) -> float:
        scales = proj_plane_pixel_scales(self.wcs)
        return float(np.mean(np.abs(scales)))

    @property
    def pixel_scale_arcsec(self) -> float:
        return self.pixel_scale_deg * 3600.0

    def offset_extent_arcsec(self) -> tuple[float, float, float, float]:
        """imshow extent (left, right, bottom, top) in arcsec offsets from ``center_pixel``.

        Right ascension grows towards the left of the sky image, so the left
        edge carries the positive offset.
        """
        ny, nx = self.data.shape
        cx, cy = self.center_pixel if self.center_pixel is not None else ((nx - 1) / 2.0, (ny - 1) / 2.0)
        s = self.pixel_scale_arcsec
        return ((cx + 0.5) * s, (cx - nx + 0.5) * s, (-0.5 - cy) * s, (ny - 0.5 - cy) * s)


def beam_from_header(header: fits.Header) -> tuple[float, float, float] | None:
    if all(key in header for key in BEAM_KEYS):
        return float(header["BMAJ"]), float(header["BMIN"]), float(header["BPA"])
    return None


def load_map(path: Path) -> MapImage:
    """Read a DifMAP map (RA, Dec, and degenerate FREQ/STOKES axes) as a 2-D celestial image."""
    with fits.open(path, memmap=False) as hdul:
        header = hdul[0].header
        data = np.squeeze(np.asarray(hdul[0].data, dtype=float))
    if data.ndim != 2:
        raise ValueError(f"{path.name}: expected a 2-D image after dropping degenerate axes, got {data.shape}")
    return MapImage(
        data=data,
        wcs=WCS(header).celestial,
        beam=beam_from_header(header),
        bunit=str(header.get("BUNIT", "Jy/beam")),
    )


def parse_center(text: str | None, image: MapImage) -> SkyCoord:
    """None -> image centre; "ra_deg,dec_deg" -> degrees; anything else -> astropy's string parser."""
    if text is None or not str(text).strip():
        ny, nx = image.data.shape
        return image.wcs.pixel_to_world(nx / 2.0, ny / 2.0)
    text = str(text).strip()
    parts = [p.strip() for p in text.split(",")]
    if len(parts) == 2:
        try:
            return SkyCoord(float(parts[0]) * u.deg, float(parts[1]) * u.deg, frame="icrs")
        except ValueError:
            pass
    return SkyCoord(text, frame="icrs", unit=(u.hourangle, u.deg))


def cutout(image: MapImage, center: SkyCoord, size_arcsec: tuple[float, float]) -> MapImage:
    width, height = size_arcsec
    cut = Cutout2D(
        image.data, position=center, size=(height * u.arcsec, width * u.arcsec), wcs=image.wcs,
        mode="partial", fill_value=np.nan,
    )
    cx, cy = cut.input_position_cutout
    return MapImage(
        data=cut.data, wcs=cut.wcs, beam=image.beam, bunit=image.bunit, center_pixel=(float(cx), float(cy))
    )


def colour_limits(data: np.ndarray, pmax: float, vmin: float | None) -> tuple[float, float]:
    """(vmin, vmax): the top from the ``pmax`` percentile, the bottom fixed or from ``100 - pmax``."""
    finite = data[np.isfinite(data)]
    if finite.size == 0:
        return 0.0, 1.0
    vmax = float(np.percentile(finite, pmax))
    low = float(np.percentile(finite, 100.0 - pmax)) if vmin is None else vmin
    if not vmax > low:
        vmax = low + (abs(low) or 1.0) * 1e-6
    return low, vmax


def draw_map(
    ax, image: MapImage, settings: ImageSettings, title: str, kind: str, *,
    limits: tuple[float, float] | None = None, label_x: bool = True, label_y: bool = True,
) -> None:
    """One panel in arcsec offsets, the beam in the lower left, a colour bar in the image units.

    ``limits`` fixes the colour scale (shared across an all-epochs page); without
    it the panel's own percentiles are used. In a grid only the outer panels
    carry axis labels, otherwise a label runs into the neighbouring colour bar.
    """
    vmin, vmax = limits if limits is not None else colour_limits(image.data, settings.pmax_for(kind), settings.vmin)
    left, right, bottom, top = image.offset_extent_arcsec()
    shown = ax.imshow(
        image.data, origin="lower", cmap=settings.cmap, vmin=vmin, vmax=vmax, interpolation="nearest",
        extent=(left, right, bottom, top), aspect="equal",
    )
    ax.set_xlim(left, right)
    ax.set_ylim(bottom, top)
    ax.set_title(title)
    ax.set_xlabel(r"$\Delta$RA [arcsec]" if label_x else " ")
    ax.set_ylabel(r"$\Delta$Dec [arcsec]" if label_y else " ")
    ax.tick_params(labelsize=8, direction="in", top=True, right=True)
    if image.beam is not None:
        bmaj, bmin, bpa = (value * 3600.0 for value in image.beam)
        width_arcsec = abs(left - right)
        height_arcsec = abs(top - bottom)
        # Lower-left corner of the panel: the left edge carries the larger (eastern) offset.
        x0 = max(left, right) - 0.1 * width_arcsec - bmaj / 2.0
        y0 = bottom + 0.1 * height_arcsec + bmaj / 2.0
        # The x axis runs east to the left (decreasing to the right), so an ellipse
        # drawn in data units already has north up and east left; BPA is from north
        # through east, i.e. counter-clockwise from the vertical on this axis.
        ax.add_patch(
            Ellipse((x0, y0), width=bmin, height=bmaj, angle=bpa, facecolor="none", edgecolor="white", linewidth=1.2)
        )
    bar = ax.figure.colorbar(shown, ax=ax, pad=0.02, fraction=0.046)
    bar.set_label(image.bunit)


def stage1_images_for(project_root: Path, source: str, epoch: str) -> tuple[Path, Path]:
    """Paths of the Stage 1 clean and residual maps for one epoch (they may not exist)."""
    directory = Path(project_root) / "stage1" / f"{source}.{epoch}"
    return directory / f"{source}.{epoch}.cln.fits", directory / f"{source}.{epoch}.resid.fits"


def _panel(image_path: Path, settings: ImageSettings) -> MapImage:
    image = load_map(image_path)
    return cutout(image, parse_center(settings.center, image), settings.size_arcsec)


def write_visit_images(
    clean_path: Path, residual_path: Path, base_prefix: Path, settings: ImageSettings, title: str
) -> dict[str, str]:
    """One A4 portrait page per map for one visit: ``<prefix>.image_clean`` and ``<prefix>.image_residual``.

    Returns the file stem (no extension) written for each kind.
    """
    written: dict[str, str] = {}
    for kind, path in zip(KINDS, (clean_path, residual_path), strict=True):
        image = _panel(path, settings)
        fig = plt.figure(figsize=A4_PORTRAIT)
        ax = fig.add_axes([0.14, 0.32, 0.72, 0.5])  # square-ish panel mid-page
        draw_map(ax, image, settings, f"{title}  {kind}", kind)
        fig.text(
            0.5, 0.2,
            f"{title} {kind} map. Cutout {settings.size_arcsec[0]:g} x {settings.size_arcsec[1]:g} arcsec "
            f"around the reference position; axes are offsets from it. Colour scale "
            f"{settings.vmin if settings.vmin is not None else f'p{100 - settings.pmax_for(kind):g}'} "
            f"to p{settings.pmax_for(kind):g}, {settings.cmap}.",
            ha="center", va="top", fontsize=9, wrap=True,
        )
        base = base_prefix.with_name(f"{base_prefix.name}.image_{kind}")
        plotting.save_figure(fig, base, dpi=200, bbox_inches=None)  # keep the A4 page
        written[kind] = base.name  # names, not paths: the plots dir is renamed when the visit completes
    return written


def write_all_epochs_images(
    entries: list[tuple[str, Path, Path]], base_prefix: Path, settings: ImageSettings, source: str
) -> dict[str, Any]:
    """A4 portrait pages per map kind with every visit, two per row, ``GRID_ROWS`` rows per page.

    Every panel of a kind shares the colour scale of the reference visit
    (``settings.reference_epoch``, else the first visit), so one colour means
    the same flux on every panel. Six visits fit one page; more continue on
    ``..._p2``, ``..._p3``. Returns the file stems written per kind, the
    reference visit used and the shared limits.
    """
    written: dict[str, Any] = {}
    if not entries:
        return written
    epochs = [epoch for epoch, _, _ in entries]
    reference = settings.reference_epoch if settings.reference_epoch in epochs else epochs[0]
    reference_entry = next(entry for entry in entries if entry[0] == reference)
    shared_limits: dict[str, tuple[float, float]] = {}
    for kind_index, kind in enumerate(KINDS):
        reference_image = _panel(reference_entry[1 + kind_index], settings)
        shared_limits[kind] = colour_limits(reference_image.data, settings.pmax_for(kind), settings.vmin)

    per_page = GRID_COLUMNS * GRID_ROWS
    pages = [entries[i:i + per_page] for i in range(0, len(entries), per_page)]
    for kind_index, kind in enumerate(KINDS):
        written[kind] = []
        for page_number, page in enumerate(pages, start=1):
            n = len(page)
            columns = min(GRID_COLUMNS, n)
            rows = math.ceil(n / columns)
            fig = plt.figure(figsize=A4_PORTRAIT)
            title = f"{source}  {kind} maps, all visits (colour scale from visit {reference})"
            if len(pages) > 1:
                title += f"  (page {page_number} of {len(pages)})"
            fig.suptitle(title, y=0.97)
            for index, (epoch, clean_path, residual_path) in enumerate(page):
                path = (clean_path, residual_path)[kind_index]
                image = _panel(path, settings)
                row, column = divmod(index, columns)
                ax = fig.add_subplot(GRID_ROWS, columns, index + 1)
                draw_map(
                    ax, image, settings, f"{source}.{epoch}", kind, limits=shared_limits[kind],
                    label_x=(row == rows - 1), label_y=(column == 0),
                )
            fig.subplots_adjust(left=0.12, right=0.9, bottom=0.06, top=0.93, wspace=0.5, hspace=0.3)
            suffix = "" if len(pages) == 1 else f"_p{page_number}"
            base = base_prefix.with_name(f"{base_prefix.name}.images_{kind}_all_epochs{suffix}")
            plotting.save_figure(
                fig, base, dpi=200, bbox_inches=None, transparent=settings.transparent_all_epochs
            )  # keep the A4 page
            written[kind].append(base.name)
    written["reference_epoch"] = reference
    written["shared_limits"] = {kind: list(limits) for kind, limits in shared_limits.items()}
    return written

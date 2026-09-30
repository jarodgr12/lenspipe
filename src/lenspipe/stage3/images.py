"""Clean and residual map figures from the Stage 1 FITS images.

Stage 1 leaves two images per epoch: the restored (clean) map and the residual
map that DifMAP's ``wdmap`` writes. These figures show a cutout of each around
a chosen centre, with the restoring beam drawn, per visit and as an all-epochs
grid in the combined product. No APLpy: astropy's WCS axes and Cutout2D do the
work, so nothing beyond the package's existing dependencies is needed.
"""

from __future__ import annotations

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
    "ImageSettings",
    "MapImage",
    "beam_from_header",
    "load_map",
    "parse_center",
    "stage1_images_for",
    "write_all_epochs_images",
    "write_visit_images",
]

BEAM_KEYS = ("BMAJ", "BMIN", "BPA")


@dataclass(frozen=True)
class ImageSettings:
    """What to show: a cutout of ``size_arcsec`` around ``center`` with a percentile colour scale."""

    enabled: bool = True
    center: str | None = None  # None: image centre; "ra_deg,dec_deg"; or "15h58m00s +37d20m00s"
    size_arcsec: tuple[float, float] = (2.0, 2.0)  # width, height
    cmap: str = "viridis"
    pmax: float = 99.5
    vmin: float | None = 0.0

    @classmethod
    def from_config(cls, config: Any) -> ImageSettings:
        size = config.size_arcsec
        if isinstance(size, (int, float)):
            pair = (float(size), float(size))
        else:
            values = [float(v) for v in size]
            pair = (values[0], values[0]) if len(values) == 1 else (values[0], values[1])
        return cls(
            enabled=bool(config.enabled),
            center=(str(config.center).strip() or None) if config.center is not None else None,
            size_arcsec=pair,
            cmap=str(config.cmap),
            pmax=float(config.pmax),
            vmin=None if config.vmin is None else float(config.vmin),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "center": self.center,
            "size_arcsec": list(self.size_arcsec),
            "cmap": self.cmap,
            "pmax": self.pmax,
            "vmin": self.vmin,
        }


@dataclass
class MapImage:
    data: np.ndarray  # 2-D, Dec along axis 0
    wcs: WCS  # celestial only
    beam: tuple[float, float, float] | None  # BMAJ, BMIN, BPA in degrees
    bunit: str

    @property
    def pixel_scale_deg(self) -> float:
        scales = proj_plane_pixel_scales(self.wcs)
        return float(np.mean(np.abs(scales)))


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
    return MapImage(data=cut.data, wcs=cut.wcs, beam=image.beam, bunit=image.bunit)


def _colour_limits(data: np.ndarray, settings: ImageSettings) -> tuple[float, float]:
    finite = data[np.isfinite(data)]
    if finite.size == 0:
        return 0.0, 1.0
    vmax = float(np.percentile(finite, settings.pmax))
    vmin = float(np.percentile(finite, 100.0 - settings.pmax)) if settings.vmin is None else settings.vmin
    if not vmax > vmin:
        vmax = vmin + (abs(vmin) or 1.0) * 1e-6
    return vmin, vmax


def draw_map(
    ax, image: MapImage, settings: ImageSettings, title: str, *, label_x: bool = True, label_y: bool = True
) -> None:
    """One panel: the image with WCS ticks, the beam in the lower left, a colour bar in the image units.

    In a grid only the outer panels carry axis labels, otherwise the declination
    label of one column runs into the colour bar of the column before it.
    """
    vmin, vmax = _colour_limits(image.data, settings)
    shown = ax.imshow(image.data, origin="lower", cmap=settings.cmap, vmin=vmin, vmax=vmax, interpolation="nearest")
    ax.set_title(title)
    ax.coords[0].set_axislabel("Right ascension (J2000)" if label_x else " ")
    ax.coords[1].set_axislabel("Declination (J2000)" if label_y else " ")
    ax.coords[0].set_major_formatter("hh:mm:ss.ss")
    ax.coords[1].set_major_formatter("dd:mm:ss.s")
    ax.coords[0].set_ticklabel(rotation=0, size=8)
    ax.coords[1].set_ticklabel(size=8)
    if image.beam is not None:
        bmaj, bmin, bpa = image.beam
        scale = image.pixel_scale_deg
        ny, nx = image.data.shape
        ax.add_patch(
            Ellipse(
                (0.1 * nx + bmaj / scale / 2.0, 0.1 * ny + bmaj / scale / 2.0),
                width=bmin / scale, height=bmaj / scale, angle=90.0 + bpa,
                facecolor="none", edgecolor="white", linewidth=1.2,
            )
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
    clean_path: Path, residual_path: Path, base_path: Path, settings: ImageSettings, title: str
) -> None:
    """Clean and residual cutouts side by side for one visit."""
    clean = _panel(clean_path, settings)
    residual = _panel(residual_path, settings)
    fig = plt.figure(figsize=(11.0, 4.8))
    for column, (image, label) in enumerate(((clean, "clean"), (residual, "residual")), start=1):
        ax = fig.add_subplot(1, 2, column, projection=image.wcs)
        draw_map(ax, image, settings, f"{title}  {label}")
    fig.subplots_adjust(left=0.08, right=0.97, bottom=0.12, top=0.9, wspace=0.35)
    plotting.save_figure(fig, base_path, dpi=200)


def write_all_epochs_images(
    entries: list[tuple[str, Path, Path]], base_path: Path, settings: ImageSettings, source: str
) -> None:
    """A grid of every visit: clean maps on the top row, residual maps below, one column per epoch."""
    if not entries:
        return
    n = len(entries)
    fig = plt.figure(figsize=(max(5.5, 5.0 * n), 9.0))
    for column, (epoch, clean_path, residual_path) in enumerate(entries, start=1):
        for row, (path, label) in enumerate(((clean_path, "clean"), (residual_path, "residual"))):
            image = _panel(path, settings)
            ax = fig.add_subplot(2, n, row * n + column, projection=image.wcs)
            draw_map(
                ax, image, settings, f"{source}.{epoch}  {label}",
                label_x=(row == 1), label_y=(column == 1),
            )
    fig.subplots_adjust(left=0.07, right=0.98, bottom=0.07, top=0.95, wspace=0.55, hspace=0.3)
    plotting.save_figure(fig, base_path, dpi=200)

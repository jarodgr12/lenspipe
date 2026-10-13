#!/usr/bin/env python3
"""DifMAP Spectral Pipeline — Stage 1 (v1.2.2)

Calibrate labelled lens models, fit a final broadband spectral model, and image
all selected epochs on one pixel grid.

PROJECT LAYOUT
  <project>/inputs/<source>.gmod
  <project>/inputs/<source>.<epoch>.uvfits

CALIBRATION MODES
  --calibration-mode full-band (default): select the entire band, use the
      original model (spectral-index entries such as 0v remain variable), and
      run the established five-step selfcal/modelfit sequence.
  --calibration-mode per-if: for each IF in turn select its contiguous channel
      interval, load a temporary master model with the final spectral-index
      column set to fixed 0, and run phase-only selfcal/modelfit plus gscale.
      Save each IF model. Finally select the full band, reload the original
      free-index master model, and run modelfit WITHOUT further selfcal.
      The per-IF models are independent starting fits, not chained fits.

IMPORTANT: DifMAP gain-state and model-reset behaviour, gscale normalisation,
UVFITS IF channel ordering, and spectral-model interpretation MUST be checked
on real data before scientific use. This script does not assert that per-IF
solutions are independent or retained correctly merely because commands run.

IMAGING (TWO PASSES)
  1. Each epoch is calibrated and imaged with 'mapsize SIZE', allowing DifMAP
     to choose its native cell size. Native images are retained as
     *.native.cln.fits and *.native.resid.fits.
  2. Read the native FITS WCS and beam. Choose the smallest absolute pixel
     scale, then reimage each calibrated UV dataset using that common cell
     size and a common image size. Validate final WCS and pixel alignment.
     Different restoring beams are retained and recorded.
  --reimage-only repeats step 2 from existing calibrated data and models.

OUTPUTS PER EPOCH (same standard Stage 2 product names)
  stage1/<calibration-mode>/<source>.<epoch>/<source>.<epoch>.cal.uvf
  stage1/<calibration-mode>/<source>.<epoch>/<source>.<epoch>.gmod
  stage1/<calibration-mode>/<source>.<epoch>/<source>.<epoch>.cln.fits
  stage1/<calibration-mode>/<source>.<epoch>/<source>.<epoch>.resid.fits
  ...native.cln.fits, ...native.resid.fits, ...model.json,
  ...stage1.metadata.json, ...stage1.manifest.json, ...stage1.difmap.log
  per-if mode additionally: <source>.<epoch>.ifNNN.gmod

EXAMPLES
  python run_difmap_stage1_v1_2_2.py /data/MG0414
  python run_difmap_stage1_v1_2_2.py /data/MG0414 --calibration-mode per-if \\
      --if-count 48 --channels-per-if 64 --edge-channels 2
  python run_difmap_stage1_v1_2_2.py /data/MG0414 --unflag-all
  python run_difmap_stage1_v1_2_2.py /data/MG0414 --reimage-only
  python run_difmap_stage1_v1_2_2.py /data/MG0414 --dry-run
  python run_difmap_stage1_v1_2_2.py /data/MG0414 --overwrite

OPTIONS
  --calibration-mode {full-band,per-if}    default full-band
  --if-count N                             default 48 (per-if)
  --channels-per-if N                      default 64 (per-if)
  --edge-channels N                        default 0 (per-if)
  --unflag-all                             disabled by default; preserves flags
  --phase-intervals A,B,C,D                default 3600,1,0.5,0.5 minutes
  --amplitude-interval MIN                 default 3600 (full-band only)
  --fit-iterations N                       default 50
  --map-size N                             default 1024 pixels per side
  --reimage-only                           skip calibration; redo common maps
  --overwrite                              allow replacement of existing outputs
  --dry-run                                print commands, write nothing
  --epoch E                                repeat for multiple epochs
  --pattern GLOB                           default '*.uvfits'
  --difmap PATH                            default difmap
  --describe / --examples / --version

CAUTIONS
  DifMAP selection is explicitly 'select i,START,END'. A single full-range
  selection is used for the final spectral fit; excluded IF edge channels
  are therefore included at that point unless already flagged in UVFITS.
  The script refuses to proceed if final WCS grids differ; do not silently
  stack images with different reference coordinates. --overwrite uses staged
  calibration output directories, but does not provide transactional rollback
  of the final image regridding phase.
"""
from __future__ import annotations
import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

VERSION = '1.2.2'

@dataclass(frozen=True)
class Dataset:
    root: Path
    raw: Path
    source: str
    epoch: str
    master: Path
    directory: Path
    @property
    def prefix(self): return f'{self.source}.{self.epoch}'
    def path(self, suffix): return self.directory / f'{self.prefix}.{suffix}'


def dataset_for(root, raw, calibration_mode):
    stem = raw.name.removesuffix('.uvfits')
    if not raw.name.endswith('.uvfits') or '.' not in stem:
        raise ValueError(f'Expected <source>.<epoch>.uvfits: {raw.name}')
    source, epoch = stem.rsplit('.', 1)
    if not source or not epoch: raise ValueError(f'Invalid dataset name: {raw.name}')
    return Dataset(root, raw.resolve(), source, epoch,
                   (root/'inputs'/f'{source}.gmod').resolve(),
                   (root/'stage1'/calibration_mode/stem).resolve())


def model_rows(path):
    lines = path.read_text().splitlines()
    groups, components, seen_groups, seen_components = [], [], set(), set()
    group = pending = None
    for i, line in enumerate(lines, 1):
        s = line.strip()
        gm = re.fullmatch(r'!\s*GROUP\s+(\S+)', s, re.I)
        cm = re.fullmatch(r'!\s*COMPONENT\s+(\S+)', s, re.I)
        if gm:
            if pending: raise ValueError(f'{path}:{i}: missing row for {pending}')
            group = gm[1]
            if group in seen_groups: raise ValueError(f'Duplicate group {group}')
            seen_groups.add(group)
            groups.append(group)
        elif cm:
            if not group or pending: raise ValueError(f'{path}:{i}: misplaced COMPONENT')
            pending = cm[1]
            if pending in seen_components: raise ValueError(f'Duplicate component {pending}')
            seen_components.add(pending)
        elif s and not s.startswith('!'):
            if not pending: raise ValueError(f'{path}:{i}: unlabelled model row')
            components.append((group, pending, s))
            pending = None
    if pending or not components: raise ValueError(f'Incomplete master model {path}')
    if set(groups) != {g for g, _, _ in components}: raise ValueError('Empty GROUP')
    return components


def fixed_alpha_model(master, dest):
    """Only replace the last (spectral-index) field, preserving other v flags."""
    output = []
    count = 0
    for line in master.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('!'):
            output.append(line)
            continue
        fields = line.split()
        if len(fields) != 9:
            raise ValueError(f'Expected nine DifMAP columns, found {len(fields)}: {line}')
        try: float(fields[-1].removesuffix('v'))
        except ValueError as exc: raise ValueError(f'Invalid spectral index: {fields[-1]}') from exc
        fields[-1] = '0'
        output.append(' '.join(fields))
        count += 1
    dest.write_text('\n'.join(output)+'\n')
    return count


def label_model(path, hierarchy):
    lines = path.read_text().splitlines()
    rows = [i for i,l in enumerate(lines) if l.strip() and not l.lstrip().startswith('!')]
    if len(rows) != len(hierarchy):
        raise ValueError(f'{path}: fitted {len(rows)} components, expected {len(hierarchy)}')
    result, last_group, n = [], None, 0
    for i,line in enumerate(lines):
        if re.match(r'^\s*!\s*(GROUP|COMPONENT)\b', line, re.I): continue
        if i in rows:
            group, name, _ = hierarchy[n]
            if group != last_group:
                result.append(f'! GROUP {group}')
                last_group = group
            result.append(f'! COMPONENT {name}')
            n += 1
        result.append(line)
    path.write_text('\n'.join(result)+'\n')


def fits_header(path):
    """Read FITS primary header cards without third-party dependencies."""
    result = {}
    with path.open('rb') as f:
        for _ in range(1000):
            block = f.read(2880)
            if len(block) != 2880: raise ValueError(f'Truncated FITS header: {path}')
            for offset in range(0,2880,80):
                card = block[offset:offset+80].decode('ascii', errors='replace')
                key = card[:8].strip()
                if key == 'END': return result
                if card[8:10] != '= ': continue
                value = card[10:80].split('/',1)[0].strip()
                if value.startswith("'"):
                    value = value[1:].split("'",1)[0].strip()
                else:
                    try: value = float(value.replace('D','E')) if any(x in value for x in '.EeDd') else int(value)
                    except ValueError: pass
                result[key] = value
    raise ValueError(f'No END card in FITS header: {path}')


def image_geometry(path):
    h = fits_header(path)
    required = ['NAXIS1','NAXIS2','CRPIX1','CRPIX2','CRVAL1','CRVAL2','CTYPE1','CTYPE2']
    if any(k not in h for k in required): raise ValueError(f'Missing WCS keys in {path}')
    # Reject rotated / skewed WCS until explicit handling is implemented.
    if any(abs(float(h.get(k,0))) > 1e-14 for k in ('CD1_2','CD2_1','PC1_2','PC2_1','CROTA2')):
        raise ValueError(f'Rotated WCS unsupported for common-grid validation: {path}')
    dx = h.get('CD1_1',h.get('CDELT1'))
    dy = h.get('CD2_2',h.get('CDELT2'))
    if dx is None or dy is None or float(dx)==0 or float(dy)==0:
        raise ValueError(f'No valid WCS pixel scale: {path}')
    return {'naxis1':int(h['NAXIS1']), 'naxis2':int(h['NAXIS2']),
            'cdelt1_deg':float(dx), 'cdelt2_deg':float(dy),
            'crpix1':float(h['CRPIX1']), 'crpix2':float(h['CRPIX2']),
            'crval1_deg':float(h['CRVAL1']), 'crval2_deg':float(h['CRVAL2']),
            'ctype1':str(h['CTYPE1']), 'ctype2':str(h['CTYPE2']),
            'bmaj_deg':h.get('BMAJ'), 'bmin_deg':h.get('BMIN'), 'bpa_deg':h.get('BPA')}


def channel_ranges(nif,nchan,edge):
    if nif<1 or nchan<1 or edge<0 or 2*edge>=nchan:
        raise ValueError('Invalid IF count, channel count or edge-channel exclusion')
    return [(1+i*nchan+edge,(i+1)*nchan-edge) for i in range(nif)]


def calibration_steps(args, per_if=False):
    a,b,c,d = args.phase_intervals
    sequence = []
    for interval in (a,b,c):
        sequence += [f'selfcal false,false,{interval:g}',f'modelfit {args.fit_iterations}']
    sequence += ['gscale' if per_if else f'selfcal true,true,{args.amplitude_interval:g}',
                 f'modelfit {args.fit_iterations}',
                 f'selfcal false,false,{d:g}', f'modelfit {args.fit_iterations}']
    return sequence


def command_lines(ds,args,alpha_model=None):
    total = args.if_count*args.channels_per_if
    cmds = [f'observe {ds.raw}', f'select i,1,{total}']
    if args.unflag_all: cmds.append('unflag *')
    cmds += [f'mapsize {args.map_size}', 'uvw 0,-1,false']
    if args.calibration_mode == 'full-band':
        cmds += [f'rmodel {ds.master}',*calibration_steps(args)]
    else:
        for i,(start,end) in enumerate(channel_ranges(args.if_count,args.channels_per_if,args.edge_channels),1):
            # Each IF starts with an unmodified copy of the same initial model.
            cmds += [f'! IF {i}: channels {start}-{end}',f'select i,{start},{end}',
                     f'rmodel {alpha_model}',*calibration_steps(args,True),
                     f'wmodel {ds.path(f"if{i:03d}.gmod")}']
        cmds += [f'select i,1,{total}', f'rmodel {ds.master}',
                 f'modelfit {args.fit_iterations}']
    cmds += [f'wobs {ds.path("cal.uvf")}',f'wmodel {ds.path("gmod")}',
             'invert','print "STAGE1_RMS"','print imstat(rms)',
             f'wdmap {ds.path("native.resid.fits")}', 'restore',
             f'wmap {ds.path("native.cln.fits")}', 'quit']
    return '\n'.join(cmds)+'\n'


def imaging_commands(ds,cell_mas,args):
    total = args.if_count*args.channels_per_if
    # DifMAP mapsize cell is in milliarcseconds.
    return '\n'.join([f'observe {ds.path("cal.uvf")}',f'select i,1,{total}',
        f'mapsize {args.map_size},{cell_mas:.12g}', 'uvw 0,-1,false',
        f'rmodel {ds.path("gmod")}', 'invert', 'print "STAGE1_RMS"',
        'print imstat(rms)',f'wdmap {ds.path("resid.fits")}',
        'restore',f'wmap {ds.path("cln.fits")}', 'quit'])+'\n'


def run_difmap(command,log,difmap,dry_run):
    if dry_run:
        print(f'\n--- {log.name} ---\n{command}--- end ---')
        return
    p = subprocess.run([difmap],input=command,text=True,stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT,check=False)
    log.write_text(p.stdout)
    if p.returncode:
        raise RuntimeError(f'DifMAP exited {p.returncode}; see {log}')
    errors = [line.strip() for line in p.stdout.splitlines()
              if re.search(r'\b(?:syntax error|unable to identify keyword|unknown command)\b', line, re.I)]
    if errors:
        raise RuntimeError(f'DifMAP command error: {errors[0]}; see {log}')


def rms_from_log(path):
    lines = path.read_text().splitlines()
    for i,line in enumerate(lines):
        if 'STAGE1_RMS' in line:
            for nextline in lines[i+1:i+12]:
                s = nextline.strip()
                if re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?',s):
                    return float(s)
    return None


def ensure_files(paths):
    missing = [str(p) for p in paths if not p.is_file()]
    if missing: raise RuntimeError('Missing DifMAP output(s): '+', '.join(missing))


def write_metadata(ds,hierarchy,args,native,common=None):
    created = datetime.now(timezone.utc).isoformat()
    model = {'schema_version':'1.0','stage':1,'stage1_version':VERSION,
        'source':ds.source,'epoch':ds.epoch,'master_model':ds.master.name,
        'fitted_model':ds.path('gmod').name,
        'n_groups':len({g for g,_,_ in hierarchy}), 'n_components':len(hierarchy),
        'groups':{g:[name for group,name,_ in hierarchy if group==g]
                  for g,_,_ in hierarchy},
        'components':[{'group':g,'name':n,'row_index':i}
                      for i,(g,n,_) in enumerate(hierarchy)],'created_utc':created}
    ds.path('model.json').write_text(json.dumps(model,indent=2)+'\n')
    previous = {}
    if ds.path('stage1.metadata.json').exists():
        previous = json.loads(ds.path('stage1.metadata.json').read_text())
    metadata = {**previous,'schema_version':'1.1','stage':1,'version':VERSION,
        'source':ds.source,'epoch':ds.epoch,'input_uvfits':str(ds.raw),
        'master_model':str(ds.master), 'calibration_mode':args.calibration_mode,
        'unflag_all':args.unflag_all,
        'if_count':args.if_count,'channels_per_if':args.channels_per_if,
        'edge_channels':args.edge_channels,
        'selected_ranges_one_based':channel_ranges(args.if_count,args.channels_per_if,args.edge_channels)
             if args.calibration_mode=='per-if' else [[1,args.if_count*args.channels_per_if]],
        'phase_intervals_minutes':args.phase_intervals,
        'amplitude_calibration':'gscale' if args.calibration_mode=='per-if' else 'selfcal',
        'amplitude_interval_minutes':None if args.calibration_mode=='per-if' else args.amplitude_interval,
        'fit_iterations':args.fit_iterations,'map_size':args.map_size,
        'difmap_map_size':args.map_size,'fits_image_size':args.map_size//2,
        'native_image_geometry':native,'common_image_geometry':common,
        'created_utc':created}
    if ds.path('stage1.difmap.log').exists():
        metadata['native_residual_rms_jy_per_beam']=rms_from_log(ds.path('stage1.difmap.log'))
    if ds.path('stage1.reimage.difmap.log').exists():
        metadata['final_residual_rms_jy_per_beam']=rms_from_log(ds.path('stage1.reimage.difmap.log'))
    ds.path('stage1.metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')
    suffixes = ['cal.uvf','gmod','cln.fits','resid.fits','native.cln.fits',
                'native.resid.fits','stage1.difmap.log','stage1.reimage.difmap.log',
                'model.json','stage1.metadata.json']
    if args.calibration_mode=='per-if':
        suffixes += [f'if{i:03d}.gmod' for i in range(1,args.if_count+1)]
    manifest = {'schema_version':'1.0','stage':1,'source':ds.source,'epoch':ds.epoch,
        'metadata':ds.path('stage1.metadata.json').name,
        'model_metadata':ds.path('model.json').name,
        'products':[ds.path(s).name for s in suffixes if ds.path(s).is_file()]}
    ds.path('stage1.manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')


def verify_uvfits_channels(ds,args):
    """Check known AIPS UVFITS channel and IF axes; fail closed on mismatch."""
    h = fits_header(ds.raw)
    axes = [(str(h.get(f'CTYPE{i}','')).upper(),int(h.get(f'NAXIS{i}',0)))
            for i in range(2,int(h.get('NAXIS',0))+1)]
    freq = [n for c,n in axes if c.startswith('FREQ')]
    ifs = [n for c,n in axes if c.startswith('IF')]
    if not freq: raise ValueError(f'Cannot identify UVFITS FREQ axis: {ds.raw}')
    if len(freq)!=1: raise ValueError(f'Ambiguous UVFITS FREQ axes: {axes}')
    # DifMAP may flatten IFs into a single channel axis. Accept either layout.
    total = args.if_count*args.channels_per_if
    if not ((freq[0]==total and (not ifs or ifs==[1])) or
            (freq[0]==args.channels_per_if and ifs==[args.if_count])):
        raise ValueError(f'UVFITS geometry {axes} inconsistent with {args.if_count} x '
                         f'{args.channels_per_if} = {total} channels')


def same_grid(a,b):
    keys = ('naxis1','naxis2','ctype1','ctype2')
    if any(a[k]!=b[k] for k in keys): return False
    for k in ('cdelt1_deg','cdelt2_deg','crpix1','crpix2','crval1_deg','crval2_deg'):
        if not math.isclose(a[k],b[k],rel_tol=1e-9,abs_tol=1e-11): return False
    return True


def main():
    p = argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('project_root',nargs='?',type=Path,default=Path('.'))
    p.add_argument('--epoch',action='append',default=[])
    p.add_argument('--pattern',default='*.uvfits')
    p.add_argument('--calibration-mode',choices=['full-band','per-if'],default='full-band')
    p.add_argument('--if-count',type=int,default=48)
    p.add_argument('--channels-per-if',type=int,default=64)
    p.add_argument('--edge-channels',type=int,default=0)
    p.add_argument('--unflag-all',action='store_true')
    p.add_argument('--phase-intervals',default='3600,1,0.5,0.5')
    p.add_argument('--amplitude-interval',type=float,default=3600)
    p.add_argument('--fit-iterations',type=int,default=50)
    p.add_argument('--map-size',type=int,default=1024)
    p.add_argument('--reimage-only',action='store_true')
    p.add_argument('--difmap',default='difmap')
    p.add_argument('--overwrite',action='store_true')
    p.add_argument('--dry-run',action='store_true')
    p.add_argument('--describe',action='store_true')
    p.add_argument('--examples',action='store_true')
    p.add_argument('--version',action='version',version=VERSION)
    args = p.parse_args()
    if args.examples or args.describe:
        print(__doc__)
        return 0
    try:
        args.phase_intervals = [float(x) for x in args.phase_intervals.split(',')]
        if len(args.phase_intervals)!=4 or any(x<=0 for x in args.phase_intervals):
            raise ValueError('Provide four positive --phase-intervals')
        if args.fit_iterations<1 or args.map_size<16 or args.map_size%2 or args.amplitude_interval<=0:
            raise ValueError('Invalid iteration count, map size or amplitude interval')
        channel_ranges(args.if_count,args.channels_per_if,args.edge_channels)
    except ValueError as exc:
        p.error(str(exc))
    root = args.project_root.expanduser().resolve()
    inputs = root/'inputs'
    if not inputs.is_dir(): p.error(f'No inputs directory: {inputs}')
    dslist = [dataset_for(root,f,args.calibration_mode) for f in sorted(inputs.glob(args.pattern))
              if f.is_file() and f.name.endswith('.uvfits')]
    dslist = [d for d in dslist if not args.epoch or d.epoch in args.epoch]
    if not dslist: p.error('No matching epochs')
    if not args.dry_run and not shutil.which(args.difmap) and not Path(args.difmap).is_file():
        p.error(f'DifMAP not found: {args.difmap}')
    errors = []
    native = {}
    for ds in dslist:
        print(f'\n{ds.prefix}: {args.calibration_mode}')
        try:
            if not ds.master.is_file(): raise ValueError(f'Missing model: {ds.master}')
            hierarchy = model_rows(ds.master)
            if not args.reimage_only:
                verify_uvfits_channels(ds,args)
                existing = [ds.path(s) for s in ('cal.uvf','gmod','native.cln.fits') if ds.path(s).exists()]
                if existing and not args.overwrite and not args.dry_run:
                    raise ValueError('Existing calibration products; use --overwrite or --reimage-only')
                # Dry runs write no temporary files into the project.
                with tempfile.TemporaryDirectory(prefix='stage1_') as td:
                    tmp = Path(td)/'fixed_alpha.gmod'
                    if args.calibration_mode=='per-if': fixed_alpha_model(ds.master,tmp)
                    commands = command_lines(ds,args,tmp)
                    if not args.dry_run:
                        ds.directory.mkdir(parents=True,exist_ok=True)
                        # Explicitly clear only known calibration outputs after inputs validate.
                        if args.overwrite:
                            for s in ('cal.uvf','gmod','native.cln.fits','native.resid.fits'):
                                ds.path(s).unlink(missing_ok=True)
                        run_difmap(commands,ds.path('stage1.difmap.log'),args.difmap,False)
                        expected = [ds.path(s) for s in ('cal.uvf','gmod','native.cln.fits','native.resid.fits')]
                        if args.calibration_mode=='per-if':
                            expected += [ds.path(f'if{i:03d}.gmod') for i in range(1,args.if_count+1)]
                        ensure_files(expected)
                        label_model(ds.path('gmod'),hierarchy)
                        if args.calibration_mode=='per-if':
                            for i in range(1,args.if_count+1):
                                label_model(ds.path(f'if{i:03d}.gmod'),hierarchy)
                    else: run_difmap(commands,ds.path('stage1.difmap.log'),args.difmap,True)
            if not args.dry_run:
                ensure_files([ds.path('cal.uvf'),ds.path('gmod'),ds.path('native.cln.fits')])
                native[ds.prefix] = image_geometry(ds.path('native.cln.fits'))
                write_metadata(ds,hierarchy,args,native[ds.prefix])
        except Exception as exc:
            errors.append(f'{ds.prefix}: {exc}')
            print(f'ERROR: {exc}',file=sys.stderr)
    if args.dry_run:
        print('\n--- Common-grid reimaging command template (example cell = 0.1 mas; actual cell derived from native FITS) ---')
        for ds in dslist:
            print(f'\n{ds.prefix}:\n{imaging_commands(ds,0.1,args)}')
        print(f'Dry run: {len(dslist)} epochs, {len(errors)} errors')
        return 2 if errors else 0
    if errors:
        print('Calibration incomplete; common-grid imaging NOT started.',file=sys.stderr)
        return 2
    # Choose a common pixel size. All native grids must have matching sign and projection.
    grids = list(native.values())
    if any(g['cdelt1_deg']*grids[0]['cdelt1_deg']<=0 or
           g['cdelt2_deg']*grids[0]['cdelt2_deg']<=0 or
           (g['ctype1'],g['ctype2'])!=(grids[0]['ctype1'],grids[0]['ctype2']) for g in grids):
        print('ERROR: native image WCS orientations/projections differ',file=sys.stderr)
        return 2
    cell_mas = min(min(abs(g['cdelt1_deg']),abs(g['cdelt2_deg'])) for g in grids)*3.6e6
    print(f'\nCommon imaging cell: {cell_mas:.9g} mas, {args.map_size} x {args.map_size}')
    final = {}
    for ds in dslist:
        try:
            commands = imaging_commands(ds,cell_mas,args)
            if not args.overwrite and not args.reimage_only and any(ds.path(s).exists() for s in ('cln.fits','resid.fits')):
                raise ValueError('Final images already exist; use --overwrite')
            if args.reimage_only and not args.overwrite and any(ds.path(s).exists() for s in ('cln.fits','resid.fits')):
                raise ValueError('Final images already exist; use --overwrite with --reimage-only')
            for s in ('cln.fits','resid.fits'): ds.path(s).unlink(missing_ok=True)
            run_difmap(commands,ds.path('stage1.reimage.difmap.log'),args.difmap,False)
            ensure_files([ds.path('cln.fits'),ds.path('resid.fits')])
            g = image_geometry(ds.path('cln.fits'))
            expected_fits_size = args.map_size // 2
            if g['naxis1']!=expected_fits_size or g['naxis2']!=expected_fits_size:
                raise ValueError(f'DifMAP exported {g["naxis1"]} x {g["naxis2"]} FITS; '
                                 f'expected {expected_fits_size} x {expected_fits_size} '
                                 f'for mapsize {args.map_size}')
            if not math.isclose(abs(g['cdelt1_deg'])*3.6e6,cell_mas,rel_tol=1e-5):
                raise ValueError('DifMAP did not produce requested cell size')
            if final and not same_grid(next(iter(final.values())),g):
                raise ValueError('Final image WCS differs from earlier epochs; cannot claim common grid')
            final[ds.prefix] = g
            write_metadata(ds,model_rows(ds.master),args,native[ds.prefix],g)
            print(f'{ds.prefix}: completed')
        except Exception as exc:
            errors.append(f'{ds.prefix}: {exc}')
            print(f'ERROR: {ds.prefix}: {exc}',file=sys.stderr)
    if errors:
        print('\n'.join(errors),file=sys.stderr)
    return 2 if errors else 0

if __name__=='__main__':
    raise SystemExit(main())

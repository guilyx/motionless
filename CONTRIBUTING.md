# Contributing to motionless

Thanks for taking the time. Bug reports, tuning suggestions and compositor
compatibility notes are all genuinely useful — especially the last one, since
overlay behaviour varies a lot between desktops.

## Getting set up

motionless draws with GTK 3 through PyGObject, which pip cannot build
reliably. Use your distribution's bindings and a virtualenv that can see them:

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-gtklayershell-0.1
git clone https://github.com/guilyx/motionless
cd motionless
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

`make dev` does the last three steps for you.

## The development loop

```bash
make check          # ruff + mypy + pytest, the same gate CI runs
make test           # pytest only
motionless run --source demo --always-on --verbose
motionless preview  # render a PNG without a display, for tuning
```

The demo source replays a synthetic drive, so you can see cue behaviour
without leaving your desk. `motionless preview` is faster still when you are
only adjusting spacing, colour or opacity.

`make screenshot` regenerates `docs/screenshot.png` by running the real
overlay on a virtual display (needs `xvfb`, `xcompmgr`, `feh` and
`imagemagick`). Note the compositing manager: without one, X has nowhere to
composite the overlay's alpha and it captures as a black rectangle — the same
reason the overlay looks wrong on a desktop with compositing switched off.

## Layout

| Path | What lives there |
| --- | --- |
| `src/motionless/motion.py` | Gravity compensation, smoothing, axis mapping |
| `src/motionless/overlay/render.py` | Dot layout and fade animation (pure maths) |
| `src/motionless/overlay/backend.py` | Which display protocol to use, decided before GTK loads |
| `src/motionless/overlay/window.py` | The only module that touches GTK |
| `src/motionless/sources/` | Motion sources |
| `src/motionless/daemon.py` | Process lifecycle, wiring, control commands |
| `src/motionless/cli.py` | Command line interface |
| `docs/` | Backdrop generator and the screenshot capture script |

The split is deliberate: everything except `window.py` runs headless, which is
why the test suite can cover the visual behaviour without a display server.
Please keep new logic on the testable side of that line.

## Adding a motion source

1. Subclass `MotionSource` in `src/motionless/sources/`.
2. Implement `_run()` as a loop that calls `self.emit(sample)` until
   `self.stopping`, and `probe()` so `motionless doctor` can report on it.
3. Register it in `SOURCES` in `sources/__init__.py`. Add it to `AUTO_ORDER`
   only if it works with no configuration and no other software running.
4. Add tests — the existing source tests show the pattern.

## Pull requests

- One logical change per PR, with a note on what you tested it against
  (compositor, distribution, sensor).
- `make check` must pass. CI runs it on Python 3.10 through 3.13.
- Update `CHANGELOG.md` under `## [Unreleased]`.
- New behaviour needs a test. Behaviour that needs a display server does not —
  say so in the PR and describe how you verified it by hand.

## Reporting compatibility

If the overlay misbehaves on your desktop, `motionless doctor` output plus your
compositor and version tells us almost everything we need.

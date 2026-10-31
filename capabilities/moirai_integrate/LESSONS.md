# Lessons: moirai_integrate

- [L-0e661662] integrate_validate env_import runs the moirai venv's own Python (moirai_env.sh puts moirai-venv/bin first on PATH), so a pass does not prove the app's separate venv (elmerfold: openfoldenv) can import moirai. openfoldenv has no moirai/dftracer; both venvs are CPython 3.13.2 from the same anaconda base. Exposing the moirai venv's whole site-packages via PYTHONPATH would shadow openfoldenv's typing_extensions/packaging/setuptools; use a directory with only moirai, dftracer, dftracer_utils, pydftracer.

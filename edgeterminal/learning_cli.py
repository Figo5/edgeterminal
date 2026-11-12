"""Offline CLI entrypoint with one canonical learning-worker module.

Executing learning_worker itself as __main__ creates a second job ContextVar
when a model store imports edgeterminal.learning_worker to save a checkpoint.
Importing the worker normally keeps the job owner and checkpoint writer shared.
"""
from __future__ import annotations

import os


def offline_environment(environ):
    # Bootstrap before importing configuration or numeric libraries. Keep this
    # small boundary in parity with learning_worker's frozen environment policy.
    clean = {key: value for key, value in environ.items()
             if not any(part in key.upper() for part in ('KEY', 'SECRET', 'PASSWORD', 'TOKEN', 'CREDENTIAL'))}
    clean.update(EDGETERMINAL_OFFLINE_LEARNER='1', PYTHONDONTWRITEBYTECODE='1',
                 OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1',
                 NUMEXPR_NUM_THREADS='1', VECLIB_MAXIMUM_THREADS='1')
    return clean


def main():
    clean = offline_environment(os.environ)
    os.environ.clear()
    os.environ.update(clean)
    from .learning_worker import main as worker_main
    return worker_main()


if __name__ == '__main__':
    raise SystemExit(main())

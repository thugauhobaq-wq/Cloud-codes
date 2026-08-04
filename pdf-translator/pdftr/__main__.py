"""Точка входа: `python -m pdftr`."""

from __future__ import annotations

import os
import sys

from .cli import main

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        # Вывод оборвали через `| head` — это не ошибка. Поток надо подменить,
        # иначе Python при выходе попробует дописать буфер и упадёт ещё раз.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
